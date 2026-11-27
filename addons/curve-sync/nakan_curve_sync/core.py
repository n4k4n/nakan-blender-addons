"""NLA evaluation core (shared by the Graph Editor and VSE overlays)."""
import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

import numpy as np


# ---------------------------------------------------------------------------
# F-Curve access (slotted actions in 4.4+, legacy fallback)
# ---------------------------------------------------------------------------

def action_fcurves(action, slot):
    """Return the F-Curves of `action` that belong to `slot`."""
    if action is None:
        return []
    layers = getattr(action, "layers", None)
    if layers is not None and len(layers) and slot is not None:
        out = []
        for layer in layers:
            for st in layer.strips:
                cb = st.channelbag(slot)
                if cb is not None:
                    out.extend(cb.fcurves)
        return out
    return list(getattr(action, "fcurves", []))


def fcurve_map(action, slot):
    m = {}
    for fc in action_fcurves(action, slot):
        if fc.mute:
            continue
        m[(fc.data_path, fc.array_index)] = fc
    return m


# ---------------------------------------------------------------------------
# Property info: default value, array length, mix mode, unit
# ---------------------------------------------------------------------------

_info_cache = {}


def _split_path(path):
    # custom property:  ...["prop"]
    if path.endswith('"]'):
        i = path.rfind('["')
        return path[:i], None
    if "." in path:
        owner, prop = path.rsplit(".", 1)
        return owner, prop
    return "", path


def prop_info(id_data, path):
    """(length, defaults, mix, is_rotation_unit)"""
    key = (id_data.as_pointer(), path)
    hit = _info_cache.get(key)
    if hit is not None:
        return hit
    owner_path, prop = _split_path(path)
    length, defaults, mix, is_rot = 1, [0.0], "ADD", False
    try:
        if prop is not None:
            owner = id_data.path_resolve(owner_path) if owner_path else id_data
            p = owner.bl_rna.properties[prop]
            alen = getattr(p, "array_length", 0) or 0
            if alen > 0:
                length = alen
                defaults = [float(v) for v in p.default_array]
            else:
                defaults = [float(getattr(p, "default", 0.0))]
            is_rot = getattr(p, "unit", "NONE") == "ROTATION"
            if prop == "rotation_quaternion":
                mix = "QUAT"
            elif prop == "scale":
                mix = "SCALE"
        else:
            val = id_data.path_resolve(path)
            if hasattr(val, "__len__"):
                length = len(val)
                defaults = [0.0] * length
    except Exception:
        pass
    res = (length, defaults, mix, is_rot)
    _info_cache[key] = res
    return res


_limit_cache = {}


def prop_limits(id_data, path):
    """(kind, lo, hi): how Blender coerces an animated value when writing it.

    kind is "FLOAT", "INT" (truncated) or "BOOLEAN" (int(v) > 0).
    """
    key = (id_data.as_pointer(), path)
    hit = _limit_cache.get(key)
    if hit is not None:
        return hit
    kind, lo, hi = "FLOAT", -math.inf, math.inf
    owner_path, prop = _split_path(path)
    try:
        if prop is not None:
            owner = id_data.path_resolve(owner_path) if owner_path else id_data
            p = owner.bl_rna.properties[prop]
            if p.type == "BOOLEAN":
                kind = "BOOLEAN"
            elif p.type in {"INT", "ENUM"}:
                kind = "INT"
            if p.type in {"INT", "FLOAT"}:
                lo, hi = float(p.hard_min), float(p.hard_max)
            if isinstance(owner, bpy.types.ShapeKey) and prop == "value":
                lo, hi = owner.slider_min, owner.slider_max
        else:
            i = path.rfind('["')
            owner = id_data.path_resolve(path[:i]) if i > 0 else id_data
            name = path[i + 2:-2]
            val = owner[name]
            if isinstance(val, bool):
                kind = "BOOLEAN"
            elif isinstance(val, int):
                kind = "INT"
            try:
                ui = owner.id_properties_ui(name).as_dict()
                lo = float(ui.get("min", lo))
                hi = float(ui.get("max", hi))
            except Exception:
                pass
    except Exception:
        pass
    res = (kind, lo, hi)
    _limit_cache[key] = res
    return res


def _coerce(arr, limits):
    kind, lo, hi = limits
    if kind == "BOOLEAN":
        return (np.trunc(arr) > 0).astype(np.float64)
    if kind == "INT":
        arr = np.trunc(arr)
    if lo > -math.inf or hi < math.inf:
        arr = np.clip(arr, lo, hi)
    return arr


