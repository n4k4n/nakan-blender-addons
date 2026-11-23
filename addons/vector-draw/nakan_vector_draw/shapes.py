"""Live shapes: rectangles, ellipses / drops, polygons / stars that stay editable after creation.

The parameters live on the curve object (Object > nkpf_shape). Changing one rewrites the curve's
Bezier spline in place, so the add-on's Extrude / Thickness solids (which rebuild when the curve data
changes) follow. Shapes are built in the object's local XY plane, centred on the origin: Vector Draw
gives its shapes local axes = CPlane axes and the origin at the centre, so this is the CPlane.
"""
import math

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty,
                       StringProperty)
from mathutils import Matrix, Vector

from . import common

KAPPA = 0.5522847498
KINDS = [
    ('RECT', "Rectangle", "Rectangle with rounded corners", 'MESH_PLANE', 0),
    ('ELLIPSE', "Ellipse", "Ellipse that can turn into a drop", 'MESH_CIRCLE', 1),
    ('POLY', "Polygon", "Polygon that can turn into a star", 'SEQ_CHROMA_SCOPE', 2),
]
NAMES = {'RECT': "Rectangle", 'ELLIPSE': "Ellipse", 'POLY': "Polygon"}
_suspend = [0]          # >0: property updates do not rebuild (used while filling the values in)


# ------------------------------------------------------------------------------------ geometry
def _P(co, hl=None, hr=None, lt='VECTOR', rt='VECTOR'):
    co = Vector(co)
    return {"co": co, "hl": Vector(hl) if hl is not None else co.copy(),
            "hr": Vector(hr) if hr is not None else co.copy(), "lt": lt, "rt": rt}


def rounded_polygon(corners, radii):
    """Closed polygon (2D Vectors, in order) with a circular fillet of radius radii[i] on each corner.
    Sharp corners are VECTOR points (Vector Draw 'corners': its live-corner dots still work on them).
    Fillets that would overlap are shrunk so they just meet."""
    n = len(corners)
    info = []
    for i in range(n):
        c, a, b = corners[i], corners[i - 1], corners[(i + 1) % n]
        u1, u2 = a - c, b - c
        l1, l2 = u1.length, u2.length
        if l1 < 1e-12 or l2 < 1e-12:
            info.append(None)
            continue
        u1, u2 = u1 / l1, u2 / l2
        th = u1.angle(u2)                      # inner angle of the corner
        r = max(0.0, radii[i])
        t = r / math.tan(th / 2) if (r > 1e-12 and 1e-6 < th < math.pi - 1e-6) else 0.0
        info.append([u1, u2, th, t, l2])       # l2 = length of the edge to the next corner
    for _ in range(4):                         # fillets on both ends of an edge must fit in it
        fac = [1.0] * n                        # (all corners at once, so the result stays symmetric)
        for i in range(n):
            j = (i + 1) % n
            if info[i] is None or info[j] is None:
                continue
            s = info[i][3] + info[j][3]
            if s > info[i][4] > 0:
                f = info[i][4] / s
                fac[i] = min(fac[i], f)
                fac[j] = min(fac[j], f)
        if all(f >= 1.0 for f in fac):
            break
        for i in range(n):
            if info[i] is not None:
                info[i][3] *= fac[i]
    pts = []
    for i in range(n):
        c, inf = corners[i], info[i]
        if inf is None or inf[3] < 1e-9:
            pts.append(_P(c.to_3d()))
            continue
        u1, u2, th, t, _l = inf
        r = t * math.tan(th / 2)
        h = 4.0 / 3.0 * math.tan((math.pi - th) / 4) * r
        p1, p2 = c + u1 * t, c + u2 * t
        pts.append(_P(p1.to_3d(), None, (p1 - u1 * h).to_3d(), 'VECTOR', 'FREE'))
        pts.append(_P(p2.to_3d(), (p2 - u2 * h).to_3d(), None, 'FREE', 'VECTOR'))
    # two fillets meeting on an edge: one smooth point instead of two on top of each other
    out = []
    for p in pts:
        if out and (out[-1]["co"] - p["co"]).length < 1e-7 and out[-1]["rt"] == 'VECTOR' and p["lt"] == 'VECTOR':
            q = out[-1]
            q["hr"], q["rt"] = p["hr"], p["rt"]
            q["lt"] = q["rt"] = 'ALIGNED'
            continue
        out.append(p)
    if len(out) > 2 and (out[0]["co"] - out[-1]["co"]).length < 1e-7 and out[-1]["rt"] == 'VECTOR' \
            and out[0]["lt"] == 'VECTOR':
        q = out.pop()
        out[0]["hl"] = q["hl"]
        out[0]["lt"] = out[0]["rt"] = 'ALIGNED'
    return out


