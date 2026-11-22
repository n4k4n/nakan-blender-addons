"""View projection, construction planes and Bezier helpers (no UI: testable headless)."""

import math

from mathutils import Matrix, Vector
from mathutils.geometry import intersect_line_plane


class View:
    """What the pen needs from a 3D viewport: projection to pixels and the mouse ray.
    Built from region/rv3d in the UI, or from plain matrices in tests."""

    def __init__(self, persp_matrix, width, height, is_persp, view_matrix=None):
        self.pm = Matrix(persp_matrix)
        self.ipm = self.pm.inverted_safe()
        self.w, self.h = width, height
        self.is_persp = is_persp
        self.vm = Matrix(view_matrix) if view_matrix is not None else None

    @classmethod
    def from_context(cls, region, rv3d):
        return cls(rv3d.perspective_matrix, region.width, region.height, rv3d.is_perspective, rv3d.view_matrix)

    def key(self):
        return (tuple(round(v, 7) for row in self.pm for v in row), self.w, self.h)

    def project(self, p):
        v = self.pm @ Vector((p[0], p[1], p[2], 1.0))
        if v.w <= 1e-9:
            return None
        return Vector(((v.x / v.w * 0.5 + 0.5) * self.w, (v.y / v.w * 0.5 + 0.5) * self.h))

    def ray(self, mx, my):
        x = mx / self.w * 2.0 - 1.0
        y = my / self.h * 2.0 - 1.0
        a = self.ipm @ Vector((x, y, -1.0, 1.0))
        b = self.ipm @ Vector((x, y, 1.0, 1.0))
        a = a.xyz / a.w
        b = b.xyz / b.w
        d = (b - a)
        if d.length < 1e-12:
            d = Vector((0, 0, -1))
        return a, d.normalized()

    def pixel_size(self, p):
        """World size of one pixel at point p (for tolerances and handle lengths)."""
        s = self.project(p)
        if s is None:
            return 0.01
        o, d = self.ray(s.x, s.y)
        o2, d2 = self.ray(s.x + 1.0, s.y)
        # distance between the two rays at the depth of p
        t = (Vector(p) - o).dot(d)
        q1 = o + d * t
        t2 = (Vector(p) - o2).dot(d2)
        q2 = o2 + d2 * t2
        return max((q2 - q1).length, 1e-9)


class Plane:
    """Construction plane: origin + orthonormal x, y axes (normal = x cross y)."""

    def __init__(self, origin, xaxis, yaxis, name="Top"):
        self.origin = Vector(origin)
        self.x = Vector(xaxis).normalized()
        self.y = Vector(yaxis).normalized()
        self.n = self.x.cross(self.y).normalized()
        self.name = name

    def through(self, p):
        return Plane(p, self.x, self.y, self.name)

    def hit(self, origin, direction):
        far = origin + direction * 1e7
        p = intersect_line_plane(origin, far, self.origin, self.n)
        if p is None or abs(direction.dot(self.n)) < 1e-6:
            # ray parallel to the plane: use the closest point on the plane to the ray origin
            return self.project(origin + direction * 10.0)
        return p

    def project(self, p):
        p = Vector(p)
        return p - self.n * (p - self.origin).dot(self.n)

    def to_local(self, p):
        d = Vector(p) - self.origin
        return d.dot(self.x), d.dot(self.y), d.dot(self.n)

    def from_local(self, u, v, w=0.0):
        return self.origin + self.x * u + self.y * v + self.n * w


def make_plane(kind, cursor_loc, cursor_rot, view_matrix=None):
    """kind: TOP / FRONT / RIGHT / CURSOR / VIEW. The plane passes through the 3D cursor."""
    o = Vector(cursor_loc)
    if kind == 'FRONT':
        return Plane(o, (1, 0, 0), (0, 0, 1), "Front")
    if kind == 'RIGHT':
        return Plane(o, (0, 1, 0), (0, 0, 1), "Right")
    if kind == 'CURSOR':
        m = cursor_rot.to_matrix() if hasattr(cursor_rot, "to_matrix") else Matrix(cursor_rot)
        return Plane(o, m.col[0], m.col[1], "Cursor")
    if kind == 'VIEW' and view_matrix is not None:
        r = view_matrix.to_3x3().inverted_safe()
        return Plane(o, r.col[0], r.col[1], "View")
    return Plane(o, (1, 0, 0), (0, 1, 0), "Top")


def scene_plane(scene, view_matrix=None, kind=None):
    """The CPlane in use: the base plane (Top/Front/Right/Cursor/View through the 3D cursor)
    turned by the live rotation offset (sidebar X/Y/Z, not yet applied)."""
    from mathutils import Euler
    st = scene.nkdraw
    k = kind or st.cplane
    cur = scene.cursor
    pl = make_plane(k, cur.location, cur.rotation_euler, view_matrix)
    off = tuple(getattr(st, "cplane_offset", (0.0, 0.0, 0.0)))
    if any(abs(a) > 1e-9 for a in off):
        B = Matrix((pl.x, pl.y, pl.n)).transposed()
        R = Euler(off, 'XYZ').to_matrix()
        M = B @ R
        pl = Plane(pl.origin, M.col[0], M.col[1], pl.name + " (turned)")
    return pl


# --------------------------------------------------------------------- bezier
def bez(p0, p1, p2, p3, t):
    u = 1.0 - t
    return p0 * (u * u * u) + p1 * (3 * u * u * t) + p2 * (3 * u * t * t) + p3 * (t * t * t)


def bez_split(p0, p1, p2, p3, t):
    """De Casteljau: returns (left handle of new point, new point, right handle of new point,
    new p1, new p2) so that the curve shape does not change."""
    a = p0.lerp(p1, t)
    b = p1.lerp(p2, t)
    c = p2.lerp(p3, t)
    d = a.lerp(b, t)
    e = b.lerp(c, t)
    f = d.lerp(e, t)
    return d, f, e, a, c


def bez_samples(p0, p1, p2, p3, n=16):
    return [bez(p0, p1, p2, p3, i / n) for i in range(n + 1)]


def bez_closest(p0, p1, p2, p3, view, mx, my, n=32):
    """Parameter t of the point of the segment closest to the mouse on screen, and its pixel distance."""
    best = (None, 1e18)
    m = Vector((mx, my))
    prev = None
    for i in range(n + 1):
        t = i / n
        s = view.project(bez(p0, p1, p2, p3, t))
        if s is None:
            prev = None
            continue
        if prev is not None:
            ps, pt = prev
            seg = s - ps
            ll = seg.length_squared
            k = 0.0 if ll < 1e-12 else max(0.0, min(1.0, (m - ps).dot(seg) / ll))
            d = (ps + seg * k - m).length
            if d < best[1]:
                best = (pt + (t - pt) * k, d)
        prev = (s, t)
    return best


def angle_deg(v, plane):
    u, w, _ = (v.dot(plane.x), v.dot(plane.y), 0)
    return math.degrees(math.atan2(w, u))


def pref(name, default):
    """a Vector Draw add-on preference (Edit > Preferences > Add-ons > Nakan Vector Draw), or default"""
    import bpy
    try:
        return getattr(bpy.context.preferences.addons[__package__].preferences, name)
    except (KeyError, AttributeError):
        return default


LIMIT_NOTE = "limit %s \u00b7 raise it in Preferences \u203a Add-ons \u203a Vector Draw"