# ---------------------------------------------------------------------------
# NLA evaluation (mirrors Blender's anim_sys / nla evaluation rules)
# ---------------------------------------------------------------------------

def strip_action_time(strip, ctime):
    """Map scene time to action time for an action strip (NLATIME_CONVERT_EVAL)."""
    if strip.use_animated_time:
        fc = strip.fcurves.find("strip_time")
        if fc is not None:
            t = fc.evaluate(ctime)
            if strip.use_animated_time_cyclic:
                actstart, actend = strip.action_frame_start, strip.action_frame_end
                length = actend - actstart
                if abs(length) > 1e-6:
                    # matches Blender exactly (no actstart added back)
                    t = math.fmod(t - actstart, length)
            return t
    scale = abs(strip.scale) or 1.0
    actstart, actend = strip.action_frame_start, strip.action_frame_end
    actlength = actend - actstart
    if abs(actlength) < 1e-6:
        actlength = 1.0
    rep_is_int = abs(strip.repeat - math.floor(strip.repeat)) < 1e-6
    if strip.use_reverse:
        if abs(ctime - strip.frame_end) < 1e-6 and rep_is_int:
            return actstart
        return actend - math.fmod(ctime - strip.frame_start, actlength * scale) / scale
    if abs(ctime - strip.frame_end) < 1e-6 and rep_is_int:
        return actend
    return actstart + math.fmod(ctime - strip.frame_start, actlength * scale) / scale


def strip_influence(strip, ctime, real_time):
    if strip.use_animated_influence:
        fc = strip.fcurves.find("influence")
        if fc is not None:
            return fc.evaluate(real_time)
        return strip.influence
    bi, bo = abs(strip.blend_in), abs(strip.blend_out)
    if bi > 1e-6 and ctime <= strip.frame_start + bi:
        return abs(ctime - strip.frame_start) / bi
    if bo > 1e-6 and ctime >= strip.frame_end - bo:
        return abs(strip.frame_end - ctime) / bo
    return 1.0


def track_strip_at(strips, ctime):
    """Which strip of a track affects `ctime`, and the (clamped) time to use."""
    if not strips:
        return None, ctime
    for i, st in enumerate(strips):
        if st.frame_start <= ctime <= st.frame_end:
            return st, ctime
        if ctime < st.frame_start:
            if i == 0:
                if st.extrapolation == "HOLD":
                    return st, st.frame_start
                return None, ctime
            prev = strips[i - 1]
            if prev.extrapolation != "NOTHING":
                return prev, prev.frame_end
            return None, ctime
    last = strips[-1]
    if last.extrapolation != "NOTHING":
        return last, last.frame_end
    return None, ctime


# ---------------------------------------------------------------------------
# Vectorized blending (numpy). Rows = sampled frames, columns = array index.
# ---------------------------------------------------------------------------

def _qnormalize(q):
    n = np.sqrt((q * q).sum(axis=1, keepdims=True))
    out = np.empty_like(q)
    ok = n[:, 0] > 1e-12
    out[ok] = q[ok] / n[ok]
    out[~ok] = (1.0, 0.0, 0.0, 0.0)
    return out


def _qpow_normalized(q, fac):
    # Blender works in float32 here; acos is very sensitive near 1.0
    w = np.clip(q[:, 0].astype(np.float32).astype(np.float64), -1.0, 1.0)
    angle = fac * np.arccos(w)
    co, si = np.cos(angle), np.sin(angle)
    v = q[:, 1:]
    vl = np.sqrt((v * v).sum(axis=1))
    out = np.zeros_like(q)
    out[:, 0] = co
    ok = vl > 1e-12
    out[ok, 1:] = v[ok] * (si[ok] / vl[ok])[:, None]
    return out


def _qmul(a, b):
    aw, ax, ay, az = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    bw, bx, by, bz = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    return np.stack((
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ), axis=1)


