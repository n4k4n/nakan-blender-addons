"""Curve helpers shared by the pen and the Shape Builder (from the PenForge solid module)."""

import math

import bpy
from mathutils import Vector

_trash = []


def _straight(p0, p1, p2, p3):
    c = p3 - p0
    L2 = c.length_squared
    if L2 < 1e-18:
        return True
    return (p1 - p0).cross(c).length_squared < 1e-10 * L2 * L2 and (p2 - p0).cross(c).length_squared < 1e-10 * L2 * L2


def _is_corner(prev_pt, bp, next_pt, tol_deg=2.0):
    """True if the bezier point is a real corner (tangents broken), False if it is smooth."""
    co = bp.co
    tin = co - bp.handle_left
    if tin.length < 1e-9 and prev_pt is not None:
        tin = co - prev_pt
    tout = bp.handle_right - co
    if tout.length < 1e-9 and next_pt is not None:
        tout = next_pt - co
    if tin.length < 1e-9 or tout.length < 1e-9:
        return True
    return tin.angle(tout) > math.radians(tol_deg)


def curve_polylines_ex(cu, res=None):
    """[(points (local Vectors), closed, set of corner indices)] — straight segments stay single
    edges; corner indices are the real corners (where Miter / Round / Bevel apply)."""
    out = []
    res = max(2, int(res)) if res else max(8, cu.resolution_u)
    for s in cu.splines:
        corners = set()
        if s.type == 'BEZIER':
            bp = s.bezier_points
            n = len(bp)
            if n < 2:
                continue
            closed = s.use_cyclic_u and n >= 2           # a 2-point closed curve (a lens) is closed too
            pts = [bp[0].co.copy()]
            anchor_idx = [0]
            for i in range(n if closed else n - 1):
                a, b = bp[i], bp[(i + 1) % n]
                p0, p1, p2, p3 = a.co, a.handle_right, b.handle_left, b.co
                if _straight(p0, p1, p2, p3):
                    pts.append(p3.copy())
                else:
                    for k in range(1, res + 1):
                        t = k / res
                        u = 1 - t
                        pts.append(p0 * (u * u * u) + p1 * (3 * u * u * t) + p2 * (3 * u * t * t) + p3 * (t * t * t))
                anchor_idx.append(len(pts) - 1)
            if closed:
                pts.pop()                       # last == first
                anchor_idx.pop()
            for j, idx in enumerate(anchor_idx):
                prv = bp[j - 1].co if (closed or j > 0) else None
                nxt = bp[(j + 1) % n].co if (closed or j < n - 1) else None
                if _is_corner(prv, bp[j], nxt):
                    corners.add(idx)
        else:
            pts = [p.co.xyz.copy() for p in s.points]
            closed = s.use_cyclic_u and len(pts) > 2
            corners = set(range(len(pts)))
        clean, cmap = [], []
        for i, p in enumerate(pts):
            if not clean or (p - clean[-1]).length > 1e-7:
                clean.append(p)
                cmap.append(i in corners)
            elif i in corners:
                cmap[-1] = True
        if closed and len(clean) > 2 and (clean[0] - clean[-1]).length < 1e-7:
            clean.pop()
            c_last = cmap.pop()
            cmap[0] = cmap[0] or c_last
        if len(clean) >= 2:
            out.append((clean, closed and len(clean) > 2, {i for i, c in enumerate(cmap) if c}))
    return out


def curve_polylines(cu):
    """[(points (local Vectors), closed)]"""
    return [(p, c) for p, c, _k in curve_polylines_ex(cu)]


def newell_normal(polys):
    n = Vector()
    for pts, closed in polys:
        if not closed or len(pts) < 3:
            continue
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            n += Vector(((a.y - b.y) * (a.z + b.z), (a.z - b.z) * (a.x + b.x), (a.x - b.x) * (a.y + b.y)))
    if n.length < 1e-12:
        # open or straight curves: plane through the points if they bend, else None
        for pts, _c in polys:
            for i in range(1, len(pts) - 1):
                c = (pts[i] - pts[0]).cross(pts[i + 1] - pts[i])
                if c.length > 1e-9:
                    return c.normalized()
        return None
    return n.normalized()


# ------------------------------------------------------------------ offsets
def _basis(n):
    a = Vector((1, 0, 0)) if abs(n.x) < 0.9 else Vector((0, 1, 0))
    e1 = (a - n * a.dot(n)).normalized()
    return e1, n.cross(e1)



def _empty_trash():
    while _trash:
        ob = _trash.pop()
        try:
            data = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            if data is not None and data.users == 0:
                (bpy.data.meshes if isinstance(data, bpy.types.Mesh) else bpy.data.curves).remove(data)
        except (ReferenceError, RuntimeError, AttributeError):
            pass
    return None


def safe_remove(ob):
    """Delete an object without crashing the UI: buttons in the sidebar may still point at its
    properties (Blender would read freed memory). Unlink it now, free it a moment later."""
    try:
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        ob.parent = None
    except (ReferenceError, RuntimeError):
        return
    _trash.append(ob)
    if not bpy.app.timers.is_registered(_empty_trash):
        bpy.app.timers.register(_empty_trash, first_interval=0.25)
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except (AttributeError, RuntimeError):
        pass

