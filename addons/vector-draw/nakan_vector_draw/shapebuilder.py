"""Shape Builder (Illustrator): a 2D boolean by dragging over the regions of overlapping shapes.

The selected closed curves are flattened on one plane and cut into all their regions (every piece
where the shapes overlap or not). Drag across regions = they merge into one shape; Alt+drag = they
are deleted; a click on a region = that region becomes a shape of its own. Enter writes the result as
new curves (the originals are removed), Esc leaves everything as it was.
"""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
from mathutils.geometry import tessellate_polygon

from . import geom, stroke, curveutil as SL

SCALE = stroke.SCALE
MIN_AREA = 1e-8


# ----------------------------------------------------------------------------------- 2D regions
def _keep_collinear(c):
    try:
        c.preserveCollinear = True        # every sample stays: the result maps back onto the source curves
    except Exception:      # noqa: BLE001
        pass


def _clip(op, subject, clip=None, fill=None):
    import numpy as np
    import pyclipr
    c = pyclipr.Clipper()
    c.scaleFactor = SCALE
    _keep_collinear(c)
    subj = [np.asarray(p, dtype=float) for p in subject if len(p) >= 3]
    if not subj:
        return []
    c.addPaths(subj, pyclipr.Subject)
    if clip:
        cl = [np.asarray(p, dtype=float) for p in clip if len(p) >= 3]
        if cl:
            c.addPaths(cl, pyclipr.Clip)
    r = c.execute(op, fill if fill is not None else pyclipr.FillRule.NonZero)
    return [[(float(x), float(y)) for x, y in np.asarray(p)] for p in r]


def _pieces(paths):
    """Split a set of oriented paths into connected pieces: [[outer, hole, hole...], ...]."""
    import numpy as np
    import pyclipr
    pp = [np.asarray(p, dtype=float) for p in paths if len(p) >= 3]
    if not pp:
        return []
    c = pyclipr.Clipper()
    c.scaleFactor = SCALE
    _keep_collinear(c)
    c.addPaths(pp, pyclipr.Subject)
    tree = c.executeTree(pyclipr.Union, pyclipr.FillRule.NonZero)
    out = []

    def walk(node):
        for ch in node.children:
            if ch.isHole:
                continue
            outer = [(float(x), float(y)) for x, y in np.asarray(ch.polygon)]
            holes = []
            for h in ch.children:
                holes.append([(float(x), float(y)) for x, y in np.asarray(h.polygon)])
                walk(h)
            if abs(stroke._area(outer)) + sum(stroke._area(h) for h in holes) > MIN_AREA:
                out.append([outer] + holes)
    walk(tree)
    return out


def area(piece):
    return sum(stroke._area(p) for p in piece)


def _thin(piece, eps):
    """a sliver: average width (2 area / perimeter) under eps"""
    A = abs(area(piece))
    P = sum(math.dist(lp[i - 1], lp[i]) for lp in piece for i in range(len(lp)))
    return P > 0 and 2.0 * A / P < eps


def split_regions(shapes):
    """shapes: [[2D loops of one shape]] -> every region of the arrangement, as pieces."""
    import pyclipr
    allp = [p for loops in shapes for lp in loops for p in lp]
    if allp:
        xs, ys = [p[0] for p in allp], [p[1] for p in allp]
        eps = 2e-5 * (math.hypot(max(xs) - min(xs), max(ys) - min(ys)) or 1.0)
    else:
        eps = 0.0
    regions = []
    union = []
    for loops in shapes:
        S = _clip(pyclipr.Union, loops, fill=pyclipr.FillRule.EvenOdd)   # a shape's own holes stay holes
        if not S:
            continue
        new = []
        for R in regions:
            new += _pieces(_clip(pyclipr.Intersection, R, S))
            new += _pieces(_clip(pyclipr.Difference, R, S))
        new += _pieces(_clip(pyclipr.Difference, S, union)) if union else _pieces(S)
        regions = [r for r in new if area(r) > MIN_AREA]
        union = _clip(pyclipr.Union, union + S)
    return [r for r in regions if not _thin(r, eps)]     # hairline slivers of nearly coincident outlines