def _blend(mode, mix, lower, sval, has, infl, defaults):
    """Blend strip values `sval` (m, n) into `lower` (m, n). infl: (m,)"""
    i = infl[:, None]
    if mode == "COMBINE":
        if mix == "QUAT" and lower.shape[1] == 4:
            s = np.where(has[None, :], sval, defaults[None, :])
            s = _qpow_normalized(_qnormalize(s), infl)
            # Blender normalizes the lower quaternion before combining
            return _qmul(_qnormalize(lower), s)
        out = lower.copy()
        if mix == "SCALE":
            base = np.where(np.abs(defaults) < 1e-12, 1.0, defaults)
            ratio = sval / base[None, :]
            powed = np.abs(ratio) ** i
            res = lower * np.where(ratio < 0.0, -powed, powed)
        else:
            res = lower + (sval - defaults[None, :]) * i
        out[:, has] = res[:, has]
        return out
    if mode == "ADD":
        res = lower + sval * i
    elif mode == "SUBTRACT":
        res = lower - sval * i
    elif mode == "MULTIPLY":
        res = i * (lower * sval) + (1.0 - i) * lower
    else:  # REPLACE
        res = lower * (1.0 - i) + sval * i
    out = lower.copy()
    out[:, has] = res[:, has]
    return out


# ---------------------------------------------------------------------------
# F-Curve sampling cache: only curves that changed get re-sampled
# ---------------------------------------------------------------------------

_sample_cache = {}
_SAMPLE_CACHE_MAX = 4000


def _rna_fingerprint(struct):
    vals = [struct.bl_rna.identifier]
    for prop in struct.bl_rna.properties:
        if prop.type in {"FLOAT", "INT", "BOOLEAN", "ENUM"}:
            try:
                v = getattr(struct, prop.identifier)
            except Exception:
                continue
            if prop.type in {"FLOAT", "INT", "BOOLEAN"} and getattr(prop, "is_array", False):
                v = tuple(v)
            elif isinstance(v, set):
                v = tuple(sorted(v))
            vals.append(v)
    for sub in ("control_points", "coefficients"):
        seq = getattr(struct, sub, None)
        if seq is not None:
            try:
                vals.append(tuple(tuple(x) if hasattr(x, "__len__") else
                                  (tuple(getattr(x, "co", ())) if hasattr(x, "co") else x)
                                  for x in seq))
            except Exception:
                pass
    return tuple(vals)


def fcurve_hash(fc):
    kp = fc.keyframe_points
    n = len(kp)
    parts = [n, fc.mute, len(fc.modifiers), fc.extrapolation]
    if n:
        buf = np.empty(n * 6, dtype=np.float32)
        kp.foreach_get("co", buf[: n * 2])
        kp.foreach_get("handle_left", buf[n * 2: n * 4])
        kp.foreach_get("handle_right", buf[n * 4:])
        parts.append(hash(buf.tobytes()))
        ip = np.empty(n, dtype=np.int32)
        ez = np.empty(n, dtype=np.int32)
        try:
            kp.foreach_get("interpolation", ip)
            kp.foreach_get("easing", ez)
            parts.append(hash(ip.tobytes()) ^ (hash(ez.tobytes()) * 3))
        except Exception:
            parts.append(tuple((k.interpolation, k.easing, k.back, k.amplitude, k.period)
                               for k in kp))
    for m in fc.modifiers:
        parts.append(_rna_fingerprint(m))
    return hash(tuple(parts))


def _sample(fc, times, fc_hashes):
    key = (fc.as_pointer(), fc_hashes.get(fc.as_pointer()), hash(times.tobytes()))
    hit = _sample_cache.get(key) if key[1] is not None else None
    if hit is not None:
        return hit
    ev = fc.evaluate
    vals = np.fromiter((ev(t) for t in times.tolist()), dtype=np.float64, count=len(times))
    if key[1] is not None:
        if len(_sample_cache) > _SAMPLE_CACHE_MAX:
            _sample_cache.clear()
        _sample_cache[key] = vals
    return vals


# ---------------------------------------------------------------------------
# NLA stack evaluation over many frames at once
# ---------------------------------------------------------------------------

class _Layer:
    """One evaluation layer: an NLA track, or the active action."""
    __slots__ = ("strips", "all_strips", "is_action", "action_info")

    def __init__(self, strips=None, all_strips=None, action_info=None):
        self.strips = strips or []            # strips that can be picked
        self.all_strips = all_strips or self.strips  # track order, for transitions
        self.is_action = action_info is not None
        self.action_info = action_info  # (fcmap, blend, influence, extrapolation, frange)


