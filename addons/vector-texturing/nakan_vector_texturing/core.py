# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-numpy rasterizer for Nakan Vector Texturing.

Idea: instead of mapping vector shapes into UV space (which breaks on seams),
we bake a "position map" once: for every texel, the 3D world position and
normal of the surface point it represents. Then each texel just asks
"is my 3D point inside this shape?". Seams are handled for free and the
resolution always follows the UV layout.
"""

import numpy as np

# --------------------------------------------------------------------------
# Position map
# --------------------------------------------------------------------------


class PositionMap:
    """Texel -> world position / normal lookup for one mesh + UV + resolution."""

    def __init__(self, width, height, idx, pos, nrm, texel_size, shared=False):
        self.width = width
        self.height = height
        self.idx = idx            # (N,) flat pixel indices (row-major, bottom row first)
        self.pos = pos            # (N,3) float32 world positions
        self.nrm = nrm            # (N,3) float32 world normals (unit)
        self.texel_size = texel_size  # approx. world size of one texel
        self.shared = shared  # several surface samples share some texels
        # texel of each sample, as an index into the unique texels (the first n_primary samples)
        self.n_primary = len(idx)
        self.tex_id = None  # identity when no texel is shared
        self._build_grid()

    def _build_grid(self, max_dim=96):
        P = self.pos
        if not len(P):
            self.glo = np.zeros(3); self.gcs = 1.0; self.gdim = np.ones(3, np.int64)
            self.gorder = np.zeros(0, np.int64); self.gstart = np.zeros(2, np.int64)
            return
        lo = P.min(0).astype(np.float64)
        hi = P.max(0).astype(np.float64)
        ext = np.maximum(hi - lo, 1e-9)
        cs = max(float(ext.max()) / max_dim, self.texel_size * 4)
        dim = np.maximum(np.ceil(ext / cs).astype(np.int64), 1)
        key = np.zeros(len(P), np.int32)
        mul = 1
        for a in range(3):
            c = ((P[:, a] - np.float32(lo[a])) / np.float32(cs)).astype(np.int32)
            np.clip(c, 0, dim[a] - 1, out=c)
            key += c * np.int32(mul)
            mul *= int(dim[a])
            del c
        order = np.argsort(key, kind="stable").astype(np.int32)
        ks = key[order]
        del key
        self.glo, self.gcs, self.gdim = lo, cs, dim
        self.gorder = order
        self.gstart = np.searchsorted(ks, np.arange(int(dim.prod()) + 1))
        del ks

    def query_box(self, lo, hi):
        """Indices (into pos/nrm) of texels inside the world-space box."""
        if not len(self.gorder):
            return np.zeros(0, np.int64)
        lo = np.asarray(lo, np.float64)
        hi = np.asarray(hi, np.float64)
        i0 = np.clip(np.floor((lo - self.glo) / self.gcs).astype(np.int64), 0, self.gdim - 1)
        i1 = np.clip(np.floor((hi - self.glo) / self.gcs).astype(np.int64), 0, self.gdim - 1)
        if np.any(hi < self.glo) or np.any(lo > self.glo + self.gdim * self.gcs):
            return np.zeros(0, np.int64)
        d = self.gdim
        xs = np.arange(i0[0], i1[0] + 1)
        ys = np.arange(i0[1], i1[1] + 1)
        zs = np.arange(i0[2], i1[2] + 1)
        cells = (xs[:, None, None] + ys[None, :, None] * d[0] + zs[None, None, :] * d[0] * d[1]).ravel()
        st = self.gstart[cells]
        en = self.gstart[cells + 1]
        lens = en - st
        tot = int(lens.sum())
        if tot == 0:
            return np.zeros(0, np.int64)
        offs = np.repeat(st - np.concatenate([[0], np.cumsum(lens)[:-1]]), lens)
        ids = self.gorder[np.arange(tot) + offs]
        p = self.pos[ids]
        m = np.all((p >= lo.astype(np.float32)) & (p <= hi.astype(np.float32)), axis=1)
        return ids[m]


def _next_pow2(x):
    return 1 << int(np.ceil(np.log2(max(int(x), 1))))


def build_position_map(uv, pos, nrm, width, height, margin=4, max_batch=1_000_000):
    """Rasterize triangles in UV space.

    uv:  (T,3,2) UV coordinates per triangle corner
    pos: (T,3,3) world positions per corner
    nrm: (T,3,3) world normals per corner
    """
    W, H = int(width), int(height)
    uv = np.asarray(uv, dtype=np.float64)
    pos = np.asarray(pos, dtype=np.float32)
    nrm = np.asarray(nrm, dtype=np.float32)

    # pixel-center coordinates: pixel (x, y) center sits at ((x+.5)/W, (y+.5)/H)
    px = uv[..., 0] * W - 0.5
    py = uv[..., 1] * H - 0.5

    x0 = np.clip(np.ceil(px.min(1)), 0, W - 1).astype(np.int64)
    x1 = np.clip(np.floor(px.max(1)), 0, W - 1).astype(np.int64)
    y0 = np.clip(np.ceil(py.min(1)), 0, H - 1).astype(np.int64)
    y1 = np.clip(np.floor(py.max(1)), 0, H - 1).astype(np.int64)
    sx = x1 - x0 + 1
    sy = y1 - y0 + 1

    ax, ay = px[:, 0], py[:, 0]
    bx, by = px[:, 1], py[:, 1]
    cx, cy = px[:, 2], py[:, 2]
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

    ok = (sx > 0) & (sy > 0) & (np.abs(area) > 1e-12)
    # also reject triangles fully outside the image
    ok &= (px.max(1) >= -0.5) & (px.min(1) <= W - 0.5) & (py.max(1) >= -0.5) & (py.min(1) <= H - 0.5)

    # texel size estimate from world surface area / covered texels
    p = pos.astype(np.float64)
    tri_area = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    uv_area = 0.5 * np.abs(area) / float(W * H) * (W * H)  # in pixel^2
    with np.errstate(divide="ignore", invalid="ignore"):
        good = (uv_area > 1e-9) & (tri_area > 0)
    if good.any():
        texel_size = float(np.sqrt(tri_area[good].sum() / uv_area[good].sum()))
    else:
        texel_size = 1e-3

    thr = 3 * texel_size  # samples farther than this are a different surface

    pos_img = np.zeros((H * W, 3), np.float32)
    nrm_img = np.zeros((H * W, 3), np.float32)
    filled = np.zeros(H * W, bool)
    # every surface sample is kept: several surfaces can share the same texels
    # (Solidify inner shells, mirrored or stacked UV islands)
    e_flat, e_pos, e_nrm = [], [], []

    # bucket triangles by power-of-two bbox size (kx, ky) and rasterize each bucket
    # fully vectorized: one (n_tris, kx*ky) grid per batch.
    okf = np.nonzero(ok)[0]
    kx_all = np.zeros(len(uv), np.int64)
    ky_all = np.zeros(len(uv), np.int64)
    if len(okf):
        kx_all[okf] = 1 << np.ceil(np.log2(np.maximum(sx[okf], 1))).astype(np.int64)
        ky_all[okf] = 1 << np.ceil(np.log2(np.maximum(sy[okf], 1))).astype(np.int64)
    pairs = np.unique(np.stack([kx_all[okf], ky_all[okf]], 1), axis=0) if len(okf) else []
    axf, ayf = ax.astype(np.float32), ay.astype(np.float32)
    bxf, byf = bx.astype(np.float32), by.astype(np.float32)
    cxf, cyf = cx.astype(np.float32), cy.astype(np.float32)
    inv_area = (1.0 / np.where(ok, area, 1.0)).astype(np.float32)
    for kx, ky in pairs:
        tris = okf[(kx_all[okf] == kx) & (ky_all[okf] == ky)]
        per = int(kx * ky)
        step = max(1, max_batch // per)
        g = np.arange(per)
        gx = (g % kx).astype(np.int64)
        gy = (g // kx).astype(np.int64)
        for s0 in range(0, len(tris), step):
            t = tris[s0:s0 + step]
            X = x0[t, None] + gx[None, :]
            Y = y0[t, None] + gy[None, :]
            inb = (X <= x1[t, None]) & (Y <= y1[t, None])
            Xf = X.astype(np.float32)
            Yf = Y.astype(np.float32)
            ia = inv_area[t, None]
            w0 = ((bxf[t, None] - Xf) * (cyf[t, None] - Yf) - (byf[t, None] - Yf) * (cxf[t, None] - Xf)) * ia
            w1 = ((cxf[t, None] - Xf) * (ayf[t, None] - Yf) - (cyf[t, None] - Yf) * (axf[t, None] - Xf)) * ia
            w2 = 1.0 - w0 - w1
            eps = -1e-5
            inside = inb & (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
            ti, gi = np.nonzero(inside)
            if not len(ti):
                continue
            tt = t[ti]
            b0 = w0[ti, gi][:, None]
            b1 = w1[ti, gi][:, None]
            b2 = w2[ti, gi][:, None]
            flat = Y[ti, gi] * W + X[ti, gi]
            bp = pos[tt, 0] * b0 + pos[tt, 1] * b1 + pos[tt, 2] * b2
            bn = nrm[tt, 0] * b0 + nrm[tt, 1] * b1 + nrm[tt, 2] * b2
            o = np.argsort(flat, kind="stable")
            flat, bp, bn = flat[o], bp[o], bn[o]
            first = np.ones(len(flat), bool)
            first[1:] = flat[1:] != flat[:-1]
            newm = first & ~filled[flat]
            nf = flat[newm]
            pos_img[nf] = bp[newm]
            nrm_img[nf] = bn[newm]
            filled[nf] = True
            # texels already taken: shared triangle edges (same spot, ignored) or a
            # different surface mapped to the same UVs -> keep it as an extra sample
            rest = ~newm
            if rest.any():
                rf = flat[rest]
                far = np.linalg.norm(bp[rest] - pos_img[rf], axis=1) > thr
                if far.any():
                    e_flat.append(rf[far].astype(np.int32))
                    e_pos.append(bp[rest][far])
                    e_nrm.append(bn[rest][far])


    ex_flat = np.zeros(0, np.int64)
    ex_pos = np.zeros((0, 3), np.float32)
    ex_nrm = np.zeros((0, 3), np.float32)
    if e_flat:
        ex_flat = np.concatenate(e_flat)
        ex_pos = np.concatenate(e_pos).astype(np.float32, copy=False)
        ex_nrm = np.concatenate(e_nrm).astype(np.float32, copy=False)
        e_flat.clear(); e_pos.clear(); e_nrm.clear()
        # collapse repeats of the same extra surface
        q = np.round(ex_pos / thr).astype(np.int64)
        o2 = np.lexsort((q[:, 2], q[:, 1], q[:, 0], ex_flat))
        ks = np.column_stack([ex_flat[o2], q[o2]])
        new = np.ones(len(ks), bool)
        new[1:] = np.any(ks[1:] != ks[:-1], axis=1)
        keep = np.sort(o2[new])
        del q, o2, ks, new
        ex_flat, ex_pos, ex_nrm = ex_flat[keep], ex_pos[keep], ex_nrm[keep]
        del keep
    covered = int(filled.sum())

    # gutter dilation so shapes bleed past UV island borders (avoids seams with
    # texture filtering): propagate the index of the nearest filled texel outward.
    if margin > 0 and covered:
        src = np.where(filled, np.arange(H * W, dtype=np.int64), -1).reshape(H, W)
        fil = filled.reshape(H, W).copy()
        for _ in range(int(margin)):
            new_f = fil.copy()
            grew = False
            for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                ys = slice(max(dy, 0), H + min(dy, 0))
                yd = slice(max(-dy, 0), H + min(-dy, 0))
                xs = slice(max(dx, 0), W + min(dx, 0))
                xd = slice(max(-dx, 0), W + min(-dx, 0))
                take = fil[ys, xs] & ~new_f[yd, xd]
                if take.any():
                    dst = src[yd, xd]
                    dst[take] = src[ys, xs][take]
                    nf = new_f[yd, xd]
                    nf |= take
                    grew = True
            fil = new_f
            if not grew:
                break
        srcf = src.reshape(-1)
        add = fil.reshape(-1) & ~filled
        ai = np.nonzero(add)[0]
        pos_img[ai] = pos_img[srcf[ai]]
        nrm_img[ai] = nrm_img[srcf[ai]]
        filled = fil.reshape(-1)
        del src, srcf, fil, add, ai

    idx = np.nonzero(filled)[0]
    P = pos_img[idx]
    Nn = nrm_img[idx]
    del pos_img, nrm_img, filled
    n_primary = len(idx)
    if len(ex_flat):
        idx = np.concatenate([idx, ex_flat])
        P = np.concatenate([P, ex_pos])
        Nn = np.concatenate([Nn, ex_nrm])
    ln = np.linalg.norm(Nn, axis=1, keepdims=True)
    ln[ln == 0] = 1
    Nn /= ln
    del ln
    # compact storage: int32 indices, half-precision normals (only used for facing tests)
    pm = PositionMap(W, H, idx.astype(np.int32), P.astype(np.float32), Nn.astype(np.float16),
                     texel_size, shared=bool(len(ex_flat)))
    if len(ex_flat):
        prim = idx[:n_primary]  # sorted, unique
        pm.n_primary = n_primary
        pm.tex_id = np.concatenate([np.arange(n_primary, dtype=np.int32),
                                    np.searchsorted(prim, ex_flat).astype(np.int32)])
    return pm


# --------------------------------------------------------------------------
# Shape geometry helpers
# --------------------------------------------------------------------------


class Frame:
    """Orthonormal projection frame in world units."""

    def __init__(self, origin, u, v, n):
        self.origin = np.asarray(origin, np.float64)
        self.u = _unit(u)
        self.v = _unit(v)
        self.n = _unit(n)

    def to2d(self, pts):
        d = np.asarray(pts, np.float64) - self.origin
        return np.stack([d @ self.u, d @ self.v], axis=-1)

    def depth(self, pts):
        return (np.asarray(pts, np.float64) - self.origin) @ self.n


def _unit(v):
    v = np.asarray(v, np.float64)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def fit_frame(points3d, outward_hint=None):
    """Best-fit plane (PCA) through 3D points; normal oriented by outward_hint."""
    P = np.asarray(points3d, np.float64)
    c = P.mean(0)
    if len(P) < 3:
        n = np.array([0, 0, 1.0])
    else:
        _, _, vt = np.linalg.svd(P - c, full_matrices=False)
        n = vt[2]
    if outward_hint is not None and np.dot(n, outward_hint) < 0:
        n = -n
    a = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    u = _unit(np.cross(a, n))
    v = np.cross(n, u)
    return Frame(c, u, v, n)


# --------------------------------------------------------------------------
# Coverage evaluation
# --------------------------------------------------------------------------


def _fill_mask(polys2d, cell, pad):
    """Even-odd scanline fill of closed 2D polylines into a supersampled grid."""
    allp = np.concatenate(polys2d, 0)
    lo = allp.min(0) - pad
    hi = allp.max(0) + pad
    size = hi - lo
    cell = max(cell, float(size.max()) / 4096.0, 1e-9)
    C = int(np.ceil(size[0] / cell)) + 1
    R = int(np.ceil(size[1] / cell)) + 1

    ex0, ey0, ex1, ey1 = [], [], [], []
    for p in polys2d:
        if len(p) < 3:
            continue
        q = np.roll(p, -1, axis=0)
        ex0.append(p[:, 0]); ey0.append(p[:, 1]); ex1.append(q[:, 0]); ey1.append(q[:, 1])
    if not ex0:
        return None
    ex0 = np.concatenate(ex0); ey0 = np.concatenate(ey0)
    ex1 = np.concatenate(ex1); ey1 = np.concatenate(ey1)
    keep = ey0 != ey1
    ex0, ey0, ex1, ey1 = ex0[keep], ey0[keep], ex1[keep], ey1[keep]

    counts = np.zeros((R, C + 1), np.int32)
    rows_y = lo[1] + (np.arange(R) + 0.5) * cell
    # process rows in chunks to bound memory
    E = len(ex0)
    chunk = max(1, 8_000_000 // max(E, 1))
    for r0 in range(0, R, chunk):
        ys = rows_y[r0:r0 + chunk, None]
        crosses = ((ey0 <= ys) & (ey1 > ys)) | ((ey1 <= ys) & (ey0 > ys))
        rr, ee = np.nonzero(crosses)
        if not len(rr):
            continue
        t = (ys[rr, 0] - ey0[ee]) / (ey1[ee] - ey0[ee])
        xc = ex0[ee] + t * (ex1[ee] - ex0[ee])
        col = np.clip(np.ceil((xc - lo[0]) / cell - 0.5), 0, C).astype(np.int64)
        np.add.at(counts, (rr + r0, col), 1)
    mask = (np.cumsum(counts, axis=1)[:, :C] % 2).astype(np.float32)
    return mask, lo, cell


def _sample_mask(mask, lo, cell, p2d):
    R, C = mask.shape
    fx = (p2d[:, 0] - lo[0]) / cell - 0.5
    fy = (p2d[:, 1] - lo[1]) / cell - 0.5
    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    tx = (fx - x0).astype(np.float32)
    ty = (fy - y0).astype(np.float32)

    def g(y, x):
        ok = (x >= 0) & (x < C) & (y >= 0) & (y < R)
        out = np.zeros(len(x), np.float32)
        out[ok] = mask[y[ok], x[ok]]
        return out

    a = g(y0, x0) * (1 - tx) + g(y0, x0 + 1) * tx
    b = g(y0 + 1, x0) * (1 - tx) + g(y0 + 1, x0 + 1) * tx
    return a * (1 - ty) + b * ty


# --------------------------------------------------------------------------
# Strokes
# --------------------------------------------------------------------------


class StrokePaths:
    """Flattened segments of several polylines with arc-length bookkeeping."""

    def __init__(self, polys, closed):
        A, B, S0, SP = [], [], [], []
        self.length, self.closed, self.P0, self.T0, self.Pn, self.Tn = [], [], [], [], [], []
        k = 0
        for L, c in zip(polys, closed):
            L = np.asarray(L, np.float64)
            if len(L) < 2:
                continue
            a = L[:-1]
            b = L[1:]
            if c:
                a = np.vstack([a, L[-1:]])
                b = np.vstack([b, L[:1]])
            sl = np.linalg.norm(b - a, axis=1)
            s0 = np.concatenate([[0.0], np.cumsum(sl)[:-1]])
            A.append(a); B.append(b); S0.append(s0); SP.append(np.full(len(a), k))
            self.length.append(s0[-1] + sl[-1])
            self.closed.append(bool(c))
            self.P0.append(a[0]); self.Pn.append(b[-1])
            self.T0.append((b[0] - a[0]) / max(sl[0], 1e-12))
            self.Tn.append((b[-1] - a[-1]) / max(sl[-1], 1e-12))
            k += 1
        self.n = k
        if k:
            self.A = np.concatenate(A); self.B = np.concatenate(B)
            self.S0 = np.concatenate(S0); self.SP = np.concatenate(SP)
            for name in ("length", "closed", "P0", "T0", "Pn", "Tn"):
                setattr(self, name, np.array(getattr(self, name)))

    def closest(self, pts, cand_fn, r, group=24):
        """For texels within r of any path: (idx, distance, arc param, path id).
        cand_fn(lo, hi) -> indices into pts inside that box."""
        if not self.n:
            e = np.zeros(0)
            return e.astype(np.int64), e, e, e.astype(np.int64)
        parts = []
        for g0 in range(0, len(self.A), group):
            a = self.A[g0:g0 + group]
            b = self.B[g0:g0 + group]
            lo = np.minimum(a.min(0), b.min(0)) - r
            hi = np.maximum(a.max(0), b.max(0)) + r
            cand = cand_fn(lo, hi)
            if not len(cand):
                continue
            p = pts[cand].astype(np.float64)[:, None, :]
            ab = (b - a)[None]
            ap = p - a[None]
            den = (ab * ab).sum(-1)
            den[den == 0] = 1e-20
            t = np.clip((ap * ab).sum(-1) / den, 0, 1)
            dd = ((ap - t[..., None] * ab) ** 2).sum(-1)
            j = dd.argmin(1)
            rows = np.arange(len(cand))
            d = np.sqrt(dd[rows, j])
            m = d < r
            if not m.any():
                continue
            seg = g0 + j[m]
            sl = np.sqrt(den[0, j[m]])
            sval = self.S0[seg] + t[rows[m], j[m]] * sl
            # exact path ends, so the cap logic can recognise them
            sval = np.where(t[rows[m], j[m]] >= 1.0, self.S0[seg] + sl, sval)
            parts.append((cand[m], d[m], sval, self.SP[seg]))
        if not parts:
            e = np.zeros(0)
            return e.astype(np.int64), e, e, e.astype(np.int64)
        idx = np.concatenate([q[0] for q in parts])
        d = np.concatenate([q[1] for q in parts])
        sv = np.concatenate([q[2] for q in parts])
        sp = np.concatenate([q[3] for q in parts])
        order = np.lexsort((d, idx))
        idx, d, sv, sp = idx[order], d[order], sv[order], sp[order]
        first = np.ones(len(idx), bool)
        first[1:] = idx[1:] != idx[:-1]
        return idx[first], d[first], sv[first], sp[first]


def _ease(t, e):
    t = np.clip(t, 0, 1)
    return t * (1 - e) + (t * t * (3 - 2 * t)) * e


def stroke_coverage(pts, d, s, sp, paths, st, half_w, aa):
    """Coverage of one stroke for texels with known closest-point data.
    Returns (coverage, arc fraction along the path)."""
    L = paths.length[sp]
    closed = paths.closed[sp]
    opn = ~closed
    s_ext = s.copy()
    d_lat = d.copy()
    if opn.any():
        # texels past the path ends: extend the arc parameter beyond 0 / L
        e0 = -((pts - paths.P0[sp]) * paths.T0[sp]).sum(1)
        en = ((pts - paths.Pn[sp]) * paths.Tn[sp]).sum(1)
        at0 = opn & (s <= 0) & (e0 > 0)
        atn = opn & (s >= L) & (en > 0)
        beyond = np.where(at0, e0, np.where(atn, en, 0.0))
        s_ext = s_ext - np.where(at0, e0, 0) + np.where(atn, en, 0)
        d_lat = np.sqrt(np.maximum(d * d - beyond * beyond, 0))

    # signed distance (along the path) outside the drawn intervals; < 0 = inside
    along = np.full(len(s), -1e30)
    along = np.where(opn, np.maximum(-s_ext, s_ext - L), along)

    # Trim Path (After Effects style): only [start, end] of each path is drawn,
    # slid by the offset (360° = one lap) and wrapping around the path end.
    frac = np.clip(s_ext / np.maximum(L, 1e-12), 0, 1)
    tfrac = frac
    if st.get("trim"):
        t0 = float(st.get("trim_start", 0.0))
        t1 = float(st.get("trim_end", 1.0))
        lo, hi = min(t0, t1), max(t0, t1)
        span = hi - lo
        if span <= 1e-6:
            return np.zeros(len(s), np.float32), frac
        if span < 1.0 - 1e-6:
            shift = lo + float(st.get("trim_offset", 0.0)) / (2 * np.pi)
            if st.get("trim_mode") == "INDIVIDUAL" and paths.n > 1:
                # all paths as one long path, in their order
                C = np.concatenate([[0.0], np.cumsum(paths.length)[:-1]])
                period = np.full(len(s), float(paths.length.sum()))
                base = C[sp]
                seamless = np.zeros(len(s), bool)
            else:
                period = np.maximum(L, 1e-12)
                base = np.zeros(len(s))
                seamless = closed  # a closed path has no ends: the visible part wraps freely
            a = np.mod(shift, 1.0) * period
            ln = span * period
            # visible runs in this path's own arc length (the run, and its wrapped part)
            best = np.full(len(s), 1e30)
            for g0 in (a, a - period):
                x0 = np.clip(g0 - base, 0, L)
                x1 = np.clip(g0 + ln - base, 0, L)
                valid = x1 > x0 + 1e-12
                dist = np.maximum(x0 - s_ext, s_ext - x1)
                best = np.where(valid, np.minimum(best, dist), best)
            u = np.mod(base + s_ext - a, period)
            circ = np.where(u <= ln, -np.minimum(u, ln - u), np.minimum(u - ln, period - u))
            trim_d = np.where(seamless, circ, best)
            # open paths: the trim runs replace the path ends (a cut end gets the line cap)
            along = np.where(opn, trim_d, np.maximum(along, trim_d))
            # taper follows the visible part, so a tapered line draws on like a brush stroke
            inside = u <= ln
            tfrac = np.where(inside, np.clip(u / np.maximum(ln, 1e-12), 0, 1),
                             np.where(u - ln < period - u, 1.0, 0.0))

    if st.get("dashes"):
        dash = max(float(st.get("dash", 0)), 0.0)
        gap = max(float(st.get("gap", 0)), 0.0)
        period_d = dash + gap
        if gap > 0:  # dash 0 + round caps = dots
            ud = np.mod(s_ext - float(st.get("dash_offset", 0)), period_d)
            ins = ud <= dash
            ad = np.where(ins, -np.minimum(ud, dash - ud), np.minimum(ud - dash, period_d - ud))
            along = np.maximum(along, ad)

    # width, with taper
    f = np.ones(len(s))
    if st.get("taper"):
        ls = float(st.get("taper_start_length", 0))
        le = float(st.get("taper_end_length", 0))
        if ls > 0:
            k = tfrac < ls
            sw = float(st.get("taper_start_width", 0))
            f[k] = np.minimum(f[k], sw + (1 - sw) * _ease(tfrac[k] / ls, st.get("taper_start_ease", 0)))
        if le > 0:
            k = tfrac > 1 - le
            ew = float(st.get("taper_end_width", 0))
            f[k] = np.minimum(f[k], ew + (1 - ew) * _ease((1 - tfrac[k]) / le, st.get("taper_end_ease", 0)))
    hw = half_w * f

    cap = st.get("cap", "BUTT")
    if cap == "ROUND":
        dist = np.sqrt(d_lat * d_lat + np.maximum(along, 0) ** 2)
        cov = np.clip((hw - dist) / aa + 0.5, 0, 1)
    else:
        if cap == "SQUARE":
            along = along - hw
        cov = np.clip((hw - d_lat) / aa + 0.5, 0, 1) * np.clip(-along / aa + 0.5, 0, 1)
    return cov.astype(np.float32), frac


def _gradient_t(p2d, polys2d, gtype, angle):
    allp = np.concatenate(polys2d, 0)
    if gtype == "LINEAR":
        d = np.array([np.cos(angle), np.sin(angle)])
        proj = allp @ d
        lo, hi = proj.min(), proj.max()
        t = (p2d @ d - lo) / max(hi - lo, 1e-9)
    else:  # RADIAL
        c = (allp.min(0) + allp.max(0)) * 0.5
        r = np.linalg.norm(allp - c, axis=1).max()
        t = np.linalg.norm(p2d - c, axis=1) / max(r, 1e-9)
    return np.clip(t, 0, 1)[:, None].astype(np.float32)


def _gradient_colors(c1, c2, t):
    """Interpolate premultiplied so alpha fades look right. t: (n,1)."""
    c1 = np.asarray(c1, np.float32)
    c2 = np.asarray(c2, np.float32)
    pa = c1[3] * (1 - t) + c2[3] * t
    prgb = c1[:3] * c1[3] * (1 - t) + c2[:3] * c2[3] * t
    rgb = np.where(pa > 0, prgb / np.maximum(pa, 1e-9), 0).astype(np.float32)
    return rgb, pa[:, 0]


def _box_corners_world(frame, lo2, hi2, d0, d1):
    cs = []
    for x in (lo2[0], hi2[0]):
        for y in (lo2[1], hi2[1]):
            for z in (d0, d1):
                cs.append(frame.origin + frame.u * x + frame.v * y + frame.n * z)
    cs = np.array(cs)
    return cs.min(0), cs.max(0)


def shape_fragments(pmap, sh):
    """Rasterize one shape. Returns a list of (texel_idx, rgb(n,3), alpha(n,)) to
    composite in order (fill first, then stroke)."""
    frags = []
    if not sh["polys2d"]:
        return frags
    op = float(sh.get("opacity", 1.0))
    if op <= 0:
        return frags
    aa = max(pmap.texel_size, 1e-9)
    P = pmap.pos
    Nn = pmap.nrm
    fr = sh["frame"]
    depth = float(sh["depth"])
    st = sh.get("stroke", {})
    use_stroke = sh.get("use_stroke") and sh.get("stroke_width", 0) > 0
    half_w = 0.5 * float(sh.get("stroke_width", 0.0)) if use_stroke else 0.0
    # reach of the stroke past its centre line (square caps reach the corner)
    r_stroke = half_w * (1.4143 if st.get("cap") == "SQUARE" else 1.0) + aa
    s_polys3d = sh.get("stroke_polys3d") or sh["polys3d"]
    s_polys2d = sh.get("stroke_polys2d") or sh["polys2d"]

    allp = np.concatenate(list(sh["polys2d"]) + (list(s_polys2d) if use_stroke else []), 0)
    lo2 = allp.min(0) - r_stroke - 2 * aa
    hi2 = allp.max(0) + r_stroke + 2 * aa
    reach = depth + r_stroke

    need_planar = sh.get("use_fill") or (use_stroke and sh["kind"] != "SURFACE")
    cand = np.zeros(0, np.int64)
    p2 = np.zeros((0, 2))
    if need_planar:
        wlo, whi = _box_corners_world(fr, lo2, hi2, -reach, reach)
        cand = pmap.query_box(wlo, whi)
        if len(cand):
            Pc = P[cand].astype(np.float64)
            dep = (Pc - fr.origin) @ fr.n
            facing = (Nn[cand] @ fr.n.astype(np.float32)) > 0.05
            keep = facing & (np.abs(dep) <= reach)
            cand = cand[keep]
            p2 = fr.to2d(Pc[keep])
            inbox = np.all((p2 >= lo2) & (p2 <= hi2), axis=1)
            cand = cand[inbox]
            p2 = p2[inbox]

    # --- fill
    if sh.get("use_fill") and len(cand):
        closed_polys = [p for p in sh["polys2d"] if len(p) >= 3]
        if closed_polys:
            res = _fill_mask(closed_polys, aa * 0.5, 2 * aa)
            if res is not None:
                mask, mlo, cell = res
                cov = _sample_mask(mask, mlo, cell, p2)
                hit = cov > 0
                if hit.any():
                    if sh.get("fill_type", "SOLID") == "SOLID":
                        col = np.asarray(sh["fill_color"], np.float32)
                        rgb = np.broadcast_to(col[:3], (int(hit.sum()), 3))
                        alpha = cov[hit] * col[3] * op
                    else:
                        t = _gradient_t(p2[hit], closed_polys, sh["fill_type"], sh.get("gradient_angle", 0.0))
                        rgb, pa = _gradient_colors(sh["fill_color"], sh["fill_color2"], t)
                        alpha = cov[hit] * pa * op
                    frags.append((cand[hit], rgb, alpha.astype(np.float32)))

    # --- stroke
    if use_stroke:
        if sh["kind"] == "SURFACE":
            # true 3D distance: the stroke can wrap around the object freely
            paths = StrokePaths(s_polys3d, sh["closed"])
            idx, d, sv, sp = paths.closest(P, pmap.query_box, r_stroke)
            texels = idx
            pts = P[idx].astype(np.float64)
        else:
            paths = StrokePaths(s_polys2d, sh["closed"])

            def box(lo, hi, _p=p2):
                return np.nonzero(np.all((_p >= lo) & (_p <= hi), axis=1))[0]

            sub, d, sv, sp = paths.closest(p2, box, r_stroke)
            texels = cand[sub]
            pts = p2[sub]
        if len(texels):
            cov, frac = stroke_coverage(pts, d, sv, sp, paths, st, half_w, aa)
            hit = cov > 0
            if hit.any():
                gtype = st.get("gradient", "SOLID")
                if gtype == "SOLID":
                    col = np.asarray(sh["stroke_color"], np.float32)
                    rgb = np.broadcast_to(col[:3], (int(hit.sum()), 3))
                    alpha = cov[hit] * col[3] * op
                else:
                    if gtype == "ALONG":
                        t = frac[hit][:, None].astype(np.float32)
                    else:
                        q2 = fr.to2d(P[texels[hit]].astype(np.float64))
                        t = _gradient_t(q2, s_polys2d, gtype, st.get("gradient_angle", 0.0))
                    rgb, pa = _gradient_colors(sh["stroke_color"], st["color2"], t)
                    alpha = cov[hit] * pa * op
                frags.append((texels[hit], rgb, alpha.astype(np.float32)))
    return frags


# --------------------------------------------------------------------------
# Layer modifiers (operate on the layer's own pixels, before compositing)
# --------------------------------------------------------------------------


def _to_texels(pmap, sel, rgb, alpha, per_texel=False):
    """Fragment over surface samples -> (texel ids, rgb, alpha), one per texel."""
    if per_texel:
        return sel, np.asarray(rgb), alpha
    if not pmap.shared:
        return sel, np.asarray(rgb), alpha
    t = pmap.tex_id[sel]
    order = np.lexsort((-alpha, t))
    t = t[order]
    first = np.ones(len(t), bool)
    first[1:] = t[1:] != t[:-1]
    keep = order[first]
    return t[first], np.asarray(rgb)[keep], alpha[keep]


def flatten_fragments(pmap, frags):
    """Merge a layer's fragments (fill, stroke) into one premultiplied RGBA per texel.
    Returns (texel ids, rgba premultiplied)."""
    parts = [_to_texels(pmap, *f, per_texel=isinstance(f, TexelFragment)) for f in frags]
    if not parts:
        return np.zeros(0, np.int64), np.zeros((0, 4), np.float32)
    ids = np.unique(np.concatenate([p[0] for p in parts]))
    out = np.zeros((len(ids), 4), np.float32)
    for t, rgb, alpha in parts:
        k = np.searchsorted(ids, t)
        a = np.clip(alpha, 0, 1)[:, None].astype(np.float32)
        out[k, :3] = rgb * a + out[k, :3] * (1 - a)
        out[k, 3:4] = a + out[k, 3:4] * (1 - a)
    return ids, out


def _flat_lookup(pmap):
    """texel (flat image index) -> texel id, -1 where the mesh has no texel."""
    lut = getattr(pmap, "_lut", None)
    if lut is None:
        lut = np.full(pmap.width * pmap.height, -1, np.int32)
        lut[pmap.idx[:pmap.n_primary]] = np.arange(pmap.n_primary, dtype=np.int32)
        pmap._lut = lut
    return lut


def _box_blur_axis(img, r, axis):
    """Box blur of radius r (window 2r+1) along an axis, zero padded."""
    if r <= 0:
        return img
    pad = [(0, 0)] * img.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(img, pad), axis=axis, dtype=np.float32)
    n = img.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return (hi - lo) / np.float32(2 * r + 1)


def gaussian_blur(img, sigma):
    """Fast Gaussian approximation: 3 box blurs per axis (cost independent of radius)."""
    if sigma < 0.3:
        return img
    w = np.sqrt(12.0 * sigma * sigma / 3.0 + 1.0)
    r = max(int(round((w - 1) / 2)), 1)
    for axis in (0, 1):
        for _ in range(3):
            img = _box_blur_axis(img, r, axis)
    return img


def apply_blur(pmap, ids, rgba, radius):
    """Blur a layer in texture space. radius in world units (~2 sigma).
    Returns (texel ids, premultiplied rgba)."""
    sigma = 0.5 * float(radius) / max(pmap.texel_size, 1e-9)
    if sigma < 0.3 or not len(ids):
        return ids, rgba
    W, H = pmap.width, pmap.height
    flat = pmap.idx[ids].astype(np.int64)
    xs, ys = flat % W, flat // W
    m = int(np.ceil(3 * sigma)) + 2
    x0, x1 = max(int(xs.min()) - m, 0), min(int(xs.max()) + m + 1, W)
    y0, y1 = max(int(ys.min()) - m, 0), min(int(ys.max()) + m + 1, H)
    img = np.zeros((y1 - y0, x1 - x0, 4), np.float32)
    img[ys - y0, xs - x0] = rgba
    img = gaussian_blur(img, sigma)
    lut = _flat_lookup(pmap).reshape(H, W)[y0:y1, x0:x1]
    keep = (lut >= 0) & (img[..., 3] > 1e-4)
    return lut[keep].astype(np.int64), img[keep]


def _hash3(ix, iy, iz, seed):
    """Integer lattice -> pseudo random float in [0, 1)."""
    h = (ix.astype(np.uint32) * np.uint32(0x8DA6B343)) ^ (iy.astype(np.uint32) * np.uint32(0xD8163841)) \
        ^ (iz.astype(np.uint32) * np.uint32(0xCB1AB31F)) ^ np.uint32((seed * 0x9E3779B1) & 0xFFFFFFFF)
    h ^= h >> np.uint32(15)
    h *= np.uint32(0x2C1B3C6D)
    h ^= h >> np.uint32(12)
    h *= np.uint32(0x297A2D39)
    h ^= h >> np.uint32(15)
    return h.astype(np.float32) * np.float32(1.0 / 4294967296.0)


def value_noise3(p, seed):
    """Smooth 3D value noise in [0, 1]. p: (N,3)."""
    with np.errstate(over="ignore"):
        f = np.floor(p)
        t = (p - f).astype(np.float32)
        i = f.astype(np.int64)
        t = t * t * (3 - 2 * t)
        x0, y0, z0 = i[:, 0], i[:, 1], i[:, 2]
        x1, y1, z1 = x0 + 1, y0 + 1, z0 + 1
        tx, ty, tz = t[:, 0], t[:, 1], t[:, 2]

        def lerp(a, b, w):
            return a + (b - a) * w
        c00 = lerp(_hash3(x0, y0, z0, seed), _hash3(x1, y0, z0, seed), tx)
        c10 = lerp(_hash3(x0, y1, z0, seed), _hash3(x1, y1, z0, seed), tx)
        c01 = lerp(_hash3(x0, y0, z1, seed), _hash3(x1, y0, z1, seed), tx)
        c11 = lerp(_hash3(x0, y1, z1, seed), _hash3(x1, y1, z1, seed), tx)
        return lerp(lerp(c00, c10, ty), lerp(c01, c11, ty), tz)


def fbm_noise(p, detail, roughness, seed):
    """Fractal value noise in [0, 1] (Blender Noise Texture style detail/roughness)."""
    detail = max(float(detail), 0.0)
    n_oct = int(np.floor(detail)) + 1
    frac = detail - np.floor(detail)
    total = np.zeros(len(p), np.float32)
    amp, norm, freq = 1.0, 0.0, 1.0
    for o in range(n_oct + (1 if frac > 1e-4 else 0)):
        w = amp * (frac if o == n_oct else 1.0)
        # rotate each octave a bit so the lattices do not line up
        q = p * freq + np.float32(o * 17.31)
        total += np.float32(w) * (value_noise3(q, seed + o * 1013) - 0.5)
        norm += w
        amp *= roughness
        freq *= 2.0
    # keep the contrast of a single octave whatever the detail
    return np.clip(0.5 + total / max(norm, 1e-9) * (1.0 + 0.6 * min(n_oct - 1, 3)), 0.0, 1.0)


def _erfinv(x):
    """Inverse error function (Giles' approximation, ~1e-7)."""
    x = np.clip(x, -0.999999, 0.999999).astype(np.float64)
    w = -np.log((1.0 - x) * (1.0 + x))
    small = w < 5.0
    ws = w - 2.5
    p1 = 2.81022636e-08
    for c in (3.43273939e-07, -3.5233877e-06, -4.39150654e-06, 0.00021858087, -0.00125372503,
              -0.00417768164, 0.246640727, 1.50140941):
        p1 = c + p1 * ws
    wl = np.sqrt(np.maximum(w, 5.0)) - 3.0
    p2 = -0.000200214257
    for c in (0.000100950558, 0.00134934322, -0.00367342844, 0.00573950773, -0.0076224613,
              0.00943887047, 1.00167406, 2.83297682):
        p2 = c + p2 * wl
    return np.where(small, p1, p2) * x


def _shifted(a, dy, dx, fill):
    """a shifted by (dy, dx) (out[y, x] = a[y - dy, x - dx]), padded with fill."""
    out = np.full_like(a, fill)
    h, w = a.shape
    if abs(dy) >= h or abs(dx) >= w:
        return out
    ys = slice(max(dy, 0), h + min(dy, 0)); yd = slice(max(-dy, 0), h + min(-dy, 0))
    xs = slice(max(dx, 0), w + min(dx, 0)); xd = slice(max(-dx, 0), w + min(-dx, 0))
    out[ys, xs] = a[yd, xd]
    return out


def _seed_distance(seeds, reach):
    """Euclidean distance (texels) to the nearest seed pixel, by jump flooding.
    Pixels farther than about `reach` may come back as a large value."""
    h, w = seeds.shape
    Y, X = np.mgrid[0:h, 0:w].astype(np.float32)
    far = np.float32(1e7)
    sx = np.where(seeds, X, far)
    sy = np.where(seeds, Y, far)
    best = np.where(seeds, 0.0, np.inf).astype(np.float32)
    step = 1 << max(int(np.ceil(np.log2(max(reach, 1.0)))), 0)
    steps = []
    while step >= 1:
        steps.append(step)
        step //= 2
    steps.append(1)  # one more pass at 1 cleans up JFA errors
    for st in steps:
        nx, ny, nb = sx.copy(), sy.copy(), best.copy()
        for dy in (-st, 0, st):
            for dx in (-st, 0, st):
                if dy == 0 and dx == 0:
                    continue
                cx = _shifted(sx, dy, dx, far)
                cy = _shifted(sy, dy, dx, far)
                dd = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2)
                better = dd < nb
                nx = np.where(better, cx, nx)
                ny = np.where(better, cy, ny)
                nb = np.where(better, dd, nb)
        sx, sy, best = nx, ny, nb
    return best