def rect_points(sh):
    w, h = sh.width / 2, sh.height / 2
    corners = [Vector((-w, -h)), Vector((w, -h)), Vector((w, h)), Vector((-w, h))]   # BL BR TR TL
    if sh.per_corner:
        radii = [sh.r_bl, sh.r_br, sh.r_tr, sh.r_tl]
    else:
        radii = [sh.radius] * 4
    return rounded_polygon(corners, radii)


def poly_points(sh):
    n = max(3, sh.sides)
    a0 = -math.pi / 2 + math.pi / n + sh.angle      # flat bottom edge, like the add-on's polygon
    sx, sy = sh.width / 2, sh.height / 2
    corners, radii = [], []
    for k in range(n):
        a = a0 + 2 * math.pi * k / n
        corners.append(Vector((math.cos(a) * sx, math.sin(a) * sy)))
        radii.append(sh.radius)
        if sh.star:
            a = a0 + 2 * math.pi * k / n + math.pi / n
            corners.append(Vector((math.cos(a) * sx * sh.inner, math.sin(a) * sy * sh.inner)))
            radii.append(sh.radius_inner)
    return rounded_polygon(corners, radii)


def _bez(p0, p1, p2, p3, t):
    u = 1 - t
    return p0 * u ** 3 + p1 * 3 * u * u * t + p2 * 3 * u * t * t + p3 * t ** 3


def _bbox(pts):
    xs, ys = [], []
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for k in range(33):
            p = _bez(a["co"], a["hr"], b["hl"], b["co"], k / 32)
            xs.append(p.x)
            ys.append(p.y)
    return min(xs), max(xs), min(ys), max(ys)


