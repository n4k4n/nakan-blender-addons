# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender <-> core: extract mesh/curve data, render a vector set into its image."""

import time
import numpy as np
import bpy
from mathutils import geometry

from . import core

_PMAP_CACHE = {}  # key -> (signature, PositionMap)
_FRAG_CACHE = {}  # set name -> {"pm": PositionMap, "layers": {obj name: (key, fragments)}}


# --------------------------------------------------------------------------
# Mesh -> position map
# --------------------------------------------------------------------------


def _mesh_arrays(obj, uv_name, depsgraph):
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        me.calc_loop_triangles()
        nt = len(me.loop_triangles)
        if nt == 0:
            return None
        if uv_name and uv_name in me.uv_layers:
            uvl = me.uv_layers[uv_name]
        else:
            uvl = me.uv_layers.active
        if uvl is None:
            raise RuntimeError("Target object has no UV map.")

        nv = len(me.vertices)
        co = np.empty(nv * 3, np.float32)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)

        nl = len(me.loops)
        uv = np.empty(nl * 2, np.float32)
        uvl.uv.foreach_get("vector", uv)
        uv = uv.reshape(-1, 2)

        cn = np.empty(nl * 3, np.float32)
        me.corner_normals.foreach_get("vector", cn)
        cn = cn.reshape(-1, 3)

        tl = np.empty(nt * 3, np.int32)
        me.loop_triangles.foreach_get("loops", tl)
        tl = tl.reshape(-1, 3)
        tv = np.empty(nt * 3, np.int32)
        me.loop_triangles.foreach_get("vertices", tv)
        tv = tv.reshape(-1, 3)
    finally:
        ev.to_mesh_clear()

    # everything is computed in the target's local space, so transforming the
    # target (and its parented vector layers) needs no re-render at all
    return uv[tl], co[tv], cn[tl], co


def get_position_map(obj, uv_name, width, height, depsgraph, margin):
    arr = _mesh_arrays(obj, uv_name, depsgraph)
    if arr is None:
        return None
    tri_uv, tri_pos, tri_nrm, co_w = arr
    sig = (
        width, height, margin, tri_uv.shape,
        hash(tri_uv.tobytes()), hash(co_w.astype(np.float32).tobytes()),
    )
    key = (obj.name, uv_name)
    cached = _PMAP_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    pm = core.build_position_map(tri_uv, tri_pos, tri_nrm, width, height, margin=margin)
    _PMAP_CACHE[key] = (sig, pm)
    return pm


def set_targets(coll):
    """All objects that receive this set: [(object, uv map name, image)], main target first.
    Extra objects without their own image use the set's image."""
    st = coll.vl_set
    out = []
    if st.target is not None and st.target.type == "MESH":
        out.append((st.target, st.uv_map, st.image))
    for t in st.extra_targets:
        o = t.obj
        if o is None or o.type != "MESH" or any(o == x[0] for x in out):
            continue
        out.append((o, t.uv_map, t.image or st.image))
    return out


def _frag_key(coll, obj):
    return coll.name + "\x00" + obj.name


def release_set(coll):
    """Free the memory a set keeps for fast live updates (position maps + layer caches)."""
    for k in [k for k in _FRAG_CACHE if k.split("\x00")[0] == coll.name]:
        _FRAG_CACHE.pop(k, None)
    for t, _uv, _img in set_targets(coll):
        # keep it if another live set still uses the same object
        if any(c != coll and c.vl_set.live and any(t == x[0] for x in set_targets(c))
               for c in bpy.data.collections if c.vl_set.target is not None):
            continue
        for key in [k for k in _PMAP_CACHE if k[0] == t.name]:
            _PMAP_CACHE.pop(key, None)
    import gc
    gc.collect()


def clear_cache():
    _PMAP_CACHE.clear()
    _FRAG_CACHE.clear()


# --------------------------------------------------------------------------
# Curves / text -> polylines
# --------------------------------------------------------------------------