class _Ctx:
    __slots__ = ("paths", "infos", "fcmaps", "fc_hashes")

    def __init__(self, paths, infos, fc_hashes):
        self.paths = paths
        self.infos = infos
        self.fcmaps = {}
        self.fc_hashes = fc_hashes

    def fcmap(self, st):
        key = st.as_pointer()
        m = self.fcmaps.get(key)
        if m is None:
            m = fcurve_map(st.action, st.action_slot) if st.action is not None else {}
            self.fcmaps[key] = m
        return m


def _apply(cx, sub, fcmap, times, infl, blend):
    """Blend one action (sampled at `times`) into the rows of `sub`."""
    keep = infl > 0.0
    if not keep.any():
        return
    for p in cx.paths:
        n, defaults, mix, _rot = cx.infos[p]
        fcs = [fcmap.get((p, i)) for i in range(n)]
        if not any(fc is not None for fc in fcs):
            continue
        has = np.array([fc is not None for fc in fcs])
        m = len(times)
        sval = np.zeros((m, n))
        for i, fc in enumerate(fcs):
            if fc is not None:
                sval[:, i] = _sample(fc, times, cx.fc_hashes)
        d = np.asarray(defaults, dtype=np.float64)
        if keep.all():
            sub[p] = _blend(blend, mix, sub[p], sval, has, infl, d)
        else:
            rows = np.nonzero(keep)[0]
            sub[p][rows] = _blend(blend, mix, sub[p][rows], sval[rows], has, infl[rows], d)


def _eval_strip(cx, sub, st, cts, siblings):
    """Evaluate one strip (clip, transition or meta) at clamped times `cts`."""
    m = len(cts)
    if st.type == "TRANSITION":
        try:
            i = siblings.index(st)
        except ValueError:
            return
        if i == 0 or i + 1 >= len(siblings):
            return
        # 'Reverse' on a transition has no visible effect in Blender:
        # it always goes from the previous strip's end to the next's start.
        s1, s2 = siblings[i - 1], siblings[i + 1]
        length = st.frame_end - st.frame_start
        fac = (cts - st.frame_start) / length if length > 1e-6 else np.zeros(m)
        a = {p: v.copy() for p, v in sub.items()}
        b = {p: v.copy() for p, v in sub.items()}
        _eval_strip(cx, a, s1, np.full(m, s1.frame_end), siblings)
        _eval_strip(cx, b, s2, np.full(m, s2.frame_start), siblings)
        for p in cx.paths:
            sub[p] = a[p] + (b[p] - a[p]) * fac[:, None]
        return
    if st.type == "META":
        children = list(st.strips)
        _eval_track(cx, sub, [c for c in children if not c.mute], children, cts)
        return
    if st.type != "CLIP":
        return
    ct_list = cts.tolist()
    infl = np.array([strip_influence(st, c, c) for c in ct_list])
    times = np.array([strip_action_time(st, c) for c in ct_list])
    _apply(cx, sub, cx.fcmap(st), times, infl, st.blend_type)


def _eval_track(cx, sub, strips, siblings, frames):
    """Evaluate the strips of one track into `sub` (rows = frames)."""
    groups = {}
    for row, f in enumerate(frames.tolist()):
        st, ct = track_strip_at(strips, f)
        if st is None:
            continue
        g = groups.get(st.as_pointer())
        if g is None:
            g = groups[st.as_pointer()] = (st, [], [])
        g[1].append(row)
        g[2].append(ct)
    for st, rows, cts in groups.values():
        rows = np.array(rows)
        if len(rows) == len(frames):
            _eval_strip(cx, sub, st, np.array(cts), siblings)
        else:
            part = {p: v[rows] for p, v in sub.items()}
            _eval_strip(cx, part, st, np.array(cts), siblings)
            for p in cx.paths:
                sub[p][rows] = part[p]


