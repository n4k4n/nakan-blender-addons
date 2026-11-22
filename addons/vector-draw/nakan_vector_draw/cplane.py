"""CPlane from a face: click a mesh face and the construction plane (and the grid) lies on it.
The plane is stored in the 3D cursor (location + rotation) with CPlane = Cursor, so it stays
after the command and Blender's own tools can use it too."""

import math

import bpy
from mathutils import Matrix, Vector


def face_frame(context, view, mx, my, origin_hint=None):
    """(origin, x, y, normal, object name) of the face under the mouse, or None."""
    dg = context.evaluated_depsgraph_get()
    ro, rd = view.ray(mx, my)
    hit, loc, nrm, idx, ob, mw = context.scene.ray_cast(dg, ro, rd)
    if not hit:
        return None
    n = Vector(nrm).normalized()
    if n.dot(rd) > 0:
        n = -n                                    # face the viewer
    x = None
    try:
        ev = ob.evaluated_get(dg)
        me = ev.data if ev.type == 'MESH' else ev.to_mesh()
        poly = me.polygons[idx]
        vs = [mw @ me.vertices[v].co for v in poly.vertices]
        best = 0.0
        for i in range(len(vs)):
            e = vs[(i + 1) % len(vs)] - vs[i]
            e = e - n * e.dot(n)
            if e.length > best:
                best, x = e.length, e.normalized()
        if ev.type != 'MESH':
            ev.to_mesh_clear()
    except (AttributeError, IndexError, RuntimeError):
        x = None
    if x is None:
        for a in (Vector((1, 0, 0)), Vector((0, 1, 0))):
            x = a - n * a.dot(n)
            if x.length > 1e-6:
                x.normalize()
                break
    y = n.cross(x).normalized()
    origin = Vector(origin_hint) if origin_hint is not None else Vector(loc)
    origin = origin - n * (origin - Vector(loc)).dot(n)          # keep the origin on the face plane
    return origin, x, y, n, ob.name


def apply_frame(context, frame):
    origin, x, y, n, _name = frame
    m = Matrix((x, y, n)).transposed().to_4x4()
    m.translation = origin
    write_matrix(context, m)


def write_matrix(context, m):
    """Make `m` (columns X, Y, normal, origin) the drawing plane: into the Plane Object when the plane is set
    to Object, otherwise into the 3D cursor (plane = Cursor)."""
    st = context.scene.nkdraw
    ob = getattr(st, "plane_object", None)
    if st.cplane == 'OBJECT' and ob is not None:
        sca = ob.matrix_world.to_scale()
        ob.matrix_world = Matrix.LocRotScale(m.translation, m.to_quaternion(), sca)
    else:
        context.scene.cursor.matrix = m
        st.cplane = 'CURSOR'
    st.cplane_offset = (0.0, 0.0, 0.0)


def set_world(context, kind='TOP'):
    cur = context.scene.cursor
    cur.rotation_mode = 'XYZ'
    cur.rotation_euler = (0.0, 0.0, 0.0)
    cur.location = (0.0, 0.0, 0.0)
    context.scene.nkdraw.cplane = kind
    context.scene.nkdraw.cplane_offset = (0.0, 0.0, 0.0)


# ---------------------------------------------------------------- origin / rotation (0.7)
def current_matrix(context, view_matrix=None):
    """4x4 matrix of the CPlane in use: columns X, Y, normal, origin."""
    from . import geom
    st = context.scene.nkdraw
    cur = context.scene.cursor
    pl = geom.scene_plane(context.scene, view_matrix)
    x = Vector(pl.x).normalized()
    y = Vector(pl.y).normalized()
    z = x.cross(y).normalized()
    y = z.cross(x).normalized()
    m = Matrix((x, y, z)).transposed().to_4x4()
    m.translation = Vector(pl.origin)
    return m


def move_origin(context, p):
    """CPlane (and grid) to `p`, rotation unchanged."""
    st = context.scene.nkdraw
    ob = getattr(st, "plane_object", None)
    if st.cplane == 'OBJECT' and ob is not None:
        mw = ob.matrix_world.copy()
        mw.translation = Vector(p)
        ob.matrix_world = mw
    else:
        context.scene.cursor.location = Vector(p)


def rotate(context, pivot, axis, angle, view_matrix=None):
    """Turn the CPlane by `angle` (radians) around the line through `pivot` along `axis`."""
    axis = Vector(axis)
    if axis.length < 1e-12:
        return False
    m = current_matrix(context, view_matrix)
    R = Matrix.Rotation(angle, 4, axis.normalized())
    piv = Vector(pivot)
    new = Matrix.Translation(piv) @ R @ Matrix.Translation(-piv) @ m
    write_matrix(context, new)                  # the live turn is now part of the CPlane
    return True


def rotate_local(context, which, angle, view_matrix=None):
    """Add `angle` to the live turn of the CPlane around its X / Y / Z axis (seen at once, not applied)."""
    st = context.scene.nkdraw
    off = list(st.cplane_offset)
    i = {'X': 0, 'Y': 1, 'Z': 2}[which]
    a = off[i] + angle
    a = (a + math.pi) % (2 * math.pi) - math.pi          # keep it in -180..180
    off[i] = a
    st.cplane_offset = off
    return True