def _spline_points(spline, default_res):
    res = max(int(getattr(spline, "resolution_u", 0) or default_res), 4)
    if spline.type == "BEZIER":
        bp = spline.bezier_points
        n = len(bp)
        if n == 0:
            return None
        if n == 1:
            return np.array([bp[0].co], np.float64)
        segs = list(range(n - 1))
        if spline.use_cyclic_u:
            segs.append(n - 1)
        out = []
        for i in segs:
            a = bp[i]
            b = bp[(i + 1) % n]
            pts = geometry.interpolate_bezier(a.co, a.handle_right, b.handle_left, b.co, res + 1)
            out.extend(pts[:-1])
        if not spline.use_cyclic_u:
            out.append(bp[n - 1].co)
        return np.array([tuple(p) for p in out], np.float64)
    pts = spline.points
    if len(pts) == 0:
        return None
    return np.array([tuple(p.co)[:3] for p in pts], np.float64)


def _curve_polylines(curve_data, matrix):
    M = np.array(matrix, np.float64)
    polys, closed = [], []
    for sp in curve_data.splines:
        p = _spline_points(sp, curve_data.resolution_u)
        if p is None or len(p) < 2:
            continue
        p = p @ M[:3, :3].T + M[:3, 3]
        # drop consecutive duplicates
        keep = np.ones(len(p), bool)
        keep[1:] = np.linalg.norm(np.diff(p, axis=0), axis=1) > 1e-9
        p = p[keep]
        if len(p) < 2:
            continue
        polys.append(p)
        closed.append(bool(sp.use_cyclic_u))
    return polys, closed


def object_polylines(obj, depsgraph, to_target=None):
    """Polylines in the target's local space (to_target = target.matrix_world.inverted())."""
    mw = obj.matrix_world if to_target is None else to_target @ obj.matrix_world
    if obj.type == "FONT":
        ev = obj.evaluated_get(depsgraph)
        cu = ev.to_curve(depsgraph)
        try:
            polys, closed = _curve_polylines(cu, mw)
        finally:
            ev.to_curve_clear()
        closed = [True] * len(polys)  # glyph outlines are always closed
        return polys, closed
    if obj.type == "CURVE":
        # evaluated copy so edits made in Edit Mode are seen live
        ev = obj.evaluated_get(depsgraph)
        try:
            cu = ev.to_curve(depsgraph)
        except Exception:
            cu = None
        if cu is None:
            return _curve_polylines(obj.data, mw)
        try:
            return _curve_polylines(cu, mw)
        finally:
            ev.to_curve_clear()
    return [], []


def _object_frame(obj, to_target=None):
    mw = obj.matrix_world if to_target is None else to_target @ obj.matrix_world
    M = np.array(mw, np.float64)
    return core.Frame(M[:3, 3], M[:3, 0], M[:3, 1], M[:3, 2])


def _planar_frame(obj, allp, to_target=None):
    """Projection frame if the points are coplanar, else None.
    Prefers the object's own XY plane (keeps gradient angles tied to the object),
    falls back to a best-fit plane (e.g. curves drawn on a construction plane)."""
    ext = float(np.ptp(allp, axis=0).max()) if len(allp) else 0.0
    tol = max(1e-5, ext * 2e-3)
    fr = _object_frame(obj, to_target)
    if np.ptp(fr.depth(allp)) <= tol:
        return fr
    fr = core.fit_frame(allp)
    if np.ptp(fr.depth(allp)) <= tol:
        return fr
    return None


