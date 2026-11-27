"""Video Sequencer overlay: draw a scene's Sync Curves over its scene strips."""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np

from .core import evaluate_paths, action_fcurves, _signature
from .marks import draw_curve_list, redraw_all


STRIP_BOTTOM = 0.05   # Blender draws strips from channel + 0.05 ...
STRIP_TOP = 0.95      # ... to channel + 0.95


def _has(strip, name):
    return name in strip.bl_rna.properties


def strip_range(s):
    """Visible (trimmed) range of a strip on the timeline."""
    if _has(s, "left_handle"):
        return float(s.left_handle), float(s.right_handle)
    return float(s.frame_final_start), float(s.frame_final_end)


def strip_mapping(s):
    """(content_start, content_len, scene_start, offset, reversed, speed)"""
    if _has(s, "content_start"):
        cstart = float(s.content_start)
        clen = float(s.content_duration)
        offs = float(s.animation_offset_start)
        offe = float(s.animation_offset_end)
    else:
        cstart = float(s.frame_start)
        clen = float(s.frame_duration)
        offs = float(getattr(s, "animation_offset_start", 0.0))
        offe = float(getattr(s, "animation_offset_end", 0.0))
    sub = s.scene
    src_len = float(sub.frame_end - sub.frame_start + 1) - offs - offe
    # uniform speed change (retiming or a different frame rate)
    speed = src_len / clen if clen > 0 and src_len > 0 else 1.0
    if abs(speed - 1.0) < 1e-6:
        speed = 1.0
    rev = bool(getattr(s, "use_reverse_frames", False))
    return cstart, clen, float(sub.frame_start), offs, rev, speed


def to_scene_time(t, mapping):
    cstart, clen, sstart, offs, rev, speed = mapping
    local = (t - cstart) * speed
    if rev:
        local = (clen * speed - 1.0) - local
    return sstart + offs + local


def to_timeline(sf, mapping):
    cstart, clen, sstart, offs, rev, speed = mapping
    local = sf - sstart - offs
    if rev:
        local = (clen * speed - 1.0) - local
    return cstart + local / speed


def strip_warnings(s, parent_scene=None):
    keys = getattr(s, "retiming_keys", None)
    if keys is not None and len(keys) > 2:
        return ["Speed changes inside the strip are not followed;",
                "curves use the average speed"]
    return []


# ---------------------------------------------------------------------------
# Keyframe times (scene time) of one animated property
# ---------------------------------------------------------------------------

def _strip_key_times(st, path, idx, out):
    if st.mute:
        return
    if st.type == "META":
        for c in st.strips:
            _strip_key_times(c, path, idx, out)
        return
    if st.type != "CLIP" or st.action is None:
        return
    fc = None
    for f in action_fcurves(st.action, st.action_slot):
        if f.data_path == path and f.array_index == idx:
            fc = f
            break
    if fc is None:
        return
    a0, a1 = st.action_frame_start, st.action_frame_end
    alen = a1 - a0
    scale = abs(st.scale) or 1.0
    reps = max(1, int(math.ceil(st.repeat - 1e-6)))
    for k in fc.keyframe_points:
        x = k.co[0]
        if x < a0 - 1e-4 or x > a1 + 1e-4:
            continue
        local = (a1 - x) if st.use_reverse else (x - a0)
        for r in range(reps):
            t = st.frame_start + (local + r * alen) * scale
            if st.frame_start - 1e-4 <= t <= st.frame_end + 1e-4:
                out.append(t)


def key_times(idb, path, idx):
    ad = idb.animation_data
    out = []
    if ad is None:
        return out
    if ad.action is not None and not ad.use_tweak_mode:
        for fc in action_fcurves(ad.action, ad.action_slot):
            if fc.data_path == path and fc.array_index == idx:
                out.extend(k.co[0] for k in fc.keyframe_points)
    if ad.use_nla or ad.use_tweak_mode:
        for t in ad.nla_tracks:
            if t.mute:
                continue
            for st in t.strips:
                _strip_key_times(st, path, idx, out)
    return sorted(set(round(t, 4) for t in out))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

_handle = None
_cache = {}


def _strips_shown(ed):
    stack = getattr(ed, "meta_stack", None)
    if stack is not None and len(stack):
        return stack[-1].strips
    return getattr(ed, "strips", None) or getattr(ed, "sequences", [])