def ellipse_points(sh):
    """Unit circle (4 smooth points, like the add-on's circle); Drop pulls the top point into a tip.
    The result is fitted into width x height."""
    d = max(0.0, min(1.0, sh.drop))
    k = KAPPA
    side = k * (1 + 0.6 * d)                   # the sides stay round while the tip goes up
    pts = [
        _P((1, 0, 0), (1, -k, 0), (1, side, 0), 'ALIGNED', 'ALIGNED'),
        None,
        _P((-1, 0, 0), (-1, side, 0), (-1, -k, 0), 'ALIGNED', 'ALIGNED'),
        _P((0, -1, 0), (-k, -1, 0), (k, -1, 0), 'ALIGNED', 'ALIGNED'),
    ]
    if d < 1e-6:
        pts[1] = _P((0, 1, 0), (k, 1, 0), (-k, 1, 0), 'ALIGNED', 'ALIGNED')
    else:
        tip = Vector((0, 1 + 1.1 * d, 0))
        ang = math.radians(58) * d              # the tip handles turn down: 0 = round, 58 deg = sharp drop
        ln = k * (1 - 0.45 * d)
        dx, dy = math.cos(ang) * ln, -math.sin(ang) * ln
        pts[1] = _P(tip, tip + Vector((dx, dy, 0)), tip + Vector((-dx, dy, 0)), 'FREE', 'FREE')
    x0, x1, y0, y1 = _bbox(pts)
    sx = sh.width / max(1e-9, x1 - x0)
    sy = sh.height / max(1e-9, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    M = Matrix.Diagonal((sx, sy, 1, 1)) @ Matrix.Translation((-cx, -cy, 0))
    for p in pts:
        for key in ("co", "hl", "hr"):
            p[key] = M @ p[key]
    return pts


BUILDERS = {'RECT': rect_points, 'ELLIPSE': ellipse_points, 'POLY': poly_points}


def _values(cu):
    vals = []
    for s in cu.splines:
        if s.type != 'BEZIER':
            continue
        vals.append(-1e9)                     # spline separator
        for bp in s.bezier_points:
            for v in (bp.co, bp.handle_left, bp.handle_right):
                vals += [v.x, v.y, v.z]
    return vals


def signature(cu):
    return " ".join("%.5g" % v for v in _values(cu))


def same_signature(sig, cu):
    try:
        a = [float(x) for x in sig.split()]
    except ValueError:
        return False
    b = _values(cu)
    if len(a) != len(b):
        return False
    scale = max([abs(x) for x in b if x > -1e8] + [1e-6])
    return all(abs(x - y) <= 1e-4 * scale + 1e-6 for x, y in zip(a, b))


def write(ob):
    sh = ob.nkpf_shape
    cu = ob.data
    pts = BUILDERS[sh.kind](sh)
    n = len(pts)
    for i, p in enumerate(pts):              # where Blender would put VECTOR handles (1/3 to the neighbour)
        if p["lt"] == 'VECTOR':
            p["hl"] = p["co"] + (pts[i - 1]["co"] - p["co"]) / 3
        if p["rt"] == 'VECTOR':
            p["hr"] = p["co"] + (pts[(i + 1) % n]["co"] - p["co"]) / 3
    old = next((s for s in cu.splines if s.type == 'BEZIER'), None)
    attrs = {}
    if old is not None:
        attrs = {"material_index": old.material_index, "resolution_u": old.resolution_u,
                 "use_smooth": old.use_smooth}
    for s in list(cu.splines):
        cu.splines.remove(s)
    s = cu.splines.new('BEZIER')
    s.bezier_points.add(len(pts) - 1)
    for bp, p in zip(s.bezier_points, pts):
        bp.handle_left_type = bp.handle_right_type = 'FREE'
        bp.co = p["co"]
        bp.handle_left = p["hl"]
        bp.handle_right = p["hr"]
    s.use_cyclic_u = True
    for k, v in attrs.items():
        setattr(s, k, v)
    for bp, p in zip(s.bezier_points, pts):
        if p["lt"] == 'VECTOR':
            bp.handle_left_type = 'VECTOR'
        if p["rt"] == 'VECTOR':
            bp.handle_right_type = 'VECTOR'
        if p["lt"] == 'ALIGNED' and p["rt"] == 'ALIGNED':
            bp.handle_left_type = bp.handle_right_type = 'ALIGNED'
    cu.update_tag()
    ob.update_tag()
    sh.sig = signature(cu)


def _upd(self, context):
    if _suspend[0]:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object) and ob.type == 'CURVE' and self.live:
        write(ob)


def _upd_per_corner(self, context):
    if self.per_corner and not _suspend[0]:
        _suspend[0] += 1
        try:                                   # the four corners start from the shared roundness
            self.r_tl = self.r_tr = self.r_bl = self.r_br = self.radius
        finally:
            _suspend[0] -= 1
    _upd(self, context)


def _size(name, default=1.0):
    return FloatProperty(name=name, default=default, min=0.0001, soft_max=100, unit='LENGTH',
                         subtype='DISTANCE', update=_upd, precision=3)


def _rad(name):
    return FloatProperty(name=name, default=0.0, min=0.0, soft_max=10, unit='LENGTH', subtype='DISTANCE',
                         update=_upd, precision=3)


class NKPF_Shape(bpy.types.PropertyGroup):
    live: BoolProperty(name="Live", default=False)
    checked: BoolProperty(default=False)
    sig: StringProperty(default="")
    kind: EnumProperty(name="Shape", items=KINDS, update=_upd)
    width: _size("Width")
    height: _size("Height")
    radius: _rad("Roundness")
    per_corner: BoolProperty(name="Per Corner", description="A different roundness on each corner",
                             default=False, update=_upd_per_corner)
    r_tl: _rad("Top Left")
    r_tr: _rad("Top Right")
    r_bl: _rad("Bottom Left")
    r_br: _rad("Bottom Right")
    sides: IntProperty(name="Sides", default=6, min=3, max=128, soft_max=24, update=_upd)
    star: BoolProperty(name="Star", description="Turn the polygon into a star", default=False, update=_upd)
    inner: FloatProperty(name="Inner Radius", description="Inner points of the star, relative to the outer ones",
                         default=0.5, min=0.01, max=1.5, soft_max=1.0, subtype='FACTOR', update=_upd)
    radius_inner: _rad("Inner Roundness")
    angle: FloatProperty(name="Angle", default=0.0, subtype='ANGLE', update=_upd)
    drop: FloatProperty(name="Drop", description="0 = ellipse, 1 = drop with a sharp tip (pointing to the local +Y)",
                        default=0.0, min=0.0, max=1.0, subtype='FACTOR', update=_upd)