def apply_rough_edges(pmap, ids, rgba, mod):
    """Hand-drawn edges: the outline of the layer is pushed in and out by a 3D noise
    (in the target's space, so it has no seams across UV islands).
    mod: size, detail, roughness, inside, outside, softness, evolution, seed, lut (256 floats).
    Returns (texel ids, premultiplied rgba)."""
    if not len(ids):
        return ids, rgba
    ts = max(pmap.texel_size, 1e-9)
    inside = max(float(mod["inside"]), 0.0)
    outside = max(float(mod["outside"]), 0.0)
    soft = max(float(mod["softness"]), 0.75 * ts)
    reach = max(inside, outside) + soft
    if max(inside, outside) < 0.25 * ts:
        return ids, rgba
    a_src = rgba[:, 3]
    a_max = float(a_src.max())
    if a_max <= 1e-5:
        return ids, rgba
    R = reach / ts + 2.0                      # in texels
    W, H = pmap.width, pmap.height
    flat = pmap.idx[ids].astype(np.int64)
    xs, ys = flat % W, flat // W
    m = int(np.ceil(outside / ts + soft / ts)) + 3
    x0, x1 = max(int(xs.min()) - m, 0), min(int(xs.max()) + m + 1, W)
    y0, y1 = max(int(ys.min()) - m, 0), min(int(ys.max()) + m + 1, H)
    img = np.zeros((y1 - y0, x1 - x0, 4), np.float32)
    img[ys - y0, xs - x0] = rgba
    lut = _flat_lookup(pmap).reshape(H, W)[y0:y1, x0:x1]
    on = lut >= 0
    cov = np.clip(img[..., 3] / a_max, 0.0, 1.0)
    inn = cov >= 0.5
    # edge pixels: a texel next to an on-mesh texel on the other side of the outline
    # (UV island borders are not edges, so seams do not get chewed)
    edge = np.zeros_like(inn)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        n_in = _shifted(inn, dy, dx, False)
        n_on = _shifted(on, dy, dx, False)
        edge |= on & n_on & (n_in != inn)
    if not edge.any():
        return ids, rgba
    dist = _seed_distance(edge, R)
    # signed distance to the outline in world units (+ inside); the anti-aliased
    # coverage of the edge texels gives the sub-texel part
    d_tex = np.where(edge, cov - 0.5, np.where(inn, dist + 0.5, -(dist + 0.5)))
    d_tex = np.where(np.isfinite(dist), d_tex, np.where(inn, 1e6, -1e6))
    d = d_tex * ts
    band = on & (d < reach) & (d > -reach)
    deep = on & inn & (d >= reach)
    t_ids = lut[band].astype(np.int64)
    if len(t_ids):
        size = max(float(mod["size"]), 1e-6)
        pos = pmap.pos[t_ids].astype(np.float64) / size
        evo = float(mod["evolution"])
        k = int(np.floor(evo))
        w = evo - k
        seed = int(mod["seed"])
        n = fbm_noise(pos, mod["detail"], mod["roughness"], seed + k * 7919)
        if w > 1e-4:
            n2 = fbm_noise(pos, mod["detail"], mod["roughness"], seed + (k + 1) * 7919)
            # crossfade that keeps the contrast (no flat look halfway between two seeds)
            nc = ((n - 0.5) * (1 - w) + (n2 - 0.5) * w) / np.sqrt((1 - w) ** 2 + w ** 2)
            n = np.clip(nc + 0.5, 0.0, 1.0)
        ramp = np.asarray(mod["lut"], np.float32)
        n = ramp[np.clip((n * (len(ramp) - 1) + 0.5).astype(np.int64), 0, len(ramp) - 1)]
        c = 2.0 * n - 1.0
        e = d[band] + np.where(c >= 0, c * outside, c * inside)
        nc_ = np.clip(0.5 + e / soft, 0.0, 1.0)
        new_cov = nc_ * nc_ * (3 - 2 * nc_)
    else:
        new_cov = np.zeros(0, np.float32)
    # color: the layer's own color where it was painted, else the nearest painted color
    src = img[band]
    has = src[:, 3] > 1e-4
    if (~has).any():
        sp = gaussian_blur(img.copy(), max(R / 3.0, 1.0))[band]
        rgb_out = sp[:, :3] / np.maximum(sp[:, 3:4], 1e-9)
        # far from any color the blur fades: fall back to the layer's mean color
        mean = (rgba[:, :3].sum(0) / max(float(a_src.sum()), 1e-9)).astype(np.float32)
        rgb_out = np.where(sp[:, 3:4] > 1e-6, rgb_out, mean)
    else:
        rgb_out = np.zeros((len(src), 3), np.float32)
    rgb = np.where(has[:, None], src[:, :3] / np.maximum(src[:, 3:4], 1e-9), rgb_out)
    # anti-aliased texels of the old outline are not partial any more: full layer opacity
    base = np.where(has & (src[:, 3] >= 0.98 * a_max), src[:, 3], a_max)
    alpha = base * new_cov
    keep = alpha > 1e-4
    out_ids = [t_ids[keep]]
    out_rgba = [np.concatenate([rgb[keep] * alpha[keep, None], alpha[keep, None]], 1)]
    if deep.any():
        out_ids.append(lut[deep].astype(np.int64))
        out_rgba.append(img[deep])
    o_ids = np.concatenate(out_ids)
    o = np.concatenate(out_rgba).astype(np.float32)
    order = np.argsort(o_ids, kind="stable")
    return o_ids[order], o[order]