def drawable_strips(ed):
    """[(scene strip, channel to draw on, visible start, visible end)].

    Scene strips inside a (closed) meta strip are drawn on the meta, clipped
    to the meta's visible range.
    """
    out = []

    def walk(strips, ch, lo, hi, depth):
        for s in strips:
            L, R = strip_range(s)
            L, R = max(L, lo), min(R, hi)
            if R <= L:
                continue
            if s.type == "SCENE":
                if getattr(s, "nkcs_show_curves", False) and s.scene is not None and not s.mute:
                    out.append((s, s.channel if ch is None else ch, L, R))
            elif s.type == "META" and depth < 8:
                walk(s.strips, s.channel if ch is None else ch, L, R, depth + 1)

    walk(_strips_shown(ed), None, -math.inf, math.inf, 0)
    return out


def scene_strips_in(s, depth=0):
    if s.type == "SCENE":
        return [s]
    if s.type == "META" and depth < 8:
        return [c for x in s.strips for c in scene_strips_in(x, depth + 1)]
    return []


def _sample_strip(s, marks, step, L=None, R=None):
    """{mark index: (timeline xs, values, key xs)} for one scene strip, cached."""
    if L is None:
        L, R = strip_range(s)
    mapping = strip_mapping(s)
    ts = np.arange(L, R + 1e-6, step, dtype=np.float64)
    if len(ts) < 2:
        ts = np.array([L, R])
    sf = np.array([to_scene_time(t, mapping) for t in ts.tolist()])
    by_id = {}
    for i, m in enumerate(marks):
        by_id.setdefault(m.id.as_pointer(), (m.id, []))[1].append((i, m))
    out = {}
    for _ptr, (idb, items) in by_id.items():
        paths = sorted({m.data_path for _i, m in items})
        try:
            sig, hashes = _signature(idb, set(paths))
        except Exception:
            continue
        key = (s.as_pointer(), idb.as_pointer(), tuple(paths), L, R, mapping, step)
        hit = _cache.get(key)
        if hit is None or hit[0] != sig:
            try:
                vals = evaluate_paths(idb, paths, sf.tolist(), True, hashes)
            except Exception as e:
                print("Nakan Curve Sync:", e)
                continue
            keys = {}
            for _i, m in items:
                try:
                    kt = key_times(idb, m.data_path, m.array_index)
                except Exception:
                    kt = []
                keys[(m.data_path, m.array_index)] = [to_timeline(k, mapping) for k in kt]
            if len(_cache) > 512:
                _cache.clear()
            hit = (sig, vals, keys)
            _cache[key] = hit
        _sig, vals, keys = hit
        for i, m in items:
            series = vals.get(m.data_path)
            if series is None or m.array_index >= series.shape[1]:
                continue
            out[i] = (ts, series[:, m.array_index], keys.get((m.data_path, m.array_index), []))
    return out


def _diamond(x, y, r):
    return [(x, y - r), (x + r, y), (x, y + r), (x, y - r), (x, y + r), (x - r, y)]


