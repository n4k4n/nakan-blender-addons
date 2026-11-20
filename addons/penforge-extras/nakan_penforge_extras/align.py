"""Align and distribute objects (Illustrator style) on the CPlane axes, the world axes or the active
object's axes. The bounds are the real visible shape (curve samples, evaluated mesh vertices),
children included (a PenForge curve brings its Extrude / Thickness solid)."""
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
from mathutils import Matrix, Vector

from . import common

AXES = {'X': 0, 'Y': 1, 'Z': 2}


# ------------------------------------------------------------------------------------- frame
def frame(context):
    st = context.scene.nkpf_align
    if st.space == 'CPLANE':
        o, R = common.cplane_frame(context.scene)
        return o, R
    if st.space == 'ACTIVE' and context.active_object is not None:
        mw = context.active_object.matrix_world
        return mw.translation.copy(), mw.to_3x3().normalized()
    return Vector((0, 0, 0)), Matrix.Identity(3)


# ------------------------------------------------------------------------------------ bounds
def _bez(p0, p1, p2, p3, t):
    u = 1 - t
    return p0 * u ** 3 + p1 * 3 * u * u * t + p2 * 3 * u * t * t + p3 * t ** 3


def _object_points(ob, depsgraph):
    mw = ob.matrix_world
    if ob.type == 'CURVE':
        pts = []
        for s in ob.data.splines:
            if s.type == 'BEZIER':
                bps = s.bezier_points
                n = len(bps)
                segs = n if s.use_cyclic_u else n - 1
                if n == 1:
                    pts.append(mw @ bps[0].co)
                for i in range(segs):
                    a, b = bps[i], bps[(i + 1) % n]
                    for k in range(17):
                        pts.append(mw @ _bez(a.co, a.handle_right, b.handle_left, b.co, k / 16))
            else:
                pts += [mw @ p.co.xyz for p in s.points]
        if pts:
            ev = ob.evaluated_get(depsgraph)        # bevel / extrude of the curve itself
            if ob.data.bevel_depth > 0 or ob.data.extrude > 0 or ob.data.bevel_object is not None:
                pts += [ev.matrix_world @ Vector(c) for c in ev.bound_box]
            return pts
    if ob.type == 'MESH':
        ev = ob.evaluated_get(depsgraph)
        try:
            me = ev.to_mesh()
            pts = [ev.matrix_world @ v.co for v in me.vertices]
            ev.to_mesh_clear()
            if pts:
                return pts
        except RuntimeError:
            pass
    if ob.type == 'EMPTY':
        return [mw.translation.copy()]
    return [mw @ Vector(c) for c in ob.bound_box]


def _family(ob):
    out = [ob]
    for c in ob.children:
        if c.visible_get():
            out += _family(c)
    return out


def bounds(ob, depsgraph, o, R):
    """(min, max) of the object and its visible children in frame coordinates."""
    Rt = R.transposed()
    lo = Vector((1e30, 1e30, 1e30))
    hi = Vector((-1e30, -1e30, -1e30))
    for x in _family(ob):
        for p in _object_points(x, depsgraph):
            q = Rt @ (p - o)
            for k in range(3):
                lo[k] = min(lo[k], q[k])
                hi[k] = max(hi[k], q[k])
    if lo.x > hi.x:
        q = Rt @ (ob.matrix_world.translation - o)
        return q.copy(), q.copy()
    return lo, hi


def items(context):
    """The objects to move: a selected PenForge solid stands for its curve; an object whose parent
    is moved too is left alone (it follows)."""
    out = []
    for ob in context.selected_objects:
        sp = getattr(ob, "penforge_solid", None)
        if sp is not None and getattr(sp, "is_solid", False) and sp.source is not None:
            ob = sp.source
        if ob not in out:
            out.append(ob)
    s = set(out)

    def moved_ancestor(o):
        p = o.parent
        while p is not None:
            if p in s:
                return True
            p = p.parent
        return False
    return [o for o in out if not moved_ancestor(o)]


def _active_item(context, its):
    a = context.active_object
    if a is None:
        return None
    sp = getattr(a, "penforge_solid", None)
    if sp is not None and getattr(sp, "is_solid", False) and sp.source is not None:
        a = sp.source
    return a if a in its else None


def move(ob, d_frame, R):
    d = R @ d_frame
    ob.matrix_world = Matrix.Translation(d) @ ob.matrix_world


def _key(lo, hi, k, where):
    if where == 'MIN':
        return lo[k]
    if where == 'MAX':
        return hi[k]
    return (lo[k] + hi[k]) / 2