# ------------------------------------------------------------------------- recognise a shape
def detect(ob):
    """Read a Vector Draw rectangle / circle / polygon (or a star) back into parameters.
    Returns a dict, or None if the curve is not one of them."""
    if ob is None or ob.type != 'CURVE':
        return None
    sp = [s for s in ob.data.splines]
    if len(sp) != 1 or sp[0].type != 'BEZIER' or not sp[0].use_cyclic_u:
        return None
    bps = sp[0].bezier_points
    n = len(bps)
    co = [bp.co.copy() for bp in bps]
    if any(abs(c.z) > 1e-4 for c in co):
        return None
    tol = 1e-4 * max(1e-6, max(c.length for c in co))
    sharp = all(bp.handle_left_type == 'VECTOR' and bp.handle_right_type == 'VECTOR' or
                ((bp.handle_left - bp.co).length < tol and (bp.handle_right - bp.co).length < tol)
                for bp in bps)
    # rectangle
    if n == 4 and sharp:
        xs, ys = [c.x for c in co], [c.y for c in co]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if all(abs(abs(x) - w / 2) < tol + 1e-6 for x in xs) and all(abs(abs(y) - h / 2) < tol + 1e-6 for y in ys) \
                and w > 0 and h > 0:
            return {"kind": 'RECT', "width": w, "height": h}
    # circle / ellipse (4 smooth points on the axes)
    if n == 4 and not sharp:
        xs, ys = [c.x for c in co], [c.y for c in co]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        on_axes = all(abs(c.x) < tol + 1e-6 or abs(c.y) < tol + 1e-6 for c in co)
        if on_axes and w > 0 and h > 0 and abs(sum(xs)) < tol * 4 + 1e-5 and abs(sum(ys)) < tol * 4 + 1e-5:
            return {"kind": 'ELLIPSE', "width": w, "height": h}
    # regular polygon / star
    if n >= 3 and sharp:
        rad = [c.length for c in co]
        ang = [math.atan2(c.y, c.x) for c in co]
        if n >= 6 and n % 2 == 0 and abs(rad[0] - rad[1]) > 1e-4 * rad[0]:
            outer, inner = rad[0::2], rad[1::2]
            if max(outer) - min(outer) < tol * 2 + 1e-6 and max(inner) - min(inner) < tol * 2 + 1e-6:
                m = n // 2
                if rad[1] > rad[0]:
                    outer, inner = inner, outer
                    ang = ang[1:] + ang[:1]
                R = outer[0]
                a0 = -math.pi / 2 + math.pi / m
                off = (ang[0] - a0) % (2 * math.pi / m)
                return {"kind": 'POLY', "sides": m, "star": True, "inner": inner[0] / R,
                        "width": 2 * R, "height": 2 * R, "angle": off}
        if max(rad) - min(rad) < tol * 2 + 1e-6:
            steps = [((ang[(i + 1) % n] - ang[i]) % (2 * math.pi)) for i in range(n)]
            if max(steps) - min(steps) < 1e-3 or max(steps) - min(steps) > 2 * math.pi - 1e-3:
                R = rad[0]
                a0 = -math.pi / 2 + math.pi / n
                off = (ang[0] - a0) % (2 * math.pi / n)
                if abs(off - 2 * math.pi / n) < 1e-5:
                    off = 0.0
                return {"kind": 'POLY', "sides": n, "star": False, "width": 2 * R, "height": 2 * R,
                        "angle": off}
    return None


def make_live(ob, vals, absorb_scale=True):
    sh = ob.nkpf_shape
    _suspend[0] += 1
    try:
        for k, v in vals.items():
            setattr(sh, k, v)
        if absorb_scale:
            sx, sy = abs(ob.scale.x), abs(ob.scale.y)
            if abs(sx - 1) > 1e-6 or abs(sy - 1) > 1e-6:
                sh.width *= sx
                sh.height *= sy
                ob.scale.x = math.copysign(1.0, ob.scale.x)
                ob.scale.y = math.copysign(1.0, ob.scale.y)
                absorb_scale = 'REWRITE'
        sh.live = True
        sh.checked = True
    finally:
        _suspend[0] -= 1
    if absorb_scale == 'REWRITE':
        write(ob)
    else:
        sh.sig = signature(ob.data)