def _draw():
    ctx = bpy.context
    space = ctx.space_data
    region = ctx.region
    if space is None or space.type != "SEQUENCE_EDITOR" or space.view_type == "PREVIEW":
        return
    try:
        st = ctx.window_manager.nkcs_vse
    except Exception:
        return
    if not st.enabled:
        return
    scene = ctx.scene
    ed = scene.sequence_editor
    if ed is None:
        return
    v2d = region.view2d
    x0, y0 = v2d.region_to_view(0, 0)
    x1, y1 = v2d.region_to_view(region.width, region.height)
    if x1 <= x0 or y1 <= y0:
        return
    sx = region.width / (x1 - x0)
    sy = region.height / (y1 - y0)
    # ~1 sample per 2 px, snapped to powers of two so zooming reuses the cache
    step = max(1.0 / max(1, st.substeps), 2.0 / sx)
    step = 2.0 ** math.floor(math.log2(step))

    lines_pos, lines_col, tri_pos, tri_col = [], [], [], []
    for s, ch, L, R in drawable_strips(ed):
        sub = s.scene
        if R < x0 or L > x1 or ch + 1 < y0 or ch > y1:
            continue
        marks = [m for m in sub.nkcs_curves
                 if m.show and m.id is not None and getattr(m.id, "animation_data", None)]
        if not marks:
            continue
        samples = _sample_strip(s, marks, step, L, R)
        pad = (STRIP_TOP - STRIP_BOTTOM) * (1.0 - st.height) * 0.5
        yb = ch + STRIP_BOTTOM + pad
        yt = ch + STRIP_TOP - pad
        if st.backdrop > 0.0:
            rx0, rx1 = (max(L, x0) - x0) * sx, (min(R, x1) - x0) * sx
            ry0, ry1 = (yb - y0) * sy, (yt - y0) * sy
            tri_pos.extend([(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry0), (rx1, ry1), (rx0, ry1)])
            tri_col.extend([(0.0, 0.0, 0.0, st.backdrop)] * 6)
        for i, (ts, vals, keys) in samples.items():
            m = marks[i]
            lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
            if hi - lo > 1e-9:
                norm = (vals - lo) / (hi - lo)
            else:
                norm = np.full_like(vals, 0.5)
            xs = (ts - x0) * sx
            ys = (yb + norm * (yt - yb) - y0) * sy
            vis = (ts >= x0 - 2 * step) & (ts <= x1 + 2 * step) & np.isfinite(ys)
            if vis.sum() < 2:
                continue
            pts = np.column_stack((xs[vis], ys[vis])).astype(np.float32)
            seg = np.empty((2 * (len(pts) - 1), 2), dtype=np.float32)
            seg[0::2] = pts[:-1]
            seg[1::2] = pts[1:]
            col = (*m.color, st.alpha)
            lines_pos.append(seg)
            lines_col.append(np.tile(np.asarray(col, dtype=np.float32), (len(seg), 1)))
            if st.show_keys and keys:
                kx = np.array([k for k in keys if L - 1e-4 <= k <= R + 1e-4 and x0 <= k <= x1])
                if len(kx):
                    ky = np.interp(kx, ts, ys)
                    r = st.key_size
                    for x, y in zip(((kx - x0) * sx).tolist(), ky.tolist()):
                        tri_pos.extend(_diamond(x, y, r))
                        tri_col.extend([(*m.color, 1.0)] * 6)

    if not lines_pos and not tri_pos:
        return
    gpu.state.blend_set("ALPHA")
    if tri_pos:
        flat = gpu.shader.from_builtin("FLAT_COLOR")
        # split: backdrop quads (alpha < 1) under the lines, diamonds over them
        back = [(p, c) for p, c in zip(tri_pos, tri_col) if c[3] < 1.0]
        front = [(p, c) for p, c in zip(tri_pos, tri_col) if c[3] >= 1.0]
        if back:
            batch_for_shader(flat, "TRIS", {"pos": [p for p, _c in back],
                                            "color": [c for _p, c in back]}).draw(flat)
    if lines_pos:
        shader = gpu.shader.from_builtin("POLYLINE_FLAT_COLOR")
        shader.uniform_float("viewportSize", (region.width, region.height))
        shader.uniform_float("lineWidth", st.width)
        batch_for_shader(shader, "LINES", {"pos": np.concatenate(lines_pos),
                                           "color": np.concatenate(lines_col)}).draw(shader)
    if tri_pos and front:
        batch_for_shader(flat, "TRIS", {"pos": [p for p, _c in front],
                                        "color": [c for _p, c in front]}).draw(flat)
    gpu.state.blend_set("NONE")


# ---------------------------------------------------------------------------
# Redraw when the animation changes
# ---------------------------------------------------------------------------

def _on_depsgraph(scene, depsgraph):
    wm = bpy.context.window_manager
    if wm is None or not getattr(wm, "nkcs_vse", None) or not wm.nkcs_vse.enabled:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "SEQUENCE_EDITOR":
                area.tag_redraw()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class NKCS_VSESettings(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Show Sync Curves", default=True, update=redraw_all,
        description="Draw the Sync Curves of each scene strip that has them turned on")
    show_keys: bpy.props.BoolProperty(
        name="Keyframes", default=True, update=redraw_all,
        description="Draw a dot on the curve at each keyframe")
    width: bpy.props.FloatProperty(
        name="Line Width", default=2.0, min=1.0, max=6.0, update=redraw_all)
    alpha: bpy.props.FloatProperty(
        name="Opacity", default=0.95, min=0.05, max=1.0, subtype="FACTOR", update=redraw_all)
    height: bpy.props.FloatProperty(
        name="Height", default=0.7, min=0.1, max=1.0, subtype="FACTOR", update=redraw_all,
        description="How much of the strip height the curves use")
    backdrop: bpy.props.FloatProperty(
        name="Backdrop", default=0.35, min=0.0, max=0.9, subtype="FACTOR", update=redraw_all,
        description="Darken the strip behind the curves so they read better")
    key_size: bpy.props.FloatProperty(
        name="Key Size", default=3.5, min=1.5, max=8.0, update=redraw_all)
    substeps: bpy.props.IntProperty(
        name="Subframe Samples", default=1, min=1, max=8, update=redraw_all,
        description="Samples per frame when zoomed in")


