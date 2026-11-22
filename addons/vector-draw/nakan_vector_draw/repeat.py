"""Repeat (Illustrator Object > Repeat): Radial, Grid and Mirror copies of a shape that stay live.

The copies are made by a Geometry Nodes modifier on the curve itself ("NKDraw Repeat"), so editing the
original (with the pen, live shape settings or Blender's own tools) updates every copy. The repeat works in
the object's local space: the pen draws with local X / Y on the CPlane, so the copies stay on the plane.

Expand turns the repeat into separate curve objects (exact Bezier copies) and takes the modifier off.
"""

import math

import bpy
from bpy.props import EnumProperty
from mathutils import Matrix, Vector

GROUP = "NKDraw Repeat"
MOD = "Repeat"
VERSION = 1
TYPES = [('RADIAL', "Radial", "Copies around a centre", 'FORCE_VORTEX', 0),
         ('GRID', "Grid", "Copies in rows and columns", 'MESH_GRID', 1),
         ('MIRROR', "Mirror", "A mirrored copy", 'MOD_MIRROR', 2)]
TYPE_INDEX = {t[0]: t[4] for t in TYPES}

# name, socket type, default, min, max, subtype
INPUTS = [
    ("Type", 'NodeSocketInt', 0, 0, 2, 'NONE'),
    ("Count", 'NodeSocketInt', 6, 1, 1000, 'NONE'),
    ("Angle", 'NodeSocketFloat', math.tau, 0.0, math.tau, 'ANGLE'),
    ("Center", 'NodeSocketVector', (0.0, 0.0, 0.0), None, None, 'TRANSLATION'),
    ("Columns", 'NodeSocketInt', 3, 1, 1000, 'NONE'),
    ("Rows", 'NodeSocketInt', 3, 1, 1000, 'NONE'),
    ("Step X", 'NodeSocketFloat', 1.0, None, None, 'DISTANCE'),
    ("Step Y", 'NodeSocketFloat', 1.0, None, None, 'DISTANCE'),
    ("Axis", 'NodeSocketInt', 0, 0, 1, 'NONE'),
    ("Axis Position", 'NodeSocketFloat', 0.0, None, None, 'DISTANCE'),
]


# ------------------------------------------------------------------------------------- node group
def _math(nt, op, a=None, b=None, loc=(0, 0)):
    n = nt.nodes.new('ShaderNodeMath')
    n.operation = op
    n.location = loc
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if isinstance(v, bpy.types.NodeSocket):
            nt.links.new(v, n.inputs[i])
        else:
            n.inputs[i].default_value = v
    return n.outputs[0]


def _xyz(nt, x=0.0, y=0.0, z=0.0, loc=(0, 0)):
    n = nt.nodes.new('ShaderNodeCombineXYZ')
    n.location = loc
    for i, v in enumerate((x, y, z)):
        if isinstance(v, bpy.types.NodeSocket):
            nt.links.new(v, n.inputs[i])
        else:
            n.inputs[i].default_value = v
    return n.outputs[0]


def _index_switch(nt, data_type, index, items, loc=(0, 0)):
    n = nt.nodes.new('GeometryNodeIndexSwitch')
    n.data_type = data_type
    n.location = loc
    while len(n.index_switch_items) < len(items):
        n.index_switch_items.new()
    nt.links.new(index, n.inputs[0])
    for i, v in enumerate(items):
        if isinstance(v, bpy.types.NodeSocket):
            nt.links.new(v, n.inputs[i + 1])
        else:
            n.inputs[i + 1].default_value = v
    return n.outputs[0]


def _vadd(nt, a, b, loc=(0, 0)):
    n = nt.nodes.new('ShaderNodeVectorMath')
    n.operation = 'ADD'
    n.location = loc
    nt.links.new(a, n.inputs[0])
    nt.links.new(b, n.inputs[1])
    return n.outputs[0]