# --------------------------------------------------------------------------------- operators
class NKPF_OT_align(bpy.types.Operator):
    """Align the selected objects"""
    bl_idname = "nkpf.align"
    bl_label = "Align"
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(items=[(a, a, "") for a in AXES])
    where: EnumProperty(items=[('MIN', "Min", ""), ('CENTER', "Center", ""), ('MAX', "Max", "")])

    @classmethod
    def description(cls, context, props):
        names = {('X', 'MIN'): "left edges", ('X', 'CENTER'): "horizontal centres", ('X', 'MAX'): "right edges",
                 ('Y', 'MIN'): "bottom edges", ('Y', 'CENTER'): "vertical centres", ('Y', 'MAX'): "top edges",
                 ('Z', 'MIN'): "lowest points (along the normal)", ('Z', 'CENTER'): "middles along the normal",
                 ('Z', 'MAX'): "highest points (along the normal)"}
        return "Align the " + names[(props.axis, props.where)]

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        st = context.scene.nkpf_align
        its = items(context)
        o, R = frame(context)
        dg = context.evaluated_depsgraph_get()
        k = AXES[self.axis]
        b = {ob: bounds(ob, dg, o, R) for ob in its}
        act = _active_item(context, its)
        if st.align_to == 'ORIGIN':
            target = 0.0                         # the frame origin (CPlane origin / world origin / active origin)
        elif st.align_to == 'WORLD':
            target = (R.transposed() @ (-o))[k]  # the world centre in the frame
        elif st.align_to == 'ACTIVE' and act is not None:
            target = _key(*b[act], k, self.where)
        else:
            if len(its) < 2:
                self.report({'INFO'}, "One object: choose To: World or CPlane to align it")
                return {'CANCELLED'}
            lo = min(v[0][k] for v in b.values())
            hi = max(v[1][k] for v in b.values())
            target = lo if self.where == 'MIN' else hi if self.where == 'MAX' else (lo + hi) / 2
        for ob in its:
            if st.align_to == 'ACTIVE' and ob is act:
                continue
            d = Vector((0, 0, 0))
            d[k] = target - _key(*b[ob], k, self.where)
            if abs(d[k]) > 1e-12:
                move(ob, d, R)
        return {'FINISHED'}


class NKPF_OT_distribute(bpy.types.Operator):
    """Distribute the selected objects"""
    bl_idname = "nkpf.distribute"
    bl_label = "Distribute"
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(items=[(a, a, "") for a in AXES])
    where: EnumProperty(items=[('MIN', "Min", ""), ('CENTER', "Center", ""), ('MAX', "Max", ""),
                               ('GAP', "Spacing", "")])

    @classmethod
    def description(cls, context, props):
        if props.where == 'GAP':
            return "Same spacing between the objects along %s (or the Spacing value when it is on)" % props.axis
        w = {'MIN': "minimum edges", 'CENTER': "centres", 'MAX': "maximum edges"}[props.where]
        return "Space the %s evenly along %s, between the two outer objects" % (w, props.axis)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and len(context.selected_objects) >= 2

    def execute(self, context):
        st = context.scene.nkpf_align
        its = items(context)
        o, R = frame(context)
        dg = context.evaluated_depsgraph_get()
        k = AXES[self.axis]
        b = {ob: bounds(ob, dg, o, R) for ob in its}
        n = len(its)
        if self.where == 'GAP':
            if n < 2:
                return {'CANCELLED'}
            order = sorted(its, key=lambda ob: (b[ob][0][k] + b[ob][1][k]) / 2)
            act = _active_item(context, its) if st.align_to == 'ACTIVE' else None
            if st.use_spacing:
                gap = st.spacing
                # the active object (if 'Active') or the first one stays, the others line up from it
                anchor = order.index(act) if act is not None else 0
                pos = {order[anchor]: b[order[anchor]][0][k]}
                x = b[order[anchor]][1][k]
                for ob in order[anchor + 1:]:
                    pos[ob] = x + gap
                    x = pos[ob] + (b[ob][1][k] - b[ob][0][k])
                x = b[order[anchor]][0][k]
                for ob in reversed(order[:anchor]):
                    size = b[ob][1][k] - b[ob][0][k]
                    pos[ob] = x - gap - size
                    x = pos[ob]
            else:
                if n < 3:
                    self.report({'INFO'}, "Equal spacing needs 3 objects (or turn Spacing on)")
                    return {'CANCELLED'}
                lo, hi = b[order[0]][0][k], max(b[ob][1][k] for ob in order)
                sizes = sum(b[ob][1][k] - b[ob][0][k] for ob in order)
                gap = (hi - lo - sizes) / (n - 1)
                pos, x = {}, lo
                for ob in order:
                    pos[ob] = x
                    x += (b[ob][1][k] - b[ob][0][k]) + gap
            for ob in its:
                d = Vector((0, 0, 0))
                d[k] = pos[ob] - b[ob][0][k]
                if abs(d[k]) > 1e-12:
                    move(ob, d, R)
            return {'FINISHED'}
        if n < 3:
            self.report({'INFO'}, "Distribute needs 3 objects")
            return {'CANCELLED'}
        order = sorted(its, key=lambda ob: _key(*b[ob], k, self.where))
        a = _key(*b[order[0]], k, self.where)
        z = _key(*b[order[-1]], k, self.where)
        for i, ob in enumerate(order):
            d = Vector((0, 0, 0))
            d[k] = a + (z - a) * i / (n - 1) - _key(*b[ob], k, self.where)
            if abs(d[k]) > 1e-12:
                move(ob, d, R)
        return {'FINISHED'}


