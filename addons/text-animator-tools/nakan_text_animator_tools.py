bl_info = {
    "name": "Nakan Text Animator Tools",
    "author": "Nakan Art",
    "version": (1, 2, 0),
    "blender": (5, 0, 0),
    "location": "Properties > Text (Object Data)",
    "description": "After Effects style text animators (range selector, opacity, position, "
                   "scale, rotation, tracking, material fill) on regular, editable Text objects",
    "category": "Animation",
}

import hashlib
import math
import uuid

import bpy

NG_VERSION = 12
BASE_NAME = "AE Text Base"
ANIM_PREFIX = "AE Text Animator"
GEO_NAME = "AE Text Geometry"
SRC_PREFIX = "AE src · "
WRAP_PREFIX = "AE · "
BASE_MOD = "Text Animator"
GEO_MOD = "Text Geometry"

ALIGN_X = {'LEFT': 'Left', 'CENTER': 'Center', 'RIGHT': 'Right',
           'JUSTIFY': 'Justify', 'FLUSH': 'Flush'}
ALIGN_FAC = {'LEFT': 0.0, 'CENTER': 0.5, 'RIGHT': 1.0, 'JUSTIFY': 0.0, 'FLUSH': 0.0}
ALIGN_Y = {'TOP_BASELINE': ['Top Baseline'], 'TOP': ['Top'],
           'CENTER': ['Middle', 'Center'], 'BOTTOM_BASELINE': ['Bottom Baseline'],
           'BOTTOM': ['Bottom']}

# kind id, label, description, icon
ANIM_KINDS = [
    ('OPACITY', "Opacity", "Fade characters", 'HIDE_OFF'),
    ('POSITION', "Position", "Move characters", 'ORIENTATION_GLOBAL'),
    ('SCALE', "Scale", "Scale characters around their anchor", 'FULLSCREEN_ENTER'),
    ('ROTATION', "Rotation", "Rotate characters around their anchor", 'DRIVER_ROTATIONAL_DIFFERENCE'),
    ('TRACKING', "Tracking", "Spread selected characters apart", 'ARROW_LEFTRIGHT'),
    ('FILL', "Fill (Material)", "Blend selected characters into another material", 'MATERIAL'),
    ('EXTRUDE', "Extrude", "Per-character depth", 'MOD_SOLIDIFY'),
    ('BEVEL', "Bevel", "Per-character bevel depth", 'MOD_BEVEL'),
]
KIND_LABEL = {k[0]: k[1] for k in ANIM_KINDS}
KIND_PROPS = {
    'OPACITY': ["Opacity"],
    'POSITION': ["Position"],
    'SCALE': ["Scale", "Anchor", "Anchor Offset"],
    'ROTATION': ["Rotation", "Anchor", "Anchor Offset"],
    'TRACKING': ["Tracking", "Anchor"],
    'FILL': ["Material"],
    'EXTRUDE': ["Extrude", "Depth Anchor"],
    'BEVEL': ["Bevel"],
}
FILL_MODES = {'NONE': 0, 'BACK': 1, 'FRONT': 2, 'BOTH': 3, 'HALF': 3, 'FULL': 3}
FALLOFF_NODE = "AE Falloff"


# ======================================================================================
# Node-building helpers
# ======================================================================================