def _build(ng):
    nt = ng
    nt.nodes.clear()
    nt.interface.clear()
    nt.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    nt.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    for name, st, dv, mn, mx, sub in INPUTS:
        s = nt.interface.new_socket(name, in_out='INPUT', socket_type=st)
        s.default_value = dv
        if mn is not None:
            s.min_value = mn
        if mx is not None:
            s.max_value = mx
        if sub != 'NONE':
            try:
                s.subtype = sub
            except (TypeError, AttributeError):
                pass
    gi = nt.nodes.new('NodeGroupInput')
    gi.location = (-1400, 0)
    go = nt.nodes.new('NodeGroupOutput')
    go.location = (900, 0)
    I = {s.name: s for s in gi.outputs if s.name}
    typ = I["Type"]

    # --- pivot P (the instance geometry is moved by -P, the points sit at P + offset)
    ax = I["Axis"]
    ap = I["Axis Position"]
    p_mirror = _index_switch(nt, 'VECTOR', ax, [_xyz(nt, ap, 0.0, 0.0, (-1000, -700)),
                                                _xyz(nt, 0.0, ap, 0.0, (-1000, -820))], (-820, -720))
    P = _index_switch(nt, 'VECTOR', typ, [I["Center"], _xyz(nt, loc=(-820, -560)), p_mirror], (-620, -560))

    # --- how many copies
    cr = _math(nt, 'MULTIPLY', I["Columns"], I["Rows"], (-1000, 300))
    N = _index_switch(nt, 'INT', typ, [I["Count"], cr, 2], (-820, 300))

    idx = nt.nodes.new('GeometryNodeInputIndex')
    idx.location = (-1200, 500)
    i = idx.outputs[0]

    # radial: angle step = Angle / N for a full turn, Angle / (N - 1) for an arc
    full = _math(nt, 'GREATER_THAN', I["Angle"], math.tau - 1e-4, (-1000, 700))
    nm1 = _math(nt, 'MAXIMUM', _math(nt, 'SUBTRACT', I["Count"], 1.0, (-1000, 620)), 1.0, (-860, 620))
    div = _math(nt, 'ADD', _math(nt, 'MULTIPLY', full, _math(nt, 'SUBTRACT', I["Count"], nm1, (-860, 760)),
                                 (-700, 760)), nm1, (-560, 700))
    step = _math(nt, 'DIVIDE', I["Angle"], div, (-420, 700))
    ang = _math(nt, 'MULTIPLY', i, step, (-280, 700))

    # grid: column / row of point i, rows go down (-Y) like Illustrator
    col = _math(nt, 'FLOORED_MODULO', i, I["Columns"], (-1000, 460))
    row = _math(nt, 'FLOOR', _math(nt, 'DIVIDE', i, I["Columns"], (-1000, 380)), None, (-860, 380))
    gx = _math(nt, 'MULTIPLY', col, I["Step X"], (-700, 460))
    gy = _math(nt, 'MULTIPLY', row, _math(nt, 'MULTIPLY', I["Step Y"], -1.0, (-860, 300)), (-700, 380))
    off = _index_switch(nt, 'VECTOR', typ, [_xyz(nt, loc=(-560, 560)), _xyz(nt, gx, gy, 0.0, (-560, 440)),
                                            _xyz(nt, loc=(-560, 320))], (-380, 460))
    pos = _vadd(nt, P, off, (-200, 300))

    rot = _xyz(nt, 0.0, 0.0, _index_switch(nt, 'FLOAT', typ, [ang, 0.0, 0.0], (-120, 700)), (40, 700))

    # mirror: the second point flips across the axis
    flip = _math(nt, 'SUBTRACT', 1.0, _math(nt, 'MULTIPLY', _math(nt, 'MINIMUM', i, 1.0, (-560, 140)), 2.0,
                                            (-420, 140)), (-280, 140))
    sx = _index_switch(nt, 'FLOAT', ax, [flip, 1.0], (-120, 160))
    sy = _index_switch(nt, 'FLOAT', ax, [1.0, flip], (-120, 60))
    is_mirror = _math(nt, 'COMPARE', typ, 2.0, (-280, 0))
    sx = _math(nt, 'ADD', _math(nt, 'MULTIPLY', is_mirror, _math(nt, 'SUBTRACT', sx, 1.0, (0, 160)), (140, 160)),
               1.0, (280, 160))
    sy = _math(nt, 'ADD', _math(nt, 'MULTIPLY', is_mirror, _math(nt, 'SUBTRACT', sy, 1.0, (0, 60)), (140, 60)),
               1.0, (280, 60))
    scale = _xyz(nt, sx, sy, 1.0, (420, 100))

    pts = nt.nodes.new('GeometryNodePoints')
    pts.location = (40, 400)
    nt.links.new(N, pts.inputs["Count"])
    nt.links.new(pos, pts.inputs["Position"])

    neg = nt.nodes.new('ShaderNodeVectorMath')
    neg.operation = 'SCALE'
    neg.location = (-420, -400)
    nt.links.new(P, neg.inputs[0])
    neg.inputs["Scale"].default_value = -1.0
    tr = nt.nodes.new('GeometryNodeTransform')
    tr.location = (-200, -300)
    nt.links.new(gi.outputs[0], tr.inputs["Geometry"])
    nt.links.new(neg.outputs[0], tr.inputs["Translation"])

    iop = nt.nodes.new('GeometryNodeInstanceOnPoints')
    iop.location = (560, 300)
    nt.links.new(pts.outputs[0], iop.inputs["Points"])
    nt.links.new(tr.outputs[0], iop.inputs["Instance"])
    nt.links.new(rot, iop.inputs["Rotation"])
    nt.links.new(scale, iop.inputs["Scale"])
    real = nt.nodes.new('GeometryNodeRealizeInstances')
    real.location = (740, 300)
    nt.links.new(iop.outputs[0], real.inputs[0])
    nt.links.new(real.outputs[0], go.inputs[0])
    ng["nkdraw_version"] = VERSION