def build_layers(ob, include_upper=True):
    ad = ob.animation_data
    if ad is None:
        return []
    layers = []
    tweak = ad.use_tweak_mode
    if ad.use_nla or tweak:
        tracks = list(ad.nla_tracks)
        solo = [t for t in tracks if t.is_solo]
        tweak_track_idx = None
        tweak_strip = None
        if tweak:
            for ti, t in enumerate(tracks):
                for st in t.strips:
                    if st.active and st.action == ad.action:
                        tweak_track_idx, tweak_strip = ti, st
                        break
                if tweak_strip is not None:
                    break
            if tweak_strip is None:
                for ti, t in enumerate(tracks):
                    for st in t.strips:
                        if st.action == ad.action:
                            tweak_track_idx, tweak_strip = ti, st
                            break
                    if tweak_strip is not None:
                        break
        for ti, t in enumerate(tracks):
            # mute / solo apply to the tweaked track as well (matches Blender)
            if solo and not t.is_solo:
                continue
            if t.mute:
                continue
            if tweak and tweak_track_idx is not None:
                if ti > tweak_track_idx and not include_upper:
                    continue
                if ti == tweak_track_idx:
                    if not tweak_strip.mute:
                        layers.append(_Layer(strips=[tweak_strip]))
                    continue
            all_strips = list(t.strips)
            strips = [s for s in all_strips if not s.mute]
            if strips:
                layers.append(_Layer(strips=strips, all_strips=all_strips))
    if not tweak and ad.action is not None:
        fr = tuple(ad.action.frame_range)
        if not ad.use_nla or len(ad.nla_tracks) == 0:
            # Blender evaluates the action directly here: no blending,
            # influence or extrapolation settings are applied.
            info = (fcurve_map(ad.action, ad.action_slot), "REPLACE", 1.0, "HOLD", fr)
        else:
            info = (fcurve_map(ad.action, ad.action_slot),
                    ad.action_blend_type, ad.action_influence,
                    ad.action_extrapolation, fr)
        layers.append(_Layer(action_info=info))
    return layers


def evaluate_paths(ob, paths, frames, include_upper=True, fc_hashes=None):
    """Final NLA-combined values.

    Returns {data_path: ndarray of shape (len(frames), array_length)}.
    `fc_hashes` ({fcurve pointer: hash}) enables re-using cached samples.
    """
    layers = build_layers(ob, include_upper)
    infos = {p: prop_info(ob, p) for p in paths}
    cx = _Ctx(paths, infos, fc_hashes or {})
    F = np.asarray(frames, dtype=np.float64)
    m = len(F)
    snap = {p: np.tile(np.asarray(infos[p][1], dtype=np.float64), (m, 1)) for p in paths}
    for lay in layers:
        if lay.is_action:
            fcmap, blend, infl, extrap, fr = lay.action_info
            if infl <= 0.0:
                continue
            w = np.full(m, float(infl))
            if extrap == "NOTHING":
                w[(F < fr[0]) | (F > fr[1])] = 0.0
            _apply(cx, snap, fcmap, F, w, blend)
        else:
            _eval_track(cx, snap, lay.strips, lay.all_strips, F)
    # Blender clamps / converts the final value when writing it to the property
    for p in paths:
        snap[p] = _coerce(snap[p], prop_limits(ob, p))
    return snap

def _signature(idb, layers_paths):
    """Fingerprint of everything that can change the result.

    Returns (signature, {fcurve pointer: fcurve hash}).
    """
    ad = idb.animation_data
    sig = [ad.use_nla, ad.use_tweak_mode, ad.action_blend_type,
           round(ad.action_influence, 6), ad.action_extrapolation,
           ad.action.as_pointer() if ad.action else 0]
    fcs = []

    def strips_sig(strips):
        for st in strips:
            sig.extend((st.as_pointer(), st.type, st.mute, st.active, st.blend_type,
                        st.extrapolation, st.frame_start, st.frame_end,
                        st.action_frame_start, st.action_frame_end, st.repeat,
                        st.scale, st.blend_in, st.blend_out, st.use_reverse,
                        st.use_animated_influence, st.use_animated_time,
                        st.use_animated_time_cyclic, st.influence,
                        st.action.as_pointer() if st.action else 0))
            fcs.extend(st.fcurves)
            if st.action is not None:
                fcs.extend(action_fcurves(st.action, st.action_slot))
            if st.type == "META":
                strips_sig(st.strips)

    for t in ad.nla_tracks:
        sig.extend((t.as_pointer(), t.mute, t.is_solo))
        strips_sig(t.strips)
    if ad.action is not None and not ad.use_tweak_mode:
        fcs.extend(action_fcurves(ad.action, ad.action_slot))
    hashes = {}
    for fc in fcs:
        if fc.data_path not in layers_paths and fc.data_path not in ("influence", "strip_time"):
            continue
        h = fcurve_hash(fc)
        hashes[fc.as_pointer()] = h
        sig.append((fc.as_pointer(), h))
    return hash(tuple(sig)), hashes


