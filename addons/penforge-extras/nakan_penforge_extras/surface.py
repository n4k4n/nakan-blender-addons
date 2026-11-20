"""Surface Mode: draw on the CPlane, see it on an object.

Every curve / text / solid made while "Draw on: Object" is set gets a Geometry Nodes modifier
("NKPF Surface Wrap") that projects it onto the target object along the CPlane normal (the object's
own local Z: PenForge objects have local axes = CPlane axes). The height above the plane is kept
along the surface normal, so an Extrude stays an Extrude, now following the curvature.
The curve itself stays flat on the CPlane: the pen, Shape Builder, booleans, primitives, align and
G / R / S keep working as on the grid, and the result slides over the surface while you move it.
"""
import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, PointerProperty
from mathutils import Matrix, Vector

GROUP = "NKPF Surface Wrap"
MOD = "NKPF Surface Wrap"
VERSION = 2


# ------------------------------------------------------------------------------- node group
def _menu(sock, *names):
    for n in names:
        try:
            sock.default_value = n
            return
        except (TypeError, ValueError):
            continue


def build_group():
    ng = bpy.data.node_groups.get(GROUP)
    if ng is not None and ng.get("nkpf_version") == VERSION:
        return ng
    if ng is None:
        ng = bpy.data.node_groups.new(GROUP, 'GeometryNodeTree')
    ng.nodes.clear()
    ng.interface.clear()
    itf = ng.interface
    itf.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    s = itf.new_socket("Target", in_out='INPUT', socket_type='NodeSocketObject')
    s = itf.new_socket("Offset", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value = 0.002
    s = itf.new_socket("Nearest", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = False
    s = itf.new_socket("Spacing", in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value = 0.02
    s.min_value = 0.0005
    s = itf.new_socket("Subdivide", in_out='INPUT', socket_type='NodeSocketInt')
    s.default_value = 2
    s.min_value = 0
    s.max_value = 6
    s = itf.new_socket("Fill", in_out='INPUT', socket_type='NodeSocketBool')
    s.default_value = False
    itf.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    N, L = ng.nodes, ng.links

    def node(t, x, y, **props):
        n = N.new(t)
        n.location = (x, y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    gi = node('NodeGroupInput', -1600, 0)
    go = node('NodeGroupOutput', 900, 0)

    # geometry: curves resampled finely, texts filled, meshes subdivided
    rs = node('GeometryNodeResampleCurve', -1300, 200)
    _menu(rs.inputs["Mode"], "Length")
    L.new(gi.outputs["Geometry"], rs.inputs["Curve"])
    L.new(gi.outputs["Spacing"], rs.inputs["Length"])
    fill = node('GeometryNodeFillCurve', -1100, 320)
    _menu(fill.inputs["Mode"], "Triangles")
    L.new(rs.outputs["Curve"], fill.inputs["Curve"])
    swg = node('GeometryNodeSwitch', -900, 200, input_type='GEOMETRY')
    L.new(gi.outputs["Fill"], swg.inputs["Switch"])
    L.new(rs.outputs["Curve"], swg.inputs["False"])
    L.new(fill.outputs["Mesh"], swg.inputs["True"])
    sub = node('GeometryNodeSubdivideMesh', -700, 200)
    L.new(swg.outputs["Output"], sub.inputs["Mesh"])
    L.new(gi.outputs["Subdivide"], sub.inputs["Level"])

    # target in this object's space
    oi = node('GeometryNodeObjectInfo', -1300, -300, transform_space='RELATIVE')
    L.new(gi.outputs["Target"], oi.inputs["Object"])

    # position on the plane (local XY) and height above it (local Z)
    pos = node('GeometryNodeInputPosition', -1300, -600)
    sep = node('ShaderNodeSeparateXYZ', -1100, -600)
    L.new(pos.outputs["Position"], sep.inputs["Vector"])
    base = node('ShaderNodeCombineXYZ', -900, -700)
    L.new(sep.outputs["X"], base.inputs["X"])
    L.new(sep.outputs["Y"], base.inputs["Y"])
    # the ray starts just above the target (not from far away: a start point thousands of units
    # away leaves float precision of millimetres on the hit, and the projected line trembles)
    bb = node('GeometryNodeBoundBox', -1100, -400)
    L.new(oi.outputs["Geometry"], bb.inputs["Geometry"])
    bmax = node('ShaderNodeSeparateXYZ', -950, -380)
    L.new(bb.outputs["Max"], bmax.inputs["Vector"])
    bmin = node('ShaderNodeSeparateXYZ', -950, -460)
    L.new(bb.outputs["Min"], bmin.inputs["Vector"])
    top = node('ShaderNodeMath', -800, -380, operation='ADD')
    L.new(bmax.outputs["Z"], top.inputs[0])
    top.inputs[1].default_value = 1.0
    # also start above the point itself, so curves above the target still hit it from their side
    top2 = node('ShaderNodeMath', -700, -380, operation='MAXIMUM')
    L.new(top.outputs["Value"], top2.inputs[0])
    L.new(sep.outputs["Z"], top2.inputs[1])
    span = node('ShaderNodeMath', -800, -460, operation='SUBTRACT')
    L.new(top2.outputs["Value"], span.inputs[0])
    L.new(bmin.outputs["Z"], span.inputs[1])
    rlen = node('ShaderNodeMath', -650, -460, operation='ADD')
    L.new(span.outputs["Value"], rlen.inputs[0])
    rlen.inputs[1].default_value = 1.0
    src = node('ShaderNodeCombineXYZ', -900, -500)
    L.new(sep.outputs["X"], src.inputs["X"])
    L.new(sep.outputs["Y"], src.inputs["Y"])
    L.new(top2.outputs["Value"], src.inputs["Z"])

    # project: a ray down the CPlane normal; the smooth (interpolated) normal of the surface
    ray = node('GeometryNodeRaycast', -600, -300, data_type='FLOAT_VECTOR')
    _menu(ray.inputs["Interpolation"], "Interpolated")
    tn = node('GeometryNodeInputNormal', -900, -250)
    L.new(oi.outputs["Geometry"], ray.inputs["Target Geometry"])
    L.new(tn.outputs["Normal"], ray.inputs["Attribute"])
    L.new(src.outputs["Vector"], ray.inputs["Source Position"])
    ray.inputs["Ray Direction"].default_value = (0.0, 0.0, -1.0)
    L.new(rlen.outputs["Value"], ray.inputs["Ray Length"])

    # nearest: the closest point of the surface to the point on the plane
    tp = node('GeometryNodeInputPosition', -900, -900)
    snp = node('GeometryNodeSampleNearestSurface', -600, -800, data_type='FLOAT_VECTOR')
    L.new(oi.outputs["Geometry"], snp.inputs["Mesh"])
    L.new(tp.outputs["Position"], snp.inputs["Value"])
    L.new(base.outputs["Vector"], snp.inputs["Sample Position"])
    tn2 = node('GeometryNodeInputNormal', -900, -1050)
    snn = node('GeometryNodeSampleNearestSurface', -600, -1050, data_type='FLOAT_VECTOR')
    L.new(oi.outputs["Geometry"], snn.inputs["Mesh"])
    L.new(tn2.outputs["Normal"], snn.inputs["Value"])
    L.new(base.outputs["Vector"], snn.inputs["Sample Position"])

    sw_p = node('GeometryNodeSwitch', -300, -400, input_type='VECTOR')
    L.new(gi.outputs["Nearest"], sw_p.inputs["Switch"])
    L.new(ray.outputs["Hit Position"], sw_p.inputs["False"])
    L.new(snp.outputs["Value"], sw_p.inputs["True"])
    sw_n = node('GeometryNodeSwitch', -300, -600, input_type='VECTOR')
    L.new(gi.outputs["Nearest"], sw_n.inputs["Switch"])
    L.new(ray.outputs["Attribute"], sw_n.inputs["False"])
    L.new(snn.outputs["Value"], sw_n.inputs["True"])
    sw_h = node('GeometryNodeSwitch', -300, -200, input_type='BOOLEAN')
    L.new(gi.outputs["Nearest"], sw_h.inputs["Switch"])
    L.new(ray.outputs["Is Hit"], sw_h.inputs["False"])
    L.new(snp.outputs["Is Valid"], sw_h.inputs["True"])

    nrm = node('ShaderNodeVectorMath', -100, -600, operation='NORMALIZE')
    L.new(sw_n.outputs["Output"], nrm.inputs[0])
    h = node('ShaderNodeMath', -100, -800, operation='ADD')
    L.new(sep.outputs["Z"], h.inputs[0])
    L.new(gi.outputs["Offset"], h.inputs[1])
    up = node('ShaderNodeVectorMath', 100, -650, operation='SCALE')
    L.new(nrm.outputs["Vector"], up.inputs[0])
    L.new(h.outputs["Value"], up.inputs["Scale"])
    new = node('ShaderNodeVectorMath', 300, -500, operation='ADD')
    L.new(sw_p.outputs["Output"], new.inputs[0])
    L.new(up.outputs["Vector"], new.inputs[1])
    keep = node('GeometryNodeSwitch', 500, -300, input_type='VECTOR')
    L.new(sw_h.outputs["Output"], keep.inputs["Switch"])
    L.new(pos.outputs["Position"], keep.inputs["False"])       # missed the surface: stays on the plane
    L.new(new.outputs["Vector"], keep.inputs["True"])

    sp = node('GeometryNodeSetPosition', 700, 0)
    L.new(sub.outputs["Mesh"], sp.inputs["Geometry"])
    L.new(keep.outputs["Output"], sp.inputs["Position"])
    L.new(sp.outputs["Geometry"], go.inputs["Geometry"])
    ng["nkpf_version"] = VERSION
    return ng


def _ids(ng):
    return {it.name: it.identifier for it in ng.interface.items_tree
            if getattr(it, "in_out", None) == 'INPUT' and it.item_type == 'SOCKET'}


# --------------------------------------------------------------------------------- modifier
def get_mod(ob):
    for m in ob.modifiers:
        if m.type == 'NODES' and m.node_group is not None and m.node_group.name.startswith(GROUP):
            return m
    return None


def _set(mod, ident, value):
    """Modifier input, on both APIs (Blender 5.3+: mod.properties.inputs; before: mod[identifier])."""
    props = getattr(mod, "properties", None)
    inputs = getattr(props, "inputs", None) if props is not None else None
    if inputs is not None and hasattr(inputs, ident):
        getattr(inputs, ident).value = value
        return
    mod[ident] = value


def set_inputs(ob, mod, st):
    ids = _ids(mod.node_group)
    _set(mod, ids["Target"], st.target)
    _set(mod, ids["Offset"], st.offset)
    _set(mod, ids["Nearest"], st.method == 'NEAREST')
    _set(mod, ids["Spacing"], st.spacing)
    _set(mod, ids["Subdivide"], st.subdivide if ob.type in {'MESH', 'FONT'} else 0)
    _set(mod, ids["Fill"], ob.type == 'FONT')
    ob.update_tag()


def wrap(ob, st):
    if ob.type not in {'CURVE', 'FONT', 'MESH'} or st.target is None or ob == st.target:
        return None
    ng = build_group()
    mod = get_mod(ob)
    if mod is None:
        mod = ob.modifiers.new(MOD, 'NODES')
        mod.node_group = ng
        # first in the stack: the booleans of PenForge come after and see the wrapped solids
        idx = list(ob.modifiers).index(mod)
        if idx > 0:
            try:
                ob.modifiers.move(idx, 0)
            except (AttributeError, RuntimeError):
                pass
    set_inputs(ob, mod, st)
    return mod


def unwrap(ob):
    mod = get_mod(ob)
    if mod is not None:
        ob.modifiers.remove(mod)
        return True
    return False


def is_solid(ob):
    sp = getattr(ob, "penforge_solid", None)
    return ob.type == 'MESH' and sp is not None and getattr(sp, "is_solid", False)


def auto_wrap(scene, objects):
    """Called for every object made in Vector Mode."""
    st = scene.nkpf_surface
    if st.mode != 'OBJECT' or st.target is None:
        return
    for ob in objects:
        if ob.type in {'CURVE', 'FONT'} or is_solid(ob):
            wrap(ob, st)


def _family(objs):
    out = []
    for o in objs:
        for x in [o] + [c for c in o.children if is_solid(c)]:
            if x not in out:
                out.append(x)
    return out


def _update_all(self, context):
    """Settings changed: every wrapped object of the scene follows."""
    st = context.scene.nkpf_surface
    if st.target is None:
        return
    for ob in context.scene.objects:
        mod = get_mod(ob)
        if mod is not None:
            set_inputs(ob, mod, st)


def _poll_target(self, ob):
    return ob.type == 'MESH' and get_mod(ob) is None


class NKPF_Surface(bpy.types.PropertyGroup):
    mode: EnumProperty(name="Draw on", items=[
        ('GRID', "Grid", "Draw on the CPlane, as usual"),
        ('OBJECT', "Object", "Draw on the CPlane, see it projected on an object")], default='GRID')
    target: PointerProperty(name="Object", type=bpy.types.Object, poll=_poll_target, update=_update_all,
                            description="The surface the drawings are projected on")
    method: EnumProperty(name="Method", items=[
        ('PROJECT', "Project", "Straight down the CPlane normal (like a decal seen from the CPlane)"),
        ('NEAREST', "Nearest", "The closest point of the surface")], default='PROJECT', update=_update_all)
    offset: FloatProperty(name="Offset", default=0.002, min=-1.0, soft_max=0.1, unit='LENGTH', subtype='DISTANCE',
                          update=_update_all, description="Lift above the surface (avoids flickering)")
    spacing: FloatProperty(name="Detail", default=0.02, min=0.0005, soft_max=0.5, unit='LENGTH', subtype='DISTANCE',
                           update=_update_all, description="Distance between the points of the wrapped curves "
                                                          "(smaller = follows the surface better, heavier)")
    subdivide: IntProperty(name="Subdivide", default=2, min=0, max=6, update=_update_all,
                           description="Subdivisions of the solids / texts so their faces bend with the surface")


class NKPF_OT_wrap(bpy.types.Operator):
    """Project the selected curves / texts / solids on the Surface object (or take the projection off)"""
    bl_idname = "nkpf.wrap"
    bl_label = "Wrap Selected"
    bl_options = {'REGISTER', 'UNDO'}

    off: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        st = context.scene.nkpf_surface
        if not self.off and st.target is None:
            self.report({'WARNING'}, "Choose the surface Object first")
            return {'CANCELLED'}
        n = 0
        for ob in _family(context.selected_objects):
            if self.off:
                n += unwrap(ob)
            elif wrap(ob, st) is not None:
                n += 1
        self.report({'INFO'}, "%d object(s) %s" % (n, "unwrapped" if self.off else "wrapped"))
        return {'FINISHED'}


# ------------------------------------------------------------------------------ re-project
# ------------------------------------------------------------------------- apply projection
def _eval_wrap_only(ob, context):
    """Evaluated geometry of ob with only the Surface Wrap modifier (the ones after it are paused)."""
    mod = get_mod(ob)
    after = list(ob.modifiers)[list(ob.modifiers).index(mod) + 1:]
    saved = [(m, m.show_viewport) for m in after]
    for m in after:
        m.show_viewport = False
    try:
        dg = context.evaluated_depsgraph_get()
        dg.update()
        return ob.evaluated_get(dg), saved
    except Exception:
        for m, v in saved:
            m.show_viewport = v
        raise


def _bake_curve(ob, context):
    """Replace the curve with the projected one: poly splines that lie on the surface."""
    ev, saved = _eval_wrap_only(ob, context)
    try:
        gs = ev.evaluated_geometry()     # keep the set alive while reading its curves
        c = gs.curves
        if c is None or not len(c.curves):
            return False
        pos = [p.position.copy() for p in c.points]
        rad = [p.radius for p in c.points] if hasattr(c.points[0], "radius") else None
        tilt_a = c.attributes.get("tilt")
        tilt = [d.value for d in tilt_a.data] if tilt_a else None
        cyc_a = c.attributes.get("cyclic")
        cyclic = [d.value for d in cyc_a.data] if cyc_a else [False] * len(c.curves)
        slices = [(cs.first_point_index, cs.points_length) for cs in c.curves]
    finally:
        for m, v in saved:
            m.show_viewport = v
    old = ob.data
    cu = old.copy()
    cu.splines.clear()
    cu.dimensions = '3D'
    for k, (first, n) in enumerate(slices):
        sp = cu.splines.new('POLY')
        sp.points.add(n - 1)
        for j in range(n):
            pt = sp.points[j]
            co = pos[first + j]
            pt.co = (co.x, co.y, co.z, 1.0)
            if rad:
                pt.radius = rad[first + j]
            if tilt:
                pt.tilt = tilt[first + j]
        sp.use_cyclic_u = bool(cyclic[k])
    ob.data = cu
    if old.users == 0:
        bpy.data.curves.remove(old)
    return True


def _bake_to_mesh(ob, context):
    """Text (or anything that can't hold the result): becomes a mesh object in its place."""
    ev, saved = _eval_wrap_only(ob, context)
    try:
        me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True,
                                             depsgraph=context.evaluated_depsgraph_get())
    finally:
        for m, v in saved:
            m.show_viewport = v
    new = bpy.data.objects.new(ob.name, me)
    new.matrix_world = ob.matrix_world.copy()
    new.parent = ob.parent
    if ob.parent:
        new.matrix_parent_inverse = ob.matrix_parent_inverse.copy()
    for col in ob.users_collection:
        col.objects.link(new)
    mod = get_mod(ob)
    for m in list(ob.modifiers)[list(ob.modifiers).index(mod) + 1:]:
        nm = new.modifiers.new(m.name, m.type)
        for prop in m.bl_rna.properties:
            if prop.is_readonly or prop.identifier in {"name", "type"}:
                continue
            try:
                setattr(nm, prop.identifier, getattr(m, prop.identifier))
            except Exception:
                pass
    for ch in list(ob.children):
        mw = ch.matrix_world.copy()
        ch.parent = new
        ch.matrix_world = mw
    name = ob.name
    bpy.data.objects.remove(ob)
    new.name = name
    return new


def apply_wrap(ob, context):
    mod = get_mod(ob)
    if mod is None:
        return None
    if ob.type == 'MESH':
        with context.temp_override(object=ob, active_object=ob, selected_objects=[ob],
                                   selected_editable_objects=[ob]):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        return ob
    if ob.type == 'CURVE':
        if _bake_curve(ob, context):
            ob.modifiers.remove(mod)
            return ob
        return None
    if ob.type == 'FONT':
        return _bake_to_mesh(ob, context)
    return None


class NKPF_OT_apply_wrap(bpy.types.Operator):
    """Make the projection permanent: the selected curves keep the shape they have on the surface
    and no longer depend on the Surface object or on Vector Mode. Texts become meshes"""
    bl_idname = "nkpf.apply_wrap"
    bl_label = "Apply Projection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(get_mod(o) for o in context.selected_objects)

    def execute(self, context):
        done, failed = [], 0
        for ob in _family(context.selected_objects):
            if get_mod(ob) is None:
                continue
            try:
                res = apply_wrap(ob, context)
            except Exception as e:
                print("Nakan Extras for PenForge (apply projection):", e)
                res = None
            if res is None:
                failed += 1
            else:
                done.append(res)
        for o in done:
            try:
                o.select_set(True)
            except ReferenceError:
                pass
        if done:
            context.view_layer.objects.active = done[-1]
        msg = "%d object(s) baked on the surface" % len(done)
        if failed:
            msg += ", %d failed" % failed
        self.report({'INFO'} if not failed else {'WARNING'}, msg)
        return {'FINISHED'} if done else {'CANCELLED'}


def _local_center(ob):
    """Centre of the drawing on its plane (local XY, z = 0)."""
    pts = []
    if ob.type == 'CURVE':
        for s in ob.data.splines:
            if s.type == 'BEZIER':
                pts += [bp.co for bp in s.bezier_points]
            else:
                pts += [p.co.xyz for p in s.points]
    elif ob.type == 'FONT':
        pts = [Vector(c) for c in ob.bound_box]
    elif ob.type == 'MESH':
        pts = [v.co for v in ob.data.vertices]
    if not pts:
        return Vector((0.0, 0.0, 0.0))
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), 0.0))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), 0.0))
    return (lo + hi) / 2


