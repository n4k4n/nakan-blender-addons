"""Rhino-style object snaps: End, Vertex, Mid, Cen, Int, Perp, Near, Grid (+ Project).

The scene geometry is gathered once per command (numpy arrays, world space) and projected
to the screen only when the view changes, so hovering stays fast on real models.
"""

import math

import numpy as np
from mathutils import Vector
from mathutils.geometry import intersect_line_line

from . import geom

LABELS = {"END": "End", "VERT": "Vertex", "MID": "Mid", "CEN": "Cen", "INT": "Int",
          "PERP": "Perp", "NEAR": "Near", "GRID": "Grid", "TAN": "Tan", "TRACK": "Track",
          "AXIS": "Mirror axis"}
# pixel handicap among point snaps: a vertex a few pixels away beats a perpendicular foot
BIAS = {"END": 0.0, "VERT": 0.0, "INT": 0.5, "MID": 1.0, "CEN": 1.0, "PERP": 4.0, "NEAR": 0.0, "GRID": 0.0}
TIER = {"END": 0, "VERT": 0, "MID": 0, "CEN": 0, "INT": 0, "PERP": 0, "NEAR": 1, "GRID": 2}
FLAG = {"END": "snap_end", "VERT": "snap_vert", "MID": "snap_mid", "CEN": "snap_cen", "INT": "snap_int",
        "PERP": "snap_perp", "NEAR": "snap_near", "GRID": "snap_grid"}
KIND_ID = {"END": 0, "VERT": 1, "MID": 2, "CEN": 3}
ID_KIND = {v: k for k, v in KIND_ID.items()}


class Snap:
    __slots__ = ("kind", "point", "screen", "owner")

    def __init__(self, kind, point, screen, owner=None):
        self.kind, self.point, self.screen, self.owner = kind, Vector(point), Vector(screen), owner

    @property
    def label(self):
        if self.kind in ("TRACK", "AXIS") and isinstance(self.owner, str):
            return self.owner
        return LABELS.get(self.kind, self.kind)


def _world(mw, co):
    m = np.array(mw, dtype=np.float64)
    return co @ m[:3, :3].T + m[:3, 3]


def curve_geometry(ob_matrix, splines_pts, cyclic_flags, samples=16):
    """splines_pts: list of lists of (co, hl, hr) world Vectors (bezier) -> points, kinds, segments.
    Also used for the curve being drawn."""
    pts, kinds, segs = [], [], []
    for pts_s, cyc in zip(splines_pts, cyclic_flags):
        n = len(pts_s)
        if n == 0:
            continue
        for co, _hl, _hr in pts_s:
            pts.append(tuple(co))
            kinds.append(KIND_ID["END"])
        nseg = n if cyc else n - 1
        all_samples = []
        for i in range(nseg):
            a = pts_s[i]
            b = pts_s[(i + 1) % n]
            sm = geom.bez_samples(a[0], a[2], b[1], b[0], samples)
            # arc-length midpoint of this segment
            L = [0.0]
            for p, q in zip(sm, sm[1:]):
                L.append(L[-1] + (q - p).length)
            half = L[-1] / 2
            for k in range(1, len(L)):
                if L[k] >= half:
                    f = (half - L[k - 1]) / max(L[k] - L[k - 1], 1e-12)
                    pts.append(tuple(sm[k - 1].lerp(sm[k], f)))
                    kinds.append(KIND_ID["MID"])
                    break
            for p, q in zip(sm, sm[1:]):
                segs.append((tuple(p), tuple(q)))
            all_samples += sm
        if cyc and all_samples:
            c = sum(all_samples, Vector()) / len(all_samples)
            pts.append(tuple(c))
            kinds.append(KIND_ID["CEN"])
    return pts, kinds, segs