class _B:
    def __init__(self, ng):
        self.ng, self.N, self.L = ng, ng.nodes, ng.links
        self.i = 0

    def node(self, idname, props=None, inputs=None):
        n = self.N.new(idname)
        for k, v in (props or {}).items():
            setattr(n, k, v)
        for k, v in (inputs or {}).items():
            self.set(self.inp(n, k), v)
        n.location = ((self.i % 14) * 200, -(self.i // 14) * 260)
        self.i += 1
        return n

    @staticmethod
    def _pick(socks, k):
        if isinstance(k, int):
            return socks[k]
        c = [s for s in socks if s.identifier == k] or [s for s in socks if s.name == k]
        en = [s for s in c if getattr(s, "enabled", True)]
        return (en or c)[0]

    def inp(self, n, k):
        return self._pick(n.inputs, k)

    def out(self, n, k=0):
        return self._pick(n.outputs, k)

    def set(self, sock, v):
        if isinstance(v, bpy.types.NodeSocket):
            self.L.new(v, sock)
        elif v is not None:
            sock.default_value = v

    def m(self, op, a, b=None, c=None, clamp=False):
        n = self.node('ShaderNodeMath', {'operation': op, 'use_clamp': clamp})
        self.set(n.inputs[0], a)
        if b is not None:
            self.set(n.inputs[1], b)
        if c is not None:
            self.set(n.inputs[2], c)
        return n.outputs[0]

    def v(self, op, a, b=None, c=None, scale=None):
        n = self.node('ShaderNodeVectorMath', {'operation': op})
        self.set(n.inputs[0], a)
        if b is not None:
            self.set(n.inputs[1], b)
        if c is not None:
            self.set(n.inputs[2], c)
        if scale is not None:
            self.set(self.inp(n, 'Scale'), scale)
        return n.outputs[0]

    def lerp(self, a, b, t):
        return self.m('MULTIPLY_ADD', self.m('SUBTRACT', b, a), t, a)

    def attr(self, name, dtype='FLOAT'):
        n = self.node('GeometryNodeInputNamedAttribute', {'data_type': dtype})
        self.set(self.inp(n, 'Name'), name)
        return self.out(n, 'Attribute')

    def store(self, geo, name, dtype, value, domain='INSTANCE'):
        n = self.node('GeometryNodeStoreNamedAttribute', {'data_type': dtype, 'domain': domain})
        self.set(self.inp(n, 'Geometry'), geo)
        self.set(self.inp(n, 'Name'), name)
        self.set(self.inp(n, 'Value'), value)
        return n.outputs[0]

    def switch(self, typ, cond, false, true):
        n = self.node('GeometryNodeSwitch', {'input_type': typ})
        self.set(self.inp(n, 'Switch'), cond)
        self.set(self.inp(n, 'False'), false)
        self.set(self.inp(n, 'True'), true)
        return n.outputs[0]

    def menu(self, dtype, menu_sock, items):
        n = self.node('GeometryNodeMenuSwitch', {'data_type': dtype})
        n.enum_items.clear()
        for name, _ in items:
            n.enum_items.new(name)
        self.set(self.inp(n, 'Menu'), menu_sock)
        for name, val in items:
            self.set(self.inp(n, name), val)
        return n.outputs[0]

    def xyz(self, x=0.0, y=0.0, z=0.0):
        n = self.node('ShaderNodeCombineXYZ')
        self.set(n.inputs[0], x)
        self.set(n.inputs[1], y)
        self.set(n.inputs[2], z)
        return n.outputs[0]


def _iface_socket(ng, name, typ, default=None, parent=None, subtype=None,
                  min_value=None, max_value=None, desc="", hidden=False):
    kw = {"in_out": 'INPUT', "socket_type": typ, "description": desc}
    if parent is not None:
        kw["parent"] = parent
    s = ng.interface.new_socket(name, **kw)
    for attr, val in (("subtype", subtype), ("default_value", default),
                      ("min_value", min_value), ("max_value", max_value)):
        if val is not None:
            try:
                setattr(s, attr, val)
            except Exception:
                pass
    if hidden:
        try:
            s.hide_in_modifier = True
        except Exception:
            pass
    return s


def _new_group(name, kind=None):
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    ng["ae_text_version"] = NG_VERSION
    if kind:
        ng["ae_kind"] = kind
    try:
        ng.is_modifier = True
    except Exception:
        pass
    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    return ng


# ======================================================================================
# Base: string -> curve instances, one per character (no geometry yet)
# ======================================================================================

FORMAT_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
MAX_SLOTS = 8


def build_base_group():
    """String -> one curve instance per character.

    Plain text uses String to Curves directly. When some characters are bold / italic, every
    font variant is laid out separately; each character takes its glyph and its advance width
    from its own variant and the line is re-laid out from those advances.
    """
    ng = _new_group(BASE_NAME)
    ng.description = "One curve instance per character (managed by Nakan Text Animator Tools)"
    p = ng.interface.new_panel("Synced from Text (automatic)", default_closed=True)
    S = lambda *a, **k: _iface_socket(ng, *a, parent=p, **k)
    S("Text", 'NodeSocketString', "Text")
    S("Font", 'NodeSocketFont')
    S("Font Bold", 'NodeSocketFont')
    S("Font Italic", 'NodeSocketFont')
    S("Font Bold Italic", 'NodeSocketFont')
    S("Format", 'NodeSocketString', "",
      desc="One character per text character: style (0 regular, 1 bold, 2 italic, 3 both) "
           "+ 4 x material slot, in base 36")
    S("Mixed Styles", 'NodeSocketBool', False)
    S("Size", 'NodeSocketFloat', 1.0, subtype='DISTANCE', min_value=0.0)
    S("Character Spacing", 'NodeSocketFloat', 1.0)
    S("Word Spacing", 'NodeSocketFloat', 1.0)
    S("Line Spacing", 'NodeSocketFloat', 1.0)
    S("Align X", 'NodeSocketMenu')
    S("Align Y", 'NodeSocketMenu')
    S("Align Factor", 'NodeSocketFloat', 0.5, hidden=True)
    S("Text Box Width", 'NodeSocketFloat', 0.0, subtype='DISTANCE', min_value=0.0)
    S("Text Box Height", 'NodeSocketFloat', 0.0, subtype='DISTANCE', min_value=0.0)
    S("Offset", 'NodeSocketVector', (0.0, 0.0, 0.0), subtype='TRANSLATION')
    S("Extrude", 'NodeSocketFloat', 0.0, subtype='DISTANCE', min_value=0.0)
    S("Bevel", 'NodeSocketFloat', 0.0, subtype='DISTANCE', min_value=0.0)

    b = _B(ng)
    gi = b.node('NodeGroupInput')
    go = b.node('NodeGroupOutput')
    G = lambda name: b.out(gi, name)
    idx = b.out(b.node('GeometryNodeInputIndex'), 'Index')
    ipos = b.out(b.node('GeometryNodeInputPosition'), 'Position')

    def s2c(font, pivot, align_x=None):
        n = b.node('GeometryNodeStringToCurves', inputs={
            'String': G("Text"), 'Size': G("Size"), 'Font': font,
            'Align Y': G("Align Y"),
            'Character Spacing': G("Character Spacing"), 'Word Spacing': G("Word Spacing"),
            'Line Spacing': G("Line Spacing"), 'Text Box Width': G("Text Box Width"),
            'Text Box Height': G("Text Box Height"),
        })
        if align_x is None:
            b.set(b.inp(n, 'Align X'), G("Align X"))
        else:
            b.inp(n, 'Align X').default_value = align_x
        b.inp(n, 'Pivot Point').default_value = pivot
        return n

    def sample(geo, value, index, dtype='FLOAT'):
        n = b.node('GeometryNodeSampleIndex', {'data_type': dtype, 'domain': 'INSTANCE', 'clamp': True})
        b.set(b.inp(n, 'Geometry'), geo)
        b.set(b.inp(n, 'Value'), value)
        b.set(b.inp(n, 'Index'), index)
        return b.out(n, 'Value')

    def sx(vec):
        n = b.node('ShaderNodeSeparateXYZ')
        b.set(n.inputs[0], vec)
        return n

    def tag_char(geo_tl, n_tl, n_br):
        """Index, line, word and the glyph box of every character of one layout run."""
        tl = sx(b.out(n_tl, 'Pivot Point'))
        br = sx(sample(b.out(n_br, 'Curve Instances'), b.out(n_br, 'Pivot Point'), idx, 'FLOAT_VECTOR'))
        g = b.store(geo_tl, 'ae_index', 'INT', idx)
        g = b.store(g, 'ae_line', 'INT', b.out(n_tl, 'Line'))
        g = b.store(g, 'ae_word', 'INT', b.out(n_tl, 'Word'))
        g = b.store(g, 'ae_bbmin', 'FLOAT_VECTOR', b.xyz(tl.outputs[0], br.outputs[1], 0.0))
        g = b.store(g, 'ae_bbmax', 'FLOAT_VECTOR', b.xyz(br.outputs[0], tl.outputs[1], 0.0))
        return g, br

    # per-character format code (style + 4 * material slot)
    sl = b.node('FunctionNodeSliceString')
    b.set(b.inp(sl, 'String'), G("Format"))
    b.set(b.inp(sl, 'Position'), b.attr('ae_index', 'INT'))
    b.inp(sl, 'Length').default_value = 1
    sv = b.node('FunctionNodeStringToValue', {'data_type': 'INT'})
    b.set(b.inp(sv, 'String'), sl.outputs[0])
    b.inp(sv, 'Base').default_value = 36
    code = b.out(sv, 'Value')
    style = b.m('FLOORED_MODULO', code, 4.0)
    matidx = b.m('FLOOR', b.m('DIVIDE', code, 4.0))

    # --- plain path: one font, native alignment --------------------------------------------
    p_tl = s2c(G("Font"), 'Top Left')
    p_br = s2c(G("Font"), 'Bottom Right')
    plain, _ = tag_char(b.out(p_tl, 'Curve Instances'), p_tl, p_br)

    # --- mixed path: one left-aligned run per font variant ---------------------------------
    fonts = [G("Font"), G("Font Bold"), G("Font Italic"), G("Font Bold Italic")]
    m_reg = None
    join = b.node('GeometryNodeJoinGeometry')
    for v, font in enumerate(fonts):
        n_tl = s2c(font, 'Top Left', 'Left')
        n_br = s2c(font, 'Bottom Right', 'Left')
        geo = b.out(n_tl, 'Curve Instances')
        if m_reg is None:
            m_reg = geo
        g, br = tag_char(geo, n_tl, n_br)
        # advance of each character in this font = distance to the next one on the same line
        dsz = b.node('GeometryNodeAttributeDomainSize', {'component': 'INSTANCES'})
        b.set(b.inp(dsz, 'Geometry'), geo)
        nxt = b.m('ADD', idx, 1.0)
        px = sx(ipos)
        nx = sx(sample(geo, ipos, nxt, 'FLOAT_VECTOR'))
        same = b.node('FunctionNodeCompare', {'data_type': 'INT', 'operation': 'EQUAL'})
        b.set(b.inp(same, 'A'), sample(geo, b.out(n_tl, 'Line'), nxt, 'INT'))
        b.set(b.inp(same, 'B'), b.out(n_tl, 'Line'))
        inside = b.node('FunctionNodeCompare', {'data_type': 'INT', 'operation': 'LESS_THAN'})
        b.set(b.inp(inside, 'A'), nxt)
        b.set(b.inp(inside, 'B'), b.out(dsz, 'Instance Count'))
        both = b.node('FunctionNodeBooleanMath', {'operation': 'AND'})
        b.set(both.inputs[0], same.outputs[0])
        b.set(both.inputs[1], inside.outputs[0])
        adv = b.switch('FLOAT', both.outputs[0], br.outputs[0],
                       b.m('SUBTRACT', nx.outputs[0], px.outputs[0]))
        g = b.store(g, 'ae_adv', 'FLOAT', adv)
        g = b.store(g, 'ae_y', 'FLOAT', sx(sample(m_reg, ipos, idx, 'FLOAT_VECTOR')).outputs[1])
        keep = b.node('FunctionNodeCompare', {'data_type': 'FLOAT', 'operation': 'NOT_EQUAL'})
        b.set(b.inp(keep, 'A'), style)
        b.set(b.inp(keep, 'B'), float(v))
        b.inp(keep, 'Epsilon').default_value = 0.01
        dl = b.node('GeometryNodeDeleteGeometry', {'domain': 'INSTANCE'})
        b.set(b.inp(dl, 'Geometry'), g)
        b.set(b.inp(dl, 'Selection'), keep.outputs[0])
        b.L.new(dl.outputs[0], join.inputs[0])
    srt = b.node('GeometryNodeSortElements', {'domain': 'INSTANCE'})
    b.set(b.inp(srt, 'Geometry'), join.outputs[0])
    b.set(b.inp(srt, 'Sort Weight'), b.attr('ae_index', 'INT'))
    acc = b.node('GeometryNodeAccumulateField', {'data_type': 'FLOAT', 'domain': 'INSTANCE'})
    b.set(b.inp(acc, 'Value'), b.attr('ae_adv'))
    b.set(b.inp(acc, 'Group Index'), b.attr('ae_line', 'INT'))
    width = b.out(acc, 'Total')
    box = G("Text Box Width")
    room = b.switch('FLOAT', b.m('GREATER_THAN', box, 0.0), b.m('MULTIPLY', width, -1.0),
                    b.m('SUBTRACT', box, width))
    xoff = b.m('MULTIPLY', room, G("Align Factor"))
    setp = b.node('GeometryNodeSetPosition')
    b.set(b.inp(setp, 'Geometry'), srt.outputs[0])
    b.set(b.inp(setp, 'Position'), b.xyz(b.m('ADD', b.out(acc, 'Trailing'), xoff), b.attr('ae_y'), 0.0))
    mixed = setp.outputs[0]

    g = b.switch('GEOMETRY', G("Mixed Styles"), plain, mixed)
    bmn, bmx = sx(b.attr('ae_bbmin', 'FLOAT_VECTOR')), sx(b.attr('ae_bbmax', 'FLOAT_VECTOR'))
    g = b.store(g, 'ae_pivot', 'FLOAT_VECTOR',
                b.xyz(b.m('MULTIPLY', b.m('ADD', bmn.outputs[0], bmx.outputs[0]), 0.5), 0.0, 0.0))
    g = b.store(g, 'ae_matidx', 'INT', matidx)
    g = b.store(g, 'ae_size', 'FLOAT', G("Size"))
    g = b.store(g, 'ae_align', 'FLOAT', G("Align Factor"))
    g = b.store(g, 'ae_extrude', 'FLOAT', G("Extrude"))
    g = b.store(g, 'ae_bevel', 'FLOAT', G("Bevel"))
    tr = b.node('GeometryNodeTransform')
    b.set(b.inp(tr, 'Geometry'), g)
    b.set(b.inp(tr, 'Translation'), G("Offset"))
    b.set(go.inputs[0], b.out(tr, 'Geometry'))
    return ng


# ======================================================================================
# Geometry: curves -> mesh (fill + extrude), realized so native modifiers work after it
# ======================================================================================

def build_geometry_group():
    """Caps + swept side profile, built per character so depth and bevel can be animated.

    Mirrors the Text object's Geometry panel (Fill Mode, Extrude, Bevel Round depth and
    resolution); per-character values come from the ae_extrude / ae_bevel attributes.
    """
    ng = _new_group(GEO_NAME)
    ng.description = "Turns the animated character curves into a mesh (managed by Nakan Text Animator Tools)"
    _iface_socket(ng, "Fill Mode", 'NodeSocketInt', 3, min_value=0, max_value=3, hidden=True,
                  desc="0 None, 1 Back, 2 Front, 3 Both (synced from the Text object)")
    _iface_socket(ng, "Bevel Resolution", 'NodeSocketInt', 4, min_value=0, max_value=32, hidden=True)
    for k in range(MAX_SLOTS):
        _iface_socket(ng, "Material %d" % k, 'NodeSocketMaterial', hidden=True)

    b = _B(ng)
    gi = b.node('NodeGroupInput')
    go = b.node('NodeGroupOutput')
    G = lambda name: b.out(gi, name)
    fm = G("Fill Mode")

    def ieq(a, v):
        n = b.node('FunctionNodeCompare', {'data_type': 'INT', 'operation': 'EQUAL'})
        b.set(b.inp(n, 'A'), a)
        b.set(b.inp(n, 'B'), v)
        return n.outputs[0]

    def bor(a, c):
        n = b.node('FunctionNodeBooleanMath', {'operation': 'OR'})
        b.set(n.inputs[0], a)
        b.set(n.inputs[1], c)
        return n.outputs[0]

    # each character's local Z (follows the animators' rotation and scale)
    itr = b.node('GeometryNodeInstanceTransform')
    tdir = b.node('FunctionNodeTransformDirection')
    tdir.inputs['Direction'].default_value = (0.0, 0.0, 1.0)
    b.set(b.inp(tdir, 'Transform'), itr.outputs[0])
    g1 = b.store(G("Geometry"), 'ae_zdir', 'FLOAT_VECTOR', tdir.outputs[0])

    zd = b.attr('ae_zdir', 'FLOAT_VECTOR')
    zdn = b.v('NORMALIZE', zd)
    ext = b.attr('ae_extrude')
    bev = b.attr('ae_bevel')
    thick = b.m('ADD', ext, bev)
    thin = b.m('LESS_THAN', thick, 1e-5)
    # depth anchor: -1 keeps the back face on the text plane, +1 keeps the front face
    za = b.attr('ae_zanchor')
    zshift = b.m('MULTIPLY', b.m('MULTIPLY', za, -1.0), thick)

    # --- caps ----------------------------------------------------------------------------
    fill = b.node('GeometryNodeFillCurve')
    b.set(b.inp(fill, 'Curve'), g1)
    rf = b.node('GeometryNodeRealizeInstances')
    b.set(b.inp(rf, 'Geometry'), b.out(fill, 'Mesh'))
    flat = rf.outputs[0]
    top = b.node('GeometryNodeSetPosition')
    b.set(b.inp(top, 'Geometry'), flat)
    b.set(b.inp(top, 'Offset'), b.v('SCALE', zd, scale=b.m('ADD', thick, zshift)))
    bot = b.node('GeometryNodeSetPosition')
    b.set(b.inp(bot, 'Geometry'), flat)
    b.set(b.inp(bot, 'Offset'), b.v('SCALE', zd, scale=b.m('SUBTRACT', zshift, thick)))
    flp = b.node('GeometryNodeFlipFaces')
    b.set(b.inp(flp, 'Mesh'), bot.outputs[0])
    dbot = b.node('GeometryNodeDeleteGeometry', {'domain': 'POINT'})
    b.set(b.inp(dbot, 'Geometry'), flp.outputs[0])
    b.set(b.inp(dbot, 'Selection'), thin)
    top_on = bor(ieq(fm, 2), ieq(fm, 3))
    bot_on = bor(ieq(fm, 1), ieq(fm, 3))
    top_g = b.switch('GEOMETRY', top_on, None, top.outputs[0])
    bot_g = b.switch('GEOMETRY', bot_on, None, dbot.outputs[0])

    # --- side: sweep a profile along the outlines ---------------------------------------------
    rc = b.node('GeometryNodeRealizeInstances')
    b.set(b.inp(rc, 'Geometry'), g1)
    # work on the evaluated (poly) points so the profile follows the real outline
    rs = b.node('GeometryNodeResampleCurve')
    b.set(b.inp(rs, 'Curve'), rc.outputs[0])
    b.inp(rs, 'Mode').default_value = 'Evaluated'
    curves = rs.outputs[0]
    tan = b.out(b.node('GeometryNodeInputTangent'), 'Tangent')
    pos = b.out(b.node('GeometryNodeInputPosition'), 'Position')
    n0 = b.v('NORMALIZE', b.v('CROSS_PRODUCT', tan, zdn))
    eps = b.m('MULTIPLY', b.attr('ae_size'), 0.003)
    probe = b.v('ADD', b.v('ADD', pos, b.v('SCALE', n0, scale=eps)), b.v('SCALE', zdn, scale=eps))
    ray = b.node('GeometryNodeRaycast')
    b.set(b.inp(ray, 'Target Geometry'), flat)
    b.set(b.inp(ray, 'Source Position'), probe)
    b.set(b.inp(ray, 'Ray Direction'), b.v('SCALE', zdn, scale=-1.0))
    b.set(b.inp(ray, 'Ray Length'), b.m('MULTIPLY', eps, 2.0))
    # the outward normal is the side that is NOT inside the filled letter
    nrm = b.v('SCALE', n0, scale=b.switch('FLOAT', b.out(ray, 'Is Hit'), 1.0, -1.0))
    curves = b.store(curves, 'ae_P', 'FLOAT_VECTOR', pos, domain='POINT')
    curves = b.store(curves, 'ae_N', 'FLOAT_VECTOR', nrm, domain='POINT')

    res = G("Bevel Resolution")
    arc = b.node('GeometryNodeCurveArc', {'mode': 'RADIUS'})
    b.set(b.inp(arc, 'Resolution'), b.m('MULTIPLY_ADD', res, 2.0, 3.0))
    arc.inputs['Radius'].default_value = 1.0
    arc.inputs['Start Angle'].default_value = -math.pi / 2.0
    arc.inputs['Sweep Angle'].default_value = math.pi
    circ = b.node('GeometryNodeCurvePrimitiveCircle', {'mode': 'RADIUS'})
    b.set(b.inp(circ, 'Resolution'), b.m('MULTIPLY', b.m('ADD', res, 1.0), 4.0))
    circ.inputs['Radius'].default_value = 1.0
    prof = b.switch('GEOMETRY', ieq(fm, 0), arc.outputs[0], circ.outputs[0])
    psep = b.node('ShaderNodeSeparateXYZ')
    b.set(psep.inputs[0], pos)
    prof = b.store(prof, 'ae_px', 'FLOAT', psep.outputs[0], domain='POINT')
    prof = b.store(prof, 'ae_py', 'FLOAT', psep.outputs[1], domain='POINT')
    c2m = b.node('GeometryNodeCurveToMesh')
    b.set(b.inp(c2m, 'Curve'), curves)
    b.set(b.inp(c2m, 'Profile Curve'), prof)
    px, py = b.attr('ae_px'), b.attr('ae_py')
    side_pos = b.v('ADD', b.attr('ae_P', 'FLOAT_VECTOR'),
                   b.v('ADD', b.v('SCALE', b.attr('ae_N', 'FLOAT_VECTOR'), scale=b.m('MULTIPLY', px, bev)),
                       b.v('ADD', b.v('SCALE', zdn, scale=b.m('MULTIPLY', py, bev)),
                           b.v('SCALE', zd, scale=b.m('MULTIPLY_ADD', b.m('SIGN', py), ext, zshift)))))
    sp = b.node('GeometryNodeSetPosition')
    b.set(b.inp(sp, 'Geometry'), c2m.outputs[0])
    b.set(b.inp(sp, 'Position'), side_pos)
    dside = b.node('GeometryNodeDeleteGeometry', {'domain': 'POINT'})
    b.set(b.inp(dside, 'Geometry'), sp.outputs[0])
    b.set(b.inp(dside, 'Selection'), thin)
    smooth = b.node('GeometryNodeSetShadeSmooth')
    b.set(b.inp(smooth, 'Geometry'), dside.outputs[0])
    b.set(b.inp(smooth, 'Shade Smooth'), b.m('GREATER_THAN', bev, 1e-5))

    join = b.node('GeometryNodeJoinGeometry')
    for geo in (top_g, bot_g, smooth.outputs[0]):
        b.L.new(geo, join.inputs[0])
    out = join.outputs[0]
    for k in range(MAX_SLOTS):
        eq = b.node('FunctionNodeCompare', {'data_type': 'INT', 'operation': 'EQUAL'})
        b.set(b.inp(eq, 'A'), b.attr('ae_matidx', 'INT'))
        b.set(b.inp(eq, 'B'), k)
        smat = b.node('GeometryNodeSetMaterial')
        b.set(b.inp(smat, 'Geometry'), out)
        b.set(b.inp(smat, 'Selection'), eq.outputs[0])
        b.set(b.inp(smat, 'Material'), G("Material %d" % k))
        out = smat.outputs[0]
    b.set(go.inputs[0], out)
    return ng


# ======================================================================================
# Animators: one group per property, sharing the same range selector
# ======================================================================================

def _build_selector(ng, b, G):
    geo0 = G("Geometry")
    u_raw = b.menu('FLOAT', G("Based On"), [
        ("Characters", b.attr('ae_index', 'INT')),
        ("Words", b.attr('ae_word', 'INT')),
        ("Lines", b.attr('ae_line', 'INT')),
    ])

    # randomize order: rank units by a random value, then restore the character order
    rnd = b.node('FunctionNodeRandomValue', {'data_type': 'FLOAT'})
    b.set(b.inp(rnd, 'ID'), u_raw)
    b.set(b.inp(rnd, 'Seed'), G("Random Seed"))
    srt = b.node('GeometryNodeSortElements', {'domain': 'INSTANCE'})
    b.set(b.inp(srt, 'Geometry'), geo0)
    b.set(b.inp(srt, 'Sort Weight'), b.out(rnd, 'Value'))
    gs = srt.outputs[0]
    idx = b.out(b.node('GeometryNodeInputIndex'), 'Index')
    smp = b.node('GeometryNodeSampleIndex', {'data_type': 'FLOAT', 'domain': 'INSTANCE', 'clamp': True})
    b.set(b.inp(smp, 'Geometry'), gs)
    b.set(b.inp(smp, 'Value'), u_raw)
    b.set(b.inp(smp, 'Index'), b.m('SUBTRACT', idx, 1.0))
    neq = b.node('FunctionNodeCompare', {'data_type': 'FLOAT', 'operation': 'NOT_EQUAL'})
    b.set(b.inp(neq, 'A'), u_raw)
    b.set(b.inp(neq, 'B'), b.out(smp, 'Value'))
    b.inp(neq, 'Epsilon').default_value = 0.01
    first = b.node('FunctionNodeCompare', {'data_type': 'FLOAT', 'operation': 'EQUAL'})
    b.set(b.inp(first, 'A'), idx)
    b.set(b.inp(first, 'B'), 0.0)
    bor = b.node('FunctionNodeBooleanMath', {'operation': 'OR'})
    b.set(bor.inputs[0], neq.outputs[0])
    b.set(bor.inputs[1], first.outputs[0])
    acc_r = b.node('GeometryNodeAccumulateField', {'data_type': 'FLOAT', 'domain': 'INSTANCE'})
    b.set(b.inp(acc_r, 'Value'), bor.outputs[0])
    gs = b.store(gs, 'ae_rank', 'FLOAT', b.m('SUBTRACT', b.out(acc_r, 'Leading'), 1.0))
    srt2 = b.node('GeometryNodeSortElements', {'domain': 'INSTANCE'})
    b.set(b.inp(srt2, 'Geometry'), gs)
    b.set(b.inp(srt2, 'Sort Weight'), b.attr('ae_index', 'INT'))
    geo = b.switch('GEOMETRY', G("Randomize Order"), geo0, srt2.outputs[0])
    u0 = b.switch('FLOAT', G("Randomize Order"), u_raw, b.attr('ae_rank'))

    stat = b.node('GeometryNodeAttributeStatistic', {'data_type': 'FLOAT', 'domain': 'INSTANCE'})
    b.set(b.inp(stat, 'Geometry'), geo)
    b.set(b.inp(stat, 'Attribute'), u0)
    n = b.m('ADD', b.out(stat, 'Max'), 1.0)
    # reverse: count from the last unit
    u = b.switch('FLOAT', G("Reverse"), u0, b.m('SUBTRACT', b.m('SUBTRACT', n, 1.0), u0))

    # Range selector with a soft edge. Units are mapped into [w, 1-w] so that a full
    # range (0-100%) affects every unit completely and an empty one affects none.
    x = b.m('DIVIDE', u, b.m('MAXIMUM', b.m('SUBTRACT', n, 1.0), 1.0))
    w = b.m('MAXIMUM', b.m('MULTIPLY', G("Falloff"), 0.005), 0.0005)
    xp = b.m('MULTIPLY_ADD', x, b.m('SUBTRACT', 1.0, b.m('MULTIPLY', w, 2.0)), w)
    s0 = b.m('DIVIDE', b.m('ADD', b.m('MINIMUM', G("Start"), G("End")), G("Offset")), 100.0)
    e0 = b.m('DIVIDE', b.m('ADD', b.m('MAXIMUM', G("Start"), G("End")), G("Offset")), 100.0)
    up = b.m('DIVIDE', b.m('SUBTRACT', xp, s0), w, clamp=True)
    down = b.m('DIVIDE', b.m('SUBTRACT', e0, xp), w, clamp=True)
    r = b.m('MINIMUM', up, down)
    # Ease High / Ease Low (like AE's Advanced range selector): a Hermite curve on the
    # 0-1 transition of each unit. Slope 1 is linear, slope 0 eases fully at that end.
    t2 = b.m('MULTIPLY', r, r)
    t3 = b.m('MULTIPLY', t2, r)
    h10 = b.m('ADD', b.m('MULTIPLY_ADD', t2, -2.0, r), t3)
    h01 = b.m('MULTIPLY', t2, b.m('MULTIPLY_ADD', r, -2.0, 3.0))
    h11 = b.m('SUBTRACT', t3, t2)
    m_low = b.m('SUBTRACT', 1.0, b.m('DIVIDE', G("Ease Low"), 100.0))
    m_high = b.m('SUBTRACT', 1.0, b.m('DIVIDE', G("Ease High"), 100.0))
    eased = b.m('MULTIPLY_ADD', m_low, h10, b.m('MULTIPLY_ADD', m_high, h11, h01))
    fc = b.node('ShaderNodeFloatCurve')
    fc.name = FALLOFF_NODE
    fc.label = "Falloff"
    b.set(b.inp(fc, 'Factor'), 1.0)
    b.set(b.inp(fc, 'Value'), eased)
    hard = b.m('GREATER_THAN', r, 0.4999)
    v = b.switch('FLOAT', G("Hard"), fc.outputs[0], hard)
    shape = b.switch('FLOAT', G("Invert"), v, b.m('SUBTRACT', 1.0, v))
    sel = b.m('MULTIPLY', shape, b.m('DIVIDE', G("Amount"), 100.0))
    return geo, sel


def _anchor_sockets(P):
    P("Anchor", 'NodeSocketVector', (0.0, -100.0, 0.0),
      desc="Pivot in % of each character's box: X -100 left / 100 right, "
           "Y -100 bottom / 100 top, Z -100 back / 100 front")
    P("Anchor Offset", 'NodeSocketVector', (0.0, 0.0, 0.0), subtype='TRANSLATION',
      desc="Extra pivot offset in scene units")


def build_animator_group(kind):
    ng = _new_group("%s · %s" % (ANIM_PREFIX, KIND_LABEL[kind]), kind)
    ng.description = "After Effects style %s animator (managed by Nakan Text Animator Tools)" % KIND_LABEL[kind]

    ps = ng.interface.new_panel("Selector")
    S = lambda *a, **k: _iface_socket(ng, *a, parent=ps, **k)
    S("Start", 'NodeSocketFloat', 0.0, subtype='PERCENTAGE', min_value=0.0, max_value=100.0)
    S("End", 'NodeSocketFloat', 100.0, subtype='PERCENTAGE', min_value=0.0, max_value=100.0)
    S("Offset", 'NodeSocketFloat', 0.0, subtype='PERCENTAGE', min_value=-100.0, max_value=100.0)
    S("Falloff", 'NodeSocketFloat', 30.0, subtype='PERCENTAGE', min_value=0.0, max_value=100.0,
      desc="Width of the soft edge of the range (0%: hard edge)")
    S("Ease High", 'NodeSocketFloat', 0.0, subtype='PERCENTAGE', min_value=0.0, max_value=100.0,
      desc="Eases each unit's transition where it is fully selected (the animator's values)")
    S("Ease Low", 'NodeSocketFloat', 0.0, subtype='PERCENTAGE', min_value=0.0, max_value=100.0,
      desc="Eases each unit's transition where it is unselected (the text's own resting state)")
    S("Hard", 'NodeSocketBool', False, desc="No interpolation: characters snap in or out")
    S("Based On", 'NodeSocketMenu')
    S("Amount", 'NodeSocketFloat', 100.0, subtype='PERCENTAGE', min_value=-100.0, max_value=100.0)
    S("Invert", 'NodeSocketBool', False,
      desc="Affect the characters outside the range instead")
    S("Reverse", 'NodeSocketBool', False, desc="Start from the last character")
    S("Randomize Order", 'NodeSocketBool', False)
    S("Random Seed", 'NodeSocketInt', 0)

    pp = ng.interface.new_panel("Properties")
    P = lambda *a, **k: _iface_socket(ng, *a, parent=pp, **k)
    if kind == 'OPACITY':
        P("Opacity", 'NodeSocketFloat', 0.0, subtype='PERCENTAGE', min_value=0.0, max_value=100.0)
    elif kind == 'POSITION':
        P("Position", 'NodeSocketVector', (0.0, -0.5, 0.0), subtype='TRANSLATION')
    elif kind == 'SCALE':
        P("Scale", 'NodeSocketVector', (0.0, 0.0, 100.0), desc="Scale in percent")
        _anchor_sockets(P)
    elif kind == 'ROTATION':
        P("Rotation", 'NodeSocketVector', (0.0, 0.0, math.radians(-90.0)), subtype='EULER')
        _anchor_sockets(P)
    elif kind == 'TRACKING':
        P("Tracking", 'NodeSocketFloat', 150.0, desc="Tracking amount in 1/1000 em, like After Effects")
        P("Anchor", 'NodeSocketMenu',
          desc="Which part of each line stays in place while the letters spread: the text's own "
               "alignment, its left edge, its center or its right edge")
    elif kind == 'EXTRUDE':
        P("Extrude", 'NodeSocketFloat', 0.0, subtype='DISTANCE', min_value=0.0,
          desc="Extrude of the selected characters (the Text object's own value elsewhere)")
        P("Depth Anchor", 'NodeSocketFloat', 0.0, subtype='PERCENTAGE', min_value=-100.0, max_value=100.0,
          desc="-100%: back face stays on the text plane (grows forward). 0%: centered. "
               "100%: front face stays on the text plane (grows backward)")
    elif kind == 'BEVEL':
        P("Bevel", 'NodeSocketFloat', 0.0, subtype='DISTANCE', min_value=0.0,
          desc="Bevel depth of the selected characters (the Text object's own value elsewhere)")
    elif kind == 'FILL':
        P("Material", 'NodeSocketMaterial', desc="Material the selected characters blend into")
        P("Fill Attribute", 'NodeSocketString', "ae_fill", hidden=True)

    b = _B(ng)
    gi = b.node('NodeGroupInput')
    go = b.node('NodeGroupOutput')
    G = lambda name: b.out(gi, name)
    geo, sel = _build_selector(ng, b, G)

    if kind == 'OPACITY':
        opf = b.lerp(1.0, b.m('DIVIDE', G("Opacity"), 100.0), sel)
        tr0 = b.attr('ae_transp')
        tr1 = b.m('SUBTRACT', 1.0, b.m('MULTIPLY', b.m('SUBTRACT', 1.0, tr0), opf), clamp=True)
        geo = b.store(geo, 'ae_transp', 'FLOAT', tr1)

    elif kind == 'POSITION':
        tp = b.node('GeometryNodeTranslateInstances')
        b.set(b.inp(tp, 'Instances'), geo)
        b.set(b.inp(tp, 'Translation'), b.v('SCALE', G("Position"), scale=sel))
        b.inp(tp, 'Local Space').default_value = False
        geo = tp.outputs[0]

    elif kind in ('SCALE', 'ROTATION'):
        # pivot inside each character's box (X/Y) and depth (Z), plus an absolute offset
        asep = b.node('ShaderNodeSeparateXYZ')
        b.set(asep.inputs[0], b.v('SCALE', G("Anchor"), scale=0.01))
        bmn = b.node('ShaderNodeSeparateXYZ')
        b.set(bmn.inputs[0], b.attr('ae_bbmin', 'FLOAT_VECTOR'))
        bmx = b.node('ShaderNodeSeparateXYZ')
        b.set(bmx.inputs[0], b.attr('ae_bbmax', 'FLOAT_VECTOR'))
        fx = b.m('MULTIPLY_ADD', asep.outputs[0], 0.5, 0.5)
        fy = b.m('MULTIPLY_ADD', asep.outputs[1], 0.5, 0.5)
        thick = b.m('ADD', b.attr('ae_extrude'), b.attr('ae_bevel'))
        cz = b.m('MULTIPLY', b.m('SUBTRACT', asep.outputs[2], b.attr('ae_zanchor')), thick)
        center = b.v('ADD', b.xyz(b.lerp(bmn.outputs[0], bmx.outputs[0], fx),
                                  b.lerp(bmn.outputs[1], bmx.outputs[1], fy), cz),
                     G("Anchor Offset"))
        if kind == 'SCALE':
            target = b.v('SCALE', G("Scale"), scale=0.01)
            sc = b.v('MULTIPLY_ADD', b.v('SUBTRACT', target, (1.0, 1.0, 1.0)), sel, (1.0, 1.0, 1.0))
            nd = b.node('GeometryNodeScaleInstances')
            b.set(b.inp(nd, 'Instances'), geo)
            b.set(b.inp(nd, 'Scale'), sc)
            b.set(b.inp(nd, 'Center'), center)
        else:
            e2r = b.node('FunctionNodeEulerToRotation')
            b.set(b.inp(e2r, 'Euler'), b.v('SCALE', G("Rotation"), scale=sel))
            nd = b.node('GeometryNodeRotateInstances')
            b.set(b.inp(nd, 'Instances'), geo)
            b.set(b.inp(nd, 'Rotation'), e2r.outputs[0])
            b.set(b.inp(nd, 'Pivot Point'), center)
        b.inp(nd, 'Local Space').default_value = True
        geo = nd.outputs[0]

    elif kind == 'TRACKING':
        tv = b.m('DIVIDE', b.m('MULTIPLY', b.m('MULTIPLY', G("Tracking"), sel), b.attr('ae_size')), 1000.0)
        acc = b.node('GeometryNodeAccumulateField', {'data_type': 'FLOAT', 'domain': 'INSTANCE'})
        b.set(b.inp(acc, 'Value'), tv)
        b.set(b.inp(acc, 'Group Index'), b.attr('ae_line', 'INT'))
        anchor = b.menu('FLOAT', G("Anchor"), [
            ("Text Alignment", b.attr('ae_align')),
            ("Left", 0.0),
            ("Center", 0.5),
            ("Right", 1.0),
        ])
        # the visible spread of a line is the tracking up to its last visible character (the
        # space added after it, trailing spaces and the line break do not show), so an
        # anchored edge or center stays put
        bmn = b.node('ShaderNodeSeparateXYZ')
        b.set(bmn.inputs[0], b.attr('ae_bbmin', 'FLOAT_VECTOR'))
        bmx = b.node('ShaderNodeSeparateXYZ')
        b.set(bmx.inputs[0], b.attr('ae_bbmax', 'FLOAT_VECTOR'))
        vis = b.m('GREATER_THAN', b.m('SUBTRACT', bmx.outputs[0], bmn.outputs[0]), 1e-6)
        cnt = b.node('GeometryNodeAccumulateField', {'data_type': 'FLOAT', 'domain': 'INSTANCE'})
        b.set(b.inp(cnt, 'Value'), vis)
        b.set(b.inp(cnt, 'Group Index'), b.attr('ae_line', 'INT'))
        is_last = b.m('MULTIPLY', vis, b.m('COMPARE', b.out(cnt, 'Leading'), b.out(cnt, 'Total'), 0.5))
        lastv = b.node('GeometryNodeAccumulateField', {'data_type': 'FLOAT', 'domain': 'INSTANCE'})
        b.set(b.inp(lastv, 'Value'), b.m('MULTIPLY', b.out(acc, 'Trailing'), is_last))
        b.set(b.inp(lastv, 'Group Index'), b.attr('ae_line', 'INT'))
        spread = b.out(lastv, 'Total')
        shift = b.m('SUBTRACT', b.out(acc, 'Trailing'), b.m('MULTIPLY', spread, anchor))
        tn = b.node('GeometryNodeTranslateInstances')
        b.set(b.inp(tn, 'Instances'), geo)
        b.set(b.inp(tn, 'Translation'), b.xyz(shift, 0.0, 0.0))
        b.inp(tn, 'Local Space').default_value = False
        geo = tn.outputs[0]

    elif kind in ('EXTRUDE', 'BEVEL'):
        name = 'ae_extrude' if kind == 'EXTRUDE' else 'ae_bevel'
        val = b.lerp(b.attr(name), G(KIND_PROPS[kind][0]), sel)
        geo = b.store(geo, name, 'FLOAT', b.m('MAXIMUM', val, 0.0))
        if kind == 'EXTRUDE':
            geo = b.store(geo, 'ae_zanchor', 'FLOAT', b.m('DIVIDE', G("Depth Anchor"), 100.0))

    elif kind == 'FILL':
        # each Fill animator writes its own blend factor; the generated material reads it
        geo = b.store(geo, G("Fill Attribute"), 'FLOAT', b.m('ADD', sel, 0.0, clamp=True))

    b.set(go.inputs[0], geo)
    return ng


def _get_group(name, builder, *args):
    for ng in bpy.data.node_groups:
        if ng.name == name and ng.get("ae_text_version") == NG_VERSION:
            return ng
    return builder(*args)


def base_group():
    return _get_group(BASE_NAME, build_base_group)


def geometry_group():
    return _get_group(GEO_NAME, build_geometry_group)


def animator_group(kind):
    return _get_group("%s · %s" % (ANIM_PREFIX, KIND_LABEL[kind]), build_animator_group, kind)


# ======================================================================================
# Modifier input access
# ======================================================================================

def _ident(ng, name):
    for it in ng.interface.items_tree:
        if getattr(it, "item_type", None) == 'SOCKET' and it.in_out == 'INPUT' and it.name == name:
            return it.identifier
    return None


def _holder(mod, name):
    if mod.node_group is None:
        return None, None
    ident = _ident(mod.node_group, name)
    if ident is None:
        return None, None
    props = getattr(mod, "properties", None)
    if props is not None and hasattr(props, "inputs"):
        return getattr(props.inputs, ident), "value"
    return mod, '["%s"]' % ident


def get_in(mod, name):
    h, attr = _holder(mod, name)
    if h is None:
        return None
    if attr == "value":
        return h.value
    return h[attr[2:-2]]


def _tag(mod):
    try:
        mod.id_data.update_tag()
    except Exception:
        pass


def set_in(mod, name, value):
    """Set a modifier input only if it changed, and tag the object for re-evaluation."""
    h, attr = _holder(mod, name)
    if h is None:
        return False
    cur = get_in(mod, name)
    try:
        if isinstance(value, float) and cur is not None and abs(cur - value) < 1e-6:
            return False
        if isinstance(value, (tuple, list)) and cur is not None and \
                all(abs(a - b) < 1e-6 for a, b in zip(cur, value)):
            return False
        if not isinstance(value, (float, tuple, list)) and cur == value:
            return False
    except Exception:
        pass
    if attr == "value":
        h.value = value
    else:
        h[attr[2:-2]] = value
    _tag(mod)
    return True


def set_menu(mod, name, candidates):
    h, attr = _holder(mod, name)
    if h is None or attr != "value":
        return
    try:
        items = [e.identifier for e in h.bl_rna.properties["value"].enum_items]
    except Exception:
        return
    for c in candidates:
        if c in items:
            if h.value != c:
                h.value = c
                _tag(mod)
            return


def draw_in(layout, mod, name, text=None):
    h, attr = _holder(mod, name)
    if h is not None:
        layout.prop(h, attr, text=text if text is not None else name)


# ======================================================================================
# Object helpers
# ======================================================================================

def _is_group(mod, prefix):
    return mod.type == 'NODES' and mod.node_group is not None and \
        mod.node_group.name.startswith(prefix)


def find_base(ob):
    for m in ob.modifiers:
        if _is_group(m, BASE_NAME):
            return m
    return None


def find_geometry(ob):
    for m in ob.modifiers:
        if _is_group(m, GEO_NAME):
            return m
    return None


def animators(ob):
    return [m for m in ob.modifiers if _is_group(m, ANIM_PREFIX)]


def anim_kind(mod):
    return mod.node_group.get("ae_kind") if mod.node_group else None


def _move_modifier(ob, mod, index):
    try:
        cur = list(ob.modifiers).index(mod)
        if cur != index:
            ob.modifiers.move(cur, index)
    except Exception:
        pass


def is_outdated(mod):
    return mod.node_group is not None and mod.node_group.get("ae_text_version") != NG_VERSION


def upgrade_animator(ob, mod):
    """Swap an animator built by an older version for a current one, keeping its values."""
    kind = anim_kind(mod)
    if kind not in KIND_LABEL:
        return
    keep = {}
    for it in mod.node_group.interface.items_tree:
        if getattr(it, "item_type", None) == 'SOCKET' and it.in_out == 'INPUT' \
                and it.socket_type not in ('NodeSocketGeometry', 'NodeSocketMenu'):
            try:
                v = get_in(mod, it.name)
                keep[it.name] = tuple(v) if hasattr(v, "__len__") and not isinstance(v, str) else v
            except Exception:
                pass
    based = get_in(mod, "Based On")
    anchor = get_in(mod, "Anchor") if _ident(mod.node_group, "Anchor") is not None else None
    old = mod.node_group
    ng = animator_group(kind).copy()
    ng.name = "%s · %s · %s" % (ANIM_PREFIX, KIND_LABEL[kind], ob.name)
    mod.node_group = ng
    for k, v in keep.items():
        if _ident(ng, k) is not None:
            try:
                set_in(mod, k, v)
            except Exception:
                pass
    set_menu(mod, "Based On", [based] if based else ["Characters"])
    if _ident(ng, "Anchor") is not None:
        set_menu(mod, "Anchor", [anchor] if anchor else ["Text Alignment"])
    if old.users == 0:
        bpy.data.node_groups.remove(old)


def sync_object(ob, mod=None):
    mod = mod or find_base(ob)
    if mod is None:
        return
    cu = ob.data
    set_in(mod, "Text", cu.body)
    for sock, font in (("Font", cu.font), ("Font Bold", cu.font_bold or cu.font),
                       ("Font Italic", cu.font_italic or cu.font),
                       ("Font Bold Italic", cu.font_bold_italic or cu.font)):
        if font is not None and _ident(mod.node_group, sock) is not None and get_in(mod, sock) != font:
            set_in(mod, sock, font)
    if _ident(mod.node_group, "Format") is not None:
        codes, mixed = [], False
        top = max(0, min(len(ob.material_slots), MAX_SLOTS) - 1)
        for f in cu.body_format:
            st = int(f.use_bold) + 2 * int(f.use_italic)
            mixed = mixed or st > 0
            # newer Blender versions report -1 for "no slot set": that is slot 0, and an index
            # past the last slot falls back to it too (as Blender itself does)
            mi = f.material_index
            mi = 0 if mi < 0 else min(mi, top)
            codes.append(FORMAT_ALPHABET[st + 4 * mi])
        set_in(mod, "Format", "".join(codes))
        set_in(mod, "Mixed Styles", mixed)
    set_in(mod, "Size", float(cu.size))
    set_in(mod, "Character Spacing", float(cu.space_character))
    set_in(mod, "Word Spacing", float(cu.space_word))
    set_in(mod, "Line Spacing", float(cu.space_line))
    set_menu(mod, "Align X", [ALIGN_X.get(cu.align_x, 'Left')])
    set_menu(mod, "Align Y", ALIGN_Y.get(cu.align_y, ['Top Baseline']))
    set_in(mod, "Align Factor", ALIGN_FAC.get(cu.align_x, 0.0))
    if len(cu.text_boxes):
        tb = cu.text_boxes[0]
        set_in(mod, "Text Box Width", float(tb.width))
        set_in(mod, "Text Box Height", float(tb.height))
    set_in(mod, "Offset", (float(cu.offset_x), float(cu.offset_y), 0.0))
    set_in(mod, "Extrude", float(cu.extrude))
    set_in(mod, "Bevel", float(cu.bevel_depth))
    geo = find_geometry(ob)
    if geo is not None:
        set_in(geo, "Fill Mode", FILL_MODES.get(cu.fill_mode, 3))
        set_in(geo, "Bevel Resolution", int(cu.bevel_resolution))


# ======================================================================================
# Materials: the user's materials are never edited. The add-on mirrors each one into a
# shader node group and builds a per-object material that blends them per character.
# ======================================================================================

_NODE_SKIP = {
    'rna_type', 'name', 'label', 'location', 'location_absolute', 'width', 'height',
    'dimensions', 'parent', 'select', 'show_options', 'show_preview', 'show_texture',
    'hide', 'type', 'is_active_output', 'warning_propagation', 'bl_idname', 'bl_label',
    'bl_description', 'bl_icon', 'bl_static_type', 'bl_width_default', 'bl_width_min',
    'bl_width_max', 'bl_height_default', 'bl_height_min', 'bl_height_max',
}
_SIG_SKIP = _NODE_SKIP | {'label'}


def _val(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if hasattr(v, "name") and isinstance(getattr(v, "name"), str):
        return "ID:" + v.name
    try:
        return tuple(round(c, 5) if isinstance(c, float) else c for c in v)
    except Exception:
        return str(v)


def _tree_sig(nt):
    parts = []
    for n in nt.nodes:
        parts.append((n.bl_idname, n.name, n.parent.name if n.parent else None, n.mute))
        for p in n.bl_rna.properties:
            if p.identifier in _SIG_SKIP or p.type == 'COLLECTION':
                continue
            if p.type == 'POINTER' and p.is_readonly:
                continue
            try:
                parts.append((p.identifier, _val(getattr(n, p.identifier))))
            except Exception:
                pass
        for s in list(n.inputs) + list(n.outputs):
            if hasattr(s, "default_value"):
                parts.append(_val(s.default_value))
        cr = getattr(n, "color_ramp", None)
        if cr is not None:
            parts.append((cr.interpolation, cr.color_mode,
                          [(round(e.position, 5), _val(e.color)) for e in cr.elements]))
        mp = getattr(n, "mapping", None)
        if mp is not None and hasattr(mp, "curves"):
            parts.append([[(_val(pt.location), pt.handle_type) for pt in c.points] for c in mp.curves])
    for l in nt.links:
        parts.append((l.from_node.name, l.from_socket.identifier, l.to_node.name,
                      l.to_socket.identifier, l.is_muted))
    return hashlib.md5(repr(parts).encode()).hexdigest()


def _copy_struct(a, b):
    for p in a.bl_rna.properties:
        if p.is_readonly or p.type in ('POINTER', 'COLLECTION') or p.identifier == 'rna_type':
            continue
        try:
            setattr(b, p.identifier, getattr(a, p.identifier))
        except Exception:
            pass


def _sock(socks, ident):
    for s in socks:
        if s.identifier == ident:
            return s
    return None


def _copy_node(src, tree):
    n = tree.nodes.new(src.bl_idname)
    n.name = src.name
    n.label = src.label
    n.location = src.location
    n.width = src.width
    for _ in range(2):      # some properties only become valid after others are set
        for p in src.bl_rna.properties:
            if p.is_readonly or p.identifier in _NODE_SKIP or p.type == 'COLLECTION':
                continue
            try:
                setattr(n, p.identifier, getattr(src, p.identifier))
            except Exception:
                pass
    cr_s, cr_d = getattr(src, "color_ramp", None), getattr(n, "color_ramp", None)
    if cr_s is not None and cr_d is not None:
        _copy_struct(cr_s, cr_d)
        while len(cr_d.elements) > 1:
            cr_d.elements.remove(cr_d.elements[-1])
        for i, e in enumerate(cr_s.elements):
            d = cr_d.elements[0] if i == 0 else cr_d.elements.new(e.position)
            d.position = e.position
            d.color = e.color
    mp_s, mp_d = getattr(src, "mapping", None), getattr(n, "mapping", None)
    if mp_s is not None and mp_d is not None and hasattr(mp_s, "curves"):
        _copy_struct(mp_s, mp_d)
        for cs, cd in zip(mp_s.curves, mp_d.curves):
            while len(cd.points) < len(cs.points):
                cd.points.new(0.0, 0.0)
            while len(cd.points) > len(cs.points):
                cd.points.remove(cd.points[-1])
            for ps, pd in zip(cs.points, cd.points):
                pd.location = ps.location
                pd.handle_type = ps.handle_type
        try:
            mp_d.update()
        except Exception:
            pass
    for name in ("texture_mapping", "color_mapping", "image_user"):
        a, c = getattr(src, name, None), getattr(n, name, None)
        if a is not None and c is not None:
            _copy_struct(a, c)
    for socks_s, socks_d in ((src.inputs, n.inputs), (src.outputs, n.outputs)):
        for s in socks_s:
            d = _sock(socks_d, s.identifier)
            if d is not None and hasattr(s, "default_value"):
                try:
                    d.default_value = s.default_value
                except Exception:
                    pass
            if d is not None:
                d.hide = s.hide
    n.mute = src.mute
    n.hide = src.hide
    return n


def _material_output(nt):
    for target in ('EEVEE', 'ALL', 'CYCLES'):
        try:
            o = nt.get_output_node(target)
        except Exception:
            o = None
        if o is not None:
            return o
    for n in nt.nodes:
        if n.bl_idname == 'ShaderNodeOutputMaterial':
            return n
    return None


def ensure_source_group(mat):
    """Mirror a material's node tree into a shader node group (refreshed when it changes)."""
    nt = mat.node_tree if mat is not None else None
    gname = SRC_PREFIX + (mat.name if mat is not None else "Default")
    sig = _tree_sig(nt) if nt is not None else "default"
    g = bpy.data.node_groups.get(gname)
    if g is not None and g.get("ae_sig") == sig and g.bl_idname == 'ShaderNodeTree':
        return g
    if g is None:
        g = bpy.data.node_groups.new(gname, 'ShaderNodeTree')
    g.nodes.clear()
    # keep the interface stable so links in the generated material survive a refresh
    names = [it.name for it in g.interface.items_tree if getattr(it, "item_type", None) == 'SOCKET']
    if names != ["Surface", "Displacement"]:
        g.interface.clear()
        g.interface.new_socket("Surface", in_out='OUTPUT', socket_type='NodeSocketShader')
        g.interface.new_socket("Displacement", in_out='OUTPUT', socket_type='NodeSocketVector')
    go = g.nodes.new('NodeGroupOutput')
    has_disp = False
    if nt is None:
        pb = g.nodes.new('ShaderNodeBsdfPrincipled')
        g.links.new(pb.outputs[0], go.inputs["Surface"])
    else:
        out = _material_output(nt)
        mapping = {}
        for n in nt.nodes:
            if n.bl_idname == 'ShaderNodeOutputMaterial':
                continue
            mapping[n.name] = _copy_node(n, g)
        for n in nt.nodes:
            if n.parent is not None and n.name in mapping and n.parent.name in mapping:
                mapping[n.name].parent = mapping[n.parent.name]
        for l in nt.links:
            if l.to_node.bl_idname == 'ShaderNodeOutputMaterial':
                if l.to_node == out and l.from_node.name in mapping:
                    fs = _sock(mapping[l.from_node.name].outputs, l.from_socket.identifier)
                    if fs is not None and l.to_socket.name in ("Surface", "Displacement"):
                        g.links.new(fs, go.inputs[l.to_socket.name])
                        has_disp = has_disp or l.to_socket.name == "Displacement"
                continue
            fn, tn = mapping.get(l.from_node.name), mapping.get(l.to_node.name)
            if fn is None or tn is None:
                continue
            fs, ts = _sock(fn.outputs, l.from_socket.identifier), _sock(tn.inputs, l.to_socket.identifier)
            if fs is not None and ts is not None:
                nl = g.links.new(fs, ts)
                nl.is_muted = l.is_muted
        if out is not None:
            go.location = (out.location.x, out.location.y)
    g["ae_sig"] = sig
    g["ae_has_disp"] = has_disp
    return g


def _attr_sum(nt, name, out, x, y):
    a = nt.nodes.new('ShaderNodeAttribute')
    a.attribute_type = 'GEOMETRY'
    a.attribute_name = name
    a.location = (x, y)
    i = nt.nodes.new('ShaderNodeAttribute')
    i.attribute_type = 'INSTANCER'
    i.attribute_name = name
    i.location = (x, y - 180)
    s = nt.nodes.new('ShaderNodeMath')
    s.operation = 'ADD'
    s.use_clamp = True
    s.location = (x + 200, y)
    nt.links.new(a.outputs[out], s.inputs[0])
    nt.links.new(i.outputs[out], s.inputs[1])
    return s.outputs[0]


_MAT_SETTINGS = ("use_backface_culling", "use_backface_culling_shadow", "use_backface_culling_lightprobe_volume",
                 "displacement_method", "pass_index", "use_screen_refraction", "use_raytrace_refraction",
                 "thickness_mode", "use_thickness_from_shadow", "use_transparency_overlap",
                 "diffuse_color", "metallic", "roughness", "volume_intersection_method")


def ensure_object_material(ob, slot=0):
    """Build/refresh the material that blends a slot's material with every Fill target."""
    base_mat = ob.material_slots[slot].material if len(ob.material_slots) > slot else None
    if base_mat is not None and base_mat.name.startswith(WRAP_PREFIX):
        base_mat = None
    fills = []
    for mod in animators(ob):
        if anim_kind(mod) == 'FILL' and mod.show_viewport:
            target = get_in(mod, "Material")
            if target is not None and not target.name.startswith(WRAP_PREFIX):
                fills.append((get_in(mod, "Fill Attribute"), target))
    ga = ensure_source_group(base_mat)
    gfs = [(attr, ensure_source_group(m)) for attr, m in fills]
    sig = repr((ga.name, [(a, g.name) for a, g in gfs], base_mat.name if base_mat else None))
    name = WRAP_PREFIX + ob.name + ("" if slot == 0 else " · %d" % (slot + 1))
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    if base_mat is not None:
        for p in _MAT_SETTINGS:
            try:
                setattr(mat, p, getattr(base_mat, p))
            except Exception:
                pass
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'BLENDED' if base_mat.surface_render_method == 'BLENDED' else 'DITHERED'
    if mat.get("ae_sig") == sig and mat.node_tree is not None and len(mat.node_tree.nodes):
        return mat
    try:
        mat.use_nodes = True
    except Exception:
        pass
    nt = mat.node_tree
    nt.nodes.clear()
    x = 0
    na = nt.nodes.new('ShaderNodeGroup')
    na.node_tree = ga
    na.location = (x, 0)
    shader, disp = na.outputs["Surface"], na.outputs["Displacement"]
    any_disp = bool(ga.get("ae_has_disp"))
    for k, (attr, g) in enumerate(gfs):
        x += 400
        nb = nt.nodes.new('ShaderNodeGroup')
        nb.node_tree = g
        nb.location = (x - 200, -300)
        f = _attr_sum(nt, attr, 'Fac', x - 400, 300)
        ms = nt.nodes.new('ShaderNodeMixShader')
        ms.location = (x, 0)
        nt.links.new(f, ms.inputs[0])
        nt.links.new(shader, ms.inputs[1])
        nt.links.new(nb.outputs["Surface"], ms.inputs[2])
        shader = ms.outputs[0]
        if g.get("ae_has_disp") or any_disp:
            md = nt.nodes.new('ShaderNodeMix')
            md.data_type = 'VECTOR'
            md.location = (x, -500)
            nt.links.new(f, md.inputs['Factor'])
            nt.links.new(disp, _sock(md.inputs, 'A_Vector'))
            nt.links.new(nb.outputs["Displacement"], _sock(md.inputs, 'B_Vector'))
            disp = _sock(md.outputs, 'Result_Vector')
            any_disp = True
    x += 400
    transp = _attr_sum(nt, 'ae_transp', 'Fac', x - 400, 300)
    tb = nt.nodes.new('ShaderNodeBsdfTransparent')
    tb.location = (x - 200, -300)
    mt = nt.nodes.new('ShaderNodeMixShader')
    mt.location = (x, 0)
    nt.links.new(transp, mt.inputs[0])
    nt.links.new(shader, mt.inputs[1])
    nt.links.new(tb.outputs[0], mt.inputs[2])
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (x + 250, 0)
    nt.links.new(mt.outputs[0], out.inputs["Surface"])
    if any_disp:
        nt.links.new(disp, out.inputs["Displacement"])
    mat["ae_sig"] = sig
    return mat


def sync_materials(ob):
    geo = find_geometry(ob)
    if geo is None:
        return
    n = max(1, min(MAX_SLOTS, len(ob.material_slots)))
    for k in range(MAX_SLOTS):
        mat = ensure_object_material(ob, k) if k < n else None
        if _ident(geo.node_group, "Material %d" % k) is None:
            if k == 0 and get_in(geo, "Material") != mat:    # groups from older versions
                set_in(geo, "Material", mat)
            continue
        if get_in(geo, "Material %d" % k) != mat:
            set_in(geo, "Material %d" % k, mat)


# ======================================================================================
# Live sync (also while typing in Edit Mode)
# ======================================================================================

_tick = [0]


def _sync_timer():
    _tick[0] += 1
    try:
        for ob in bpy.data.objects:
            if ob.type != 'FONT':
                continue
            mod = find_base(ob)
            if mod is None:
                continue
            if ob.mode == 'EDIT':
                ob.update_from_editmode()
            sync_object(ob, mod)
            if _tick[0] % 4 == 0:
                sync_materials(ob)
    except Exception:
        pass
    return 0.1


# ======================================================================================
# Operators
# ======================================================================================

def _font_poll(context):
    ob = context.active_object
    return ob is not None and ob.type == 'FONT'


class AETEXT_OT_make(bpy.types.Operator):
    bl_idname = "ae_text.make"
    bl_label = "Make Animatable Text"
    bl_description = "Turn this Text object into an After Effects style animatable text (stays editable)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _font_poll(context) and find_base(context.active_object) is None

    def execute(self, context):
        ob = context.active_object
        if not len(ob.material_slots) or ob.material_slots[0].material is None:
            mat = bpy.data.materials.new("Text Material")
            try:
                mat.use_nodes = True
            except Exception:
                pass
            if len(ob.material_slots):
                ob.material_slots[0].material = mat
            else:
                ob.data.materials.append(mat)
        base = ob.modifiers.new(BASE_MOD, 'NODES')
        base.node_group = base_group()
        base.show_in_editmode = True
        _move_modifier(ob, base, 0)
        geo = ob.modifiers.new(GEO_MOD, 'NODES')
        geo.node_group = geometry_group()
        geo.show_in_editmode = True
        sync_object(ob, base)
        sync_materials(ob)
        return {'FINISHED'}


class AETEXT_OT_add_animator(bpy.types.Operator):
    bl_idname = "ae_text.add_animator"
    bl_label = "Add Animator"
    bl_description = "Add an After Effects style animator with its own range selector"
    bl_options = {'REGISTER', 'UNDO'}

    kind: bpy.props.EnumProperty(items=[k[:4] + (i,) for i, k in enumerate(ANIM_KINDS)], name="Property")

    @classmethod
    def poll(cls, context):
        return _font_poll(context) and find_base(context.active_object) is not None

    def execute(self, context):
        ob = context.active_object
        count = len(animators(ob)) + 1
        mod = ob.modifiers.new("Animator %d - %s" % (count, KIND_LABEL[self.kind]), 'NODES')
        # every animator gets its own copy so its falloff curve is independent
        ng = animator_group(self.kind).copy()
        ng.name = "%s · %s · %s" % (ANIM_PREFIX, KIND_LABEL[self.kind], ob.name)
        mod.node_group = ng
        mod.show_in_editmode = True
        mods = list(ob.modifiers)
        last = max(i for i, m in enumerate(mods)
                   if m != mod and (_is_group(m, BASE_NAME) or _is_group(m, ANIM_PREFIX)))
        _move_modifier(ob, mod, last + 1)
        set_menu(mod, "Based On", ["Characters"])
        if self.kind == 'POSITION':
            set_in(mod, "Position", (0.0, -0.5 * ob.data.size, 0.0))
        if self.kind == 'FILL':
            set_in(mod, "Fill Attribute", "ae_fill_" + uuid.uuid4().hex[:8])
            fm = bpy.data.materials.new("Fill Material")
            try:
                fm.use_nodes = True
                bsdf = next(n for n in fm.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
                bsdf.inputs["Base Color"].default_value = (1.0, 0.25, 0.05, 1.0)
            except Exception:
                pass
            fm.diffuse_color = (1.0, 0.25, 0.05, 1.0)
            set_in(mod, "Material", fm)
            sync_materials(ob)
        mod.show_expanded = True
        return {'FINISHED'}


class AETEXT_OT_remove_animator(bpy.types.Operator):
    bl_idname = "ae_text.remove_animator"
    bl_label = "Remove Animator"
    bl_options = {'REGISTER', 'UNDO'}
    name: bpy.props.StringProperty()

    def execute(self, context):
        ob = context.active_object
        m = ob.modifiers.get(self.name)
        if m:
            ob.modifiers.remove(m)
            sync_materials(ob)
        return {'FINISHED'}


class AETEXT_OT_resync(bpy.types.Operator):
    bl_idname = "ae_text.resync"
    bl_label = "Resync"
    bl_description = "Re-sync text settings, upgrade old animators and rebuild the generated material"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _font_poll(context) and find_base(context.active_object) is not None

    def execute(self, context):
        ob = context.active_object
        for mod in animators(ob):
            if is_outdated(mod):
                upgrade_animator(ob, mod)
        base, geo = find_base(ob), find_geometry(ob)
        if base is not None and is_outdated(base):
            base.node_group = base_group()
        if geo is not None and is_outdated(geo):
            geo.node_group = geometry_group()
        for g in bpy.data.node_groups:
            if g.name.startswith(SRC_PREFIX) and "ae_sig" in g:
                del g["ae_sig"]
        m = bpy.data.materials.get(WRAP_PREFIX + ob.name)
        if m is not None and "ae_sig" in m:
            del m["ae_sig"]
        sync_object(ob)
        sync_materials(ob)
        return {'FINISHED'}


class AETEXT_OT_add_geometry(bpy.types.Operator):
    bl_idname = "ae_text.add_geometry"
    bl_label = "Add Text Geometry"
    bl_description = "Add the step that turns the animated curves into a mesh (fill + extrude)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _font_poll(context) and find_base(context.active_object) is not None \
            and find_geometry(context.active_object) is None

    def execute(self, context):
        ob = context.active_object
        geo = ob.modifiers.new(GEO_MOD, 'NODES')
        geo.node_group = geometry_group()
        geo.show_in_editmode = True
        mods = list(ob.modifiers)
        last = max(i for i, m in enumerate(mods)
                   if m != geo and (_is_group(m, BASE_NAME) or _is_group(m, ANIM_PREFIX)))
        _move_modifier(ob, geo, last + 1)
        sync_object(ob)
        sync_materials(ob)
        return {'FINISHED'}


class AETEXT_MT_add(bpy.types.Menu):
    bl_idname = "AETEXT_MT_add"
    bl_label = "Add Animator"

    def draw(self, context):
        for k in ANIM_KINDS:
            self.layout.operator(AETEXT_OT_add_animator.bl_idname, text=k[1], icon=k[3]).kind = k[0]


# ======================================================================================
# UI
# ======================================================================================

def draw_ui(self, context):
    layout = self.layout
    ob = context.active_object
    if ob is None or ob.type != 'FONT':
        layout.label(text="Select a Text object")
        return
    base = find_base(ob)
    if base is None:
        layout.operator(AETEXT_OT_make.bl_idname, icon='FONT_DATA')
        layout.label(text="The text stays editable (Tab)", icon='INFO')
        return

    row = layout.row(align=True)
    row.menu(AETEXT_MT_add.bl_idname, icon='ADD', text="Add Animator")
    row.operator(AETEXT_OT_resync.bl_idname, icon='FILE_REFRESH', text="")

    for mod in animators(ob):
        kind = anim_kind(mod)
        box = layout.box()
        hdr = box.row(align=True)
        hdr.prop(mod, "show_expanded", text="", emboss=False,
                 icon='TRIA_DOWN' if mod.show_expanded else 'TRIA_RIGHT')
        hdr.prop(mod, "name", text="")
        hdr.prop(mod, "show_viewport", text="")
        hdr.prop(mod, "show_render", text="")
        hdr.operator(AETEXT_OT_remove_animator.bl_idname, text="", icon='X').name = mod.name
        if is_outdated(mod):
            box.label(text="Made with an older version: click Resync to upgrade", icon='ERROR')
        if not mod.show_expanded:
            continue
        col = box.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = True
        col.label(text="Selector", icon='SELECT_SET')
        for n in ("Start", "End", "Offset", "Falloff"):
            draw_in(col, mod, n)
        if not get_in(mod, "Hard"):
            for n in ("Ease High", "Ease Low"):
                draw_in(col, mod, n)
        draw_in(col, mod, "Hard", text="Hard (no falloff)")
        fc = mod.node_group.nodes.get(FALLOFF_NODE) if mod.node_group else None
        if fc is not None and not get_in(mod, "Hard"):
            box.label(text="Falloff curve (range edge -> inside)")
            box.template_curve_mapping(fc, "mapping")
        col = box.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = True
        for n in ("Based On", "Amount"):
            draw_in(col, mod, n)
        col.separator()
        draw_in(col, mod, "Invert")
        for n in ("Reverse", "Randomize Order"):
            draw_in(col, mod, n)
        if get_in(mod, "Randomize Order"):
            draw_in(col, mod, "Random Seed", text="Seed")
        col = box.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = True
        col.label(text=KIND_LABEL.get(kind, "Properties"), icon='PROPERTIES')
        for n in KIND_PROPS.get(kind, []):
            draw_in(col, mod, n)

    geo = find_geometry(ob)
    box = layout.box()
    box.label(text="Geometry (rest state)", icon='MESH_DATA')
    if geo is None:
        box.label(text="No mesh step: the text stays as curves.", icon='INFO')
        box.operator(AETEXT_OT_add_geometry.bl_idname, icon='ADD')
    else:
        if is_outdated(geo) or is_outdated(base):
            box.label(text="Made with an older version: click Resync to upgrade", icon='ERROR')
        col = box.column(align=True)
        col.use_property_split = True
        col.label(text="Starting values for the animators", icon='INFO')
        col.prop(ob.data, "fill_mode")
        col.prop(ob.data, "extrude")
        col.prop(ob.data, "bevel_depth", text="Bevel Depth")
        col.prop(ob.data, "bevel_resolution", text="Bevel Resolution")
        if ob.data.bevel_mode != 'ROUND':
            box.label(text="Only Round bevel is supported", icon='INFO')


class AETEXT_PT_properties(bpy.types.Panel):
    bl_label = "Nakan Text Animator Tools"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"

    @classmethod
    def poll(cls, context):
        return _font_poll(context)

    draw = draw_ui


classes = (
    AETEXT_OT_make, AETEXT_OT_add_animator, AETEXT_OT_remove_animator, AETEXT_OT_resync,
    AETEXT_OT_add_geometry, AETEXT_MT_add, AETEXT_PT_properties,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    if not bpy.app.timers.is_registered(_sync_timer):
        bpy.app.timers.register(_sync_timer, first_interval=0.5, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_sync_timer):
        bpy.app.timers.unregister(_sync_timer)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