def node_group():
    ng = bpy.data.node_groups.get(GROUP)
    if ng is None or ng.bl_idname != 'GeometryNodeTree':
        ng = bpy.data.node_groups.new(GROUP, 'GeometryNodeTree')
        _build(ng)
    elif ng.get("nkdraw_version", 0) != VERSION:
        _build(ng)
    return ng


# ---------------------------------------------------------------------------------- modifier inputs
def _ident(ng, name):
    for it in ng.interface.items_tree:
        if getattr(it, "in_out", None) == 'INPUT' and it.name == name:
            return it.identifier
    raise KeyError(name)


def get(mod, name):
    ident = _ident(mod.node_group, name)
    try:
        v = mod[ident]
    except (KeyError, TypeError):
        v = getattr(mod.properties.inputs, ident).value
    try:
        return tuple(v)
    except TypeError:
        return v


def set_(mod, name, value):
    ident = _ident(mod.node_group, name)
    try:
        mod[ident] = value
    except (KeyError, TypeError):
        getattr(mod.properties.inputs, ident).value = value


def draw_input(layout, mod, name, text=None):
    ident = _ident(mod.node_group, name)
    try:
        layout.prop(mod, '["%s"]' % ident, text=text if text is not None else name)
    except (TypeError, KeyError, AttributeError):
        layout.prop(getattr(mod.properties.inputs, ident), "value", text=text if text is not None else name)


def find(ob):
    if ob is None:
        return None
    for m in ob.modifiers:
        if m.type == 'NODES' and m.node_group is not None and m.node_group.name.startswith(GROUP):
            return m
    return None


def type_of(mod):
    t = int(get(mod, "Type"))
    return next((k for k, v in TYPE_INDEX.items() if v == t), 'RADIAL')


# ------------------------------------------------------------------------------------- the maths
def local_bounds(ob):
    """(min, max) of the curve's points and handles in object space"""
    pts = []
    if ob.type == 'CURVE':
        for s in ob.data.splines:
            if s.type == 'BEZIER':
                for b in s.bezier_points:
                    pts += [b.co, b.handle_left, b.handle_right]
            else:
                pts += [p.co.xyz for p in s.points]
    elif ob.type == 'FONT':
        pts = [Vector(c) for c in ob.bound_box]
    if not pts:
        return Vector(), Vector()
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return mn, mx