def merge(pieces_list):
    import pyclipr
    paths = [p for piece in pieces_list for p in piece]
    return _pieces(_clip(pyclipr.Union, paths))


def inside(piece, x, y):
    def pip(poly):
        c = False
        n = len(poly)
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0 + 1e-300) + x0:
                c = not c
            # (even-odd test: holes flip it back)
        return c
    hits = sum(1 for p in piece if pip(p))
    return hits % 2 == 1


def simplify(loop, tol):
    """Douglas-Peucker on a closed loop (keeps the shape within tol)."""
    if len(loop) < 5:
        return loop
    # split at the two farthest points so the closed loop becomes two open runs
    i0 = 0
    i1 = max(range(len(loop)), key=lambda i: math.dist(loop[i0], loop[i]))

    def dp(pts):
        if len(pts) < 3:
            return pts
        a, b = pts[0], pts[-1]
        ab = math.dist(a, b)
        best, bi = -1.0, 0
        for i in range(1, len(pts) - 1):
            p = pts[i]
            if ab < 1e-12:
                d = math.dist(a, p)
            else:
                d = abs((b[0] - a[0]) * (a[1] - p[1]) - (a[0] - p[0]) * (b[1] - a[1])) / ab
            if d > best:
                best, bi = d, i
        if best <= tol:
            return [a, b]
        left = dp(pts[:bi + 1])
        return left[:-1] + dp(pts[bi:])
    run1 = loop[i0:i1 + 1]
    run2 = loop[i1:] + loop[:1]
    out = dp(run1)[:-1] + dp(run2)[:-1]
    return out if len(out) >= 3 else loop


# ----------------------------------------------------------------------------------- scene in / out
def plane_of(objs):
    """Plane of the first curve with a normal (origin, x, y, n) in world space."""
    for ob in objs:
        polys = SL.curve_polylines(ob.data)
        mw = ob.matrix_world
        wp = [([mw @ p for p in pts], closed) for pts, closed in polys]
        nrm = SL.newell_normal(wp)
        if nrm is not None and wp and wp[0][0]:
            e1, e2 = SL._basis(nrm)
            return wp[0][0][0].copy(), e1, e2, nrm
    return Vector(), Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))


def gather(objs, frame, res=32):
    o, e1, e2, _n = frame
    shapes = []
    for ob in objs:
        mw = ob.matrix_world
        loops = []
        for pts, closed in SL.curve_polylines(ob.data) if res is None else \
                [(p, c) for p, c, _k in SL.curve_polylines_ex(ob.data, res)]:
            if not closed or len(pts) < 3:
                continue
            loops.append([((mw @ p - o).dot(e1), (mw @ p - o).dot(e2)) for p in pts])
        if loops:
            shapes.append(loops)
    return shapes