class SnapCache:
    last_edge = None

    def __init__(self):
        self.P = np.zeros((0, 3))          # snap points
        self.K = np.zeros((0,), dtype=np.int8)
        self.SA = np.zeros((0, 3))         # segment starts
        self.SB = np.zeros((0, 3))
        self.SN = np.zeros((0, 2), dtype=np.int64)   # node ids of the segment ends (int skip)
        self._vkey = None
        self._p2 = self._a2 = self._b2 = None
        self.dyn = None                    # geometry of the curve being drawn (changes)
        self.max_verts = 250000

    # ------------------------------------------------------------- gathering
    def gather(self, context, exclude=None):
        import bpy
        dg = context.evaluated_depsgraph_get()
        P, K, SA, SB, SN = [], [], [], [], []
        node_base = 0
        for ob in context.visible_objects:
            if (exclude is not None and ob in exclude) or ob.type not in {'MESH', 'CURVE'}:
                continue
            if ob.type == 'CURVE':
                spl, cyc = [], []
                mw = ob.matrix_world
                for s in ob.data.splines:
                    if s.type == 'BEZIER':
                        spl.append([(mw @ p.co, mw @ p.handle_left, mw @ p.handle_right) for p in s.bezier_points])
                    else:
                        spl.append([(mw @ p.co.xyz,) * 3 for p in s.points])
                    cyc.append(s.use_cyclic_u)
                pts, kinds, segs = curve_geometry(mw, spl, cyc)
                if pts:
                    P.append(np.array(pts))
                    K.append(np.array(kinds, dtype=np.int8))
                if segs:
                    a = np.array([s[0] for s in segs])
                    b = np.array([s[1] for s in segs])
                    ids = node_base + np.arange(len(segs))
                    SA.append(a)
                    SB.append(b)
                    SN.append(np.stack([ids, ids + 1], axis=1))   # consecutive samples share a node
                    node_base += len(segs) + 2
                continue
            ev = ob.evaluated_get(dg)
            try:
                me = ev.to_mesh()
            except RuntimeError:
                continue
            try:
                nv = len(me.vertices)
                if nv == 0 or nv > self.max_verts:
                    continue
                co = np.empty(nv * 3)
                me.vertices.foreach_get("co", co)
                co = _world(ob.matrix_world, co.reshape(-1, 3))
                P.append(co)
                K.append(np.full(nv, KIND_ID["VERT"], dtype=np.int8))
                ne = len(me.edges)
                if ne:
                    ev_ = np.empty(ne * 2, dtype=np.int64)
                    me.edges.foreach_get("vertices", ev_)
                    ev_ = ev_.reshape(-1, 2)
                    a, b = co[ev_[:, 0]], co[ev_[:, 1]]
                    SA.append(a)
                    SB.append(b)
                    SN.append(ev_ + node_base)
                    P.append((a + b) * 0.5)
                    K.append(np.full(ne, KIND_ID["MID"], dtype=np.int8))
                npoly = len(me.polygons)
                if npoly:
                    c = np.empty(npoly * 3)
                    me.polygons.foreach_get("center", c)
                    P.append(_world(ob.matrix_world, c.reshape(-1, 3)))
                    K.append(np.full(npoly, KIND_ID["CEN"], dtype=np.int8))
                node_base += nv + 1
            finally:
                ev.to_mesh_clear()
        self.P = np.concatenate(P) if P else np.zeros((0, 3))
        self.K = np.concatenate(K) if K else np.zeros((0,), dtype=np.int8)
        self.SA = np.concatenate(SA) if SA else np.zeros((0, 3))
        self.SB = np.concatenate(SB) if SB else np.zeros((0, 3))
        self.SN = np.concatenate(SN) if SN else np.zeros((0, 2), dtype=np.int64)
        self._vkey = None

    def set_dynamic(self, pts, kinds, segs):
        """Geometry of the curve being edited (its own ends, mids, segments)."""
        if pts:
            self.dyn = (np.array(pts, dtype=np.float64), np.array(kinds, dtype=np.int8),
                        np.array([s[0] for s in segs]) if segs else np.zeros((0, 3)),
                        np.array([s[1] for s in segs]) if segs else np.zeros((0, 3)))
        else:
            self.dyn = None

    # ------------------------------------------------------------- projection
    @staticmethod
    def _proj(view, X):
        if len(X) == 0:
            return np.zeros((0, 2)), np.zeros((0,), dtype=bool)
        pm = np.array(view.pm, dtype=np.float64)
        H = X @ pm[:3, :3].T + pm[:3, 3]
        w = X @ pm[3, :3] + pm[3, 3]
        ok = w > 1e-9
        ws = np.where(ok, w, 1.0)
        sx = (H[:, 0] / ws * 0.5 + 0.5) * view.w
        sy = (H[:, 1] / ws * 0.5 + 0.5) * view.h
        return np.stack([sx, sy], axis=1), ok

    def _ensure(self, view):
        k = view.key()
        if k != self._vkey:
            self._p2 = self._proj(view, self.P)
            self._a2 = self._proj(view, self.SA)
            self._b2 = self._proj(view, self.SB)
            self._vkey = k

    # ------------------------------------------------------------- query
    def find(self, view, mx, my, st, last=None, plane=None, radius=None, visible=None):
        """Best snap near the mouse, or None. st: settings with snap_* flags, snap_radius,
        grid_step, snap_project."""
        r = float(radius if radius is not None else st.snap_radius)
        m = np.array([mx, my], dtype=np.float64)
        self._ensure(view)
        cands = []

        def enabled(kind):
            return getattr(st, FLAG[kind], False)

        # ---- point snaps (static + dynamic)
        sources = [(self.P, self.K, self._p2)]
        if self.dyn is not None:
            dp, dk, _da, _db = self.dyn
            sources.append((dp, dk, self._proj(view, dp)))
        for P, K, (p2, ok) in sources:
            if len(P) == 0:
                continue
            d = np.hypot(p2[:, 0] - mx, p2[:, 1] - my)
            d = np.where(ok, d, 1e18)
            for kid, kind in ID_KIND.items():
                if not enabled(kind):
                    continue
                dk_ = np.where(K == kid, d, 1e18)
                i = int(np.argmin(dk_))
                if dk_[i] <= r:
                    cands.append((TIER[kind], float(dk_[i]), Snap(kind, P[i], p2[i])))

        # ---- segment snaps
        seg_sets = [(self.SA, self.SB, self._a2, self._b2, self.SN)]
        if self.dyn is not None and len(self.dyn[2]):
            da, db = self.dyn[2], self.dyn[3]
            nn = len(da)
            ids = 10 ** 9 + np.arange(nn)
            seg_sets.append((da, db, self._proj(view, da), self._proj(view, db), np.stack([ids, ids + 1], axis=1)))
        near_segs = []
        for SA, SB, (a2, oka), (b2, okb), SN in seg_sets:
            if len(SA) == 0:
                continue
            ab = b2 - a2
            ll = np.maximum((ab * ab).sum(1), 1e-12)
            t = np.clip(((m - a2) * ab).sum(1) / ll, 0.0, 1.0)
            q = a2 + ab * t[:, None]
            d = np.hypot(q[:, 0] - mx, q[:, 1] - my)
            d = np.where(oka & okb, d, 1e18)
            idx = np.nonzero(d <= r * 1.5)[0]
            if len(idx) > 64:
                idx = idx[np.argsort(d[idx])[:64]]
            for i in idx:
                near_segs.append((float(d[i]), Vector(SA[i]), Vector(SB[i]), tuple(SN[i])))
        near_segs.sort(key=lambda s: s[0])
        self.last_edge = (near_segs[0][1], near_segs[0][2]) if near_segs and near_segs[0][0] <= r else None
        ro, rd = view.ray(mx, my)
        mvec = Vector((mx, my))

        if enabled("NEAR"):
            for dist, a, b, _n in near_segs[:8]:
                if dist > r:
                    continue
                res = intersect_line_line(ro, ro + rd, a, b)
                if res is None:
                    continue
                pb = res[1]
                ab3 = b - a
                tt = max(0.0, min(1.0, (pb - a).dot(ab3) / max(ab3.length_squared, 1e-18)))
                p = a + ab3 * tt
                s = view.project(p)
                if s is not None:
                    cands.append((TIER["NEAR"], (s - mvec).length, Snap("NEAR", p, s, (a, b))))
                break

        if enabled("PERP") and last is not None:
            L = Vector(last)
            for dist, a, b, _n in near_segs[:16]:
                ab3 = b - a
                l2 = ab3.length_squared
                if l2 < 1e-18:
                    continue
                tt = (L - a).dot(ab3) / l2
                if not (-1e-6 <= tt <= 1 + 1e-6):
                    continue
                p = a + ab3 * tt
                if (p - L).length < 1e-9:
                    continue
                s = view.project(p)
                if s is not None and (s - mvec).length <= r:
                    cands.append((TIER["PERP"], (s - mvec).length, Snap("PERP", p, s)))

        if enabled("INT"):
            segs = near_segs[:24]
            for i in range(len(segs)):
                for j in range(i + 1, len(segs)):
                    _d1, a1, b1, n1 = segs[i]
                    _d2, a2_, b2_, n2 = segs[j]
                    if set(n1) & set(n2):
                        continue
                    res = intersect_line_line(a1, b1, a2_, b2_)
                    if res is None:
                        continue
                    p1, p2 = res
                    d1 = b1 - a1
                    d2 = b2_ - a2_
                    t1 = (p1 - a1).dot(d1) / max(d1.length_squared, 1e-18)
                    t2 = (p2 - a2_).dot(d2) / max(d2.length_squared, 1e-18)
                    if not (-1e-6 <= t1 <= 1 + 1e-6 and -1e-6 <= t2 <= 1 + 1e-6):
                        continue
                    tol = view.pixel_size(p1) * 2.0
                    if (p1 - p2).length > tol:
                        continue
                    p = (p1 + p2) * 0.5
                    s = view.project(p)
                    if s is not None and (s - mvec).length <= r:
                        cands.append((TIER["INT"], (s - mvec).length, Snap("INT", p, s)))

        best = None
        if cands:
            cands.sort(key=lambda c: (c[0], c[1] + BIAS.get(c[2].kind, 0.0)))
            for c in cands[:12]:
                # skip points hidden behind other objects (the back of a box seen from the front)
                if visible is None or visible(c[2].point):
                    best = c[2]
                    break

        if best is None and enabled("GRID") and plane is not None:
            hp = plane.hit(ro, rd)
            u, v, _w = plane.to_local(hp)
            g = max(float(st.grid_step), 1e-9)
            p = plane.from_local(round(u / g) * g, round(v / g) * g)
            s = view.project(p)
            if s is not None:
                best = Snap("GRID", p, s)

        if best is not None and getattr(st, "snap_project", False) and plane is not None and best.kind != "GRID":
            p = plane.project(best.point)
            s = view.project(p) or best.screen
            best = Snap(best.kind, p, s)
        return best
