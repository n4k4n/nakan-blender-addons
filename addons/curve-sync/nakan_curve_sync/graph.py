"""Graph Editor overlay: final (NLA-combined) curve of each visible channel."""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
import numpy as np

from .core import (action_fcurves, prop_info, evaluate_paths, _signature,
                   _normalization, _include_upper, _upper_guess)

# ---------------------------------------------------------------------------
# Graph Editor overlay
# ---------------------------------------------------------------------------

_cache = {}
_handle = None


def _settings():
    return bpy.context.window_manager.nkcs_graph


def _bone_shown(id_data, path, ds):
    """Mirror the Graph Editor's bone filters (Only Show Selected / hidden)."""
    if not path.startswith('pose.bones["'):
        return True
    pose = getattr(id_data, "pose", None)
    if pose is None:
        return True
    name = path[len('pose.bones["'):].split('"]', 1)[0]
    pb = pose.bones.get(name)
    if pb is None:
        return True
    bone = pb.bone
    if not ds.show_hidden:
        hidden = getattr(pb, "hide", None)
        if hidden is None:
            hidden = getattr(bone, "hide", False)
        if hidden:
            return False
        cols = getattr(bone, "collections", None)
        if cols is not None and len(cols) and not any(
                getattr(c, "is_visible_effectively", getattr(c, "is_visible", True)) for c in cols):
            return False
    if ds.show_only_selected:
        sel = getattr(pb, "select", None)
        if sel is None:
            sel = getattr(bone, "select", True)
        if not sel:
            return False
    return True


def _shown_fcurves(id_data, space, only_selected):
    ad = getattr(id_data, "animation_data", None)
    if ad is None or ad.action is None:
        return []
    ds = space.dopesheet
    res = []
    for fc in action_fcurves(ad.action, ad.action_slot):
        if fc.hide:
            continue
        if only_selected and not fc.select:
            continue
        if not _bone_shown(id_data, fc.data_path, ds):
            continue
        res.append(fc)
    return res


def _shown_ids(ctx, space):
    """Datablocks whose channels the Graph Editor is showing."""
    ds = space.dopesheet
    vl = ctx.view_layer
    if ds.show_only_selected:
        objs = list(vl.objects.selected)
    else:
        objs = [o for o in vl.objects if ds.show_hidden or o.visible_get()]
    ids = []
    seen = set()
    for ob in objs:
        cands = [ob]
        data = getattr(ob, "data", None)
        if data is not None:
            cands.append(data)
            keys = getattr(data, "shape_keys", None)
            if keys is not None:
                cands.append(keys)
        for idb in cands:
            ad = getattr(idb, "animation_data", None)
            if ad is None or ad.action is None:
                continue
            ptr = idb.as_pointer()
            if ptr in seen:
                continue
            seen.add(ptr)
            ids.append(idb)
    return ids


def _visible_groups(ctx, space, only_selected):
    """[(owner datablock, [fcurves])] for the curves the Graph Editor shows.

    Uses the editor's own channel filtering (context.visible_fcurves) so the
    overlay always matches what is drawn; falls back to our own filters.
    """
    owners_ok = {idb.as_pointer(): idb for idb in _shown_ids(ctx, space)}
    fcs = getattr(ctx, "visible_fcurves", None)
    if fcs is None:
        out = []
        for idb in owners_ok.values():
            shown = _shown_fcurves(idb, space, only_selected)
            if shown:
                out.append((idb, shown))
        return out
    slot_of = {}
    done_actions = set()
    groups = {}
    for fc in fcs:
        act = fc.id_data
        if not isinstance(act, bpy.types.Action):
            continue  # NLA control curves (influence / strip time) etc.
        if only_selected and not fc.select:
            continue
        if act.as_pointer() not in done_actions:
            done_actions.add(act.as_pointer())
            for layer in act.layers:
                for strip in layer.strips:
                    for cb in strip.channelbags:
                        for f in cb.fcurves:
                            slot_of[f.as_pointer()] = cb.slot
        slot = slot_of.get(fc.as_pointer())
        if slot is None:
            continue
        for user in slot.users():
            g = owners_ok.get(user.as_pointer())
            if g is None:
                continue
            ad = g.animation_data
            if ad is None or ad.action != act or ad.action_slot != slot:
                continue
            groups.setdefault(g.as_pointer(), (g, []))[1].append(fc)
    return list(groups.values())