class Source:
    """The Bezier segments behind the sampled loops (2D, on the frame), so that every region edge can be
    traced back to the curve it came from and written as that same curve (circles stay 4-point circles,
    cut only where they are crossed) instead of as a dense polyline."""

    def __init__(self, res):
        self.res = res
        self.curves = []          # per loop: {"segs": [(c0, c1, c2, c3)], "pts": [(x, y)], "par": [(seg, t)]}
        self.kd = None
        self.index = []           # kd index -> (curve, sample)

    def add_loop(self, segs):
        """segs: closed list of 2D cubic control points -> the sampled loop (2D points)"""
        cid = len(self.curves)
        pts, par = [], []
        for i, (c0, c1, c2, c3) in enumerate(segs):
            ks = [0.0] if _straight2(c0, c1, c2, c3) else [k / self.res for k in range(self.res)]
            for t in ks:
                p = _bez(c0, c1, c2, c3, t)
                if pts and math.dist(p, pts[-1]) < 1e-9:
                    continue
                pts.append(p)
                par.append((i, t))
        while len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-9:
            pts.pop()
            par.pop()
        self.curves.append({"segs": segs, "pts": pts, "par": par})
        return pts

    def build(self):
        from mathutils.kdtree import KDTree
        n = sum(len(c["pts"]) for c in self.curves)
        kd = KDTree(max(n, 1))
        k = 0
        for cid, c in enumerate(self.curves):
            for j, (x, y) in enumerate(c["pts"]):
                kd.insert((x, y, 0.0), k)
                self.index.append((cid, j))
                k += 1
        kd.balance()
        self.kd = kd

    def locate(self, x, y, tol):
        """{(curve, seg): t} of every source curve passing through (x, y)"""
        out = {}

        def put(cid, seg, t):
            nseg = len(self.curves[cid]["segs"])
            out[(cid, seg)] = t
            if t <= 1e-12:
                out[(cid, (seg - 1) % nseg)] = 1.0
            elif t >= 1.0 - 1e-12:
                out[(cid, (seg + 1) % nseg)] = 0.0
        for _co, k, _d in self.kd.find_range((x, y, 0.0), tol):
            cid, j = self.index[k]
            seg, t = self.curves[cid]["par"][j]
            put(cid, seg, t)
        if out:
            return out
        # a crossing: on a chord between two samples
        best = None
        for cid, c in enumerate(self.curves):
            pts, par = c["pts"], c["par"]
            m = len(pts)
            for j in range(m):
                a, b = pts[j], pts[(j + 1) % m]
                abx, aby = b[0] - a[0], b[1] - a[1]
                L2 = abx * abx + aby * aby
                if L2 < 1e-24:
                    continue
                u = ((x - a[0]) * abx + (y - a[1]) * aby) / L2
                if u < -1e-9 or u > 1 + 1e-9:
                    continue
                d = abs((x - a[0]) * aby - (y - a[1]) * abx) / math.sqrt(L2)
                if d > tol * 4:
                    continue
                seg, ta = par[j]
                sb, tb = par[(j + 1) % m]
                if sb != seg:
                    tb = 1.0
                t = ta + (tb - ta) * max(0.0, min(1.0, u))
                put(cid, seg, t)
                if best is None or d < best:
                    best = d
        return out

    def rebuild(self, loop, tol):
        """the loop as closed cubic pieces [(c0, c1, c2, c3)] traced on the source curves"""
        n = len(loop)
        if n < 3:
            return None
        V = [self.locate(x, y, tol) for x, y in loop]
        step = 1.0 / self.res
        keys = []
        for k in range(n):
            a, b = V[k], V[(k + 1) % n]
            best = None
            for key in set(a) & set(b):
                dt = b[key] - a[key]
                if abs(dt) < 1e-12:
                    continue
                straight = _straight2(*self.curves[key[0]]["segs"][key[1]])
                if not straight and abs(dt) > 4 * step + 1e-9:
                    continue
                if best is None or abs(dt) < abs(best[1]):
                    best = (key, dt)
            keys.append(None if best is None else (best[0], best[1] > 0))
        # runs of edges on the same source segment, same direction
        start = next((k for k in range(n) if keys[k] is None or keys[k] != keys[k - 1]), 0)
        pieces = []
        k = 0
        while k < n:
            i0 = (start + k) % n
            key = keys[i0]
            j = k + 1
            if key is not None:
                while j < n and keys[(start + j) % n] == key:
                    j += 1
            i1 = (start + j) % n
            pa, pb = loop[i0], loop[i1]
            if key is None or _straight2(*self.curves[key[0][0]]["segs"][key[0][1]]):
                # a straight piece: just the line (its parameter is not proportional to the length when
                # the handles sit on the points, so a sub-curve by t would overshoot)
                d = (pb[0] - pa[0], pb[1] - pa[1])
                pieces.append((pa, (pa[0] + d[0] / 3, pa[1] + d[1] / 3), (pa[0] + 2 * d[0] / 3, pa[1] + 2 * d[1] / 3), pb))
            else:
                (cid, seg), _fw = key
                t0, t1 = V[i0][(cid, seg)], V[i1][(cid, seg)]
                c0, c1, c2, c3 = _sub(self.curves[cid]["segs"][seg], t0, t1)
                # the ends exactly on the region's vertices (a crossing lies on the chord, not the curve)
                c1 = (c1[0] + pa[0] - c0[0], c1[1] + pa[1] - c0[1])
                c2 = (c2[0] + pb[0] - c3[0], c2[1] + pb[1] - c3[1])
                pieces.append((pa, c1, c2, pb))
            k = j
        return pieces