def apply_modifiers(pmap, frags, modifiers):
    """Run a layer's modifier stack. Returns a list with one fragment over texel ids
    (marked so compositing does not remap them)."""
    if not modifiers:
        return frags
    ids, rgba = flatten_fragments(pmap, frags)
    for mod in modifiers:
        if mod["type"] == "BLUR":
            ids, rgba = apply_blur(pmap, ids, rgba, mod["radius"])
        elif mod["type"] == "ROUGHEN":
            ids, rgba = apply_rough_edges(pmap, ids, rgba, mod)
    a = rgba[:, 3]
    rgb = np.where(a[:, None] > 0, rgba[:, :3] / np.maximum(a[:, None], 1e-9), 0).astype(np.float32)
    return [TexelFragment(ids, rgb, a.astype(np.float32))]


class TexelFragment(tuple):
    """A fragment already expressed per texel (not per surface sample)."""

    def __new__(cls, ids, rgb, alpha):
        return super().__new__(cls, (ids, rgb, alpha))


def to_texel_fragment(ids, rgba_premult):
    a = rgba_premult[:, 3]
    rgb = np.where(a[:, None] > 0, rgba_premult[:, :3] / np.maximum(a[:, None], 1e-9), 0)
    return TexelFragment(ids, rgb.astype(np.float32), a.astype(np.float32))