def _lies_on_surface(allp, surface):
    """True if the curve was drawn on the target surface (not floating on a plane)."""
    if surface is None:
        return False
    pts = allp[:: max(1, len(allp) // 64)]
    near, _ = surface.nearest(pts)
    gap = np.linalg.norm(near - pts, axis=1)
    ext = float(np.ptp(allp, axis=0).max()) if len(allp) else 0.0
    return float(gap.max()) <= max(1e-4, ext * 0.01)


def resolve_frame(obj, props, allp, target_center, surface=None, to_target=None):
    """Returns (kind, frame)."""
    outward = allp.mean(0) - target_center
    if obj.type == "FONT":
        # text: always its own plane, its +Z is the readable side
        return "PLANAR", _object_frame(obj, to_target)
    mode = props.projection
    if mode == "SURFACE":
        return "SURFACE", core.fit_frame(allp, outward_hint=outward)
    if mode == "PLANAR":
        fr = _object_frame(obj, to_target)
    else:
        # AUTO: curves drawn on the surface follow it (even flat ones, e.g. an equator);
        # flat curves floating in front of it (2D, drawn on a construction plane, imported SVG) act like decals
        if obj.data.dimensions == "3D" and _lies_on_surface(allp, surface):
            return "SURFACE", core.fit_frame(allp, outward_hint=outward)
        fr = _planar_frame(obj, allp, to_target)
        if fr is None:
            return "SURFACE", core.fit_frame(allp, outward_hint=outward)
    # curves have no "readable side": make the projection face outward
    if np.dot(fr.n, outward) < 0:
        fr.n = -fr.n
    return "PLANAR", fr


def _color(c, linear):
    c = np.array(tuple(c), np.float32)
    if linear:
        c[:3] = core.srgb_to_linear(c[:3])
    return c


class SurfaceQuery:
    """Lazy BVH of the target mesh in world space (normals / snapping for waves)."""

    def __init__(self, obj, depsgraph):
        self.obj = obj
        self.dg = depsgraph
        self._bvh = None

    def _tree(self):
        if self._bvh is None:
            from mathutils.bvhtree import BVHTree
            self._bvh = BVHTree.FromObject(self.obj, self.dg)  # target local space
            self.M = np.eye(4)
            self.Mi = np.eye(4)
            self.Nm = np.eye(3)
        return self._bvh

    def nearest(self, pts):
        from mathutils import Vector
        bvh = self._tree()
        loc = np.asarray(pts, np.float64) @ self.Mi[:3, :3].T + self.Mi[:3, 3]
        P = np.empty_like(loc)
        N = np.empty_like(loc)
        for k, q in enumerate(loc):
            hit = bvh.find_nearest(Vector(q))
            if hit[0] is None:
                P[k] = q
                N[k] = (0, 0, 1)
            else:
                P[k] = hit[0]
                N[k] = hit[1]
        Pw = P @ self.M[:3, :3].T + self.M[:3, 3]
        Nw = N @ self.Nm.T
        Nw /= np.maximum(np.linalg.norm(Nw, axis=1, keepdims=True), 1e-12)
        return Pw, Nw


def _props_key(p):
    vals = []
    for prop in p.bl_rna.properties:
        k = prop.identifier
        if k in {"rna_type", "order", "show_expanded", "active_modifier",
                 "mask_mode", "mask_object", "mask_show"} or (k == "enabled" and hasattr(p, "order")):
            continue
        v = getattr(p, k)
        if isinstance(v, bpy.types.bpy_prop_collection):
            v = tuple(_props_key(item) for item in v)
        elif hasattr(v, "__len__") and not isinstance(v, str):
            v = tuple(round(x, 6) for x in v)
        elif isinstance(v, float):
            v = round(v, 7)
        vals.append(v)
    return tuple(vals)


_LINEAR_LUT = tuple(i / 255.0 for i in range(256))


def _ramp_lut(tex):
    if tex is None or not tex.use_color_ramp:
        return _LINEAR_LUT
    cr = tex.color_ramp
    out = []
    for i in range(256):
        c = cr.evaluate(i / 255.0)
        out.append(round((c[0] + c[1] + c[2]) / 3.0, 5))
    return tuple(out)


def _modifier_dict(m, frame):
    if m.type == "ROUGHEN":
        evo = m.noise_evolution
        if m.use_boil:
            evo += float(int(frame) // max(m.boil_hold, 1))
        lut = _ramp_lut(m.noise_ramp)
        return {"type": "ROUGHEN", "size": m.noise_size, "detail": m.noise_detail,
                "roughness": m.noise_roughness, "inside": m.rough_inside,
                "outside": m.rough_outside, "softness": m.rough_softness,
                "evolution": evo, "seed": m.noise_seed, "lut": lut, "lut_key": hash(lut)}
    return {"type": m.type, "radius": m.radius}


def _layer_props(obj, depsgraph):
    """Layer settings as animated for this frame. A final render evaluates keyframes only on
    its own depsgraph (the original data keeps the viewport values), so read them from there."""
    if depsgraph is not None and getattr(depsgraph, "mode", "VIEWPORT") == "RENDER":
        try:
            return obj.evaluated_get(depsgraph).vl
        except Exception:
            pass
    return obj.vl


def build_shape(obj, depsgraph, target_center, linear, surface=None, to_target=None):
    p = _layer_props(obj, depsgraph)
    polys3d, closed = object_polylines(obj, depsgraph, to_target)
    if not polys3d:
        return None
    allp = np.concatenate(polys3d, 0)
    kind, frame = resolve_frame(obj, p, allp, target_center, surface, to_target)
    polys2d = [frame.to2d(q) for q in polys3d]
    ext2 = np.concatenate(polys2d, 0)
    extent = float(np.linalg.norm(ext2.max(0) - ext2.min(0)))
    if p.depth > 0:
        depth = p.depth
    elif kind == "PLANAR":
        depth = max(extent, 1e-4)
    else:
        depth = max(0.5 * extent, 1e-4)

    # wave only moves the stroke (as in After Effects), the fill keeps the original path
    s_polys3d = None
    s_polys2d = None
    if p.use_stroke and p.use_wave and p.wave_amount > 0 and p.wave_length > 0:
        if kind == "SURFACE" and surface is not None:
            def normal_fn(pts):
                return surface.nearest(pts)[1]

            def snap_fn(pts):
                return surface.nearest(pts)[0]
        else:
            n = frame.n

            def normal_fn(pts):
                return np.broadcast_to(n, pts.shape)

            snap_fn = None
        s_polys3d = core.wave_polylines(polys3d, closed, p.wave_amount, p.wave_length,
                                        p.wave_phase, p.wave_shape, normal_fn, snap_fn)
        s_polys2d = [frame.to2d(q) for q in s_polys3d]

    stroke = {
        "cap": p.stroke_cap,
        "gradient": p.stroke_fill_type,
        "color2": _color(p.stroke_color2, linear),
        "gradient_angle": p.stroke_gradient_angle,
        "dashes": p.use_dashes,
        "dash": p.dash,
        "gap": p.gap,
        "dash_offset": p.dash_offset,
        "trim": p.use_trim,
        "trim_start": p.trim_start / 100.0,
        "trim_end": p.trim_end / 100.0,
        "trim_offset": p.trim_offset,
        "trim_mode": p.trim_mode,
        "taper": p.use_taper,
        "taper_start_length": p.taper_start_length,
        "taper_end_length": p.taper_end_length,
        "taper_start_width": p.taper_start_width,
        "taper_end_width": p.taper_end_width,
        "taper_start_ease": p.taper_start_ease,
        "taper_end_ease": p.taper_end_ease,
    }

    try:
        cur_frame = depsgraph.scene.frame_current
    except Exception:
        cur_frame = 0
    mods = [_modifier_dict(m, cur_frame) for m in p.modifiers if m.enabled]
    key = hash((tuple(closed), kind, _props_key(p), linear,
                tuple((m.get("lut_key"), m.get("evolution")) for m in mods)))
    # geometry is compared with a tolerance: moving the target together with its
    # parented layers only produces float noise, which must not trigger a re-render
    geo = (polys3d, np.concatenate([frame.origin, frame.n, frame.u, [depth]]))
    return {
        "key": key,
        "geo": geo,
        "name": obj.name,
        "kind": kind,
        "frame": frame,
        "polys3d": polys3d,
        "polys2d": polys2d,  # open splines are closed implicitly by the scanline fill
        "stroke_polys3d": s_polys3d,
        "stroke_polys2d": s_polys2d,
        "closed": closed,
        "use_fill": p.use_fill,
        "use_stroke": p.use_stroke,
        "fill_type": p.fill_type,
        "fill_color": _color(p.fill_color, linear),
        "fill_color2": _color(p.fill_color2, linear),
        "gradient_angle": p.gradient_angle,
        "stroke_color": _color(p.stroke_color, linear),
        "stroke_width": p.stroke_width,
        "stroke": stroke,
        "opacity": p.opacity,
        "depth": depth,
        "modifiers": mods,
    }


# --------------------------------------------------------------------------
# Layers and rendering
# --------------------------------------------------------------------------


LAYER_TYPES = {"CURVE", "FONT"}


def item_order(kind, item):
    return item.vl.order if kind == "L" else item.vl_group.order


def children(parent):
    """Direct layers and groups of a set/group, bottom -> top: [("L", obj) | ("G", collection)]."""
    items = [("L", o) for o in parent.objects if o.type in LAYER_TYPES]
    items += [("G", c) for c in parent.children]
    top = max([item_order(k, x) for k, x in items] + [0])
    for k, x in sorted(items, key=lambda kx: kx[1].name):
        if item_order(k, x) <= 0:
            top += 1
            if k == "L":
                x.vl.order = top
                init_layer(x)
            else:
                x.vl_group.order = top
    return sorted(items, key=lambda kx: item_order(*kx))


def layer_objects(coll):
    """Every layer of the set, groups included."""
    seen, out = set(), []
    for o in coll.all_objects:
        if o.type in LAYER_TYPES and o.name not in seen:
            seen.add(o.name)
            out.append(o)
    return out


def tree_signature(coll):
    """Everything about the layer stack that is not part of a layer's own look."""
    sig = []

    def walk(parent, depth):
        for k, x in children(parent):
            if k == "L":
                p = x.vl
                sig.append(("L", depth, x.name, p.order, p.enabled, p.mask_mode,
                            p.mask_object.name if p.mask_object else "", p.mask_show))
            else:
                g = x.vl_group
                sig.append(("G", depth, x.name, g.order, g.enabled, round(g.opacity, 5), g.mask_mode,
                            g.mask_object.name if g.mask_object else "", g.mask_show))
                walk(x, depth + 1)
    walk(coll, 0)
    return tuple(sig)


def _mask_sources(coll):
    """Layers used as masks, and those to hide because they are only masks."""
    used, hidden = set(), set()
    for owner in [o.vl for o in layer_objects(coll)] + \
            [c.vl_group for c in coll.children_recursive]:
        if owner.mask_mode != "NONE" and owner.mask_object is not None:
            used.add(owner.mask_object.name)
            if not owner.mask_show:
                hidden.add(owner.mask_object.name)
    return used, hidden


def init_layer(obj):
    """Keep layer objects out of the way: they are guides, not render geometry."""
    if obj.type == "FONT":
        obj.hide_render = True
        obj.display_type = "WIRE"
        obj.visible_shadow = False


def ensure_orders(objs):
    top = max([o.vl.order for o in objs] + [0])
    for o in sorted(objs, key=lambda o: o.name):
        if o.vl.order <= 0:
            top += 1
            o.vl.order = top
            init_layer(o)


def _geo_close(a, b, tol=1e-5):
    pa, fa = a
    pb, fb = b
    if len(pa) != len(pb) or fa.shape != fb.shape:
        return False
    if not np.allclose(fa, fb, rtol=0, atol=tol):
        return False
    for x, y in zip(pa, pb):
        if x.shape != y.shape or not np.allclose(x, y, rtol=0, atol=tol):
            return False
    return True


def img_needs_write(img):
    # an image reloaded from disk / regenerated lost our pixels
    return not img.has_data


def _render_target(coll, obj, uv_map, img, depsgraph):
    """Layers of the set rasterized for one object. Returns a dict or an error string."""
    s = coll.vl_set
    W, H = img.size
    pm = get_position_map(obj, uv_map, W, H, depsgraph, s.margin)
    if pm is None:
        return f"{obj.name} has no faces."
    linear = bool(img.is_float)
    center = np.zeros(3)  # object origin, in its own space
    to_target = obj.matrix_world.inverted_safe()
    shapes = []
    surface = SurfaceQuery(obj, depsgraph)
    mask_used, mask_hidden = _mask_sources(coll)
    for o in layer_objects(coll):
        if not o.vl.enabled and o.name not in mask_used:
            continue
        try:
            sh = build_shape(o, depsgraph, center, linear, surface, to_target)
        except Exception as e:  # keep going on a bad layer
            print(f"[Nakan Vector Texturing] {o.name}: {e}")
            continue
        if sh:
            shapes.append(sh)

    cache = _FRAG_CACHE.setdefault(_frag_key(coll, obj), {"pm": None, "layers": {}})
    if cache["pm"] is not pm:
        cache["pm"] = pm
        cache["layers"] = {}
    by_name = {}
    fresh = {}
    recomputed = 0
    for sh in shapes:
        hit = cache["layers"].get(sh["name"])
        if hit and hit[0] == sh["key"] and _geo_close(hit[1], sh["geo"]):
            frags = hit[2]
            geo = hit[1]
        else:
            frags = core.shape_fragments(pm, sh)
            frags = core.apply_modifiers(pm, frags, sh["modifiers"])
            geo = sh["geo"]
            recomputed += 1
        fresh[sh["name"]] = (sh["key"], geo, frags)
        by_name[sh["name"]] = frags
    cache["layers"] = fresh

    masks = {}

    def mask_of(owner):
        if owner.mask_mode == "NONE" or owner.mask_object is None:
            return None
        name = owner.mask_object.name
        if name not in masks:
            src = by_name.get(name)
            masks[name] = core.mask_alpha(pm, src) if src is not None else None
        return masks[name]

    def masked(owner, frags):
        m = mask_of(owner)
        if m is None:
            return frags
        return core.apply_mask(pm, frags, m, owner.mask_mode == "OUTSIDE")

    def evaluate(parent):
        out = []
        for k, x in children(parent):
            if k == "L":
                if not x.vl.enabled or x.name in mask_hidden or x.name not in by_name:
                    continue
                fl = masked(x.vl, by_name[x.name])
            else:
                g = x.vl_group
                if not g.enabled or g.opacity <= 0:
                    continue
                sub = evaluate(x)
                if not sub:
                    continue
                ids, rgba = core.merge_group(pm, sub, g.opacity)
                fl = masked(g, [core.to_texel_fragment(ids, rgba)])
            if fl:
                out.append(fl)
        return out

    frag_lists = evaluate(coll)
    nlayers = sum(1 for o in layer_objects(coll) if o.vl.enabled and o.name not in mask_hidden)
    state = (obj.name, uv_map, img.name, W, H, tuple((sh["name"], sh["key"]) for sh in shapes),
             tree_signature(coll))
    return {"pm": pm, "frags": frag_lists, "shapes": nlayers, "recomputed": recomputed,
            "state": state, "cache": cache, "img": img, "linear": linear}


def render_set(coll, depsgraph=None, force=False):
    """Render one vector set into the image(s) of all its objects. Returns (ok, message)."""
    s = coll.vl_set
    targets = set_targets(coll)
    if not targets:
        return False, "Pick a target mesh object."
    for obj, _uv, img in targets:
        if img is None:
            return False, "Pick or create an image."
        if img.size[0] == 0 or img.size[1] == 0:
            return False, f"Image {img.name} has no pixels (missing file?)."
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()

    t0 = time.perf_counter()
    results = []
    for obj, uv_map, img in targets:
        r = _render_target(coll, obj, uv_map, img, depsgraph)
        if isinstance(r, str):
            return False, r
        results.append(r)
    t1 = time.perf_counter()

    recomputed = sum(r["recomputed"] for r in results)
    state = (tuple(r["state"] for r in results), bool(s.keep_background),
             tuple(round(x, 6) for x in s.background_color))
    images = []
    for r in results:
        if r["img"] not in images:
            images.append(r["img"])
    set_cache = _FRAG_CACHE.setdefault(coll.name + "\x00", {})
    if not force and recomputed == 0 and set_cache.get("state") == state and \
            not any(img_needs_write(i) for i in images):
        # e.g. the objects (with their parented layers) were only moved: nothing to redo
        return True, "unchanged"
    set_cache["state"] = state

    # one buffer per image; objects sharing an image are layered in order (main first)
    bufs = {}
    for img in images:
        W, H = img.size
        buf = np.zeros((W * H, 4), np.float32)
        if s.keep_background and s.background_color[3] > 0:
            buf[:] = _color(s.background_color, bool(img.is_float))
        bufs[img.name] = buf
    for r in results:
        pm = r["pm"]
        rgba = core.composite(pm, r["frags"])
        tex, rgba = core.resolve_texels(pm, rgba)
        buf = bufs[r["img"].name]
        a = rgba[:, 3:4]
        dst = buf[tex]
        out_a = a + dst[:, 3:4] * (1 - a)
        out_rgb = rgba[:, :3] * a + dst[:, :3] * dst[:, 3:4] * (1 - a)
        out_rgb = np.where(out_a > 0, out_rgb / np.maximum(out_a, 1e-9), 0)
        buf[tex] = np.concatenate([out_rgb, out_a], 1)
    for img in images:
        img.pixels.foreach_set(bufs[img.name].ravel())
        img.update()
        # make sure viewports / material previews pick the new pixels up
        try:
            img.update_tag()
        except Exception:
            pass
    t2 = time.perf_counter()
    W, H = images[0].size
    objs = f" · {len(targets)} objects" if len(targets) > 1 else ""
    s.last_info = (f"{results[0]['shapes']} layers ({recomputed} updated){objs} · {W}×{H} · "
                   f"{int((t2 - t0) * 1000)} ms")
    return True, f"Map {int((t1 - t0) * 1000)} ms, render {int((t2 - t1) * 1000)} ms"