def _bez(c0, c1, c2, c3, t):
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (c0[0] * a + c1[0] * b + c2[0] * c + c3[0] * d, c0[1] * a + c1[1] * b + c2[1] * c + c3[1] * d)


def _blossom(cp, u1, u2, u3):
    pts = list(cp)
    for u in (u1, u2, u3):
        pts = [((1 - u) * p[0] + u * q[0], (1 - u) * p[1] + u * q[1]) for p, q in zip(pts, pts[1:])]
    return pts[0]


def _sub(cp, a, b):
    """control points of the piece of a cubic between parameters a and b (b < a: reversed)"""
    return (_blossom(cp, a, a, a), _blossom(cp, a, a, b), _blossom(cp, a, b, b), _blossom(cp, b, b, b))


def _straight2(c0, c1, c2, c3):
    cx, cy = c3[0] - c0[0], c3[1] - c0[1]
    L2 = cx * cx + cy * cy
    if L2 < 1e-24:
        return True
    for p in (c1, c2):
        cr = (p[0] - c0[0]) * cy - (p[1] - c0[1]) * cx
        if cr * cr > 1e-10 * L2 * L2:
            return False
    return True


def gather_exact(objs, frame, res=32):
    """like gather, plus the Source that maps the samples back onto the Bezier segments"""
    o, e1, e2, _n = frame
    src = Source(res)
    shapes = []

    def to2(v):
        return ((v - o).dot(e1), (v - o).dot(e2))
    for ob in objs:
        mw = ob.matrix_world
        loops = []
        for sp in ob.data.splines:
            if sp.type == 'BEZIER':
                bp = sp.bezier_points
                n = len(bp)
                if not sp.use_cyclic_u or n < 2:
                    continue
                segs = []
                for i in range(n):
                    a, b = bp[i], bp[(i + 1) % n]
                    segs.append((to2(mw @ a.co), to2(mw @ a.handle_right), to2(mw @ b.handle_left), to2(mw @ b.co)))
            else:
                P = [to2(mw @ p.co.xyz) for p in sp.points]
                if not sp.use_cyclic_u or len(P) < 3:
                    continue
                segs = []
                for i in range(len(P)):
                    a, b = P[i], P[(i + 1) % len(P)]
                    d = (b[0] - a[0], b[1] - a[1])
                    segs.append((a, (a[0] + d[0] / 3, a[1] + d[1] / 3), (a[0] + 2 * d[0] / 3, a[1] + 2 * d[1] / 3), b))
            pts = src.add_loop(segs)
            if len(pts) >= 3:
                loops.append(pts)
        if loops:
            shapes.append(loops)
    src.build()
    return shapes, src