def mask_alpha(pmap, frags):
    """Coverage of a layer per texel, to use as a mask: (sorted texel ids, alpha)."""
    ids, rgba = flatten_fragments(pmap, frags)
    return ids, rgba[:, 3].copy()


def apply_mask(pmap, frags, mask, invert):
    """Track-matte style: keep the layer where the mask is (invert: where it is not).
    mask = (sorted texel ids, alpha). Returns a per-texel fragment list."""
    ids, rgba = flatten_fragments(pmap, frags)
    if not len(ids):
        return []
    m_ids, m_a = mask
    m = np.zeros(len(ids), np.float32)
    if len(m_ids):
        k = np.clip(np.searchsorted(m_ids, ids), 0, len(m_ids) - 1)
        hit = m_ids[k] == ids
        m[hit] = m_a[k[hit]]
    f = 1.0 - m if invert else m
    rgba *= f[:, None]
    keep = rgba[:, 3] > 1e-5
    return [to_texel_fragment(ids[keep], rgba[keep])]


def merge_group(pmap, frag_lists, opacity=1.0):
    """Composite a group's children (bottom -> top) into one per-texel fragment.
    Returns (texel ids, premultiplied rgba)."""
    ids, rgba = flatten_fragments(pmap, [f for fl in frag_lists for f in fl])
    if opacity < 1.0:
        rgba *= np.float32(opacity)
    return ids, rgba