def surface_hit(ob, target, nearest=False):
    """Where the centre of ob lands on the target now: (world point, world normal) or None."""
    mw = ob.matrix_world
    c = mw @ _local_center(ob)
    n = (mw.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
    ti = target.matrix_world.inverted_safe()
    t3 = target.matrix_world.to_3x3()
    tn = t3.inverted_safe().transposed()
    if not nearest:
        src = ti @ (c + n * 50000.0)
        d = (ti.to_3x3() @ -n).normalized()
        ok, loc, nor, _i = target.ray_cast(src, d, distance=200000.0)
        if ok:
            return target.matrix_world @ loc, (tn @ nor).normalized()
    ok, loc, nor, _i = target.closest_point_on_mesh(ti @ c)
    if ok:
        return target.matrix_world @ loc, (tn @ nor).normalized()
    return None


def reproject(ob, target, z_new, nearest=False):
    """Turn ob so it projects along -z_new, its centre landing where it lands now."""
    hit = surface_hit(ob, target, nearest)
    mw = ob.matrix_world
    R = mw.to_3x3().normalized()
    x_old, z_old = R.col[0], R.col[2]
    z = Vector(z_new).normalized()
    if z.dot(z_old) < 0 and hit is None:
        z = -z
    x = x_old - z * x_old.dot(z)
    if x.length < 1e-6:
        x = R.col[1].cross(z)
    x.normalize()
    y = z.cross(x)
    Rn = Matrix((x, y, z)).transposed()
    sca = mw.to_scale()
    c_local = _local_center(ob)
    H = hit[0] if hit is not None else mw @ c_local
    # the centre of the drawing goes onto H: origin = H - (new rotation and scale) @ local centre
    RS = Rn @ Matrix.Diagonal(sca)
    loc = H - RS @ Vector((c_local.x, c_local.y, 0.0))
    ob.matrix_world = Matrix.Translation(loc) @ RS.to_4x4()
    return True


class NKPF_OT_reproject(bpy.types.Operator):
    """Change where the selected drawings are projected from, keeping them where they are on the surface"""
    bl_idname = "nkpf.reproject"
    bl_label = "Re-project"
    bl_options = {'REGISTER', 'UNDO'}

    source: EnumProperty(name="From", items=[
        ('NORMAL', "Normal", "Straight onto the surface where the drawing is (the least stretch)"),
        ('VIEW', "View", "From the current view: orbit to the angle you want, then click"),
        ('CPLANE', "CPlane", "From the current CPlane")])

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        st = context.scene.nkpf_surface
        if st.target is None:
            self.report({'WARNING'}, "Choose the surface Object first")
            return {'CANCELLED'}
        sel = set(context.selected_objects)
        roots = [o for o in context.selected_objects
                 if o.type in {'CURVE', 'FONT', 'MESH'} and o != st.target and o.parent not in sel]
        n = 0
        for ob in roots:
            if self.source == 'NORMAL':
                hit = surface_hit(ob, st.target, st.method == 'NEAREST')
                if hit is None:
                    continue
                z = hit[1]
            elif self.source == 'VIEW':
                rv3d = context.region_data
                if rv3d is None:
                    self.report({'WARNING'}, "Use it from the 3D view")
                    return {'CANCELLED'}
                z = rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
            else:
                from . import common
                _o, R = common.cplane_frame(context.scene)
                z = R.col[2]
            reproject(ob, st.target, z, st.method == 'NEAREST')
            n += 1
        if not n:
            self.report({'WARNING'}, "Nothing on the surface to re-project")
            return {'CANCELLED'}
        return {'FINISHED'}


def draw_ui(layout, context):
    st = context.scene.nkpf_surface
    row = layout.row(align=True)
    row.prop(st, "mode", expand=True)
    if st.mode == 'OBJECT':
        col = layout.column(align=True)
        col.prop(st, "target", icon='MESH_DATA')
        if st.target is None:
            col.label(text="Choose the object to draw on", icon='INFO')
        row = col.row(align=True)
        row.prop(st, "method", expand=True)
        col.prop(st, "offset")
        col.prop(st, "spacing")
        col.prop(st, "subdivide")
        row = layout.row(align=True)
        row.operator(NKPF_OT_wrap.bl_idname, icon='MOD_SHRINKWRAP', text="Wrap Selected").off = False
        row.operator(NKPF_OT_wrap.bl_idname, icon='X', text="Unwrap").off = True
        layout.operator(NKPF_OT_apply_wrap.bl_idname, icon='CHECKMARK')
        col = layout.column(align=True)
        col.label(text="Re-project from:")
        row = col.row(align=True)
        row.operator(NKPF_OT_reproject.bl_idname, text="Normal", icon='NORMALS_FACE').source = 'NORMAL'
        row.operator(NKPF_OT_reproject.bl_idname, text="View", icon='HIDE_OFF').source = 'VIEW'
        row.operator(NKPF_OT_reproject.bl_idname, text="CPlane", icon='ORIENTATION_CURSOR').source = 'CPLANE'


classes = (NKPF_Surface, NKPF_OT_wrap, NKPF_OT_apply_wrap, NKPF_OT_reproject)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nkpf_surface = PointerProperty(type=NKPF_Surface)


def unregister():
    del bpy.types.Scene.nkpf_surface
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