def edited_outside(ob):
    sh = ob.nkpf_shape
    return sh.live and sh.sig != "" and not same_signature(sh.sig, ob.data)


# ---------------------------------------------------------------------------------- operators
class NKDRAW_OT_make_live(bpy.types.Operator):
    """Turn the selected Vector Draw rectangles / circles / polygons into live shapes
    (size, roundness, star, drop... editable at any time)"""
    bl_idname = "nkdraw.make_live"
    bl_label = "Make Live Shape"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'CURVE' for o in context.selected_objects)

    def execute(self, context):
        done, skip = 0, []
        for ob in context.selected_objects:
            if ob.type != 'CURVE' or ob.nkpf_shape.live:
                continue
            vals = detect(ob)
            if vals is None:
                skip.append(ob.name)
                continue
            make_live(ob, vals)
            done += 1
        if skip:
            self.report({'WARNING'}, "Not a plain rectangle / circle / polygon: " + ", ".join(skip[:5]))
        if not done:
            return {'CANCELLED'}
        return {'FINISHED'}


class NKDRAW_OT_rebuild(bpy.types.Operator):
    """Rebuild the shape from its values (drops the edits made to its points by hand)"""
    bl_idname = "nkdraw.rebuild"
    bl_label = "Rebuild Shape"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == 'CURVE' and ob.nkpf_shape.live

    def execute(self, context):
        write(context.active_object)
        return {'FINISHED'}


class NKDRAW_OT_detach(bpy.types.Operator):
    """Convert the primitive to plain curves: it looks exactly the same, its points become editable with
    the pen, and its shape values go away"""
    bl_idname = "nkdraw.detach"
    bl_label = "Convert to Curves"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.type == 'CURVE' and o.nkpf_shape.live for o in context.selected_objects)

    def execute(self, context):
        for ob in context.selected_objects:
            if ob.type == 'CURVE':
                ob.nkpf_shape.live = False
        return {'FINISHED'}


class NKDRAW_OT_add_shape(bpy.types.Operator):
    """Add a live shape on the CPlane (the add-on's, or the 3D cursor), centred on its origin"""
    bl_idname = "nkdraw.add_shape"
    bl_label = "Add Live Shape"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(name="Shape", items=KINDS)
    width: FloatProperty(name="Width", default=2.0, min=0.0001, unit='LENGTH', subtype='DISTANCE')
    height: FloatProperty(name="Height", default=2.0, min=0.0001, unit='LENGTH', subtype='DISTANCE')
    radius: FloatProperty(name="Roundness", default=0.0, min=0.0, unit='LENGTH', subtype='DISTANCE')
    sides: IntProperty(name="Sides", default=6, min=3, max=128)
    star: BoolProperty(name="Star", default=False)
    inner: FloatProperty(name="Inner Radius", default=0.5, min=0.01, max=1.5, subtype='FACTOR')
    radius_inner: FloatProperty(name="Inner Roundness", default=0.0, min=0.0, unit='LENGTH', subtype='DISTANCE')
    drop: FloatProperty(name="Drop", default=0.0, min=0.0, max=1.0, subtype='FACTOR')

    def draw(self, context):
        lay = self.layout
        lay.use_property_split = True
        lay.prop(self, "kind")
        col = lay.column(align=True)
        col.prop(self, "width")
        col.prop(self, "height")
        if self.kind in {'RECT', 'POLY'}:
            lay.prop(self, "radius")
        if self.kind == 'POLY':
            lay.prop(self, "sides")
            lay.prop(self, "star")
            if self.star:
                lay.prop(self, "inner")
                lay.prop(self, "radius_inner")
        if self.kind == 'ELLIPSE':
            lay.prop(self, "drop")

    def execute(self, context):
        o, R = common.cplane_frame(context.scene)
        name = NAMES[self.kind]
        if self.kind == 'POLY' and self.star:
            name = "Star"
        cu = bpy.data.curves.new(name, 'CURVE')
        cu.dimensions = '3D'
        cu.resolution_u = 24
        ob = bpy.data.objects.new(name, cu)
        ob.matrix_world = Matrix.Translation(o) @ R.to_4x4()
        (context.collection or context.scene.collection).objects.link(ob)
        vals = {k: getattr(self, k) for k in ("kind", "width", "height", "radius", "sides", "star", "inner",
                                               "radius_inner", "drop")}
        _suspend[0] += 1
        try:
            for k, v in vals.items():
                setattr(ob.nkpf_shape, k, v)
            ob.nkpf_shape.live = True
            ob.nkpf_shape.checked = True
        finally:
            _suspend[0] -= 1
        write(ob)
        for x in context.selected_objects:
            x.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        return {'FINISHED'}


