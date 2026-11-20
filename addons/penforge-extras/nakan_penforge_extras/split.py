"""Split: every separate shape of a curve becomes its own object.

A closed spline inside another one is a hole and stays with it (the A, B, O, R of a text);
an island inside a hole is a new shape again. Open splines are shapes of their own. Each part gets
its origin at its centre and the object's settings (modifiers, materials, Surface Wrap); parts are
named left to right. An Extrude / Thickness solid of the original is kept as an independent mesh.
"""
import bpy
from mathutils import Matrix, Vector


def _bez(p0, p1, p2, p3, t):
    u = 1 - t
    return p0 * u ** 3 + p1 * 3 * u * u * t + p2 * 3 * u * t * t + p3 * t ** 3


def _poly(s, res=12):
    if s.type == 'BEZIER':
        bp = s.bezier_points
        n = len(bp)
        segs = n if s.use_cyclic_u else n - 1
        pts = [bp[0].co.copy()] if n == 1 else []
        for i in range(segs):
            a, b = bp[i], bp[(i + 1) % n]
            for k in range(res):
                pts.append(_bez(a.co, a.handle_right, b.handle_left, b.co, k / res))
        if not s.use_cyclic_u and n:
            pts.append(bp[-1].co.copy())
    else:
        pts = [p.co.xyz for p in s.points]
    return [(p.x, p.y) for p in pts]


def _area(pl):
    a = 0.0
    for i in range(len(pl)):
        x1, y1 = pl[i - 1]
        x2, y2 = pl[i]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


def _inside(pt, pl):
    x, y = pt
    c = False
    j = len(pl) - 1
    for i in range(len(pl)):
        xi, yi = pl[i]
        xj, yj = pl[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi:
            c = not c
        j = i
    return c


def groups_of(cu):
    """[[spline indices]]: each outer closed spline with its holes; open splines alone."""
    polys = [_poly(s) for s in cu.splines]
    closed = [i for i, s in enumerate(cu.splines) if s.use_cyclic_u and len(polys[i]) >= 3]
    area = {i: _area(polys[i]) for i in closed}
    parent = {}
    for i in closed:                               # smallest closed spline that contains this one
        best = None
        for j in closed:
            if j == i or area[j] <= area[i]:
                continue
            if _inside(polys[i][0], polys[j]) and (best is None or area[j] < area[best]):
                best = j
        parent[i] = best

    def depth(i):
        d, p = 0, parent[i]
        while p is not None:
            d, p = d + 1, parent[p]
        return d
    out = {}
    for i in closed:
        d = depth(i)
        if d % 2 == 0:
            out.setdefault(i, []).insert(0, i)     # an outer shape (or an island inside a hole)
        else:
            out.setdefault(parent[i], []).append(i)  # a hole: goes with its outer shape
    groups = list(out.values())
    groups += [[i] for i, s in enumerate(cu.splines) if i not in closed]
    return groups


def _copy_spline(dst_cu, s, shift):
    if s.type == 'BEZIER':
        n = dst_cu.splines.new('BEZIER')
        n.bezier_points.add(len(s.bezier_points) - 1)
        for a, b in zip(s.bezier_points, n.bezier_points):
            b.handle_left_type = b.handle_right_type = 'FREE'
            b.co = a.co - shift
            b.handle_left = a.handle_left - shift
            b.handle_right = a.handle_right - shift
            b.tilt, b.radius, b.weight_softbody = a.tilt, a.radius, a.weight_softbody
        for a, b in zip(s.bezier_points, n.bezier_points):
            b.handle_left_type, b.handle_right_type = a.handle_left_type, a.handle_right_type
    else:
        n = dst_cu.splines.new(s.type)
        n.points.add(len(s.points) - 1)
        for a, b in zip(s.points, n.points):
            co = a.co.copy()
            co.x -= shift.x
            co.y -= shift.y
            co.z -= shift.z
            b.co, b.tilt, b.radius = co, a.tilt, a.radius
        n.order_u, n.use_endpoint_u = s.order_u, s.use_endpoint_u
    for attr in ("use_cyclic_u", "material_index", "resolution_u", "use_smooth"):
        setattr(n, attr, getattr(s, attr))
    return n


def split_object(context, ob):
    cu = ob.data
    groups = groups_of(cu)
    if len(groups) < 2:
        return []
    parts = []
    for g in groups:
        pts = [Vector((x, y, 0.0)) for i in g for x, y in _poly(cu.splines[i])]
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), 0.0))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), 0.0))
        parts.append((g, (lo + hi) / 2))
    parts.sort(key=lambda gc: (round(gc[1].x, 6), -gc[1].y))
    base = ob.name
    made = []
    for k, (g, c) in enumerate(parts, 1):
        ncu = cu.copy()
        for s in list(ncu.splines):
            ncu.splines.remove(s)
        if ncu.animation_data is not None:
            ncu.animation_data_clear()
        for i in g:
            _copy_spline(ncu, cu.splines[i], c)
        n = ob.copy()                              # modifiers, materials, Surface Wrap, custom values
        n.data = ncu
        n.name = ncu.name = "%s %d" % (base, k)
        n.matrix_world = ob.matrix_world @ Matrix.Translation(c)
        sh = getattr(n, "nkpf_shape", None)
        if sh is not None:
            sh.live = False
        for coll in ob.users_collection:
            coll.objects.link(n)
        made.append(n)
    # an Extrude / Thickness solid of the original: kept, as an independent mesh
    for ch in list(ob.children):
        sp = getattr(ch, "penforge_solid", None)
        mw = ch.matrix_world.copy()
        if sp is not None and getattr(sp, "is_solid", False):
            sp.is_solid = False
            sp.source = None
        ch.parent = None
        ch.matrix_world = mw
    for o in context.scene.objects:               # a text whose curves these were: forget the link
        if o.type == 'FONT' and getattr(o, "nkpf_text_curves", None) == ob:
            o.nkpf_text_curves = None
    old = ob.data
    bpy.data.objects.remove(ob, do_unlink=True)
    if old.users == 0:
        bpy.data.curves.remove(old)
    return made


class NKPF_OT_split(bpy.types.Operator):
    """Split the selected curves: every separate shape becomes its own object (holes stay with their
    letter / shape), origin at its centre, named left to right"""
    bl_idname = "nkpf.split"
    bl_label = "Split"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(o.type == 'CURVE' for o in context.selected_objects)

    def execute(self, context):
        made, prim = [], []
        for ob in [o for o in context.selected_objects if o.type == 'CURVE']:
            sh = getattr(ob, "nkpf_shape", None)
            if sh is not None and sh.live:
                prim.append(ob.name)
                continue
            made += split_object(context, ob)
        if prim and not made:
            self.report({'WARNING'}, "Primitives have one shape only: nothing to split")
            return {'CANCELLED'}
        if not made:
            self.report({'INFO'}, "Nothing to split: one shape only")
            return {'CANCELLED'}
        for x in context.selected_objects:
            x.select_set(False)
        for o in made:
            o.select_set(True)
        context.view_layer.objects.active = made[0]
        self.report({'INFO'}, "%d object(s)" % len(made))
        return {'FINISHED'}


classes = (NKPF_OT_split,)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