def _draw():
    ctx = bpy.context
    space = ctx.space_data
    region = ctx.region
    if space is None or space.type != "GRAPH_EDITOR" or space.mode != "FCURVES":
        return
    try:
        st = _settings()
    except Exception:
        return
    if not st.enabled:
        return
    scene = ctx.scene
    v2d = region.view2d
    x0, y0 = v2d.region_to_view(0, 0)
    x1, y1 = v2d.region_to_view(region.width, region.height)
    if x1 <= x0 or y1 == y0:
        return
    sx = region.width / (x1 - x0)
    sy = region.height / (y1 - y0)

    # Frames to sample: ~1 sample per 2 px. The sample spacing and the
    # sampled window are snapped to powers of two, and the window covers
    # three screens, so panning and small zooms reuse the cached result.
    span = max(1e-3, x1 - x0)
    step = max(1.0, span / max(1.0, region.width / 2.0))
    if step <= 1.0:
        step = 1.0 / max(1, int(st.substeps))
    step = 2.0 ** math.floor(math.log2(step))
    block = 2.0 ** math.ceil(math.log2(span))
    g0 = math.floor(x0 / block) * block - block
    nframes = int(3 * block / step) + 1
    while nframes > 6000:
        step *= 2.0
        nframes = int(3 * block / step) + 1
    frames = [g0 + i * step for i in range(nframes)]
    fstart, fend = g0, g0 + 3 * block

    deg = scene.unit_settings.system_rotation == "DEGREES"
    normalize = space.use_normalization

    try:
        groups = _visible_groups(ctx, space, st.only_selected)
    except Exception as e:
        print("Nakan Curve Sync:", e)
        return
    if not groups:
        return

    seg_pos = []
    seg_col = []

    F = np.asarray(frames, dtype=np.float64)
    vis = (F >= x0 - 2 * step) & (F <= x1 + 2 * step)
    xs = (F[vis] - x0) * sx
    for ob, fcs in groups:
        paths = sorted({fc.data_path for fc in fcs})
        try:
            sig, fc_hashes = _signature(ob, set(paths))
        except Exception:
            continue
        try:
            upper = _include_upper(ob, paths, st.stack_mode, scene.frame_current_final, sig)
        except Exception:
            upper = True
        key = (tuple(paths), fstart, fend, round(step, 4), upper)
        entry = _cache.get(ob.as_pointer())
        if entry is None or entry[0] != key or entry[1] != sig:
            try:
                vals = evaluate_paths(ob, paths, frames, upper, fc_hashes)
            except Exception as e:  # never break the editor
                print("Nakan Curve Sync:", e)
                continue
            entry = (key, sig, vals)
            _cache[ob.as_pointer()] = entry
        vals = entry[2]
        for fc in fcs:
            series = vals.get(fc.data_path)
            idx = fc.array_index
            if series is None or idx >= series.shape[1]:
                continue
            _n, _d, _mix, is_rot = prop_info(ob, fc.data_path)
            col_vals = series[vis, idx]
            if normalize:
                # Normalize works on raw values (no degree conversion)
                nf, noff = _normalization(fc, scene)
                ys = (col_vals + noff) * nf
            else:
                ufac = (180.0 / math.pi) if (deg and is_rot) else 1.0
                ys = col_vals * ufac
            ys = (ys - y0) * sy
            ok = np.isfinite(ys)
            if ok.sum() < 2:
                continue
            pts = np.column_stack((xs[ok], ys[ok])).astype(np.float32)
            # line segments (a, b) so every curve fits in a single draw call
            seg = np.empty((2 * (len(pts) - 1), 2), dtype=np.float32)
            seg[0::2] = pts[:-1]
            seg[1::2] = pts[1:]
            if st.use_channel_color:
                c = fc.color
                col = (min(1.0, c[0] * 0.5 + 0.5), min(1.0, c[1] * 0.5 + 0.5),
                       min(1.0, c[2] * 0.5 + 0.5), st.color[3])
            else:
                col = tuple(st.color)
            seg_pos.append(seg)
            seg_col.append(np.tile(np.asarray(col, dtype=np.float32), (len(seg), 1)))
    if not seg_pos:
        return
    shader = gpu.shader.from_builtin("POLYLINE_FLAT_COLOR")
    gpu.state.blend_set("ALPHA")
    shader.uniform_float("viewportSize", (region.width, region.height))
    shader.uniform_float("lineWidth", st.width)
    batch = batch_for_shader(shader, "LINES", {"pos": np.concatenate(seg_pos),
                                               "color": np.concatenate(seg_col)})
    batch.draw(shader)
    gpu.state.blend_set("NONE")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def _redraw(self, context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "GRAPH_EDITOR":
                area.tag_redraw()


class NKCS_GraphSettings(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Show Final Curve", default=True, update=_redraw,
        description="Draw the final NLA-combined value of each visible channel")
    only_selected: bpy.props.BoolProperty(
        name="Only Selected Channels", default=False, update=_redraw,
        description="Only draw the final curve for selected F-Curve channels")
    stack_mode: bpy.props.EnumProperty(
        name="Tweak Stack", default="AUTO", update=_redraw,
        description="Which tracks count while tweaking a strip",
        items=(
            ("AUTO", "Auto", "Detect whether Tweak Mode was entered with "
                             "Full Stack or Lower Stack evaluation"),
            ("FULL", "Full Stack", "Include the tracks above the tweaked strip"),
            ("LOWER", "Lower Stack", "Ignore the tracks above the tweaked strip"),
        ))
    use_channel_color: bpy.props.BoolProperty(
        name="Use Channel Color", default=True, update=_redraw)
    color: bpy.props.FloatVectorProperty(
        name="Color", subtype="COLOR", size=4, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0, 0.85), update=_redraw)
    width: bpy.props.FloatProperty(
        name="Line Width", default=2.5, min=1.0, max=8.0, update=_redraw)
    substeps: bpy.props.IntProperty(
        name="Subframe Samples", default=2, min=1, max=8, update=_redraw,
        description="Samples per frame when zoomed in (smoother curves)")