# --------------------------------------------------------------------------------------- UI
def draw_shape_ui(layout, ob):
    sh = ob.nkpf_shape
    row = layout.row()
    row.scale_y = 1.3
    row.operator("nkdraw.detach", icon='CURVE_BEZCURVE')
    lay = layout
    lay.use_property_split = True
    lay.use_property_decorate = False
    lay.prop(sh, "kind")
    col = lay.column(align=True)
    col.prop(sh, "width")
    col.prop(sh, "height")
    if sh.kind == 'RECT':
        row = lay.row(align=True)
        row.prop(sh, "per_corner")
        if sh.per_corner:
            col = lay.column(align=True)
            col.prop(sh, "r_tl")
            col.prop(sh, "r_tr")
            col.prop(sh, "r_bl")
            col.prop(sh, "r_br")
        else:
            lay.prop(sh, "radius")
    elif sh.kind == 'POLY':
        lay.prop(sh, "sides")
        lay.prop(sh, "radius")
        lay.prop(sh, "angle")
        lay.prop(sh, "star")
        if sh.star:
            col = lay.column(align=True)
            col.prop(sh, "inner")
            col.prop(sh, "radius_inner")
    else:
        lay.prop(sh, "drop")
    if edited_outside(ob):
        box = layout.box()
        box.label(text="Points edited by hand", icon='ERROR')
        box.label(text="Changing a value rebuilds the shape")
        row = box.row(align=True)
        row.operator("nkdraw.rebuild", icon='FILE_REFRESH', text="Rebuild")
        row.operator("nkdraw.detach", icon='CURVE_BEZCURVE', text="Convert to Curves")


class NKDRAW_PT_shape(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Vector Draw"
    bl_parent_id = "NKDRAW_PT_main"
    bl_label = "Primitive"
    bl_order = 50

    def draw(self, context):
        lay = self.layout
        ob = context.active_object
        if ob is not None and ob.type == 'CURVE' and ob.nkpf_shape.live:
            draw_shape_ui(lay, ob)
            return
        if ob is not None and ob.type == 'CURVE':
            lay.operator("nkdraw.make_live", icon='SHADERFX', text="Make Primitive")
        else:
            lay.label(text="Select a shape drawn with Rect, Circle or Polygon", icon='INFO')


class NKDRAW_PT_shape_data(bpy.types.Panel):
    """The primitive's values in Properties > Object Data (like Affinity's shape attributes)"""
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"
    bl_label = "Primitive"
    bl_order = 0

    @classmethod
    def poll(cls, context):
        ob = context.object
        return ob is not None and ob.type == 'CURVE' and ob.nkpf_shape.live

    def draw(self, context):
        draw_shape_ui(self.layout, context.object)


class NKDRAW_OT_shape_popup(bpy.types.Operator):
    """Edit the live shape of the active curve in a small pop-up at the mouse"""
    bl_idname = "nkdraw.shape_popup"
    bl_label = "Live Shape"

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == 'CURVE'

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=260)

    def draw(self, context):
        ob = context.active_object
        if ob.nkpf_shape.live:
            draw_shape_ui(self.layout, ob)
        else:
            self.layout.operator("nkdraw.make_live", icon='SHADERFX')

    def execute(self, context):
        return {'FINISHED'}


def menu_add(self, context):
    self.layout.separator()
    self.layout.operator_menu_enum("nkdraw.add_shape", "kind", text="Live Shape", icon='SHADERFX')


def menu_ctx(self, context):
    ob = context.active_object
    if ob is not None and ob.type == 'CURVE':
        self.layout.separator()
        self.layout.operator("nkdraw.shape_popup", icon='SHADERFX',
                             text="Primitive..." if ob.nkpf_shape.live else "Make Primitive...")
        if ob.nkpf_shape.live:
            self.layout.operator("nkdraw.detach", icon='CURVE_BEZCURVE')