class NKCS_OT_toggle_strips(bpy.types.Operator):
    bl_idname = "nkcs.toggle_strip_curves"
    bl_label = "Toggle Sync Curves"
    bl_description = "Show or hide the Sync Curves on the selected scene strips"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene.sequence_editor is not None

    def execute(self, context):
        ed = context.scene.sequence_editor
        sel = [c for s in _strips_shown(ed) if s.select for c in scene_strips_in(s)]
        if not sel:
            self.report({"WARNING"}, "Select one or more scene strips")
            return {"CANCELLED"}
        state = not all(s.nkcs_show_curves for s in sel)
        for s in sel:
            s.nkcs_show_curves = state
        redraw_all()
        return {"FINISHED"}


def _active_strip(context):
    ed = context.scene.sequence_editor
    if ed is None:
        return None
    return getattr(ed, "active_strip", None)


class NKCS_PT_vse(bpy.types.Panel):
    bl_label = "Sync Curves"
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Curve Sync"

    @classmethod
    def poll(cls, context):
        return context.space_data.view_type != "PREVIEW"

    def draw_header(self, context):
        self.layout.prop(context.window_manager.nkcs_vse, "enabled", text="")

    def draw(self, context):
        lay = self.layout
        act = _active_strip(context)
        strips = scene_strips_in(act) if act is not None else []
        strips = [x for x in strips if x.scene is not None]
        if not strips:
            lay.label(text="Select a scene strip", icon="INFO")
        for s in strips:
            box = lay.box()
            row = box.row()
            row.prop(s, "nkcs_show_curves", text=f"Show on '{s.name}'", toggle=True,
                     icon="IPO_EASE_IN_OUT")
            box.label(text=f"Curves marked in '{s.scene.name}':", icon="SCENE_DATA")
            if len(s.scene.nkcs_curves):
                draw_curve_list(box, s.scene)
            else:
                col = box.column(align=True)
                col.label(text="None yet. In that scene, select channels")
                col.label(text="in the Graph Editor > Mark as Sync Curves.")
            for w in strip_warnings(s, context.scene):
                box.label(text=w, icon="ERROR")
        lay.operator("nkcs.toggle_strip_curves", icon="RESTRICT_SELECT_OFF")


class NKCS_PT_vse_display(bpy.types.Panel):
    bl_label = "Display"
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Curve Sync"
    bl_parent_id = "NKCS_PT_vse"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        st = context.window_manager.nkcs_vse
        col = self.layout.column()
        col.active = st.enabled
        col.prop(st, "show_keys")
        col.prop(st, "height")
        col.prop(st, "backdrop")
        col.prop(st, "width")
        col.prop(st, "alpha")
        col.prop(st, "key_size")
        col.prop(st, "substeps")


def _strip_menu(self, context):
    self.layout.separator()
    self.layout.operator("nkcs.toggle_strip_curves", icon="IPO_EASE_IN_OUT")


classes = (NKCS_VSESettings, NKCS_OT_toggle_strips, NKCS_PT_vse, NKCS_PT_vse_display)


def _strip_type():
    return getattr(bpy.types, "Strip", None) or getattr(bpy.types, "Sequence")


def register():
    global _handle
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.nkcs_vse = bpy.props.PointerProperty(type=NKCS_VSESettings)
    _strip_type().nkcs_show_curves = bpy.props.BoolProperty(
        name="Show Sync Curves", default=False, update=redraw_all,
        description="Draw the Sync Curves marked in this strip's scene over the strip")
    _handle = bpy.types.SpaceSequenceEditor.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    menu = getattr(bpy.types, "SEQUENCER_MT_context_menu", None)
    if menu is not None:
        menu.append(_strip_menu)


def unregister():
    global _handle
    menu = getattr(bpy.types, "SEQUENCER_MT_context_menu", None)
    if menu is not None:
        try:
            menu.remove(_strip_menu)
        except Exception:
            pass
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    if _handle is not None:
        bpy.types.SpaceSequenceEditor.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    del _strip_type().nkcs_show_curves
    del bpy.types.WindowManager.nkcs_vse
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    _cache.clear()