def copy_matrices(mod):
    """object-space matrices of every copy, the same as the node group computes (copy 0 = the original)"""
    t = type_of(mod)
    if t == 'RADIAL':
        n = max(1, int(get(mod, "Count")))
        angle = float(get(mod, "Angle"))
        c = Vector(get(mod, "Center"))
        step = angle / n if angle > math.tau - 1e-4 else angle / max(n - 1, 1)
        T, Ti = Matrix.Translation(c), Matrix.Translation(-c)
        return [T @ Matrix.Rotation(k * step, 4, 'Z') @ Ti for k in range(n)]
    if t == 'GRID':
        cols, rows = max(1, int(get(mod, "Columns"))), max(1, int(get(mod, "Rows")))
        sx, sy = float(get(mod, "Step X")), float(get(mod, "Step Y"))
        return [Matrix.Translation((k % cols * sx, -(k // cols) * sy, 0.0)) for k in range(cols * rows)]
    axis = int(get(mod, "Axis"))
    a = float(get(mod, "Axis Position"))
    p = Vector((a, 0.0, 0.0)) if axis == 0 else Vector((0.0, a, 0.0))
    s = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0) if axis == 0 else (1.0, -1.0, 1.0, 1.0))
    return [Matrix.Identity(4), Matrix.Translation(p) @ s @ Matrix.Translation(-p)]


def setup_defaults(ob, mod, kind):
    mn, mx = local_bounds(ob)
    size = mx - mn
    w, h = max(size.x, 1e-3), max(size.y, 1e-3)
    centre = (mn + mx) / 2
    set_(mod, "Type", TYPE_INDEX[kind])
    # radial: the centre below the shape, as far as the shape is tall (Illustrator's default look)
    r = max(w, h) * 1.2
    set_(mod, "Center", (centre.x, centre.y - r, 0.0))
    gap = 0.15 * max(w, h)
    set_(mod, "Step X", w + gap)
    set_(mod, "Step Y", h + gap)
    set_(mod, "Axis", 0)
    set_(mod, "Axis Position", mx.x + gap / 2)


# ------------------------------------------------------------------------------------- operators
def _targets(context):
    return [o for o in context.selected_objects if o.type in {'CURVE', 'FONT'}]


class NKDRAW_OT_repeat_add(bpy.types.Operator):
    """Repeat the selected shapes: Radial, Grid or Mirror. The copies stay live: edit the original and they follow"""
    bl_idname = "nkdraw.repeat_add"
    bl_label = "Repeat"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(name="Type", items=TYPES, default='RADIAL')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(_targets(context))

    def execute(self, context):
        ng = node_group()
        for ob in _targets(context):
            mod = find(ob)
            if mod is None:
                mod = ob.modifiers.new(MOD, 'NODES')
                mod.node_group = ng
                setup_defaults(ob, mod, self.kind)
                # before "Draw on Object": the copies are made flat, then projected
                if len(ob.modifiers) > 1:
                    try:
                        ob.modifiers.move(len(ob.modifiers) - 1, 0)
                    except (AttributeError, RuntimeError, TypeError):
                        pass
            else:
                set_(mod, "Type", TYPE_INDEX[self.kind])
            ob.update_tag()
        return {'FINISHED'}


class NKDRAW_OT_repeat_center(bpy.types.Operator):
    """Put the centre / axis of the repeat back to its default place for the shape as it is now"""
    bl_idname = "nkdraw.repeat_reset"
    bl_label = "Reset Placement"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return find(context.active_object) is not None

    def execute(self, context):
        ob = context.active_object
        mod = find(ob)
        setup_defaults(ob, mod, type_of(mod))
        ob.update_tag()
        return {'FINISHED'}


class NKDRAW_OT_repeat_expand(bpy.types.Operator):
    """Turn the repeat into separate curves (exact Bezier copies) and take the Repeat off"""
    bl_idname = "nkdraw.repeat_expand"
    bl_label = "Expand"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(find(o) for o in context.selected_objects)

    def execute(self, context):
        made = []
        for ob in [o for o in context.selected_objects if find(o)]:
            mod = find(ob)
            mats = copy_matrices(mod)
            ob.modifiers.remove(mod)
            colls = list(ob.users_collection) or [context.scene.collection]
            for k, m in enumerate(mats[1:], start=1):
                c = ob.copy()
                c.data = ob.data.copy()
                if ob.type == 'CURVE':
                    c.data.transform(m)
                else:
                    c.matrix_world = ob.matrix_world @ m
                if c.animation_data is not None:
                    c.animation_data_clear()
                c.name = "%s %d" % (ob.name, k + 1)
                for col in colls:
                    col.objects.link(c)
                made.append(c)
        for c in made:
            c.select_set(True)
        self.report({'INFO'}, "Expanded into %d curves" % (len(made)))
        return {'FINISHED'}


class NKDRAW_OT_repeat_axis(bpy.types.Operator):
    """Mirror across a vertical or a horizontal axis"""
    bl_idname = "nkdraw.repeat_axis"
    bl_label = "Mirror Axis"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    axis: bpy.props.IntProperty(min=0, max=1)

    @classmethod
    def poll(cls, context):
        return find(context.active_object) is not None

    def execute(self, context):
        ob = context.active_object
        mod = find(ob)
        if int(get(mod, "Axis")) != self.axis:
            mn, mx = local_bounds(ob)
            size = mx - mn
            gap = 0.15 * max(size.x, size.y, 1e-3)
            set_(mod, "Axis", self.axis)
            set_(mod, "Axis Position", mx.x + gap / 2 if self.axis == 0 else mn.y - gap / 2)
            ob.update_tag()
        return {'FINISHED'}


class NKDRAW_OT_repeat_remove(bpy.types.Operator):
    """Take the Repeat off (only the original stays)"""
    bl_idname = "nkdraw.repeat_remove"
    bl_label = "Remove Repeat"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(find(o) for o in context.selected_objects)

    def execute(self, context):
        for ob in context.selected_objects:
            m = find(ob)
            if m is not None:
                ob.modifiers.remove(m)
        return {'FINISHED'}


def draw_panel(lay, context):
    ob = context.active_object
    row = lay.row(align=True)
    row.scale_y = 1.2
    for key, label, _d, icon, _i in TYPES:
        row.operator(NKDRAW_OT_repeat_add.bl_idname, text=label, icon=icon).kind = key
    mod = find(ob)
    if mod is None:
        return
    t = type_of(mod)
    col = lay.column(align=True)
    if t == 'RADIAL':
        draw_input(col, mod, "Count", "Copies")
        draw_input(col, mod, "Angle")
        draw_input(col, mod, "Center")
    elif t == 'GRID':
        row = col.row(align=True)
        draw_input(row, mod, "Columns")
        draw_input(row, mod, "Rows")
        draw_input(col, mod, "Step X", "Spacing X")
        draw_input(col, mod, "Step Y", "Spacing Y")
    else:
        row = col.row(align=True)
        axis = int(get(mod, "Axis"))
        for k, label in enumerate(("Vertical", "Horizontal")):
            row.operator(NKDRAW_OT_repeat_axis.bl_idname, text=label, depress=(axis == k)).axis = k
        draw_input(col, mod, "Axis Position", "Position")
    row = lay.row(align=True)
    row.operator(NKDRAW_OT_repeat_center.bl_idname, text="Reset", icon='LOOP_BACK')
    row.operator(NKDRAW_OT_repeat_expand.bl_idname, text="Expand", icon='OUTLINER_OB_CURVE')
    row.operator(NKDRAW_OT_repeat_remove.bl_idname, text="", icon='X')


class NKDRAW_PT_repeat(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Vector Draw"
    bl_parent_id = "NKDRAW_PT_main"
    bl_label = "Repeat"
    bl_order = 52

    def draw(self, context):
        draw_panel(self.layout, context)


CLASSES = (NKDRAW_OT_repeat_add, NKDRAW_OT_repeat_center, NKDRAW_OT_repeat_expand, NKDRAW_OT_repeat_axis,
           NKDRAW_OT_repeat_remove,
           NKDRAW_PT_repeat)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
