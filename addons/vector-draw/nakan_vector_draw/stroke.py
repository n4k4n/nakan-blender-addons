"""Illustrator-style stroke outline: the band around a curve as a clean 2D region.

The stroke is the union of simple, positively oriented pieces (one quad per segment plus a join
wedge on the outer side of each corner: miter / round / bevel), merged with Clipper2. Overlaps
on the inner side of tight corners and where the path crosses itself simply disappear in the
union, like "Outline Stroke" in Illustrator.

Align inside / outside on closed paths works like Illustrator too: a centred stroke twice as
wide, intersected with (inside) or subtracted from (outside) the filled path.
"""

import math

try:
    import numpy as np
    import pyclipr
    AVAILABLE = True
except Exception:          # noqa: BLE001  (wheel missing on this platform: the old offset is used)
    AVAILABLE = False

SCALE = 1_000_000          # Clipper works in integers: 1e-6 units of precision


def _area(poly):
    a = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return 0.5 * a


def _ccw(poly):
    return poly if _area(poly) >= 0 else poly[::-1]


def _pieces(q, closed, a, b, join, limit, arc_step, corners=None):
    """Positively oriented polygons whose union is the stroke. a = width on the left, b = on the right."""
    n = len(q)
    nseg = n if closed else n - 1
    segn, segd = [], []
    for i in range(nseg):
        p0, p1 = q[i], q[(i + 1) % n]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        L = math.hypot(dx, dy)
        if L < 1e-12:
            segn.append(None)
            segd.append(None)
            continue
        segn.append((-dy / L, dx / L))
        segd.append((dx / L, dy / L))
    out = []
    for i in range(nseg):
        nl = segn[i]
        if nl is None:
            continue
        p0, p1 = q[i], q[(i + 1) % n]
        out.append(_ccw([(p0[0] + nl[0] * a, p0[1] + nl[1] * a), (p1[0] + nl[0] * a, p1[1] + nl[1] * a),
                         (p1[0] - nl[0] * b, p1[1] - nl[1] * b), (p0[0] - nl[0] * b, p0[1] - nl[1] * b)]))
    # joins
    verts = range(n) if closed else range(1, n - 1)
    for i in verts:
        i1 = (i - 1) % nseg
        i2 = i % nseg
        if segn[i1] is None or segn[i2] is None:
            continue
        d1, d2 = segd[i1], segd[i2]
        turn = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(turn) < 1e-12 and d1[0] * d2[0] + d1[1] * d2[1] > 0:
            continue                                     # straight on
        # the outer side of a left turn is the right side
        if turn > 0:
            d, n1, n2 = b, (-segn[i1][0], -segn[i1][1]), (-segn[i2][0], -segn[i2][1])
        else:
            d, n1, n2 = a, segn[i1], segn[i2]
        if d <= 1e-12:
            continue
        v = q[i]
        jn, lim = (join, limit) if (corners is None or i in corners) else ('SMOOTH', 4.0)   # smooth point: plain offset
        A = (v[0] + n1[0] * d, v[1] + n1[1] * d)
        B = (v[0] + n2[0] * d, v[1] + n2[1] * d)
        mx, my = n1[0] + n2[0], n1[1] + n2[1]
        ml = math.hypot(mx, my)
        k_m = 1e9 if ml < 1e-12 else 1.0 / max((mx / ml) * n2[0] + (my / ml) * n2[1], 1e-12)
        if jn == 'SMOOTH':
            jn = 'MITER' if k_m <= lim else 'ROUND'
        if jn == 'MITER' and k_m <= lim:
            M = (v[0] + mx / ml * d * k_m, v[1] + my / ml * d * k_m)
            out.append(_ccw([v, A, M, B]))
        elif jn == 'ROUND':
            a1 = math.atan2(n1[1], n1[0])
            a2 = math.atan2(n2[1], n2[0])
            da = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi
            steps = max(1, int(math.ceil(abs(da) / arc_step)))
            arc = [(v[0] + d * math.cos(a1 + da * k / steps), v[1] + d * math.sin(a1 + da * k / steps))
                   for k in range(steps + 1)]
            out.append(_ccw([v] + arc))
        else:                                            # bevel (or miter beyond the limit)
            out.append(_ccw([v, A, B]))
    return out