# ------------------------------------------------- primitives stay out of Shape Builder / booleans
def selected_primitive(context):
    """A selected primitive (or the curve of a selected solid that is a primitive), or None."""
    for o in context.selected_objects:
        sp = getattr(o, "penforge_solid", None)
        cands = [o]
        if sp is not None and getattr(sp, "is_solid", False) and sp.source is not None:
            cands.append(sp.source)
        for x in cands:
            sh = getattr(x, "nkpf_shape", None)
            if x.type == 'CURVE' and sh is not None and sh.live:
                return x
    return None


def guard(cls, method):
    """Shape Builder / booleans refuse primitives: Convert to Curves first (like Affinity)."""
    orig = getattr(cls, method)
    if getattr(orig, "_nkpf_guard", False):
        return

    def refuse(self, context):
        ob = selected_primitive(context)
        if ob is not None:
            self.report({'WARNING'}, "\"%s\" is a primitive: Convert to Curves first" % ob.name)
            return True
        return False

    if method == "invoke":                         # Blender checks the number of arguments
        def wrapped(self, context, event):
            return {'CANCELLED'} if refuse(self, context) else orig(self, context, event)
    else:
        def wrapped(self, context):
            return {'CANCELLED'} if refuse(self, context) else orig(self, context)
    wrapped._nkpf_guard = True
    setattr(cls, method, wrapped)


# ----------------------------------------------------------- Vector Draw shapes become live by themselves
PF_NAMES = {"Rectangle", "Circle", "Polygon"}


def _base_name(name):
    b, _, suf = name.rpartition(".")
    return b if b and suf.isdigit() else name


_pending = set()


def _pen_running():
    try:
        for win in bpy.context.window_manager.windows:
            for op in win.modal_operators:
                if op.bl_idname in {"NKDRAW_OT_pen", "nkdraw.pen"}:
                    return True
    except (AttributeError, ReferenceError):
        pass
    return False


def _flush_pending():
    """Shapes drawn with the pen become primitives once the pen is closed (until then the pen
    still edits them, so the two never fight over the same points)."""
    if _pen_running():
        return 0.4
    for name in list(_pending):
        _pending.discard(name)
        ob = bpy.data.objects.get(name)
        if ob is None or ob.type != 'CURVE' or ob.nkpf_shape.checked:
            continue
        vals = detect(ob)
        if vals is None:
            ob.nkpf_shape.checked = True
            continue
        make_live(ob, vals, absorb_scale=False)
    return None


def auto_live(scene, depsgraph):
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        if not prefs.auto_live:
            return
    except (KeyError, AttributeError):
        pass
    if _pen_running():
        for u in depsgraph.updates:
            idb = u.id
            if isinstance(idb, bpy.types.Object) and idb.type == 'CURVE' and _base_name(idb.name) in PF_NAMES:
                _pending.add(idb.name)
        if _pending and not bpy.app.timers.is_registered(_flush_pending):
            bpy.app.timers.register(_flush_pending, first_interval=0.4)
        return
    for u in depsgraph.updates:
        idb = u.id
        if not isinstance(idb, bpy.types.Object) or idb.type != 'CURVE':
            continue
        ob = bpy.data.objects.get(idb.name)
        if ob is None or ob.nkpf_shape.checked or _base_name(ob.name) not in PF_NAMES:
            continue
        if not len(ob.data.splines):
            continue                              # not written yet, look again on the next update
        vals = detect(ob)
        if vals is None:
            ob.nkpf_shape.checked = True
            continue
        make_live(ob, vals, absorb_scale=False)


classes = (NKPF_Shape, NKDRAW_OT_make_live, NKDRAW_OT_rebuild, NKDRAW_OT_detach, NKDRAW_OT_add_shape,
           NKDRAW_OT_shape_popup, NKDRAW_PT_shape, NKDRAW_PT_shape_data)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.nkpf_shape = PointerProperty(type=NKPF_Shape)
    bpy.types.VIEW3D_MT_curve_add.append(menu_add)
    bpy.types.VIEW3D_MT_object_context_menu.append(menu_ctx)


def unregister():
    bpy.types.VIEW3D_MT_object_context_menu.remove(menu_ctx)
    bpy.types.VIEW3D_MT_curve_add.remove(menu_add)
    del bpy.types.Object.nkpf_shape
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