class NKCS_PT_graph_final(bpy.types.Panel):
    bl_label = "Final Curve"
    bl_space_type = "GRAPH_EDITOR"
    bl_region_type = "UI"
    bl_category = "Curve Sync"

    def draw_header(self, context):
        self.layout.prop(context.window_manager.nkcs_graph, "enabled", text="")

    def draw(self, context):
        st = context.window_manager.nkcs_graph
        col = self.layout.column()
        col.active = st.enabled
        col.prop(st, "only_selected")
        col.prop(st, "stack_mode")
        if st.stack_mode == "AUTO":
            ob = context.active_object
            guesses = []
            if ob is not None:
                for idb in (ob, getattr(ob, "data", None),
                            getattr(getattr(ob, "data", None), "shape_keys", None)):
                    ad = getattr(idb, "animation_data", None)
                    if ad is not None and ad.use_tweak_mode and idb.as_pointer() in _upper_guess:
                        guesses.append(_upper_guess[idb.as_pointer()])
            if guesses:
                row = col.row()
                row.alignment = "RIGHT"
                row.label(text="Detected: " + ("Full Stack" if guesses[0] else "Lower Stack"))
        col.separator()
        col.prop(st, "use_channel_color")
        if not st.use_channel_color:
            col.prop(st, "color")
        else:
            col.prop(st, "color", text="Alpha", index=3)
        col.prop(st, "width")
        col.prop(st, "substeps")


class NKCS_PT_graph_sync(bpy.types.Panel):
    bl_label = "Sync Curves"
    bl_space_type = "GRAPH_EDITOR"
    bl_region_type = "UI"
    bl_category = "Curve Sync"

    def draw(self, context):
        from .marks import draw_curve_list
        lay = self.layout
        lay.label(text=f"Scene: {context.scene.name}", icon="SCENE_DATA")
        row = lay.row(align=True)
        row.operator("nkcs.mark_curves", text="Mark Selected", icon="ADD")
        row.operator("nkcs.unmark_curves", text="Unmark", icon="REMOVE")
        draw_curve_list(lay, context.scene)


classes = (NKCS_GraphSettings, NKCS_PT_graph_final, NKCS_PT_graph_sync)


def register():
    global _handle
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.nkcs_graph = bpy.props.PointerProperty(type=NKCS_GraphSettings)
    _handle = bpy.types.SpaceGraphEditor.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceGraphEditor.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    del bpy.types.WindowManager.nkcs_graph
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    _cache.clear()