def _correct_bezpart(v1, v2, v3, v4):
    """BKE_fcurve_correct_bezpart: keep handles from overlapping in time."""
    h1 = (v1[0] - v2[0], v1[1] - v2[1])
    h2 = (v4[0] - v3[0], v4[1] - v3[1])
    length = v4[0] - v1[0]
    len1 = abs(h1[0])
    len2 = abs(h2[0])
    if len1 + len2 == 0.0:
        return v2, v3
    if len1 + len2 > length:
        fac = length / (len1 + len2)
        v2 = (v1[0] - fac * h1[0], v1[1] - fac * h1[1])
        v3 = (v4[0] - fac * h2[0], v4[1] - fac * h2[1])
    return v2, v3


def _normalization(fc, scene):
    """Same (factor, offset) as the Graph Editor's 'Normalize' option."""
    kps = fc.keyframe_points
    use_prv = scene.use_preview_range
    p0, p1 = scene.frame_preview_start, scene.frame_preview_end
    lo, hi = math.inf, -math.inf
    prev = None
    for i, k in enumerate(kps):
        x, y = k.co
        if use_prv and not (p0 <= x <= p1):
            continue
        if i == 0 or prev is None:
            lo, hi = min(lo, y), max(hi, y)
        elif prev.interpolation not in {"BEZIER", "BACK", "ELASTIC"}:
            lo, hi = min(lo, y), max(hi, y)
        else:
            px, py = prev.co
            resol = min(int(5.0 * math.hypot(x - px, y - py)), 32)
            if resol < 2:
                lo, hi = min(lo, py), max(hi, py)
            elif prev.interpolation == "BEZIER":
                v1 = (px, py)
                v4 = (x, y)
                v2, v3 = _correct_bezpart(v1, tuple(prev.handle_right), tuple(k.handle_left), v4)
                for j in range(resol + 1):
                    u = j / resol
                    a = (1 - u) ** 3
                    b = 3 * u * (1 - u) ** 2
                    c = 3 * u * u * (1 - u)
                    d = u ** 3
                    yy = a * v1[1] + b * v2[1] + c * v3[1] + d * v4[1]
                    lo, hi = min(lo, yy), max(hi, yy)
            else:
                step = (x - px) / resol
                for j in range(resol + 1):
                    yy = fc.evaluate(px + step * j)
                    lo, hi = min(lo, yy), max(hi, yy)
        prev = k
    if hi > lo:
        rng = hi - lo
        factor = 2.0 / rng if rng > 1.1920929e-07 else 1.0
        return factor, -lo - rng / 2.0
    if hi == lo:
        return 1.0, -lo
    return 1.0, 0.0


_upper_guess = {}


def _resolve_values(idb, paths):
    out = {}
    for p in paths:
        try:
            v = idb.path_resolve(p)
        except Exception:
            return None
        try:
            out[p] = np.array([float(x) for x in v])
        except TypeError:
            out[p] = np.array([float(v)])
    return out


_upper_checked = {}


def _include_upper(idb, paths, mode, frame, sig=None):
    """Full vs Lower stack. Blender does not expose this flag, so in Auto
    mode it is inferred by comparing both predictions with the current pose.
    The check only re-runs when the animation or the current frame changed."""
    if mode != "AUTO":
        return mode == "FULL"
    ad = idb.animation_data
    key = idb.as_pointer()
    if not ad.use_tweak_mode:
        return _upper_guess.get(key, True)
    stamp = (sig, frame)
    if sig is not None and _upper_checked.get(key) == stamp:
        return _upper_guess.get(key, True)
    _upper_checked[key] = stamp
    paths = paths[:16]
    real = _resolve_values(idb, paths)
    if real is None:
        return _upper_guess.get(key, True)
    full = evaluate_paths(idb, paths, [frame], True)
    lower = evaluate_paths(idb, paths, [frame], False)
    ef = max(float(np.abs(full[p][0] - real[p]).max()) for p in paths)
    el = max(float(np.abs(lower[p][0] - real[p]).max()) for p in paths)
    if min(ef, el) > 1e-3:
        # pose not re-evaluated yet (or driven by something else): retry later
        _upper_checked.pop(key, None)
    elif abs(ef - el) > 1e-4:
        _upper_guess[key] = ef < el
    return _upper_guess.get(key, True)


def clear_caches():
    for c in (_info_cache, _limit_cache, _sample_cache, _upper_guess, _upper_checked):
        c.clear()