def composite(pmap, frag_lists):
    """Alpha-over fragments bottom -> top. Returns (N,4) straight-alpha RGBA."""
    N = pmap.n_primary
    out = np.zeros((N, 4), np.float32)  # premultiplied, one row per texel
    for frags in frag_lists:
        for frag in frags:
            sel, rgb, alpha = frag
            if pmap.shared and not isinstance(frag, TexelFragment):
                # several surfaces share these texels: a layer paints the texel if it
                # hits any of them (keep its strongest sample per texel)
                t = pmap.tex_id[sel]
                order = np.lexsort((-alpha, t))
                t = t[order]
                first = np.ones(len(t), bool)
                first[1:] = t[1:] != t[:-1]
                keep = order[first]
                sel, rgb, alpha = t[first], np.asarray(rgb)[keep], alpha[keep]
            a = np.clip(alpha, 0, 1)[:, None]
            out[sel, :3] = rgb * a + out[sel, :3] * (1 - a)
            out[sel, 3:4] = a + out[sel, 3:4] * (1 - a)
    a = out[:, 3:4]
    with np.errstate(divide="ignore", invalid="ignore"):
        out[:, :3] = np.where(a > 0, out[:, :3] / np.maximum(a, 1e-9), 0)
    return out


def resolve_texels(pmap, rgba):
    """One color per texel: where several surface samples share a texel,
    keep the most opaque result. Returns (texel indices, rgba)."""
    return pmap.idx[:pmap.n_primary], rgba