class NKPF_Align(bpy.types.PropertyGroup):
    space: EnumProperty(name="Axes", items=[
        ('CPLANE', "CPlane", "the add-on's CPlane axes (the 3D cursor when PenForge is off)"),
        ('GLOBAL', "World", "World axes"),
        ('ACTIVE', "Active", "The axes of the active object")], default='CPLANE')
    align_to: EnumProperty(name="To", items=[
        ('SELECTION', "Selection", "The bounds of all the selected objects"),
        ('ACTIVE', "Active", "The active object (key object): it does not move"),
        ('WORLD', "World", "The world centre (0, 0, 0), measured along the chosen axes"),
        ('ORIGIN', "CPlane", "The origin of the axes: the CPlane origin (World axes: world centre; "
                             "Active axes: the active object's origin)")],
        default='SELECTION')
    use_spacing: BoolProperty(name="Spacing", description="Distribute Spacing uses this exact gap", default=False)
    spacing: FloatProperty(name="Spacing", default=0.1, unit='LENGTH', subtype='DISTANCE')
    show_z: BoolProperty(name="Normal (Z)", description="Also show aligning along the CPlane normal",
                         default=False)


def _btn(row, opid, axis, where, icon, text=""):
    op = row.operator(opid, text=text, icon=icon)
    op.axis, op.where = axis, where


class NKPF_PT_align(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PenForge"
    bl_parent_id = "PENFORGE_PT_main"
    bl_label = "Align & Distribute"
    bl_order = 51

    def draw(self, context):
        st = context.scene.nkpf_align
        lay = self.layout
        row = lay.row(align=True)
        row.prop(st, "space", expand=True)
        row = lay.row(align=True)
        row.prop(st, "align_to", expand=True)

        lay.label(text="Align")
        row = lay.row(align=True)
        _btn(row, "nkpf.align", 'X', 'MIN', 'ANCHOR_LEFT')
        _btn(row, "nkpf.align", 'X', 'CENTER', 'ANCHOR_CENTER')
        _btn(row, "nkpf.align", 'X', 'MAX', 'ANCHOR_RIGHT')
        row.separator()
        _btn(row, "nkpf.align", 'Y', 'MAX', 'ANCHOR_TOP')
        _btn(row, "nkpf.align", 'Y', 'CENTER', 'ANCHOR_CENTER')
        _btn(row, "nkpf.align", 'Y', 'MIN', 'ANCHOR_BOTTOM')
        if st.show_z:
            row = lay.row(align=True)
            row.label(text="Normal")
            _btn(row, "nkpf.align", 'Z', 'MIN', 'TRIA_DOWN_BAR', "Min")
            _btn(row, "nkpf.align", 'Z', 'CENTER', 'ANCHOR_CENTER', "Mid")
            _btn(row, "nkpf.align", 'Z', 'MAX', 'TRIA_UP_BAR', "Max")

        lay.label(text="Distribute")
        col = lay.column(align=True)
        row = col.row(align=True)
        row.label(text="X")
        _btn(row, "nkpf.distribute", 'X', 'MIN', 'ANCHOR_LEFT')
        _btn(row, "nkpf.distribute", 'X', 'CENTER', 'ANCHOR_CENTER')
        _btn(row, "nkpf.distribute", 'X', 'MAX', 'ANCHOR_RIGHT')
        _btn(row, "nkpf.distribute", 'X', 'GAP', 'ARROW_LEFTRIGHT', "Gap")
        row = col.row(align=True)
        row.label(text="Y")
        _btn(row, "nkpf.distribute", 'Y', 'MAX', 'ANCHOR_TOP')
        _btn(row, "nkpf.distribute", 'Y', 'CENTER', 'ANCHOR_CENTER')
        _btn(row, "nkpf.distribute", 'Y', 'MIN', 'ANCHOR_BOTTOM')
        _btn(row, "nkpf.distribute", 'Y', 'GAP', 'EMPTY_SINGLE_ARROW', "Gap")
        if st.show_z:
            row = col.row(align=True)
            row.label(text="Z")
            _btn(row, "nkpf.distribute", 'Z', 'MIN', 'TRIA_DOWN_BAR')
            _btn(row, "nkpf.distribute", 'Z', 'CENTER', 'ANCHOR_CENTER')
            _btn(row, "nkpf.distribute", 'Z', 'MAX', 'TRIA_UP_BAR')
            _btn(row, "nkpf.distribute", 'Z', 'GAP', 'EMPTY_SINGLE_ARROW', "Gap")
        row = lay.row(align=True)
        row.prop(st, "use_spacing", text="")
        sub = row.row(align=True)
        sub.active = st.use_spacing
        sub.prop(st, "spacing")
        lay.prop(st, "show_z")


classes = (NKPF_Align, NKPF_OT_align, NKPF_OT_distribute, NKPF_PT_align)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nkpf_align = PointerProperty(type=NKPF_Align)


def unregister():
    del bpy.types.Scene.nkpf_align
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