def _run(op, subject, clip=None):
    c = pyclipr.Clipper()
    c.scaleFactor = SCALE
    subj = [np.asarray(p, dtype=float) for p in subject if len(p) >= 3]
    if not subj:
        return []
    c.addPaths(subj, pyclipr.Subject)
    if clip:
        cl = [np.asarray(p, dtype=float) for p in clip if len(p) >= 3]
        if cl:
            c.addPaths(cl, pyclipr.Clip)
    r = c.execute(op, pyclipr.FillRule.NonZero)
    return [[(float(x), float(y)) for x, y in np.asarray(p)] for p in r]


EPS = 2e-5                 # closing distance: welds hairline cracks between pieces


def _offset(paths, delta):
    o = pyclipr.ClipperOffset()
    o.scaleFactor = SCALE
    o.miterLimit = 10.0
    pp = [np.asarray(p, dtype=float) for p in paths if len(p) >= 3]
    if not pp:
        return []
    o.addPaths(pp, pyclipr.JoinType.Miter, pyclipr.EndType.Polygon)
    r = o.execute(delta)
    return [[(float(x), float(y)) for x, y in np.asarray(p)] for p in r]


def _union_closed(pieces):
    """Union of the pieces with the hairline cracks closed (grow a hair, then shrink back)."""
    u = _run(pyclipr.Union, pieces)
    return _offset(_offset(u, EPS), -EPS)


def _clean(loop, tol=2e-5):
    """Drop points closer than tol to the previous one and points on a straight line."""
    out = []
    for p in loop:
        if not out or math.dist(p, out[-1]) > tol:
            out.append(p)
    while len(out) > 2 and math.dist(out[0], out[-1]) <= tol:
        out.pop()
    # one greedy pass: drop a point only if it lies (within a hair) on the line from the last kept
    # point to the next one -> removes the collinear leftovers of the union, never erodes curves
    if len(out) > 3:
        keep = [out[0]]
        m = len(out)
        for i in range(1, m):
            a, b, c = keep[-1], out[i], out[(i + 1) % m]
            ac = math.dist(a, c)
            cr = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            fwd = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) > 0
            if ac > 1e-12 and abs(cr) / ac <= 2.5 * EPS and fwd:
                continue
            keep.append(b)
        if len(keep) > 3:                                   # the first point too, now that the loop is known
            a, b, c = keep[-1], keep[0], keep[1]
            ac = math.dist(a, c)
            cr = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            if ac > 1e-12 and abs(cr) / ac <= 2.5 * EPS and \
                    (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]) > 0:
                keep = keep[1:]
        out = keep if len(keep) >= 3 else out
    return out


def stroke_region(rings, w, side, join='MITER', limit=4.0, arc_step=math.radians(6)):
    """rings: [(2D points, closed)] on the curve plane. Returns [(outer, [holes])], each a 2D loop."""
    if not AVAILABLE or w <= 0:
        return None
    regions = []
    for ring in rings:
        q, closed = ring[0], ring[1]
        corners = ring[2] if len(ring) > 2 else None
        if len(q) < 2:
            continue
        if closed and side in ('INSIDE', 'OUTSIDE'):
            band = _union_closed(_pieces(q, True, w, w, join, limit, arc_step, corners))
            fill = [list(q)]
            if side == 'INSIDE':
                regions += _run(pyclipr.Intersection, band, fill)
            else:
                regions += _run(pyclipr.Difference, band, fill)
        else:
            if side == 'CENTER':
                a = b = w / 2
            elif side == 'INSIDE':
                a, b = w, 0.0
            else:
                a, b = 0.0, w
            regions += _union_closed(_pieces(q, closed, a, b, join, limit, arc_step, corners))
    if not regions:
        return []
    # union of every spline's band, as a tree: outers with their holes
    c = pyclipr.Clipper()
    c.scaleFactor = SCALE
    c.addPaths([np.asarray(p, dtype=float) for p in regions if len(p) >= 3], pyclipr.Subject)
    tree = c.executeTree(pyclipr.Union, pyclipr.FillRule.NonZero)
    out = []

    def walk(node):
        for ch in node.children:
            if ch.isHole:
                continue
            outer = _clean([(float(x), float(y)) for x, y in np.asarray(ch.polygon)])
            holes = []
            for h in ch.children:
                hl = _clean([(float(x), float(y)) for x, y in np.asarray(h.polygon)])
                if len(hl) >= 3:
                    holes.append(hl)
                walk(h)                                   # islands inside the holes
            if len(outer) >= 3:
                out.append((outer, holes))
    walk(tree)
    return out