def render_layers(pmap, shapes):
    return composite(pmap, [shape_fragments(pmap, sh) for sh in shapes])


# --------------------------------------------------------------------------
# Wave (displaces the stroke path, like After Effects' stroke Wave)
# --------------------------------------------------------------------------


def wave_polylines(polys, closed, amount, wavelength, phase, shape, normal_fn, snap_fn=None,
                   max_points=20000):
    out = []
    wavelength = max(float(wavelength), 1e-6)
    for P, c in zip(polys, closed):
        P = np.asarray(P, np.float64)
        if len(P) < 2:
            out.append(P)
            continue
        Q = np.vstack([P, P[:1]]) if c else P
        seg = np.linalg.norm(np.diff(Q, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        L = cum[-1]
        if L <= 0:
            out.append(P)
            continue
        wl = wavelength
        if c:  # whole number of cycles so the seam is invisible
            wl = L / max(round(L / wavelength), 1)
        n = int(min(max(L / (wl / 32.0), 8), max_points))
        sv = np.linspace(0, L, n + 1)
        if c:
            sv = sv[:-1]
        pts = np.stack([np.interp(sv, cum, Q[:, k]) for k in range(Q.shape[1])], 1)
        if c:
            T = np.roll(pts, -1, 0) - np.roll(pts, 1, 0)
        else:
            T = np.gradient(pts, axis=0)
        T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-12)
        N = normal_fn(pts)
        side = np.cross(N, T)
        side /= np.maximum(np.linalg.norm(side, axis=1, keepdims=True), 1e-12)
        x = sv / wl + phase / (2 * np.pi)
        if shape == "ZIGZAG":
            w = 1 - 4 * np.abs(np.mod(x + 0.25, 1.0) - 0.5)  # triangle, same phase as sine
        else:
            w = np.sin(2 * np.pi * x)
        new = pts + side * (amount * w)[:, None]
        if snap_fn is not None:
            new = snap_fn(new)
        out.append(new)
    return out


def srgb_to_linear(c):
    c = np.asarray(c, np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