def apply_turn(context, view_matrix=None):
    """Make the live turn part of the CPlane (3D cursor rotation) and reset the X / Y / Z values to 0."""
    write_matrix(context, current_matrix(context, view_matrix))


class _PickSettings:
    """Snap settings for picking a point or an edge (whatever the user's Osnap)."""

    def __init__(self, st):
        self._st = st

    def __getattr__(self, name):
        if name in ("osnap", "snap_end", "snap_vert", "snap_mid", "snap_int", "snap_near"):
            return True
        if name in ("snap_cen", "snap_perp", "snap_grid", "snap_project"):
            return False
        return getattr(self._st, name)


def pick(context, view, cache, mx, my, visible=None):
    """What is under the mouse: ('point', p) | ('edge', p, a, b) | None. Faces are handled apart."""
    st = context.scene.nkdraw
    sn = cache.find(view, mx, my, _PickSettings(st), visible=visible)
    if sn is None:
        return None
    if sn.kind == "NEAR" and sn.owner is not None:
        a, b = sn.owner
        return ("edge", sn.point, Vector(a), Vector(b))
    edge = cache.last_edge
    return ("point", sn.point, edge)


def _set_frame(context, origin, x, y, z):
    m = Matrix((x, y, z)).transposed().to_4x4()
    m.translation = Vector(origin)
    write_matrix(context, m)


def align_axis(context, origin, direction, view_matrix=None, axis=None):
    """Origin at `origin`; the CPlane axis closest to `direction` (X or Y, or the one given) turns onto
    it with the smallest possible rotation. Returns the axis name."""
    d = Vector(direction)
    if d.length < 1e-12:
        move_origin(context, origin)
        return None
    d.normalize()
    m = current_matrix(context, view_matrix).to_3x3()
    x, y = m.col[0].normalized(), m.col[1].normalized()
    if axis is None:
        axis = 'X' if abs(x.dot(d)) >= abs(y.dot(d)) else 'Y'
    a = x if axis == 'X' else y
    if a.dot(d) < 0:
        d = -d
    R = a.rotation_difference(d).to_matrix()
    m2 = R @ m
    _set_frame(context, origin, m2.col[0].normalized(), m2.col[1].normalized(), m2.col[2].normalized())
    return axis


def frame_from_points(context, p0, p1, p2=None, view_matrix=None):
    """Rhino CPlane 3 points: origin p0, X towards p1, the plane through p2 (Y on p2's side).
    With two points only, X goes to p1 and the plane turns as little as possible."""
    p0, p1 = Vector(p0), Vector(p1)
    x = p1 - p0
    if x.length < 1e-9:
        return False
    x.normalize()
    if p2 is not None:
        t = Vector(p2) - p0
        z = x.cross(t)
        if z.length > 1e-9:
            z.normalize()
            _set_frame(context, p0, x, z.cross(x).normalized(), z)
            return True
    align_axis(context, p0, x, view_matrix, axis='X')
    return True


def point_under_mouse(context, view, cache, mx, my, visible=None):
    """A point for the CPlane picks: a snap (point / edge), else the surface hit, else the CPlane."""
    hit = pick(context, view, cache, mx, my, visible)
    if hit is not None:
        return Vector(hit[1])
    dg = context.evaluated_depsgraph_get()
    ro, rd = view.ray(mx, my)
    ok, loc, *_ = context.scene.ray_cast(dg, ro, rd)
    if ok:
        return Vector(loc)
    from . import geom
    return geom.scene_plane(context.scene, view.vm).hit(ro, rd)


def apply_pick(context, view, cache, mx, my, hinge=False, force_face=False, visible=None, origin_hint=None):
    """The CPlane pick (F in the pen, or the sidebar eyedropper):
    point -> origin there, rotation kept; edge -> origin on the edge, rotation kept;
    Shift on an edge -> the CPlane turns around that edge by the CPlane angle;
    face (or Alt) -> CPlane on the face. Returns a message."""
    st = context.scene.nkdraw
    vm = view.vm if view is not None else None
    hit = None if force_face else pick(context, view, cache, mx, my, visible)
    if hit is not None:
        if hit[0] == "edge" or (hinge and hit[2] is not None):
            if hit[0] == "edge":
                p, a, b = hit[1], hit[2], hit[3]
            else:
                p = hit[1]
                a, b = hit[2]
                a, b = Vector(a), Vector(b)
            ax = align_axis(context, p, b - a, vm)          # X or Y lies on the edge
            if hinge:
                rotate(context, p, b - a, st.cplane_angle, vm)
                move_origin(context, p)
                return "CPlane on the edge, turned %.4g\u00b0 around it" % math.degrees(st.cplane_angle)
            return "CPlane on the edge: %s axis along it" % ax
        move_origin(context, hit[1])
        return "CPlane origin on the point (rotation kept)"
    fr = face_frame(context, view, mx, my, origin_hint)
    if fr is None:
        return None
    apply_frame(context, fr)
    return "CPlane on a face of %s" % fr[4]