def write_exact(context, piece, frame, name, collection, src, tol):
    """a new curve for one region, every edge drawn on the curve it came from; None if it cannot be traced"""
    loops = []
    for loop in piece:
        pcs = src.rebuild(loop, tol)
        if not pcs:
            return None
        loops.append(pcs)
    o, e1, e2, n = frame
    outer = [p[0] for p in loops[0]]
    cx = sum(p[0] for p in outer) / len(outer)
    cy = sum(p[1] for p in outer) / len(outer)
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '3D'
    cu.resolution_u = 12
    ob = bpy.data.objects.new(name, cu)
    m = Matrix((e1, e2, n)).transposed().to_4x4()
    m.translation = o + e1 * cx + e2 * cy
    ob.matrix_world = m

    def L(p):
        return (p[0] - cx, p[1] - cy, 0.0)
    for pcs in loops:
        s = cu.splines.new('BEZIER')
        s.bezier_points.add(len(pcs) - 1)
        k = len(pcs)
        for i, bp in enumerate(s.bezier_points):
            c0, c1, _c2, _c3 = pcs[i]
            prev = pcs[i - 1]
            bp.handle_left_type = bp.handle_right_type = 'FREE'
            bp.co = L(c0)
            bp.handle_right = L(c1)
            bp.handle_left = L(prev[2])
            # smooth where the tangents line up
            lx, ly = c0[0] - prev[2][0], c0[1] - prev[2][1]
            rx, ry = c1[0] - c0[0], c1[1] - c0[1]
            ll, lr = math.hypot(lx, ly), math.hypot(rx, ry)
            if ll > 1e-12 and lr > 1e-12 and (lx * rx + ly * ry) / (ll * lr) > 0.9998:
                bp.handle_left_type = bp.handle_right_type = 'ALIGNED'
        s.use_cyclic_u = True
        _ = k
    collection.objects.link(ob)
    return ob


def write_piece(context, piece, frame, name, collection, tol):
    """A new Bezier curve object for one piece (outer + holes): corners stay sharp (Vector), the rest
    gets Auto handles so curved stretches stay smooth."""
    o, e1, e2, n = frame
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '3D'
    cu.resolution_u = 12
    ob = bpy.data.objects.new(name, cu)
    m = Matrix((e1, e2, n)).transposed().to_4x4()
    outer = piece[0]
    cx = sum(p[0] for p in outer) / len(outer)
    cy = sum(p[1] for p in outer) / len(outer)
    centre = o + e1 * cx + e2 * cy
    m.translation = centre
    ob.matrix_world = m
    for loop in piece:
        lp = simplify(loop, tol)
        if len(lp) < 3:
            continue
        s = cu.splines.new('BEZIER')
        s.bezier_points.add(len(lp) - 1)
        k = len(lp)
        for i, (bp, (x, y)) in enumerate(zip(s.bezier_points, lp)):
            bp.co = (x - cx, y - cy, 0.0)
        for i, bp in enumerate(s.bezier_points):
            a, b, c = lp[i - 1], lp[i], lp[(i + 1) % k]
            v1 = (b[0] - a[0], b[1] - a[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            ang = 0.0
            if l1 > 1e-12 and l2 > 1e-12:
                cosv = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
                ang = math.degrees(math.acos(cosv))
            sharp = ang > 25.0 or max(l1, l2) > 6.0 * max(min(l1, l2), 1e-12)
            bp.handle_left_type = bp.handle_right_type = 'VECTOR' if sharp else 'AUTO'
        s.use_cyclic_u = True
    collection.objects.link(ob)
    return ob


# ----------------------------------------------------------------------------------- the tool
COL_HOVER = (0.85, 0.85, 0.9, 0.22)
COL_MERGE = (0.25, 0.55, 1.0, 0.40)
COL_DEL = (1.0, 0.3, 0.3, 0.40)
COL_EDGE = (0.95, 0.95, 1.0, 0.85)


def _draw3d(op, context):
    if context.region != op.region:
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    for idx, R in enumerate(op.regions):
        col = None
        if idx in op.marked:
            col = COL_DEL if op.alt_mode else COL_MERGE
        elif idx == op.hover:
            col = COL_HOVER
        if col is None:
            continue
        tris = op.tris[idx]
        if tris:
            sh.uniform_float("color", col)
            batch_for_shader(sh, 'TRIS', {"pos": tris}).draw(sh)
    ln = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    ln.uniform_float("viewportSize", (context.region.width, context.region.height))
    ln.uniform_float("lineWidth", 1.3)
    ln.uniform_float("color", COL_EDGE)
    for edges in op.edges:
        batch_for_shader(ln, 'LINES', {"pos": edges}).draw(ln)
    if len(op.trail) > 1:
        ln.uniform_float("lineWidth", 2.0)
        ln.uniform_float("color", (1.0, 0.35, 0.35, 0.95) if op.alt_mode else (0.35, 0.65, 1.0, 0.95))
        batch_for_shader(ln, 'LINE_STRIP', {"pos": op.trail}).draw(ln)
    gpu.state.blend_set('NONE')


class NKDRAW_OT_shape_builder(bpy.types.Operator):
    """Shape Builder (Illustrator): select overlapping closed curves, then drag across their regions to merge
them, Alt+drag to delete them, click a region to make it a shape of its own. Enter applies, Esc cancels"""
    bl_idname = "nkdraw.shape_builder"
    bl_label = "Shape Builder"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return stroke.AVAILABLE and any(o.type == 'CURVE' for o in context.selected_objects)

    # ---- setup
    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}
        self.objs = [o for o in context.selected_objects if o.type == 'CURVE']
        self.frame = plane_of(self.objs)
        shapes, self.src = gather_exact(self.objs, self.frame)
        if not shapes:
            self.report({'WARNING'}, "Select closed curves")
            return {'CANCELLED'}
        self.regions = split_regions(shapes)
        if not self.regions:
            self.report({'WARNING'}, "No regions found")
            return {'CANCELLED'}
        allpts = [p for R in self.regions for p in R[0]]
        xs, ys = [p[0] for p in allpts], [p[1] for p in allpts]
        self.size = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
        self.history = []
        self.marked = []
        self.hover = None
        self.alt_mode = False
        self.dragging = False
        self.trail = []
        self.region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if context.region is not None and context.region.type == 'WINDOW':
            self.region = context.region
        self.rv3d = context.area.spaces.active.region_3d
        self._rebuild_display()
        self._h = bpy.types.SpaceView3D.draw_handler_add(_draw3d, (self, context), 'WINDOW', 'POST_VIEW')
        context.window_manager.modal_handler_add(self)
        self._status(context)
        return {'RUNNING_MODAL'}

    def _w(self, x, y):
        o, e1, e2, _n = self.frame
        return o + e1 * x + e2 * y

    def _rebuild_display(self):
        self.tris, self.edges = [], []
        for R in self.regions:
            loops = [lp for lp in R if len(lp) >= 3]
            tt = tessellate_polygon([[Vector((x, y, 0.0)) for x, y in lp] for lp in loops]) if loops else []
            flat = [p for lp in loops for p in lp]
            self.tris.append([self._w(*flat[i]) for t in tt for i in t])
            e = []
            for lp in loops:
                for i in range(len(lp)):
                    e += [self._w(*lp[i]), self._w(*lp[(i + 1) % len(lp)])]
            self.edges.append(e)

    def _status(self, context):
        context.workspace.status_text_set(
            "Shape Builder  ·  drag across regions: merge  ·  Alt/Cmd+drag or X: delete  ·  click a region: "
            "keep it as its own shape  ·  Ctrl+Z undo  ·  Enter/Space apply  ·  Esc / right click cancel")
        context.area.header_text_set("Shape Builder: %d regions" % len(self.regions))

    def _end(self, context):
        if getattr(self, "_h", None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._h, 'WINDOW')
            self._h = None
        context.workspace.status_text_set(None)
        context.area.header_text_set(None)
        context.area.tag_redraw()

    # ---- picking
    def _uv(self, event):
        r = self.region
        view = geom.View.from_context(r, self.rv3d)
        ro, rd = view.ray(event.mouse_x - r.x, event.mouse_y - r.y)
        o, e1, e2, n = self.frame
        den = rd.dot(n)
        if abs(den) < 1e-9:
            return None
        t = (o - ro).dot(n) / den
        p = ro + rd * t
        return (p - o).dot(e1), (p - o).dot(e2), p

    def _region_at(self, uv):
        if uv is None:
            return None
        x, y = uv[0], uv[1]
        best = None
        for idx, R in enumerate(self.regions):
            if inside(R, x, y):
                a = abs(area(R))
                if best is None or a < best[0]:
                    best = (a, idx)
        return best[1] if best else None

    # ---- modal
    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'} \
                or t.startswith('NDOF'):
            return {'PASS_THROUGH'}
        context.area.tag_redraw()
        if t == 'MOUSEMOVE':
            uv = self._uv(event)
            self.hover = self._region_at(uv)
            if self.dragging:
                if self.hover is not None and self.hover not in self.marked:
                    self.marked.append(self.hover)
                if uv is not None:
                    self.trail.append(uv[2])
            return {'RUNNING_MODAL'}
        if t == 'LEFTMOUSE' and v == 'PRESS':
            self.dragging = True
            self.alt_mode = event.alt or event.oskey      # Alt / Option or Cmd: delete
            uv = self._uv(event)
            self.trail = [uv[2]] if uv is not None else []
            h = self._region_at(uv)
            self.marked = [h] if h is not None else []
            return {'RUNNING_MODAL'}
        if t == 'LEFTMOUSE' and v == 'RELEASE' and self.dragging:
            self.dragging = False
            self._apply_marked()
            self.trail = []
            self._status(context)
            return {'RUNNING_MODAL'}
        if v != 'PRESS':
            return {'RUNNING_MODAL'}
        if t == 'Z' and (event.ctrl or event.oskey):
            if self.history:
                self.regions = self.history.pop()
                self._rebuild_display()
                self._status(context)
            return {'RUNNING_MODAL'}
        if t in {'X', 'DEL', 'BACK_SPACE'} and not event.ctrl and not event.oskey:
            if self.hover is not None:                       # Nakan: X / Delete = delete the region under the mouse
                self.alt_mode = True
                self.marked = [self.hover]
                self._apply_marked()
                self._status(context)
            return {'RUNNING_MODAL'}
        if t in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
            n = self._commit(context)
            self._end(context)
            self.report({'INFO'}, "Shape Builder: %d shape(s)" % n)
            return {'FINISHED'}
        if t in {'ESC', 'RIGHTMOUSE'}:
            self._end(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _apply_marked(self):
        idx = [i for i in self.marked if i is not None]
        self.marked = []
        if not idx:
            return
        self.history.append([list(R) for R in self.regions])
        keep = [R for i, R in enumerate(self.regions) if i not in idx]
        if self.alt_mode:
            self.regions = keep                              # Alt: those regions are gone
        else:
            self.regions = keep + merge([self.regions[i] for i in idx])
        self.hover = None
        self._rebuild_display()

    def _commit(self, context):
        coll = self.objs[0].users_collection[0] if self.objs[0].users_collection else context.scene.collection
        base = self.objs[0].name.split(".")[0]
        while base.endswith(" Shape"):
            base = base[:-6]
        tol = self.size * 2e-4
        new = []
        for k, R in enumerate(self.regions):
            ob = None
            try:
                ob = write_exact(context, R, self.frame, "%s Shape" % base, coll, self.src, 3.0 / SCALE + 1e-9)
            except Exception:      # noqa: BLE001
                import traceback
                traceback.print_exc()
            if ob is None:
                ob = write_piece(context, R, self.frame, "%s Shape" % base, coll, tol)
            new.append(ob)
        for ob in self.objs:
            SL.safe_remove(ob)
        for o in context.selected_objects:
            o.select_set(False)
        for ob in new:
            ob.select_set(True)
        if new:
            context.view_layer.objects.active = new[0]
        return len(new)


_keymaps = []


def register():
    bpy.utils.register_class(NKDRAW_OT_shape_builder)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Object Mode", space_type='EMPTY')
        _keymaps.append((km, km.keymap_items.new(NKDRAW_OT_shape_builder.bl_idname, 'M', 'PRESS',
                                                 ctrl=True, shift=True)))


def unregister():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    bpy.utils.unregister_class(NKDRAW_OT_shape_builder)
