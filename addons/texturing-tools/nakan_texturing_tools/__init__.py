bl_info = {
    "name": "Nakan Texturing Tools",
    "author": "Nakan + Claude",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D View / Image Editor > Sidebar (N) > Nakan Texturing Tools  |  Alt+V no Texture Paint",
    "description": "Elliptical tip, automatic spacing, align to normal, restrict to the clicked face, color variation, painting on two layers (primary and secondary color), seamless blur and distort on the mesh surface, 2D blur on the texture and normals from colors. Vector layers are a separate add-on: Nakan Vector Texturing",
    "category": "Paint",
}

import bpy
import json
import math
import random
import colorsys
import numpy as np
from mathutils import Vector, Matrix
from bpy_extras import view3d_utils
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper, ImportHelper

TIP_NAME = "PaintingTools_TipMask"
TIP_RES = 256


# ---------------------------------------------------------------------------
# Elliptical tip (through a Texture Mask)
# ---------------------------------------------------------------------------

def _get_brush(context):
    ip = context.tool_settings.image_paint
    return ip.brush if ip else None


def _build_tip_image(roundness, hardness):
    img = bpy.data.images.get(TIP_NAME)
    if img is None or tuple(img.size) != (TIP_RES, TIP_RES):
        if img:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(TIP_NAME, TIP_RES, TIP_RES, alpha=True)
        img.colorspace_settings.name = "Non-Color"
    img.use_fake_user = True

    c = (np.arange(TIP_RES) + 0.5) / TIP_RES * 2.0 - 1.0  # -1..1
    x, y = np.meshgrid(c, c)
    ry = max(roundness, 0.01)
    d = np.sqrt(x * x + (y / ry) ** 2)  # 1.0 = edge of the ellipse
    h = min(max(hardness, 0.0), 0.999)
    t = np.clip((1.0 - d) / (1.0 - h), 0.0, 1.0)
    v = t * t * (3.0 - 2.0 * t)  # smoothstep

    px = np.empty((TIP_RES, TIP_RES, 4), dtype=np.float32)
    px[..., 0] = v
    px[..., 1] = v
    px[..., 2] = v
    px[..., 3] = 1.0
    img.pixels.foreach_set(px.ravel())
    img.update()
    return img


def _get_tip_texture(img):
    tex = bpy.data.textures.get(TIP_NAME)
    if tex is None or tex.type != 'IMAGE':
        tex = bpy.data.textures.new(TIP_NAME, 'IMAGE')
    tex.image = img
    tex.use_alpha = False
    tex.extension = 'CLIP'
    tex.use_fake_user = True
    return tex


def _ensure_local_brush(context):
    """Default brushes (Essentials) are linked assets and don't accept a local texture.
    Creates/reuses a local copy "<name> (Elliptical)" and activates it."""
    brush = _get_brush(context)
    if brush is None or brush.library is None:
        return brush
    name = brush.name + " (Elliptical)"
    local = next((b for b in bpy.data.brushes if b.name == name and b.library is None), None)
    if local is None:
        local = brush.copy()
        local.name = name
    if local.asset_data is None:
        local.asset_mark()
    bpy.ops.brush.asset_activate(asset_library_type='LOCAL',
                                 relative_asset_identifier="Brush/" + local.name)
    return _get_brush(context)


def apply_tip(context):
    props = context.scene.painting_tools
    brush = _ensure_local_brush(context)
    if brush is None:
        return False
    img = _build_tip_image(props.roundness, props.hardness)
    tex = _get_tip_texture(img)
    brush.mask_texture = tex
    slot = brush.mask_texture_slot
    slot.mask_map_mode = 'VIEW_PLANE'
    slot.scale = (1.0, 1.0, 1.0)
    slot.offset = (0.0, 0.0, 0.0)
    slot.angle = props.angle
    slot.use_rake = props.angle_mode == 'RAKE'
    slot.use_random = props.angle_mode == 'RANDOM'
    if props.angle_mode == 'RANDOM':
        slot.random_angle = props.random_range
    _apply_spacing(brush, props)
    return True


def _auto_spacing_value(props):
    # spacing is relative to the full diameter; compensate for the thin side of the ellipse
    return max(1, round(props.base_spacing * props.roundness))


def _apply_spacing(brush, props):
    if props.auto_spacing:
        brush.spacing = _auto_spacing_value(props)


def remove_tip(context):
    brush = _get_brush(context)
    if brush and brush.mask_texture and brush.mask_texture.name == TIP_NAME:
        brush.mask_texture = None
        props = context.scene.painting_tools
        if props.auto_spacing:
            brush.spacing = props.base_spacing


def _live_update(self, context):
    brush = _get_brush(context)
    if brush and brush.mask_texture and brush.mask_texture.name == TIP_NAME:
        apply_tip(context)


# ---------------------------------------------------------------------------
# Color variation per stroke (between primary and secondary color)
# ---------------------------------------------------------------------------

def _color_owner(context):
    """Where the active color lives: unified settings or the brush itself."""
    ip = context.tool_settings.image_paint
    ups = getattr(ip, "unified_paint_settings", None) or context.tool_settings.unified_paint_settings
    if ups is not None and ups.use_unified_color:
        return ups
    return ip.brush


def _to_srgb(c):
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _mix_perceptual(a, b, t):
    """Mix in sRGB (like the color picker), return in linear."""
    return [_to_linear(_to_srgb(x) + (_to_srgb(y) - _to_srgb(x)) * t) for x, y in zip(a, b)]


def _same(a, b, eps=1e-4):
    return all(abs(x - y) < eps for x, y in zip(a, b))


def _jitter_hsv(base_linear, hue, sat, val):
    """Vary H/S/V around the base color (in sRGB, like the picker). hue: 1 = ±180°."""
    r, g, b = (min(max(_to_srgb(c), 0.0), 1.0) for c in base_linear)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h = (h + random.uniform(-hue, hue) * 0.5) % 1.0
    s = min(max(s + random.uniform(-sat, sat), 0.0), 1.0)
    v = min(max(v + random.uniform(-val, val), 0.0), 1.0)
    return [_to_linear(c) for c in colorsys.hsv_to_rgb(h, s, v)]


def _varied_color(props, base, secondary):
    if props.color_vary_mode == 'AROUND':
        return _jitter_hsv(list(base), props.color_hue_var,
                           props.color_sat_var, props.color_val_var)
    t = random.uniform(0.0, props.color_vary_amount)
    return _mix_perceptual(list(base), list(secondary), t)


def _jitter_props(props, color):
    return _jitter_hsv(list(color), props.color_hue_var, props.color_sat_var, props.color_val_var)


def vary_stroke_color(context, props, force_around=False):
    owner = _color_owner(context)
    if owner is None:
        return
    cur = list(owner.color)
    # if the current color isn't the one the add-on applied, the user changed it: it becomes the new base
    if not props.color_has_last or not _same(cur, props.color_last):
        props.color_base = cur
    if force_around:
        new = _jitter_props(props, props.color_base)
    else:
        new = _varied_color(props, props.color_base, owner.secondary_color)
    owner.color = new
    props.color_last = new
    props.color_has_last = True


def _restore_base_color(context, props):
    owner = _color_owner(context)
    if owner is not None and props.color_has_last and _same(list(owner.color), props.color_last):
        owner.color = props.color_base
    props.color_has_last = False


def _watch_needed(props):
    return props.region_mode or props.color_vary or props.dual_enable


def _ensure_watcher():
    if not PTL_OT_stroke_watch._running:
        bpy.ops.painting_tools.stroke_watch('INVOKE_DEFAULT')


def _color_vary_update(self, context):
    if self.color_vary:
        self.color_has_last = False
        _ensure_watcher()
    else:
        _restore_base_color(context, self)


# ---------------------------------------------------------------------------
# Dual painting: the stroke paints layer A live; the path is recorded and
# replayed on layer B (with another color) as soon as the stroke ends.
# ---------------------------------------------------------------------------

_DUAL = {}


def _dual_update(self, context):
    if self.dual_enable:
        _ensure_watcher()


def _brush_radius(context):
    """Brush radius in screen pixels (what Blender's recorded stroke uses)."""
    ip = context.tool_settings.image_paint
    ups = getattr(ip, "unified_paint_settings", None) or context.tool_settings.unified_paint_settings
    brush = ip.brush
    owner = ups if (ups is not None and ups.use_unified_size) else brush
    size = owner.size
    # in Blender 5.x "Size" is the diameter; in older versions it was the radius
    if owner.bl_rna.properties["size"].subtype == 'PIXEL_DIAMETER':
        return size / 2.0
    return size


DUAL_BRUSH_TYPES = {'DRAW', 'SOFTEN', 'SMEAR'}  # Fill, Clone and Mask are not replayed


def _brush_kind(context):
    """'COLOR' (paints color), 'OTHER' (smudge/soften/eraser: color doesn't matter) or None."""
    brush = _get_brush(context)
    if brush is None:
        return None
    btype = getattr(brush, "image_brush_type", None) or getattr(brush, "image_tool", 'DRAW')
    if btype not in DUAL_BRUSH_TYPES:
        return None
    if btype == 'DRAW' and brush.blend not in {'ERASE_ALPHA', 'ADD_ALPHA'}:
        return 'COLOR'
    return 'OTHER'


def dual_begin(context, hit_view, event, props):
    """Called on the click that starts the stroke: layer 1 gets the primary color (live)
    and layer 2 will get the secondary color when the stroke ends."""
    ip = context.tool_settings.image_paint
    a, b = props.dual_a_image, props.dual_b_image
    if a is None or b is None or a == b:
        return
    owner = _color_owner(context)
    if owner is None:
        return
    kind = _brush_kind(context)
    if kind is None:
        return  # Fill/Clone/Mask: paint only the active layer, no replay
    ip.mode = 'IMAGE'
    ip.canvas = a
    vary = props.color_vary and kind == 'COLOR'
    if vary:
        # in dual painting the variation is always around each color
        vary_stroke_color(context, props, force_around=True)
    col_a = list(owner.color)
    col_b = list(owner.secondary_color)
    if vary and props.dual_vary_b:
        # color 2 has its own controls; the original secondary color is not changed
        col_b = _jitter_hsv(col_b, props.color2_hue_var, props.color2_sat_var, props.color2_val_var)

    region, _rv3d, coord = hit_view
    area = next((ar for ar in context.window.screen.areas
                 if any(r == region for r in ar.regions)), None)
    _DUAL.clear()
    _DUAL.update(window=context.window, area=area, region=region,
                 pts=[(coord[0], coord[1], event.pressure if event.pressure > 0 else 1.0)],
                 color_a=col_a, color_b=col_b, done=False)
    bpy.app.timers.register(_dual_start_recorder, first_interval=0.0)


def _dual_start_recorder():
    st = _DUAL
    if not st or st.get("done") or st.get("area") is None:
        return None
    try:
        with bpy.context.temp_override(window=st["window"], area=st["area"], region=st["region"]):
            bpy.ops.painting_tools.dual_recorder('INVOKE_DEFAULT')
    except Exception as e:
        print("Texturing Tools (recorder):", e)
    return None


def _resample(pts, step):
    """Resample the path every `step` pixels, interpolating the pressure."""
    if len(pts) == 1:
        return list(pts)
    out = [pts[0]]
    carry = 0.0
    for (x0, y0, p0), (x1, y1, p1) in zip(pts, pts[1:]):
        dx, dy = x1 - x0, y1 - y0
        seg = (dx * dx + dy * dy) ** 0.5
        if seg <= 0:
            continue
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append((x0 + dx * t, y0 + dy * t, p0 + (p1 - p0) * t))
            d += step
        carry = seg - (d - step)
    return out


def dual_replay(context=None):
    st = _DUAL
    if not st or st.get("done"):
        return None
    st["done"] = True
    context = bpy.context
    props = context.scene.painting_tools
    ip = context.tool_settings.image_paint
    b = props.dual_b_image
    a = props.dual_a_image
    if b is None or st.get("area") is None:
        return None
    brush = ip.brush
    radius = _brush_radius(context)
    step = max(1.0, 2.0 * radius * max(brush.spacing, 1) / 100.0)
    pts = _resample(st["pts"], step)
    items = [{"name": "", "location": (0, 0, 0), "mouse": (x, y), "mouse_event": (x, y),
              "pressure": p, "size": radius, "x_tilt": 0.0, "y_tilt": 0.0,
              "time": i * 0.01, "is_start": i == 0}
             for i, (x, y, p) in enumerate(pts)]
    owner = _color_owner(context)
    try:
        ip.canvas = b
        owner.color = st["color_b"]
        with context.temp_override(window=st["window"], area=st["area"], region=st["region"]):
            bpy.ops.paint.image_paint('EXEC_DEFAULT', stroke=items, mode='NORMAL')
    except Exception as e:
        print("Texturing Tools (layer B):", e)
    finally:
        if a is not None:
            ip.canvas = a
        owner.color = st["color_a"]
        st["area"].tag_redraw()
    return None


class PTL_OT_dual_recorder(bpy.types.Operator):
    """Record the path of the stroke in progress (sits in front of the brush, without consuming events).

    Important: this operator is started inside a context override (from a
    timer). If it ends returning FINISHED + PASS_THROUGH, the same event goes on to
    the brush with the override context already released, and Blender hangs when the stroke closes.
    So it never ends by passing an event on: after the stroke, it ends on the next
    TIMER or mouse move, consuming that event."""
    bl_idname = "painting_tools.dual_recorder"
    bl_label = "Record stroke"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        self._finished = False
        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _add(self, event):
        r = _DUAL.get("region")
        if r is None:
            return
        p = event.pressure if event.pressure > 0 else 1.0
        _DUAL["pts"].append((event.mouse_x - r.x, event.mouse_y - r.y, p))

    def _end(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        return {'FINISHED'}  # consume this event (don't pass it on)

    def modal(self, context, event):
        if self._finished or not _DUAL or _DUAL.get("done"):
            self._finished = True
            # end on an unimportant event (TIMER/MOUSEMOVE), consuming it;
            # clicks and keys keep going to the rest of Blender
            if event.type in {'TIMER', 'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
                return self._end(context)
            return {'PASS_THROUGH'}
        if event.type == 'TIMER':
            return {'PASS_THROUGH'}
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._add(event)
        elif event.type in {'LEFTMOUSE', 'PEN'} and event.value == 'RELEASE':
            self._add(event)
            self._finished = True
            bpy.app.timers.register(dual_replay, first_interval=0.02)
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            # we missed the release (very quick tap): replay the previous stroke now
            self._finished = True
            dual_replay()
        elif event.type in {'ESC', 'RIGHTMOUSE'}:
            self._finished = True
            _DUAL["done"] = True  # stroke cancelled
        return {'PASS_THROUGH'}


class PTL_OT_sample_secondary(bpy.types.Operator):
    """Pick the final color on screen (like Ctrl+Shift+X) and store it as the secondary color,
    without touching the primary"""
    bl_idname = "painting_tools.sample_secondary"
    bl_label = "Pick color for secondary"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'PAINT_TEXTURE' and context.area is not None
                and context.area.type == 'VIEW_3D' and context.region is not None
                and context.region.type == 'WINDOW')

    def invoke(self, context, event):
        owner = _color_owner(context)
        if owner is None:
            return {'CANCELLED'}
        primary = tuple(owner.color)
        try:
            bpy.ops.paint.sample_color('EXEC_DEFAULT',
                                       location=(event.mouse_region_x, event.mouse_region_y),
                                       merged=True)
            owner.secondary_color = tuple(owner.color)
        finally:
            owner.color = primary
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


def _mid_color(context):
    owner = _color_owner(context)
    if owner is None:
        return None
    return _mix_perceptual(list(owner.color), list(owner.secondary_color), 0.5)


def _get_mid_preview(self):
    c = _mid_color(bpy.context)
    return c if c is not None else (0.0, 0.0, 0.0)


class PTL_OT_mid_color(bpy.types.Operator):
    """Set the primary color to the color exactly halfway between the primary and the secondary
    (halfway as shown in the color picker)"""
    bl_idname = "painting_tools.mid_color"
    bl_label = "Middle color"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        owner = _color_owner(context)
        mid = _mid_color(context)
        if owner is None or mid is None:
            return {'CANCELLED'}
        owner.color = mid
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class PTL_OT_dual_swap(bpy.types.Operator):
    bl_idname = "painting_tools.dual_swap"
    bl_label = "Swap A and B"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        p = context.scene.painting_tools
        p.dual_a_image, p.dual_b_image = p.dual_b_image, p.dual_a_image
        return {'FINISHED'}


def _region_mode_update(self, context):
    ip = context.tool_settings.image_paint
    obj = context.active_object
    if self.region_mode:
        # the view angle limit gets in the way: turn it off while the mode is active
        self["_prev_view_falloff"] = ip.use_normal_falloff
        ip.use_normal_falloff = False
        _ensure_watcher()
    else:
        ip.use_normal_falloff = bool(self.get("_prev_view_falloff", ip.use_normal_falloff))
        if obj and obj.type == 'MESH':
            obj.data.use_paint_mask = False
            obj.data.update()


# ---------------------------------------------------------------------------
# Color management (color list saved in the .blend, with groups)
# ---------------------------------------------------------------------------

class PTL_ColorItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name", default="")
    color: bpy.props.FloatVectorProperty(
        name="Color", size=3, subtype='COLOR', min=0.0, max=1.0, default=(1.0, 1.0, 1.0))
    is_group: bpy.props.BoolProperty(default=False)
    expanded: bpy.props.BoolProperty(name="Open", default=True)
    level: bpy.props.IntProperty(default=0, min=0, max=1)


def _pal_tree(items):
    """Flat list -> list of nodes. Node = dict(item=data, children=[...]) for groups."""
    nodes, current = [], None
    for it in items:
        data = dict(name=it.name, color=tuple(it.color), is_group=it.is_group,
                    expanded=it.expanded, level=it.level)
        if it.is_group:
            current = dict(item=data, children=[])
            nodes.append(current)
        elif it.level > 0 and current is not None:
            current["children"].append(dict(item=data, children=None))
        else:
            current = None
            nodes.append(dict(item=data, children=None))
    return nodes


def _pal_flatten(nodes):
    flat = []
    for n in nodes:
        d = dict(n["item"])
        d["level"] = 0
        flat.append(d)
        if n["children"] is not None:
            for c in n["children"]:
                cd = dict(c["item"])
                cd["level"] = 1
                cd["is_group"] = False
                flat.append(cd)
    return flat


def _pal_write(props, flat):
    coll = props.palette_items
    coll.clear()
    for d in flat:
        it = coll.add()
        it.name = d["name"]
        it.color = d["color"]
        it.is_group = d["is_group"]
        it.expanded = d.get("expanded", True)
        it.level = d["level"]


def _pal_locate(nodes, flat_index):
    """Return (top node index, child index or None) for an index in the flat list."""
    i = 0
    for ti, n in enumerate(nodes):
        if i == flat_index:
            return ti, None
        i += 1
        if n["children"] is not None:
            for ci in range(len(n["children"])):
                if i == flat_index:
                    return ti, ci
                i += 1
    return None, None


def _pal_flat_index(nodes, ti, ci=None):
    i = 0
    for t, n in enumerate(nodes):
        if t == ti and ci is None:
            return i
        i += 1
        if n["children"] is not None:
            for c in range(len(n["children"])):
                if t == ti and c == ci:
                    return i
                i += 1
    return 0


def _pal_move(props, direction):
    """Move the active item (direction -1 = up, +1 = down), Photoshop style:
    colors go in and out of open groups; groups move with their content."""
    nodes = _pal_tree(props.palette_items)
    ti, ci = _pal_locate(nodes, props.palette_index)
    if ti is None:
        return
    if ci is not None:  # color inside a group
        kids = nodes[ti]["children"]
        nci = ci + direction
        if 0 <= nci < len(kids):
            kids[ci], kids[nci] = kids[nci], kids[ci]
            new = (ti, nci)
        else:  # leaves the group, above or below it
            node = kids.pop(ci)
            pos = ti if direction < 0 else ti + 1
            nodes.insert(pos, node)
            new = (pos, None)
    else:
        node = nodes[ti]
        nti = ti + direction
        if not (0 <= nti < len(nodes)):
            return
        other = nodes[nti]
        if node["children"] is None and other["children"] is not None and other["item"]["expanded"]:
            # loose color entering an open group
            nodes.pop(ti)
            gi = nti if direction > 0 else nti
            if direction > 0:
                gi = nti - 1  # the group moved up one position after the pop
                nodes[gi]["children"].insert(0, node)
                new = (gi, 0)
            else:
                nodes[gi]["children"].append(node)
                new = (gi, len(nodes[gi]["children"]) - 1)
        else:
            nodes[ti], nodes[nti] = nodes[nti], nodes[ti]
            new = (nti, None)
    _pal_write(props, _pal_flatten(nodes))
    props.palette_index = _pal_flat_index(nodes, *new)


def _pal_insert(props, data, into_group=True):
    """Insert a color right below the active item (inside the group, if the active one is a group/child)."""
    coll = props.palette_items
    nodes = _pal_tree(coll)
    node = dict(item=dict(name=data.get("name", ""), color=tuple(data["color"]),
                          is_group=data.get("is_group", False), expanded=True, level=0),
                children=[] if data.get("is_group") else None)
    ti, ci = _pal_locate(nodes, props.palette_index) if len(coll) else (None, None)
    if ti is None:
        nodes.append(node)
        new = (len(nodes) - 1, None)
    elif node["children"] is None and into_group and nodes[ti]["children"] is not None:
        kids = nodes[ti]["children"]
        pos = 0 if ci is None else ci + 1
        kids.insert(pos, node)
        nodes[ti]["item"]["expanded"] = True
        new = (ti, pos)
    else:
        nodes.insert(ti + 1, node)
        new = (ti + 1, None)
    _pal_write(props, _pal_flatten(nodes))
    props.palette_index = _pal_flat_index(nodes, *new)


def _drop_slot_get(self):
    return (0.18, 0.18, 0.18)


def _drop_slot_set(self, value):
    # dropping (or picking) a color in this box creates a new item in the list
    _pal_insert(self, dict(color=tuple(value)))


def _mid_preview_set(self, value):
    pass  # preview only; the setter exists so the swatch can be dragged


class PTL_UL_colors(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        if item.level > 0:
            row.separator(factor=2.0)
        if item.is_group:
            row.prop(item, "expanded", text="", emboss=False,
                     icon='DISCLOSURE_TRI_DOWN' if item.expanded else 'DISCLOSURE_TRI_RIGHT')
            row.label(text="", icon='FILE_FOLDER')
            row.prop(item, "name", text="", emboss=False)
        else:
            sub = row.row(align=True)
            sub.scale_x = 0.6
            sub.prop(item, "color", text="")
            try:
                row.prop(item, "name", text="", emboss=False, placeholder="unnamed")
            except TypeError:  # Blender without 'placeholder'
                row.prop(item, "name", text="", emboss=False)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flags = [self.bitflag_filter_item] * len(items)
        group_open = True
        for i, it in enumerate(items):
            if it.is_group:
                group_open = it.expanded
            elif it.level > 0 and not group_open:
                flags[i] = 0
            elif it.level == 0:
                group_open = True
        return flags, []


class PTL_OT_pal_add(bpy.types.Operator):
    """Add the current primary color to the list (below the selected item)"""
    bl_idname = "painting_tools.pal_add"
    bl_label = "Add color"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        owner = _color_owner(context)
        color = tuple(owner.color) if owner is not None else (1.0, 1.0, 1.0)
        _pal_insert(context.scene.painting_tools, dict(color=color))
        return {'FINISHED'}


class PTL_OT_pal_add_group(bpy.types.Operator):
    """Create a color group"""
    bl_idname = "painting_tools.pal_add_group"
    bl_label = "Add group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.painting_tools
        n = sum(1 for it in props.palette_items if it.is_group) + 1
        _pal_insert(props, dict(color=(0, 0, 0), is_group=True, name=f"Group {n}"), into_group=False)
        return {'FINISHED'}


class PTL_OT_pal_remove(bpy.types.Operator):
    """Delete the selected color (or the whole group, with the colors inside)"""
    bl_idname = "painting_tools.pal_remove"
    bl_label = "Delete"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.painting_tools.palette_items) > 0

    def execute(self, context):
        props = context.scene.painting_tools
        nodes = _pal_tree(props.palette_items)
        ti, ci = _pal_locate(nodes, props.palette_index)
        if ti is None:
            return {'CANCELLED'}
        if ci is None:
            nodes.pop(ti)
        else:
            nodes[ti]["children"].pop(ci)
        _pal_write(props, _pal_flatten(nodes))
        props.palette_index = max(0, min(props.palette_index, len(props.palette_items) - 1))
        return {'FINISHED'}


class PTL_OT_pal_move(bpy.types.Operator):
    """Move the selected color or group up or down"""
    bl_idname = "painting_tools.pal_move"
    bl_label = "Move"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])

    @classmethod
    def poll(cls, context):
        return len(context.scene.painting_tools.palette_items) > 1

    def execute(self, context):
        _pal_move(context.scene.painting_tools, -1 if self.direction == 'UP' else 1)
        return {'FINISHED'}


class PTL_OT_pal_use(bpy.types.Operator):
    """Use the selected color from the list as the brush's primary or secondary color"""
    bl_idname = "painting_tools.pal_use"
    bl_label = "Use color"
    bl_options = {'REGISTER', 'UNDO'}

    target: bpy.props.EnumProperty(items=[('PRIMARY', "Primary", ""), ('SECONDARY', "Secondary", "")])

    @classmethod
    def poll(cls, context):
        props = context.scene.painting_tools
        i = props.palette_index
        return 0 <= i < len(props.palette_items) and not props.palette_items[i].is_group

    def execute(self, context):
        props = context.scene.painting_tools
        owner = _color_owner(context)
        if owner is None:
            return {'CANCELLED'}
        c = tuple(props.palette_items[props.palette_index].color)
        if self.target == 'PRIMARY':
            owner.color = c
        else:
            owner.secondary_color = c
        return {'FINISHED'}


def _pal_to_json(props):
    out = []
    for it in props.palette_items:
        srgb = [min(max(_to_srgb(c), 0.0), 1.0) for c in it.color]
        out.append(dict(name=it.name, group=it.is_group, level=it.level, expanded=it.expanded,
                        color_linear=[round(c, 6) for c in it.color],
                        hex="#%02X%02X%02X" % tuple(int(round(c * 255)) for c in srgb)))
    return dict(format="nakan_painting_tools_palette", version=1, items=out)


def _pal_from_json(props, data, replace):
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("file is not a Nakan Texturing Tools palette")
    flat = [] if replace else [dict(name=it.name, color=tuple(it.color), is_group=it.is_group,
                                    expanded=it.expanded, level=it.level)
                               for it in props.palette_items]
    for d in data["items"]:
        if "color_linear" in d:
            color = tuple(float(c) for c in d["color_linear"][:3])
        else:
            h = d.get("hex", "#FFFFFF").lstrip("#")
            color = tuple(_to_linear(int(h[i:i + 2], 16) / 255.0) for i in (0, 2, 4))
        flat.append(dict(name=str(d.get("name", "")), color=color, is_group=bool(d.get("group")),
                         expanded=bool(d.get("expanded", True)), level=1 if d.get("level") else 0))
    _pal_write(props, _pal_flatten(_pal_tree_from_flat(flat)))
    props.palette_index = 0


def _pal_tree_from_flat(flat):
    class _It:  # adapts dicts for _pal_tree
        def __init__(s, d):
            s.name, s.color, s.is_group = d["name"], d["color"], d["is_group"]
            s.expanded, s.level = d.get("expanded", True), d["level"]
    return _pal_tree([_It(d) for d in flat])


class PTL_OT_pal_export(bpy.types.Operator, ExportHelper):
    """Save the color list to a .json file"""
    bl_idname = "painting_tools.pal_export"
    bl_label = "Export palette"
    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(_pal_to_json(context.scene.painting_tools), f, ensure_ascii=False, indent=2)
        self.report({'INFO'}, "Palette exported")
        return {'FINISHED'}


class PTL_OT_pal_import(bpy.types.Operator, ImportHelper):
    """Load a color list from a .json file"""
    bl_idname = "painting_tools.pal_import"
    bl_label = "Import palette"
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})
    mode: bpy.props.EnumProperty(
        name="Mode",
        items=[('APPEND', "Append", "Keeps the current colors and adds the ones from the file"),
               ('REPLACE', "Replace", "Deletes the current colors and loads the ones from the file")],
        default='APPEND')

    def execute(self, context):
        try:
            with open(self.filepath, encoding="utf-8") as f:
                data = json.load(f)
            _pal_from_json(context.scene.painting_tools, data, self.mode == 'REPLACE')
        except Exception as e:
            self.report({'ERROR'}, f"Could not import: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Palette imported")
        return {'FINISHED'}


class PTL_Props(bpy.types.PropertyGroup):
    roundness: bpy.props.FloatProperty(
        name="Roundness", description="1 = circle, lower = flatter ellipse",
        default=0.35, min=0.02, max=1.0, subtype='FACTOR', update=_live_update)
    hardness: bpy.props.FloatProperty(
        name="Hardness", description="Hardness of the tip edge",
        default=0.9, min=0.0, max=1.0, subtype='FACTOR', update=_live_update)
    angle: bpy.props.FloatProperty(
        name="Angle", default=0.0, subtype='ANGLE', update=_live_update)
    angle_mode: bpy.props.EnumProperty(
        name="Rotation",
        items=[
            ('FIXED', "Fixed", "Uses the set angle"),
            ('RAKE', "Follow stroke", "The tip turns with the stroke direction"),
            ('RANDOM', "Random", "Random rotation on every dab"),
        ],
        default='FIXED', update=_live_update)
    random_range: bpy.props.FloatProperty(
        name="Random range", default=6.283185, min=0.0, max=6.283185,
        subtype='ANGLE', update=_live_update)
    auto_spacing: bpy.props.BoolProperty(
        name="Automatic spacing", default=True,
        description="Adjusts the brush spacing in proportion to Roundness, "
                    "so a flat ellipse doesn't leave separate stamps",
        update=_live_update)
    base_spacing: bpy.props.IntProperty(
        name="Base spacing", default=10, min=1, max=100, subtype='PERCENTAGE',
        description="Equivalent spacing for a round tip (Roundness 1). "
                    "The applied value is Base spacing × Roundness",
        update=_live_update)
    region_mode: bpy.props.BoolProperty(
        name="Restrict to clicked face", default=False,
        description="Each stroke only paints faces with a normal similar to the face you clicked, "
                    "whatever the view angle",
        update=_region_mode_update)
    region_angle: bpy.props.FloatProperty(
        name="Angle", default=0.5236, min=0.0, max=3.14159, subtype='ANGLE',
        description="Maximum difference between each face's normal and the clicked face's")
    region_connected: bpy.props.BoolProperty(
        name="Connected faces only", default=True,
        description="Only includes faces connected to the clicked face (avoids painting the other side of the model)")
    region_lock: bpy.props.BoolProperty(
        name="Lock region", default=False,
        description="Keeps the region from the first click for the next strokes, "
                    "even if you rotate the view")
    color_vary: bpy.props.BoolProperty(
        name="Vary color per stroke", default=False,
        description="On every stroke, picks a random color between the primary and the secondary",
        update=_color_vary_update)
    color_vary_mode: bpy.props.EnumProperty(
        name="Mode",
        items=[
            ('BETWEEN', "Between the two colors", "Picks a random color between the primary and the secondary"),
            ('AROUND', "Around the primary", "Varies hue, saturation and value around the primary color"),
        ],
        default='BETWEEN')
    color_hue_var: bpy.props.FloatProperty(
        name="Hue", default=0.05, min=0.0, max=1.0, subtype='FACTOR',
        description="Maximum hue variation to each side (1 = ±180°, 0.1 = ±18°)")
    color_sat_var: bpy.props.FloatProperty(
        name="Saturation", default=0.1, min=0.0, max=1.0, subtype='FACTOR',
        description="Maximum saturation variation to each side")
    color_val_var: bpy.props.FloatProperty(
        name="Value", default=0.1, min=0.0, max=1.0, subtype='FACTOR',
        description="Maximum brightness variation to each side")
    color_vary_amount: bpy.props.FloatProperty(
        name="Reach", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="How far the color can go toward the secondary (1 = it can reach it)")
    color_base: bpy.props.FloatVectorProperty(size=3, subtype='COLOR', min=0.0, max=1.0,
                                              options={'HIDDEN'})
    color_last: bpy.props.FloatVectorProperty(size=3, subtype='COLOR', options={'HIDDEN'})
    color_has_last: bpy.props.BoolProperty(default=False, options={'HIDDEN'})
    mid_preview: bpy.props.FloatVectorProperty(
        name="Middle color", size=3, subtype='COLOR', get=_get_mid_preview, set=_mid_preview_set,
        description="Preview of the color halfway between the primary and the secondary")
    palette_items: bpy.props.CollectionProperty(type=PTL_ColorItem)
    palette_index: bpy.props.IntProperty(name="Selected color", default=0)
    drop_slot: bpy.props.FloatVectorProperty(
        name="Drop a color here", size=3, subtype='COLOR', min=0.0, max=1.0,
        get=_drop_slot_get, set=_drop_slot_set,
        description="Drag a color into this box to save it to the list")
    dual_enable: bpy.props.BoolProperty(
        name="Paint two layers", default=False,
        description="Each stroke paints layer 1 with the primary color and, on release, "
                    "repeats the same stroke on layer 2 with the secondary color",
        update=_dual_update)
    dual_vary_b: bpy.props.BoolProperty(
        name="Vary color 2", default=True,
        description="With Color Variation on, the layer 2 color also varies on every stroke")
    color2_hue_var: bpy.props.FloatProperty(
        name="Hue", default=0.02, min=0.0, max=1.0, subtype='FACTOR',
        description="Hue variation of color 2 to each side (1 = ±180°)")
    color2_sat_var: bpy.props.FloatProperty(
        name="Saturation", default=0.05, min=0.0, max=1.0, subtype='FACTOR',
        description="Saturation variation of color 2 to each side")
    color2_val_var: bpy.props.FloatProperty(
        name="Value", default=0.05, min=0.0, max=1.0, subtype='FACTOR',
        description="Brightness variation of color 2 to each side")
    dual_a_image: bpy.props.PointerProperty(
        name="Layer 1", type=bpy.types.Image,
        description="Image painted live with the primary color")
    dual_b_image: bpy.props.PointerProperty(
        name="Layer 2", type=bpy.types.Image,
        description="Image that gets the same stroke with the secondary color on release")
    align_center: bpy.props.BoolProperty(
        name="Center on point", default=True,
        description="When aligning, centers the view on the point under the cursor")
    align_ortho: bpy.props.BoolProperty(
        name="Switch to orthographic", default=False,
        description="When aligning, switches to orthographic projection (no perspective distortion)")
    align_smooth: bpy.props.BoolProperty(
        name="Smooth transition", default=True)
    blur_radius: bpy.props.IntProperty(
        name="Radius (px)", default=8, min=1, soft_max=128, max=1024,
        description="UV blur size, in texture pixels")
    blur_strength: bpy.props.FloatProperty(
        name="Strength", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="Mix between the original (0) and the blurred result (1)")
    blur_type: bpy.props.EnumProperty(
        name="Type",
        items=[('GAUSSIAN', "Gaussian", "Smooth (3 passes)"),
               ('BOX', "Box", "Faster, harder")],
        default='GAUSSIAN')
    blur_surface_radius: bpy.props.FloatProperty(
        name="Radius", default=0.02, min=0.0, soft_max=1.0, subtype='DISTANCE', unit='LENGTH',
        description="Blur size measured on the model surface (scene units)")
    blur_surface_res: bpy.props.IntProperty(
        name="Resolution", default=3, min=1, max=16,
        description="How many 3D grid cells fit in the radius. Higher = smoother gradient "
                    "and slower. Cells smaller than a texture pixel are not used")
    distort_type: bpy.props.EnumProperty(
        name="Type",
        items=[('TURBULENT', "Turbulent", "Organic noise, like After Effects' Turbulent Displace"),
               ('WAVE', "Wave", "Regular ripples")],
        default='TURBULENT')
    distort_strength: bpy.props.FloatProperty(
        name="Strength", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="Mix between the original (0) and the distorted result (1)")
    turb_amount: bpy.props.FloatProperty(
        name="Amount", default=0.02, min=0.0, soft_max=0.5, subtype='DISTANCE', unit='LENGTH',
        description="How far the paint is pushed (distance on the surface)")
    turb_size: bpy.props.FloatProperty(
        name="Size", default=0.1, min=0.0001, soft_max=2.0, subtype='DISTANCE', unit='LENGTH',
        description="Size of the noise shapes")
    turb_complexity: bpy.props.IntProperty(
        name="Complexity", default=3, min=1, max=8,
        description="Layers of noise detail (more = more jagged edges)")
    turb_seed: bpy.props.IntProperty(name="Seed", default=0, min=0,
                                     description="Changes the noise pattern")
    wave_amp: bpy.props.FloatProperty(
        name="Amplitude", default=0.02, min=0.0, soft_max=0.5, subtype='DISTANCE', unit='LENGTH',
        description="Wave height (distance on the surface)")
    wave_length: bpy.props.FloatProperty(
        name="Wavelength", default=0.2, min=0.0001, soft_max=2.0, subtype='DISTANCE', unit='LENGTH',
        description="Distance from one crest to the next")
    wave_axis: bpy.props.EnumProperty(
        name="Direction",
        items=[('X', "X", "The wave travels along the world X axis"),
               ('Y', "Y", "The wave travels along the world Y axis"),
               ('Z', "Z", "The wave travels along the world Z axis")],
        default='Z')
    wave_phase: bpy.props.FloatProperty(name="Phase", default=0.0, subtype='ANGLE',
                                        description="Shifts the wave")
    wave_mode: bpy.props.EnumProperty(
        name="Motion",
        items=[('LATERAL', "Lateral", "Shifts sideways, like Wave Warp (waving bands)"),
               ('LONGITUDINAL', "Longitudinal", "Compresses and stretches along the wave, like a Ripple")],
        default='LATERAL')
    nfc_color_image: bpy.props.PointerProperty(
        name="Color", type=bpy.types.Image,
        description="Color image with flat strokes (empty = the image being painted)")
    nfc_normal_image: bpy.props.PointerProperty(
        name="Normal", type=bpy.types.Image,
        description="Normal map (object space) that will receive the facets. Use the Non-Color color space")
    nfc_strength: bpy.props.FloatProperty(
        name="Strength", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="0 = original surface normals, 1 = each region completely flat")
    nfc_tolerance: bpy.props.FloatProperty(
        name="Color tolerance", default=0.03, min=0.004, max=0.5, subtype='FACTOR',
        description="Colors closer than this count as the same stroke")
    nfc_min_pixels: bpy.props.IntProperty(
        name="Minimum region (px)", default=40, min=1, soft_max=2000,
        description="Regions smaller than this (smoothed edges, dirt) inherit the neighbor's normal")
    nfc_mode: bpy.props.EnumProperty(
        name="Mode",
        items=[('FACETS', "Facets", "Each flat color region becomes a flat facet (ideal for flat strokes)"),
               ('CONTINUOUS', "Continuous", "Average of the neighborhood normals, weighted by color similarity "
                                         "(ideal for soft paintings with gradients and blur)")],
        default='CONTINUOUS')
    nfc_cont_radius: bpy.props.FloatProperty(
        name="Reach", default=0.2, min=0.005, max=0.5, subtype='FACTOR',
        description="Size of the neighborhood flattened together (fraction of the object size). "
                    "Larger = flatter areas of the same color")
    nfc_cont_sigma: bpy.props.FloatProperty(
        name="Color sensitivity", default=0.06, min=0.01, max=1.0, subtype='FACTOR',
        description="How different two colors can be and still blend. Lower = colors separate more")
    nfc_cont_k: bpy.props.IntProperty(
        name="Color layers", default=8, min=2, max=24,
        description="How many reference colors the approximation uses (more = more faithful and slower)")
    nfc_max_angle: bpy.props.FloatProperty(
        name="Maximum deviation", default=0.6109, min=0.0, max=math.pi, subtype='ANGLE',
        description="The painted normal never deviates more than this from the real surface normal "
                    "(avoids dark bands on the silhouette)")
    nfc_smooth: bpy.props.BoolProperty(
        name="Smooth gradients", default=True,
        description="Where the color changes gradually, the normal also changes gradually; "
                    "hard color edges stay as sharp facets")
    nfc_smooth_size: bpy.props.FloatProperty(
        name="Reach", default=0.04, min=0.0, max=0.5, subtype='FACTOR',
        description="How far the smooth transition spreads (fraction of the object size)")
    nfc_edge: bpy.props.FloatProperty(
        name="Hard edge", default=0.12, min=0.005, max=1.0, subtype='FACTOR',
        description="Color jumps between neighboring pixels larger than this count as a hard edge "
                    "(the normal doesn't blend across it)")
    nfc_max_facet: bpy.props.FloatProperty(
        name="Maximum facet", default=0.12, min=0.0, max=1.0, subtype='FACTOR',
        description="Regions larger than this fraction of the object size are split into irregular "
                    "facets (0 = never split)")
    blur_tileable: bpy.props.BoolProperty(
        name="Tileable edges", default=False,
        description="In the UV blur, makes the blur wrap around the edges (tiling textures)")
    blur_expand: bpy.props.BoolProperty(
        name="Expand area (blur alpha)", default=True,
        description="Spreads the color outward by also blurring the transparency. "
                    "Off: keeps the outline and only smooths the color inside")


class PTL_OT_tip_apply(bpy.types.Operator):
    bl_idname = "painting_tools.tip_apply"
    bl_label = "Apply elliptical tip"
    bl_description = "Generates the elliptical mask and applies it to the active brush"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not apply_tip(context):
            self.report({'WARNING'}, "No active texture paint brush")
            return {'CANCELLED'}
        return {'FINISHED'}


class PTL_OT_tip_remove(bpy.types.Operator):
    bl_idname = "painting_tools.tip_remove"
    bl_label = "Back to round tip"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        remove_tip(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Align view to the surface under the cursor
# ---------------------------------------------------------------------------

class PTL_OT_align_to_surface(bpy.types.Operator):
    bl_idname = "painting_tools.align_to_surface"
    bl_label = "Align view to surface"
    bl_description = "Rotates the view to look straight at the surface under the cursor (along its normal)"
    bl_options = {'REGISTER'}

    _timer = None

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        region = context.region
        rv3d = context.region_data
        if rv3d is None:
            return {'CANCELLED'}

        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = context.evaluated_depsgraph_get()
        hit, loc, normal, _idx, _obj, _mtx = context.scene.ray_cast(depsgraph, origin, direction)
        if not hit:
            self.report({'INFO'}, "No surface under the cursor")
            return {'CANCELLED'}

        n = normal.normalized()
        if n.dot(-direction) < 0.0:
            n = -n

        # keep the view's current "up" as much as possible
        up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
        x = up.cross(n)
        if x.length < 1e-4:
            x = (rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))).cross(n).cross(n) * -1
        x.normalize()
        y = n.cross(x).normalized()
        m = Matrix((x, y, n)).transposed()
        target_rot = m.to_quaternion()

        props = context.scene.painting_tools
        if rv3d.view_perspective == 'CAMERA':
            rv3d.view_perspective = 'PERSP'

        self.rv3d = rv3d
        self.start_rot = rv3d.view_rotation.copy()
        self.start_loc = rv3d.view_location.copy()
        self.target_rot = target_rot
        if props.align_center:
            # keep the point at the same depth to avoid a sudden zoom
            self.target_loc = loc.copy()
            self.target_dist = rv3d.view_distance
        else:
            self.target_loc = self.start_loc
            self.target_dist = rv3d.view_distance
        self.ortho = props.align_ortho

        if not props.align_smooth:
            self._finish()
            return {'FINISHED'}

        self.t = 0.0
        self.duration = 0.18
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self):
        self.rv3d.view_rotation = self.target_rot
        self.rv3d.view_location = self.target_loc
        self.rv3d.view_distance = self.target_dist
        if self.ortho:
            self.rv3d.view_perspective = 'ORTHO'

    def modal(self, context, event):
        if event.type == 'TIMER':
            self.t += 0.01 / self.duration
            if self.t >= 1.0:
                self._finish()
                context.window_manager.event_timer_remove(self._timer)
                context.area.tag_redraw()
                return {'FINISHED'}
            k = self.t * self.t * (3.0 - 2.0 * self.t)
            self.rv3d.view_rotation = self.start_rot.slerp(self.target_rot, k)
            self.rv3d.view_location = self.start_loc.lerp(self.target_loc, k)
            context.area.tag_redraw()
        return {'RUNNING_MODAL'}


# ---------------------------------------------------------------------------
# Restrict painting by the clicked face's normal (independent of the view)
# ---------------------------------------------------------------------------

_adj_cache = {}


def _face_adjacency(me):
    """Face-face adjacency (by edge) in CSR format, cached per mesh."""
    key = (me.as_pointer(), len(me.polygons), len(me.edges), len(me.loops))
    cached = _adj_cache.get(me.as_pointer())
    if cached and cached[0] == key:
        return cached[1], cached[2]

    npoly = len(me.polygons)
    totals = np.empty(npoly, dtype=np.int32)
    me.polygons.foreach_get("loop_total", totals)
    loop_edge = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("edge_index", loop_edge)
    loop_poly = np.repeat(np.arange(npoly, dtype=np.int32), totals)

    order = np.argsort(loop_edge, kind="stable")
    e_sorted = loop_edge[order]
    p_sorted = loop_poly[order]
    same = e_sorted[1:] == e_sorted[:-1]
    a = p_sorted[:-1][same]
    b = p_sorted[1:][same]
    src = np.concatenate([a, b])
    dst = np.concatenate([b, a])
    o = np.argsort(src, kind="stable")
    src, dst = src[o], dst[o]
    indptr = np.searchsorted(src, np.arange(npoly + 1))
    _adj_cache[me.as_pointer()] = (key, indptr, dst)
    return indptr, dst


def select_region_by_normal(me, face_index, angle, connected):
    npoly = len(me.polygons)
    normals = np.empty(npoly * 3, dtype=np.float32)
    me.polygons.foreach_get("normal", normals)
    normals = normals.reshape(-1, 3)
    ok = normals @ normals[face_index] >= np.cos(angle) - 1e-6

    if connected:
        indptr, nbr = _face_adjacency(me)
        sel = np.zeros(npoly, dtype=bool)
        sel[face_index] = True
        stack = [face_index]
        while stack:
            f = stack.pop()
            for g in nbr[indptr[f]:indptr[f + 1]]:
                if ok[g] and not sel[g]:
                    sel[g] = True
                    stack.append(int(g))
    else:
        sel = ok

    me.polygons.foreach_set("select", sel)
    me.use_paint_mask = True
    me.update()
    bpy.context.evaluated_depsgraph_get()  # make sure the stroke already sees the new mask
    return int(sel.sum())


def _viewport_under_mouse(context, event):
    """Return (region, rv3d, coord) of the 3D viewport under the mouse, ignoring
    sidebar/toolbar/header (which may overlap the viewport)."""
    mx, my = event.mouse_x, event.mouse_y
    for area in context.window.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        if not (area.x <= mx < area.x + area.width and area.y <= my < area.y + area.height):
            continue
        main = None
        for r in area.regions:
            inside = r.x <= mx < r.x + r.width and r.y <= my < r.y + r.height
            if not inside or r.width <= 1:
                continue
            if r.type == 'WINDOW':
                main = r
            else:
                return None  # click on a panel/header
        if main is None:
            return None
        rv3d = area.spaces.active.region_3d
        return main, rv3d, (mx - main.x, my - main.y)
    return None


class PTL_OT_stroke_watch(bpy.types.Operator):
    """Watch the click that starts each stroke (normal restriction and color variation)"""
    bl_idname = "painting_tools.stroke_watch"
    bl_label = "Watch strokes"
    bl_options = {'INTERNAL'}

    _running = False

    def invoke(self, context, event):
        if PTL_OT_stroke_watch._running:
            return {'CANCELLED'}
        PTL_OT_stroke_watch._running = True
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        props = context.scene.painting_tools
        if not _watch_needed(props):
            # it may have been started through a context override: never end by passing the
            # event (Blender hangs). End on a TIMER/MOUSEMOVE, consuming it.
            if event.type in {'TIMER', 'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
                PTL_OT_stroke_watch._running = False
                return {'FINISHED'}
            return {'PASS_THROUGH'}
        if (event.type == 'LEFTMOUSE' and event.value == 'PRESS'
                and context.mode == 'PAINT_TEXTURE'):
            try:
                hit_view = _viewport_under_mouse(context, event)
                if hit_view is not None:
                    if props.region_mode:
                        self._pick(context, hit_view, props)
                    if props.dual_enable and props.dual_a_image and props.dual_b_image:
                        dual_begin(context, hit_view, event, props)
                    elif props.color_vary:
                        vary_stroke_color(context, props)
            except Exception as e:  # never block the stroke because of the add-on
                print("Texturing Tools:", e)
        return {'PASS_THROUGH'}

    def cancel(self, context):
        PTL_OT_stroke_watch._running = False

    def _pick(self, context, hit_view, props):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return
        if props.region_lock and obj.data.use_paint_mask:
            return
        region, rv3d, coord = hit_view
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        inv = obj.matrix_world.inverted()
        o = inv @ origin
        d = (inv.to_3x3() @ direction).normalized()
        hit, _loc, _nrm, idx = obj.ray_cast(o, d)
        if hit and idx >= 0:
            select_region_by_normal(obj.data, idx, props.region_angle, props.region_connected)


class PTL_OT_region_clear(bpy.types.Operator):
    bl_idname = "painting_tools.region_clear"
    bl_label = "Release region"
    bl_description = "Removes the current mask so you can pick another region on the next click"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if obj and obj.type == 'MESH':
            obj.data.use_paint_mask = False
            obj.data.update()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Blur: on the mesh surface (ignores seams) or on the whole texture (UV, 2D)
# ---------------------------------------------------------------------------

_BLUR_BACKUPS = {}  # image name -> list of arrays (h, w, 4)
_BLUR_MAX_BACKUPS = 5


def _box_blur_axis(a, r, axis, wrap):
    """Separable box blur along one axis, using a cumulative sum."""
    if r < 1:
        return a
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r, r)
    p = np.pad(a, pad, mode="wrap" if wrap else "edge")
    c = np.cumsum(p, axis=axis, dtype=np.float32)
    zshape = list(c.shape)
    zshape[axis] = 1
    c = np.concatenate([np.zeros(zshape, dtype=np.float32), c], axis=axis)
    hi = np.take(c, np.arange(2 * r + 1, n + 2 * r + 1), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return (hi - lo) / float(2 * r + 1)


def _blur_array(px, radius, passes, wrap):
    """3 passes de box blur ~ gaussian blur."""
    out = px
    for _ in range(passes):
        out = _box_blur_axis(out, radius, 0, wrap)
        out = _box_blur_axis(out, radius, 1, wrap)
    return out


def _blur_pixels(px, props, wrap, expand_alpha):
    """Blur with premultiplied alpha: transparent pixels don't dirty the color,
    so the color spreads outward instead of darkening/shrinking at the edge."""
    passes = 3 if props.blur_type == 'GAUSSIAN' else 1
    # with 3 passes the effective radius grows; compensate so the value feels intuitive
    r = props.blur_radius if passes == 1 else max(1, round(props.blur_radius / 1.7))
    alpha = px[..., 3:4]
    prem = px.copy()
    prem[..., :3] *= alpha
    b = _blur_array(prem, r, passes, wrap)
    b_alpha = b[..., 3:4]
    rgb = b[..., :3] / np.maximum(b_alpha, 1e-6)
    rgb = np.where(b_alpha > 1e-6, rgb, px[..., :3])
    blurred = np.empty_like(px)
    blurred[..., :3] = rgb
    blurred[..., 3:4] = b_alpha if expand_alpha else alpha
    k = props.blur_strength
    return px * (1.0 - k) + blurred * k


def _read_pixels(img):
    w, h = img.size
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    return px.reshape(h, w, 4)


def _blur_backup(img):
    """Store the current state for Undo. 8-bit images are stored as uint8
    (4x less memory, lossless: their pixels are already multiples of 1/255)."""
    stack = _BLUR_BACKUPS.setdefault(img.name, [])
    px = _read_pixels(img)
    if not img.is_float:
        px = np.rint(px * 255.0).astype(np.uint8)
    stack.append(px)
    if len(stack) > _BLUR_MAX_BACKUPS:
        stack.pop(0)


def _backup_pixels(a):
    """Convert a backup back to float32 (h, w, 4)."""
    if a.dtype == np.uint8:
        return a.astype(np.float32) * np.float32(1.0 / 255.0)
    return a


def _paint_target_image(context):
    """Active image: the one in the Image Editor, or the one being painted in 3D
    (canvas in Image mode, or the material's active slot)."""
    space = getattr(context, "space_data", None)
    if space is not None and space.type == 'IMAGE_EDITOR':
        return space.image
    ip = context.tool_settings.image_paint
    if ip.mode == 'IMAGE' and ip.canvas:
        return ip.canvas
    obj = context.active_object
    if obj and obj.active_material:
        mat = obj.active_material
        imgs = getattr(mat, "texture_paint_images", None)
        if imgs:
            idx = mat.paint_active_slot
            if 0 <= idx < len(imgs):
                return imgs[idx]
    return None


def _refresh_image(img):
    """After changing pixels from a script, the viewport may keep showing the old
    version: it reuses the last drawing while nothing in the scene changes (it only redraws
    when you rotate the view). Right after the operator, recreate the GPU texture (scale() to the same
    size doesn't change any pixel) and tag the active object so the viewport renders again.
    Don't use img.update_tag(): on a Generated image that erases the painting."""
    img.update()
    name = img.name

    def _later():
        im = bpy.data.images.get(name)
        if im is not None and im.size[0] and im.size[1]:
            im.scale(im.size[0], im.size[1])
        try:
            obj = bpy.context.view_layer.objects.active
            if obj is not None:
                obj.update_tag()
        except Exception:
            pass
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
        return None

    bpy.app.timers.register(_later, first_interval=0.05)


def _redraw_all(context):
    for area in context.screen.areas:
        area.tag_redraw()


# --- Blur on the surface (whole mesh, ignores UV and view) -------------------

def _material_uses_image(mat, img):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    seen = set()
    stack = [mat.node_tree]
    while stack:
        nt = stack.pop()
        if nt is None or nt.as_pointer() in seen:
            continue
        seen.add(nt.as_pointer())
        for n in nt.nodes:
            if getattr(n, "image", None) == img:
                return True
            if n.type == 'GROUP' and n.node_tree is not None:
                stack.append(n.node_tree)
    return False


def _paint_uv_name(context, obj):
    ip = context.tool_settings.image_paint
    me = obj.data
    if ip.mode == 'MATERIAL' and obj.active_material:
        mat = obj.active_material
        slots = getattr(mat, "texture_paint_slots", None)
        i = mat.paint_active_slot
        if slots and 0 <= i < len(slots):
            name = getattr(slots[i], "uv_layer", "")
            if name and name in me.uv_layers:
                return name
    return me.uv_layers.active.name if me.uv_layers.active else None


def _surface_sample(me, mw, uv_name, tri_ok, tri_sel, w, h, want_normals=False):
    """Map each texture pixel to a point on the 3D surface (in world space).
    Returns (covered, sel_covered, pos[, nrm]) as flat arrays of w*h."""
    lt = me.loop_triangles
    nt = len(lt)
    tri_loops = np.empty(nt * 3, np.int32)
    lt.foreach_get("loops", tri_loops)
    tri_verts = np.empty(nt * 3, np.int32)
    lt.foreach_get("vertices", tri_verts)

    uvl = me.uv_layers[uv_name]
    uv = np.empty(len(me.loops) * 2, np.float32)
    uvl.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)

    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    M = np.array(mw, dtype=np.float32)
    co = co @ M[:3, :3].T + M[:3, 3]

    uvp = (uv[tri_loops] * np.array([w, h], np.float32)).reshape(nt, 3, 2)
    P = co[tri_verts].reshape(nt, 3, 3)
    if want_normals:
        vn = np.empty(len(me.vertices) * 3, np.float32)
        me.vertex_normals.foreach_get("vector", vn)
        vn = vn.reshape(-1, 3) @ np.linalg.inv(M[:3, :3])  # inverse transpose
        vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
        NV = vn[tri_verts].reshape(nt, 3, 3)
        nrm = np.zeros((w * h, 3), np.float32)

    e = np.stack([uvp[:, 1] - uvp[:, 0], uvp[:, 2] - uvp[:, 1], uvp[:, 0] - uvp[:, 2]], 1)
    maxe = np.sqrt((e * e).sum(-1)).max(1)
    # sampling spacing <= 0.5 px: no inner pixel is left without a sample
    m = np.clip(np.ceil(maxe * 2.0).astype(np.int64) + 1, 1, 4096)

    covered = np.zeros(w * h, bool)
    selcov = np.zeros(w * h, bool)
    pos = np.zeros((w * h, 3), np.float32)

    for mv in np.unique(m[tri_ok]):
        tris = np.nonzero(tri_ok & (m == mv))[0]
        ii, jj = np.meshgrid(np.arange(mv + 1), np.arange(mv + 1), indexing="ij")
        keep = (ii + jj) <= mv
        b1 = (ii[keep] / mv).astype(np.float32)
        b2 = (jj[keep] / mv).astype(np.float32)
        b0 = 1.0 - b1 - b2
        npts = len(b0)
        chunk = max(1, 2_000_000 // npts)
        for k in range(0, len(tris), chunk):
            t = tris[k:k + chunk]
            U = uvp[t]
            pu = b0[None] * U[:, 0, 0:1] + b1[None] * U[:, 1, 0:1] + b2[None] * U[:, 2, 0:1]
            pv = b0[None] * U[:, 0, 1:2] + b1[None] * U[:, 1, 1:2] + b2[None] * U[:, 2, 1:2]
            px = np.floor(pu).astype(np.int64) % w
            py = np.floor(pv).astype(np.int64) % h
            idx = (py * w + px).ravel()
            Q = P[t]
            pts = (b0[None, :, None] * Q[:, None, 0] + b1[None, :, None] * Q[:, None, 1]
                   + b2[None, :, None] * Q[:, None, 2]).reshape(-1, 3)
            covered[idx] = True
            pos[idx] = pts
            s_mask = np.repeat(tri_sel[t], npts)
            if want_normals:
                Nq = NV[t]
                nn = (b0[None, :, None] * Nq[:, None, 0] + b1[None, :, None] * Nq[:, None, 1]
                      + b2[None, :, None] * Nq[:, None, 2]).reshape(-1, 3)
                nrm[idx] = nn
            if s_mask.any():
                selcov[idx[s_mask]] = True
                pos[idx[s_mask]] = pts[s_mask]  # border pixel: the selected face wins
                if want_normals:
                    nrm[idx[s_mask]] = nn[s_mask]
    if want_normals:
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
        return covered, selcov, pos, nrm
    return covered, selcov, pos


def _lookup(keys, q):
    i = np.searchsorted(keys, q)
    i = np.minimum(i, len(keys) - 1)
    return i, keys[i] == q


_MAX_BLUR_CELLS = 800_000


_CORNERS = [(dx, dy, dz) for dx in (0, 1) for dy in (0, 1) for dz in (0, 1)]


def _corner_weights(f):
    """Trilinear weights (8, N) for the fractions f (N, 3)."""
    fx, fy, fz = f[:, 0], f[:, 1], f[:, 2]
    gx, gy, gz = 1.0 - fx, 1.0 - fy, 1.0 - fz
    out = []
    for dx, dy, dz in _CORNERS:
        out.append((fx if dx else gx) * (fy if dy else gy) * (fz if dz else gz))
    return out


_DENSE_LIMIT = 40_000_000  # cells: up to here use direct maps (fast); above, binary search


_BRICK = 8          # block side (8x8x8 = 512 cells)
_BRICK_GRID_LIMIT = 50_000_000


class _CellIndex:
    """Sorted set of cell keys with fast lookup.
    - dense: mask + map over the whole grid (direct access; small grids)
    - blocks: the grid is split into 8x8x8 blocks and only blocks with cells get a
      direct map (index access, memory proportional to the surface)
    - sparse: sorted keys + searchsorted (fallback)"""

    def __init__(self, keys_or_mask, dense, dims=None):
        self.mode = 'dense' if dense else 'sparse'
        if dense:
            self.mask = keys_or_mask
            self.keys = np.flatnonzero(self.mask)
            self.map = np.cumsum(self.mask, dtype=np.int32) - 1
            return
        self.keys = keys_or_mask
        if dims is None:
            return
        DX, DY, DZ = (int(d) for d in dims)
        BX, BY, BZ = (-(-d // _BRICK) for d in (DX, DY, DZ))
        if BX * BY * BZ > _BRICK_GRID_LIMIT or len(self.keys) >= 2 ** 31 - 1:
            return
        self.mode = 'brick'
        self.DZ, self.DYZ, self.BY, self.BZ = DZ, DY * DZ, BY, BZ
        bl, loc = self._split(self.keys)
        ubr = np.unique(bl)
        self.bmap = np.full(BX * BY * BZ, -1, np.int32)
        self.bmap[ubr] = np.arange(len(ubr), dtype=np.int32)
        self.cmap = np.full(len(ubr) * _BRICK ** 3, -1, np.int32)
        self.cmap[self.bmap[bl].astype(np.int64) * _BRICK ** 3 + loc] = np.arange(len(self.keys), dtype=np.int32)

    def _split(self, q):
        x = q // self.DYZ
        r = q - x * self.DYZ
        y = r // self.DZ
        z = r - y * self.DZ
        bl = ((x // _BRICK) * self.BY + (y // _BRICK)) * self.BZ + (z // _BRICK)
        loc = ((x % _BRICK) * _BRICK + (y % _BRICK)) * _BRICK + (z % _BRICK)
        return bl, loc

    def __len__(self):
        return len(self.keys)

    def find(self, q):
        if self.mode == 'dense':
            return self.map[q], self.mask[q]
        if self.mode == 'brick':
            bl, loc = self._split(q)
            slot = self.bmap[bl]
            okb = slot >= 0
            cell = np.where(okb, slot.astype(np.int64) * _BRICK ** 3 + loc, 0)
            idx = self.cmap[cell]
            ok = okb & (idx >= 0)
            return np.where(ok, idx, 0), ok
        return _lookup(self.keys, q)


def _dilate_keys(keys, deltas, dims):
    """Union of keys + d for each d in deltas, without sorting tens of millions of keys:
    cells are marked in 8x8x8 blocks (only touched blocks use memory).
    If the block grid is too large, falls back to np.unique."""
    DX, DY, DZ = (int(d) for d in dims)
    BX, BY, BZ = (-(-d // _BRICK) for d in (DX, DY, DZ))
    if BX * BY * BZ > _BRICK_GRID_LIMIT:
        return np.unique((keys[:, None] + np.asarray(deltas)[None, :]).ravel())
    DYZ = DY * DZ
    B3 = _BRICK ** 3

    def split(q):
        x = q // DYZ
        r = q - x * DYZ
        y = r // DZ
        z = r - y * DZ
        return (((x // _BRICK) * BY + (y // _BRICK)) * BZ + (z // _BRICK),
                ((x % _BRICK) * _BRICK + (y % _BRICK)) * _BRICK + (z % _BRICK))

    bmask = np.zeros(BX * BY * BZ, bool)
    parts = []
    for d in deltas:
        bl, loc = split(keys + d)
        bmask[bl] = True
        parts.append((bl, loc))
    bricks = np.flatnonzero(bmask)
    bmap = np.full(BX * BY * BZ, -1, np.int32)
    bmap[bricks] = np.arange(len(bricks), dtype=np.int32)
    del bmask
    mark = np.zeros(len(bricks) * B3, bool)
    for bl, loc in parts:
        mark[bmap[bl].astype(np.int64) * B3 + loc] = True
    del parts
    cells = np.flatnonzero(mark)  # already in block order; order doesn't matter in block mode
    slot, loc = cells // B3, cells % B3
    b = bricks[slot]
    bz = b % BZ
    by = (b // BZ) % BY
    bx = b // (BY * BZ)
    lz = loc % _BRICK
    ly = (loc // _BRICK) % _BRICK
    lx = loc // (_BRICK * _BRICK)
    return (bx * _BRICK + lx) * DYZ + (by * _BRICK + ly) * DZ + (bz * _BRICK + lz)


def _grid_blur(pos, vals, radius, min_cell, area, res=3):
    """3D Gaussian along the surface.

    1. Each pixel is splatted (trilinear) onto the 8 grid nodes around it.
       Pixels that fall in the same cell are summed together (bincount), so the
       heavy work is done per cell, not per pixel.
    2. Separable Gaussian (x, y, z) only on the nodes near the surface.
    3. Read back with the same trilinear weights.
    vals: (N, C). Returns (mean (N, C), cell size)."""
    sigma = max(radius / 2.0, 1e-6)
    cs = max(radius / max(res, 1), min_cell, 1e-6)
    # limit the number of cells (memory); very small radii come out a bit smoother
    if area > 0 and area / (cs * cs) > _MAX_BLUR_CELLS:
        cs = math.sqrt(area / _MAX_BLUR_CELLS)
    k = int(max(1, min(16, math.ceil(2.0 * sigma / cs))))
    sc = max(sigma / cs, 0.35)
    w1 = np.exp(-0.5 * (np.arange(-k, k + 1) / sc) ** 2)
    w1 = (w1 / w1.sum()).astype(np.float32)

    g = (pos - pos.min(0)) / cs
    b = np.floor(g).astype(np.int64)
    f = (g - b).astype(np.float32)
    del g
    sh = 2 * k + 3  # margin: nodes (+1) + dilation (k) + Gaussian steps (k)
    dims = b.max(0) + 2 * sh + 3
    DZ = int(dims[2])
    DYZ = int(dims[1] * dims[2])
    P = int(dims[0]) * DYZ
    dense = P <= _DENSE_LIMIT
    offs_c = [dx * DYZ + dy * DZ + dz for dx, dy, dz in _CORNERS]

    bk = (b[:, 0] + sh) * DYZ + (b[:, 1] + sh) * DZ + (b[:, 2] + sh)
    del b
    if dense:
        occ = _CellIndex(np.zeros(P, bool), True)
        occ.mask[bk] = True
        occ = _CellIndex(occ.mask, True)
        binv = occ.map[bk]
        ub = occ.keys
        del occ
    else:
        ub, binv = np.unique(bk, return_inverse=True)
    del bk
    U = len(ub)
    W = _corner_weights(f)
    del f

    if dense:
        m = np.zeros(P, bool)
        for o in offs_c:
            m[ub + o] = True
        nodes = _CellIndex(m, True)
    else:
        nodes = _CellIndex(np.unique(np.concatenate([ub + o for o in offs_c])), False, dims)
    C = vals.shape[1]
    S = np.zeros((len(nodes), C + 1))
    for wc, o in zip(W, offs_c):
        idx, _ok = nodes.find(ub + o)
        for ch in range(C):
            S[idx, ch] += np.bincount(binv, weights=wc * vals[:, ch], minlength=U)
        S[idx, C] += np.bincount(binv, weights=wc, minlength=U)

    # Gaussian domain: nodes dilated k times on each axis
    if dense:
        dm = nodes.mask.reshape(int(dims[0]), int(dims[1]), DZ)
        for axis in range(3):
            src = dm
            dm = src.copy()
            n = src.shape[axis]
            for st in range(1, k + 1):
                a = [slice(None)] * 3
                bb = [slice(None)] * 3
                a[axis] = slice(st, n)
                bb[axis] = slice(0, n - st)
                dm[tuple(a)] |= src[tuple(bb)]
                dm[tuple(bb)] |= src[tuple(a)]
        D = _CellIndex(dm.ravel(), True)
        del dm
    else:
        offs = np.arange(-k, k + 1, dtype=np.int64)
        Dk = nodes.keys
        for stride in (DYZ, DZ, 1):
            Dk = _dilate_keys(Dk, offs * stride, dims)
        D = _CellIndex(Dk, False, dims)
    nD = len(D)
    V = np.zeros((nD + 1, C + 1), np.float32)  # last row = zero (missing neighbor)
    V[D.find(nodes.keys)[0]] = S
    del S, nodes
    for stride in (DYZ, DZ, 1):
        Vn = np.zeros_like(V)
        for j, o in enumerate(range(-k, k + 1)):
            i, ok = D.find(D.keys + o * stride)
            i = np.where(ok, i, nD)
            Vn[:nD] += w1[j] * V[i]
        V = Vn

    out = np.zeros((len(pos), C + 1), np.float32)
    for wc, o in zip(W, offs_c):
        Vc = V[D.find(ub + o)[0]]  # corner value, once per cell
        out += wc[:, None] * Vc[binv]
    return out[:, :C] / np.maximum(out[:, C:C + 1], 1e-12), cs


def _dilate_into(img_px, filled, allowed, iters):
    """Spread the 'filled' pixels to the 'allowed' neighbors (UV island margin/gutter).
    Only the pixels on the growing edge are computed."""
    H, Wd = filled.shape
    flat = img_px.reshape(-1, img_px.shape[-1])
    fil = filled.ravel().copy()
    allw = allowed.ravel()
    for _ in range(iters):
        f2 = fil.reshape(H, Wd)
        nb = (np.roll(f2, 1, 0) | np.roll(f2, -1, 0) | np.roll(f2, 1, 1) | np.roll(f2, -1, 1)).ravel()
        idx = np.flatnonzero(allw & ~fil & nb)
        if len(idx) == 0:
            break
        y, x = idx // Wd, idx % Wd
        acc = np.zeros((len(idx), flat.shape[1]), np.float32)
        cnt = np.zeros(len(idx), np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            j = ((y + dy) % H) * Wd + (x + dx) % Wd
            mk = fil[j]
            acc += flat[j] * mk[:, None]
            cnt += mk
        flat[idx] = acc / cnt[:, None]
        fil[idx] = True


_PREP_CACHE = {}


def _prepare_key(context, obj, img, want_normals):
    """Signature of everything that changes the pixel -> surface mapping."""
    import hashlib
    me = obj.data
    hsh = hashlib.md5()
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    hsh.update(co.tobytes())
    uv_name = _paint_uv_name(context, obj)
    if uv_name:
        uv = np.empty(len(me.loops) * 2, np.float32)
        me.uv_layers[uv_name].data.foreach_get("uv", uv)
        hsh.update(uv.tobytes())
    npoly = len(me.polygons)
    mi = np.empty(npoly, np.int32)
    me.polygons.foreach_get("material_index", mi)
    hsh.update(mi.tobytes())
    if me.use_paint_mask:
        sel = np.empty(npoly, bool)
        me.polygons.foreach_get("select", sel)
        hsh.update(sel.tobytes())
    hsh.update(np.array(obj.matrix_world, np.float32).tobytes())
    hsh.update(repr((obj.name, me.as_pointer(), len(me.loops), uv_name, tuple(img.size), img.name,
                     me.use_paint_mask, [s.material.name if s.material else "" for s in obj.material_slots]
                     )).encode())
    return hsh.hexdigest(), want_normals


def _surface_prepare(context, obj, img, want_normals=False):
    """Map the image pixels onto the mesh surface (respecting material and Face Mask).
    The result is cached while the mesh, UV, selection and image don't change."""
    key, _ = _prepare_key(context, obj, img, want_normals)
    cached = _PREP_CACHE.get("info")
    if cached is not None and _PREP_CACHE.get("key") == key and (cached["nrm"] is not None or not want_normals):
        return cached
    _PREP_CACHE.clear()
    info = _surface_prepare_uncached(context, obj, img, want_normals)
    _PREP_CACHE.update(key=key, info=info)
    return info


def _surface_prepare_uncached(context, obj, img, want_normals=False):
    me = obj.data
    if not me.uv_layers:
        raise RuntimeError("the mesh has no UV")
    uv_name = _paint_uv_name(context, obj)
    w, h = img.size
    if w == 0 or h == 0:
        raise RuntimeError("image empty or not loaded")
    me.calc_loop_triangles()
    lt = me.loop_triangles
    nt = len(lt)
    tri_poly = np.empty(nt, np.int32)
    lt.foreach_get("polygon_index", tri_poly)

    npoly = len(me.polygons)
    mat_idx = np.empty(npoly, np.int32)
    me.polygons.foreach_get("material_index", mat_idx)
    slots_using = [i for i, sl in enumerate(obj.material_slots) if _material_uses_image(sl.material, img)]
    poly_ok = np.isin(mat_idx, slots_using) if slots_using else np.ones(npoly, bool)
    if me.use_paint_mask:
        sel = np.empty(npoly, bool)
        me.polygons.foreach_get("select", sel)
        poly_sel = poly_ok & sel
    else:
        poly_sel = poly_ok
    if not poly_sel.any():
        raise RuntimeError("no faces selected" if me.use_paint_mask else "no face uses this image")

    res = _surface_sample(me, obj.matrix_world, uv_name, poly_ok[tri_poly], poly_sel[tri_poly],
                          w, h, want_normals)
    covered, selcov, pos = res[:3]
    tex = np.nonzero(selcov)[0]
    if len(tex) == 0:
        raise RuntimeError("the faces don't cover any pixel of the image")

    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    M = np.array(obj.matrix_world, np.float32)
    cw = co.reshape(-1, 3) @ M[:3, :3].T
    tv = np.empty(nt * 3, np.int32)
    lt.foreach_get("vertices", tv)
    T = cw[tv].reshape(nt, 3, 3)[poly_sel[tri_poly]]
    area = float(0.5 * np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1).sum())
    texel = math.sqrt(area / len(tex)) if area > 0 else 0.0
    return dict(w=w, h=h, covered=covered, tex=tex, pos=pos[tex],
                nrm=res[3][tex] if want_normals else None, area=area, texel=texel)


def _surface_finish(context, img, px, info):
    """Update the margin (gutter) around the islands and write the image."""
    w, h = info["w"], info["h"]
    filled = np.zeros(w * h, bool)
    filled[info["tex"]] = True
    bleed = context.tool_settings.image_paint.seam_bleed + 2
    img2 = px.reshape(h, w, 4)
    _dilate_into(img2, filled.reshape(h, w), (~info["covered"]).reshape(h, w), bleed)
    img.pixels.foreach_set(img2.ravel())
    _refresh_image(img)


def surface_blur(context, obj, img, radius, strength, expand, res=3):
    info = _surface_prepare(context, obj, img)
    tex = info["tex"]
    px = _read_pixels(img).reshape(-1, 4)
    orig = px[tex]
    prem = orig.copy()
    prem[:, :3] *= orig[:, 3:4]

    avg, _cs = _grid_blur(info["pos"], prem, radius, info["texel"], info["area"], res)
    a = avg[:, 3:4]
    rgb = np.where(a > 1e-6, avg[:, :3] / np.maximum(a, 1e-6), orig[:, :3])
    blurred = np.empty_like(orig)
    blurred[:, :3] = rgb
    blurred[:, 3:4] = a if expand else orig[:, 3:4]
    px[tex] = orig * (1.0 - strength) + blurred * strength
    _surface_finish(context, img, px, info)
    return len(tex)


# --- Distort on the surface ------------------------------------------------

class _SurfaceSampler:
    """Read the paint color at any 3D point near the surface (sparse grid with
    trilinear interpolation; if there are no pixels nearby, tries a coarser grid).
    Lookups are done per cell (points in the same cell share the neighbors)."""

    def __init__(self, pos, vals, cs, levels=5):
        self.C = vals.shape[1]
        margin = cs * (2 ** levels) * 4
        self.origin = pos.min(0) - margin
        top = pos.max(0) + margin
        self.levels = []
        ci = np.floor((pos - self.origin) / cs).astype(np.int64)
        dims = np.ceil((top - self.origin) / cs).astype(np.int64) + 2
        keys, inv = np.unique(self._enc(ci, dims), return_inverse=True)
        S = np.empty((len(keys), self.C + 1), np.float32)
        for ch in range(self.C):
            S[:, ch] = np.bincount(inv, weights=vals[:, ch], minlength=len(keys))
        S[:, self.C] = np.bincount(inv, minlength=len(keys))
        cells = self._dec(keys, dims)
        self.levels.append((cs, dims, keys, S))
        for L in range(1, levels):
            c = cs * (2 ** L)
            dims = np.ceil((top - self.origin) / c).astype(np.int64) + 2
            cells = cells // 2
            k2, inv2 = np.unique(self._enc(cells, dims), return_inverse=True)
            S2 = np.empty((len(k2), self.C + 1), np.float32)
            for ch in range(self.C + 1):
                S2[:, ch] = np.bincount(inv2, weights=S[:, ch], minlength=len(k2))
            cells = self._dec(k2, dims)
            S = S2
            self.levels.append((c, dims, k2, S2))

    @staticmethod
    def _enc(ci, dims):
        ci = np.clip(ci, 0, dims - 1)
        return (ci[:, 0] * dims[1] + ci[:, 1]) * dims[2] + ci[:, 2]

    @staticmethod
    def _dec(keys, dims):
        z = keys % dims[2]
        y = (keys // dims[2]) % dims[1]
        x = keys // (dims[1] * dims[2])
        return np.stack([x, y, z], 1)

    def sample(self, q):
        C = self.C
        out = np.zeros((len(q), C), np.float32)
        done = np.zeros(len(q), bool)
        for c, dims, keys, S in self.levels:
            idx = np.nonzero(~done)[0]
            if len(idx) == 0:
                break
            g = (q[idx] - self.origin) / c - 0.5
            b = np.floor(g).astype(np.int64)
            f = (g - b).astype(np.float32)
            ub, qinv = np.unique(self._enc(b, dims), return_inverse=True)
            DZ, DYZ = int(dims[2]), int(dims[1] * dims[2])
            acc = np.zeros((len(idx), C + 1), np.float32)
            for wc, (dx, dy, dz) in zip(_corner_weights(f), _CORNERS):
                i, ok = _lookup(keys, ub + dx * DYZ + dy * DZ + dz)
                Sc = S[i] * ok[:, None]  # one lookup per cell
                acc += wc[:, None] * Sc[qinv]
            good = acc[:, C] > 1e-6
            out[idx[good]] = acc[good, :C] / acc[good, C:C + 1]
            done[idx[good]] = True
        return out, done


_GRAD3 = np.array([(1, 1, 0), (-1, 1, 0), (1, -1, 0), (-1, -1, 0), (1, 0, 1), (-1, 0, 1),
                   (1, 0, -1), (-1, 0, -1), (0, 1, 1), (0, -1, 1), (0, 1, -1), (0, -1, -1),
                   (1, 1, 0), (-1, 1, 0), (0, -1, 1), (0, -1, -1)], np.float32)


def _perlin3(p, perm):
    """Vectorized 3D Perlin noise. p: (N, 3). Returns (N,) in ~[-1, 1]."""
    pi = np.floor(p).astype(np.int64)
    pf = (p - pi).astype(np.float32)
    pi &= 255
    u = pf * pf * pf * (pf * (pf * 6 - 15) + 10)
    X, Y, Z = pi[:, 0], pi[:, 1], pi[:, 2]
    res = None
    vals = {}
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                hsh = perm[perm[perm[X + dx] + Y + dy] + Z + dz] & 15
                d = pf - np.array([dx, dy, dz], np.float32)
                vals[(dx, dy, dz)] = (_GRAD3[hsh] * d).sum(1)
    lx = lambda a, b: a + u[:, 0] * (b - a)
    ly = lambda a, b: a + u[:, 1] * (b - a)
    x00 = lx(vals[(0, 0, 0)], vals[(1, 0, 0)])
    x10 = lx(vals[(0, 1, 0)], vals[(1, 1, 0)])
    x01 = lx(vals[(0, 0, 1)], vals[(1, 0, 1)])
    x11 = lx(vals[(0, 1, 1)], vals[(1, 1, 1)])
    y0 = ly(x00, x10)
    y1 = ly(x01, x11)
    res = y0 + u[:, 2] * (y1 - y0)
    return res


def _fbm_vector(p, octaves, seed):
    rng = np.random.default_rng(seed)
    perm = rng.permutation(256)
    perm = np.concatenate([perm, perm, perm]).astype(np.int64)
    offs = rng.uniform(0, 256, (3, 3)).astype(np.float32)
    out = np.zeros((len(p), 3), np.float32)
    amp, freq, total = 1.0, 1.0, 0.0
    for _ in range(max(1, octaves)):
        for c in range(3):
            out[:, c] += amp * _perlin3(p * freq + offs[c], perm)
        total += amp
        amp *= 0.5
        freq *= 2.0
    return out / total * 1.8  # ~[-1, 1]


def _tangent(v, n):
    return v - (v * n).sum(1, keepdims=True) * n


_AXES = {'X': (1.0, 0.0, 0.0), 'Y': (0.0, 1.0, 0.0), 'Z': (0.0, 0.0, 1.0)}


def _distort_offsets(props, P, N):
    if props.distort_type == 'TURBULENT':
        size = max(props.turb_size, 1e-6)
        D = _fbm_vector(P / size, props.turb_complexity, props.turb_seed) * props.turb_amount
        return _tangent(D, N)
    k = np.array(_AXES[props.wave_axis], np.float32)
    lam = max(props.wave_length, 1e-6)
    s = np.sin(2.0 * math.pi * (P @ k) / lam + props.wave_phase).astype(np.float32)
    if props.wave_mode == 'LATERAL':
        t = np.cross(N, k)  # perpendicular to the wave direction, on the surface
    else:
        t = _tangent(np.broadcast_to(k, N.shape).copy(), N)  # along the direction (ripple)
    return (props.wave_amp * s)[:, None] * t


def surface_distort(context, obj, img, props):
    info = _surface_prepare(context, obj, img, want_normals=True)
    tex = info["tex"]
    P, N = info["pos"], info["nrm"]
    px = _read_pixels(img).reshape(-1, 4)
    orig = px[tex]
    prem = orig.copy()
    prem[:, :3] *= orig[:, 3:4]

    sampler = _SurfaceSampler(P, prem, max(info["texel"] * 0.75, 1e-6))
    D = _distort_offsets(props, P, N)
    val, ok = sampler.sample(P + D)
    a = val[:, 3:4]
    rgb = np.where(a > 1e-6, val[:, :3] / np.maximum(a, 1e-6), orig[:, :3])
    moved = np.concatenate([rgb, a], 1)
    moved[~ok] = orig[~ok]
    k = props.distort_strength
    px[tex] = orig * (1.0 - k) + moved * k
    _surface_finish(context, img, px, info)
    return len(tex)


# --- Normals from colors -----------------------------------------------------

def _connected_components(n, ea, eb):
    """Connected component labels (vectorized union-find: hooks + pointer jumping).
    Returns, for each element, the smallest index of its component."""
    lab = np.arange(n, dtype=np.int64)
    if len(ea) == 0:
        return lab
    while True:
        la, lb = lab[ea], lab[eb]
        diff = la != lb
        if not diff.any():
            break
        la, lb = la[diff], lb[diff]
        np.minimum.at(lab, np.maximum(la, lb), np.minimum(la, lb))
        while True:
            nl = lab[lab]
            if np.array_equal(nl, lab):
                break
            lab = nl
        ea, eb = ea[diff], eb[diff]
    return lab


def _hash01(c, salt):
    """Stable pseudo-random number in [0, 1) for each integer cell (N, 3)."""
    h = (c[:, 0] * 73856093) ^ (c[:, 1] * 19349663) ^ (c[:, 2] * 83492791) ^ (salt * 2654435761)
    h = (h ^ (h >> 13)) * 1274126177
    h = h ^ (h >> 16)
    return ((h & 0xFFFFFF).astype(np.float32)) / float(0x1000000)


def _voronoi_ids(P, L):
    """Voronoi cell (jittered seeds on a grid of side L) of each point."""
    g = (P / L).astype(np.float64)
    c0 = np.floor(g).astype(np.int64)
    best_d = np.full(len(P), np.inf)
    best = np.zeros((len(P), 3), np.int64)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                c = c0 + (dx, dy, dz)
                sp = c + np.stack([_hash01(c, 1), _hash01(c, 2), _hash01(c, 3)], 1)
                d = ((g - sp) ** 2).sum(1)
                m = d < best_d
                best_d[m] = d[m]
                best[m] = c[m]
    best -= best.min(0)
    dims = best.max(0) + 1
    return (best[:, 0] * dims[1] + best[:, 1]) * dims[2] + best[:, 2]


def _seam_edges(idx_border, pos, keys, radius):
    """Pairs of island-border pixels that touch in 3D (on both sides of a seam)
    and have the same color. idx_border: (compact) indices of the border pixels."""
    if len(idx_border) < 2:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    P = pos[idx_border]
    c = np.floor((P - P.min(0)) / radius).astype(np.int64) + 1
    dims = c.max(0) + 2
    ck = (c[:, 0] * dims[1] + c[:, 1]) * dims[2] + c[:, 2]
    order = np.argsort(ck, kind="stable")
    sk = ck[order]
    ea, eb = [], []
    K = 6  # candidates per neighbor cell
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                q = ck + (dx * dims[1] + dy) * dims[2] + dz
                lo = np.searchsorted(sk, q, "left")
                hi = np.searchsorted(sk, q, "right")
                for t in range(K):
                    j = lo + t
                    ok = j < hi
                    if not ok.any():
                        break
                    a = np.nonzero(ok)[0]
                    b = order[j[ok]]
                    keep = a < b
                    a, b = a[keep], b[keep]
                    ga, gb = idx_border[a], idx_border[b]
                    close = ((pos[ga] - pos[gb]) ** 2).sum(1) < radius * radius
                    same = keys[ga] == keys[gb]
                    m = close & same
                    ea.append(ga[m])
                    eb.append(gb[m])
    if not ea:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    return np.concatenate(ea), np.concatenate(eb)


def _dt_rows(F, V):
    """Recursive filter along axis 0 (rows contiguous in memory), forward and back.
    F: (L, M, C), V: (L, M) feedback between row i-1 and i."""
    L = F.shape[0]
    for i in range(1, L):
        F[i] += V[i, :, None] * (F[i - 1] - F[i])
    for i in range(L - 2, -1, -1):
        F[i] += V[i + 1, :, None] * (F[i + 1] - F[i])


def _domain_transform(F, guide, valid, sigma_s, sigma_r, iters=3):
    """Edge-aware smoothing (Gastal & Oliveira 2011, recursive filter).
    Blends neighboring pixels only where the guide color changes little; stops at color edges and
    at UV island edges (invalid pixels). F: (H, W, C) float, guide: (H, W, 3)."""
    F = np.ascontiguousarray(F, np.float32)
    H, W = F.shape[:2]
    ratio = sigma_s / max(sigma_r, 1e-6)
    big = np.float32(1e6)
    dx = (1.0 + ratio * np.abs(np.diff(guide, axis=1)).sum(2)).astype(np.float32)
    dx[~(valid[:, 1:] & valid[:, :-1])] = big
    dy = (1.0 + ratio * np.abs(np.diff(guide, axis=0)).sum(2)).astype(np.float32)
    dy[~(valid[1:, :] & valid[:-1, :])] = big
    # distances in the layout where each pass walks axis 0
    dHx_T = np.ascontiguousarray(np.concatenate([np.ones((H, 1), np.float32), dx], 1).T)  # (W, H)
    dVy = np.concatenate([np.ones((1, W), np.float32), dy], 0)                             # (H, W)
    del dx, dy
    for i in range(iters):
        sig = sigma_s * math.sqrt(3.0) * 2.0 ** (iters - i - 1) / math.sqrt(4.0 ** iters - 1.0)
        a = np.float32(math.exp(-math.sqrt(2.0) / max(sig, 1e-6)))
        FT = np.ascontiguousarray(F.transpose(1, 0, 2))   # (W, H, C): horizontal pass
        _dt_rows(FT, np.power(a, dHx_T))
        F = np.ascontiguousarray(FT.transpose(1, 0, 2))   # (H, W, C): vertical pass
        del FT
        _dt_rows(F, np.power(a, dVy))
    return F


def surface_normals_from_colors(context, obj, src_img, dst_img, props):
    """For each flat color region in the source image, write into the target normal map
    (object space) the average of the surface normals in that region."""
    if tuple(src_img.size) != tuple(dst_img.size):
        raise RuntimeError("the color image and the normal map must be the same size")
    info = _surface_prepare(context, obj, src_img, want_normals=True)
    w, h = info["w"], info["h"]
    tex = info["tex"]
    N = len(tex)

    # surface normals in object space
    M3 = np.array(obj.matrix_world, np.float64)[:3, :3]
    ngeo = (info["nrm"].astype(np.float64) @ M3)  # n_obj = M^T n_world
    ngeo /= np.maximum(np.linalg.norm(ngeo, axis=1, keepdims=True), 1e-12)

    src_full = _read_pixels(src_img)
    src = src_full.reshape(-1, 4)[tex]
    opaque = src[:, 3] >= 0.5
    if props.nfc_mode == 'CONTINUOUS':
        n_reg = _continuous_normals(info, np.clip(src[:, :3], 0.0, 1.0), ngeo, opaque, props,
                                    colors_full=np.clip(src_full, 0.0, 1.0))
        return _nfc_write(context, dst_img, info, ngeo, n_reg, props, props.nfc_cont_k)
    tol = max(props.nfc_tolerance, 1.0 / 255.0)
    q = np.floor(np.clip(src[:, :3], 0.0, 1.0) / tol + 1e-6).astype(np.int64)
    levels = int(1.0 / tol) + 2
    keys = (q[:, 0] * levels + q[:, 1]) * levels + q[:, 2]
    keys[~opaque] = -1

    # neighborhood in UV (without wrapping around the image edges)
    cmap = np.full(w * h, -1, np.int64)
    cmap[tex] = np.arange(N)
    y, x = tex // w, tex % w
    ea, eb = [], []
    for dx, dy in ((1, 0), (0, 1)):
        okn = (x + dx < w) & (y + dy < h)
        a = np.nonzero(okn)[0]
        b = cmap[(y[a] + dy) * w + (x[a] + dx)]
        m = (b >= 0)
        a, b = a[m], b[m]
        m = (keys[a] == keys[b]) & (keys[a] >= 0)
        ea.append(a[m])
        eb.append(b[m])

    # join the parts of the same patch split by seams
    nbr_out = np.zeros(N, bool)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        xx, yy = x + dx, y + dy
        inside = (xx >= 0) & (xx < w) & (yy >= 0) & (yy < h)
        j = np.where(inside, yy * w + xx, 0)
        nbr_out |= ~inside | (cmap[j] < 0)
    border = np.nonzero(nbr_out & opaque)[0]
    sa, sb = _seam_edges(border, info["pos"], keys, 2.5 * max(info["texel"], 1e-9))
    ea.append(sa)
    eb.append(sb)
    lab = _connected_components(N, np.concatenate(ea), np.concatenate(eb))

    # large regions become several irregular facets
    ulab, inv = np.unique(lab, return_inverse=True)
    counts = np.bincount(inv)
    texel = max(info["texel"], 1e-9)
    if props.nfc_max_facet > 0:
        bb = info["pos"].max(0) - info["pos"].min(0)
        L = max(float(np.linalg.norm(bb)) * props.nfc_max_facet, texel * 4)
        big = (counts[inv] * texel * texel > L * L) & opaque
        if big.any():
            vid = np.zeros(N, np.int64)
            # computed per sub-cell (L/64, min. 2 px) and passed on to the pixels: much faster
            Pb = info["pos"][big]
            sub = max(L / 64.0, texel * 2.0)
            sk = np.floor((Pb - Pb.min(0)) / sub).astype(np.int64)
            sdims = sk.max(0) + 1
            skey = (sk[:, 0] * sdims[1] + sk[:, 1]) * sdims[2] + sk[:, 2]
            ukey, sinv = np.unique(skey, return_inverse=True)
            centers = np.stack([ukey // (sdims[1] * sdims[2]), (ukey // sdims[2]) % sdims[1],
                                ukey % sdims[2]], 1) * sub + sub * 0.5 + Pb.min(0)
            vid[big] = _voronoi_ids(centers, L)[sinv] + 1
            _, vinv = np.unique(vid, return_inverse=True)
            comb = inv.astype(np.int64) * (int(vinv.max()) + 1) + vinv
            _, inv = np.unique(comb, return_inverse=True)
            counts = np.bincount(inv)

    # average of the normals per region (only regions with minimum size and opaque)
    R = len(counts)
    sums = np.stack([np.bincount(inv, weights=ngeo[:, c], minlength=R) for c in range(3)], 1)
    reg_n = sums / np.maximum(np.linalg.norm(sums, axis=1, keepdims=True), 1e-12)
    assigned = opaque & (counts[inv] >= props.nfc_min_pixels)

    # small fragments (smoothed edges) inherit the normal of the neighboring region
    full = np.zeros((h, w, 4), np.float32)
    flat_full = full.reshape(-1, 4)
    flat_full[tex[assigned], :3] = reg_n[inv[assigned]]
    flat_full[tex[assigned], 3] = 1.0
    filled = np.zeros(w * h, bool)
    filled[tex[assigned]] = True
    allowed = np.zeros(w * h, bool)
    allowed[tex[opaque & ~assigned]] = True
    _dilate_into(full, filled.reshape(h, w), allowed.reshape(h, w), 64)
    n_reg = flat_full[tex, :3].astype(np.float64)
    got = flat_full[tex, 3] > 0.5
    n_reg[~got] = ngeo[~got]

    # color gradients become smooth normal transitions (stopping at hard edges)
    if props.nfc_smooth and props.nfc_smooth_size > 0:
        bb = info["pos"].max(0) - info["pos"].min(0)
        sigma_px = float(np.linalg.norm(bb)) * props.nfc_smooth_size / texel
        sigma_px = min(max(sigma_px, 1.0), max(w, h) / 4.0)
        valid = np.zeros(w * h, bool)
        valid[tex[opaque]] = True
        F = np.zeros((h * w, 3), np.float32)
        F[tex] = n_reg
        guide = np.clip(src_full[..., :3], 0.0, 1.0)
        F = _domain_transform(F.reshape(h, w, 3), guide, valid.reshape(h, w), sigma_px, props.nfc_edge)
        sm = F.reshape(-1, 3)[tex].astype(np.float64)
        n_reg = np.where(opaque[:, None], sm, n_reg)
    nl = np.linalg.norm(n_reg, axis=1, keepdims=True)
    n_reg = np.where(nl > 1e-6, n_reg / np.maximum(nl, 1e-12), ngeo)

    n_regions = len(np.unique(inv[assigned])) if assigned.any() else 0
    return _nfc_write(context, dst_img, info, ngeo, n_reg, props, n_regions)


def _limit_angle(g, t, max_angle):
    """Bring t toward g so the angle between them doesn't exceed max_angle (radians)."""
    d = np.clip((g * t).sum(1), -1.0, 1.0)
    ang = np.arccos(d)
    over = ang > max_angle
    if not over.any():
        return t
    perp = t[over] - d[over, None] * g[over]
    pn = np.linalg.norm(perp, axis=1, keepdims=True)
    # t opposite to g (rare): pick any perpendicular
    bad = pn[:, 0] < 1e-9
    if bad.any():
        alt = np.cross(g[over][bad], np.array([0.0, 0.0, 1.0]))
        alt[np.linalg.norm(alt, axis=1) < 1e-6] = (1.0, 0.0, 0.0)
        perp[bad] = alt
        pn = np.linalg.norm(perp, axis=1, keepdims=True)
    perp /= np.maximum(pn, 1e-12)
    out = t.copy()
    out[over] = g[over] * math.cos(max_angle) + perp * math.sin(max_angle)
    return out


def _nfc_write(context, dst_img, info, ngeo, n_reg, props, n_regions):
    tex = info["tex"]
    k = props.nfc_strength
    out_n = ngeo * (1.0 - k) + n_reg * k
    out_n /= np.maximum(np.linalg.norm(out_n, axis=1, keepdims=True), 1e-12)
    if props.nfc_max_angle < math.pi - 1e-3:
        out_n = _limit_angle(ngeo, out_n, props.nfc_max_angle)
    dst = _read_pixels(dst_img).reshape(-1, 4).copy()
    dst[tex, :3] = (out_n * 0.5 + 0.5).astype(np.float32)
    dst[tex, 3] = 1.0
    _surface_finish(context, dst_img, dst, info)
    return len(tex), n_regions


def _color_centers(colors, K, seed=0):
    """Color centers (simple k-means on a sample)."""
    rng = np.random.default_rng(seed)
    if len(colors) == 0:
        return np.zeros((1, 3), np.float32)
    sample = colors[rng.choice(len(colors), min(len(colors), 200_000), replace=False)].astype(np.float32)
    # k-means++ initialization
    cents = [sample[rng.integers(len(sample))]]
    for _ in range(1, K):
        d = np.min(((sample[:, None, :] - np.array(cents)[None]) ** 2).sum(2), 1)
        if d.sum() <= 0:
            break
        cents.append(sample[rng.choice(len(sample), p=d / d.sum())])
    C = np.array(cents, np.float32)
    for _ in range(12):
        lab = ((sample[:, None, :] - C[None]) ** 2).sum(2).argmin(1)
        for j in range(len(C)):
            m = lab == j
            if m.any():
                C[j] = sample[m].mean(0)
    return C


def _box_mean(a, r):
    """Mean over a (2r+1)^2 window using cumulative sums (edges: truncated window)."""
    out = a
    for ax in (0, 1):
        c = np.cumsum(out, axis=ax, dtype=np.float64)
        n = out.shape[ax]
        idx = np.arange(n)
        hi = np.minimum(idx + r, n - 1)
        lo = idx - r - 1
        shape = [1, 1]
        shape[ax] = n
        s = np.take(c, hi, axis=ax) - np.take(c, np.maximum(lo, 0), axis=ax) * (lo >= 0).reshape(shape)
        cnt = (hi - np.maximum(lo, -1)).reshape(shape)
        out = (s / cnt).astype(np.float32)
    return out


def _color_flatness(info, colors_full, g0=0.012, rr=4):
    """0..1 confidence that the pixel is in an area of stable color, and not in the blurred
    transition between two strokes (average color gradient in a small image window,
    only between valid pixels, so UV island edges don't count)."""
    w, h, tex = info["w"], info["h"], info["tex"]
    valid = np.zeros(w * h, bool)
    valid[tex] = True
    valid = valid.reshape(h, w)
    C = colors_full.reshape(h, w, -1)[..., :3]
    g = np.zeros((h, w), np.float32)
    dx = np.abs(np.diff(C, axis=1)).sum(2)
    dx[~(valid[:, 1:] & valid[:, :-1])] = 0
    g[:, 1:] += dx
    g[:, :-1] += dx
    del dx
    dy = np.abs(np.diff(C, axis=0)).sum(2)
    dy[~(valid[1:] & valid[:-1])] = 0
    g[1:] += dy
    g[:-1] += dy
    del dy
    g *= 0.5
    vf = valid.astype(np.float32)
    vm = np.maximum(_box_mean(vf, rr), 1e-6)
    gm = _box_mean(g, rr) / vm
    conf = np.exp(-(gm / g0) ** 2).astype(np.float32)
    conf = _box_mean(conf * vf, rr) / vm
    return conf.reshape(-1)[tex]


def _continuous_normals(info, colors, ngeo, opaque, props, colors_full=None):
    """Bilateral on the surface guided by color: each pixel gets the average of the normals
    of its 3D neighborhood, weighted by color similarity. Approximated with color layers
    (k-means centers): G_color(cp, cq) ~ sum_k u_k(p) u_k(q)."""
    bb = info["pos"].max(0) - info["pos"].min(0)
    diag = float(np.linalg.norm(bb))
    radius = max(diag * props.nfc_cont_radius, info["texel"] * 2)
    sig = max(props.nfc_cont_sigma, 1e-3)
    # Pixels in the blurred transition between strokes have intermediate colors. If they
    # went into the average, they would form their own "layers" (rings around each stroke)
    # and a dark line would show at the edge. So: only stable areas define the reference
    # colors and the averages; the transitions then interpolate both sides.
    if colors_full is not None:
        conf = _color_flatness(info, colors_full)
    else:
        conf = np.ones(len(colors), np.float32)
    conf[~opaque] = 0.0
    flat = conf > 0.5
    cents = _color_centers(colors[flat] if flat.sum() > 1000 else colors[opaque], props.nfc_cont_k)
    K = len(cents)

    # When "choosing the layer" of each pixel, use a transition at least as wide
    # as the spacing between the reference colors: otherwise, in a gradient, the pixel switches
    # layer suddenly and a line appears. The separation between colors is still the
    # user's (sig), applied to what gets blended.
    if K > 1:
        dc = np.sqrt(((cents[:, None, :] - cents[None]) ** 2).sum(2))
        np.fill_diagonal(dc, np.inf)
        spacing = float(np.median(dc.min(1)))
    else:
        spacing = 0.0
    sig_pick = max(sig, 0.6 * spacing)

    def weights(k, sg):
        d2 = ((colors - cents[k]) ** 2).sum(1)
        return np.exp(-d2 / (2.0 * sg * sg)).astype(np.float32)

    den = np.zeros(len(colors), np.float32)
    den_pick = np.zeros(len(colors), np.float32)
    for k in range(K):
        den += weights(k, sig)
        den_pick += weights(k, sig_pick)
    den = np.maximum(den, 1e-12)
    den_pick = np.maximum(den_pick, 1e-12)
    acc = np.zeros((len(colors), 3), np.float64)
    ng32 = ngeo.astype(np.float32)
    group = 2
    for k0 in range(0, K, group):
        ks = list(range(k0, min(K, k0 + group)))
        us = [weights(k, sig) / den * conf for k in ks]
        vals = np.concatenate([np.concatenate([u[:, None] * ng32, u[:, None]], 1) for u in us], 1)
        avg, _cs = _grid_blur(info["pos"], vals, radius, info["texel"], info["area"], 3)
        for j, k in enumerate(ks):
            pick = weights(k, sig_pick) / den_pick
            # average of layer k's normals in the neighborhood (weighted sum / layer weight)
            mean_k = avg[:, 4 * j:4 * j + 3] / np.maximum(avg[:, 4 * j + 3:4 * j + 4], 1e-6)
            acc += pick[:, None] * mean_k
    n = acc / np.maximum(np.linalg.norm(acc, axis=1, keepdims=True), 1e-12)
    bad = (np.linalg.norm(acc, axis=1) < 1e-9) | ~opaque
    n[bad] = ngeo[bad]
    if colors_full is not None and (conf < 0.99).any():
        # transitions: normal interpolated from the stable areas around
        r2 = max(diag * 0.03, info["texel"] * 4)
        c = conf.astype(np.float32)
        vals = np.concatenate([c[:, None] * n.astype(np.float32), c[:, None]], 1)
        avg, _cs = _grid_blur(info["pos"], vals, r2, info["texel"], info["area"], 3)
        wsum = avg[:, 3:4]
        ni = np.where(wsum > 1e-4, avg[:, :3] / np.maximum(wsum, 1e-6), n)
        n = c[:, None] * n + (1.0 - c[:, None]) * ni
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        n[~opaque] = ngeo[~opaque]
    return n


class PTL_OT_normals_from_colors(bpy.types.Operator):
    """Each flat color region of the color image becomes a facet in the normal map
    (object space): the region's normal is the average of the surface normals there"""
    bl_idname = "painting_tools.normals_from_colors"
    bl_label = "Generate normals from colors"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        import time
        props = context.scene.painting_tools
        obj = context.active_object
        src = props.nfc_color_image or _paint_target_image(context)
        dst = props.nfc_normal_image
        if src is None or dst is None:
            self.report({'WARNING'}, "Choose the color image and the normal map")
            return {'CANCELLED'}
        if src == dst:
            self.report({'WARNING'}, "The color image and the normal map must be different images")
            return {'CANCELLED'}
        _blur_backup(dst)
        t0 = time.time()
        try:
            n, regions = surface_normals_from_colors(context, obj, src, dst, props)
        except Exception as e:
            _BLUR_BACKUPS[dst.name].pop()
            self.report({'WARNING'}, f"Normals from colors: {e}")
            return {'CANCELLED'}
        _redraw_all(context)
        self.report({'INFO'}, f"Normals from colors: {regions:,} facets, {n:,} pixels in {time.time() - t0:.1f}s")
        return {'FINISHED'}


class PTL_OT_distort_surface(bpy.types.Operator):
    """Distort the paint along the surface of the whole mesh (no seams).
    With Face Selection Masking on, only the selected faces change"""
    bl_idname = "painting_tools.distort_surface"
    bl_label = "Apply Distort"
    bl_options = {'REGISTER'}

    replace_last: bpy.props.BoolProperty(
        name="Replace the last one", default=False, options={'SKIP_SAVE'},
        description="Undoes the last Blur/Distort and applies it again with the current values")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return context.mode == 'PAINT_TEXTURE' and obj is not None and obj.type == 'MESH'

    def execute(self, context):
        import time
        props = context.scene.painting_tools
        obj = context.active_object
        img = _paint_target_image(context)
        if img is None:
            self.report({'WARNING'}, "No image being painted")
            return {'CANCELLED'}
        stack = _BLUR_BACKUPS.get(img.name)
        if self.replace_last and stack and tuple(img.size) == (stack[-1].shape[1], stack[-1].shape[0]):
            img.pixels.foreach_set(_backup_pixels(stack[-1]).ravel())  # back to the previous state
        else:
            _blur_backup(img)
        t0 = time.time()
        try:
            n = surface_distort(context, obj, img, props)
        except Exception as e:
            if not self.replace_last:
                _BLUR_BACKUPS[img.name].pop()
            self.report({'WARNING'}, f"Distort: {e}")
            return {'CANCELLED'}
        _redraw_all(context)
        self.report({'INFO'}, f"Distort: {n:,} pixels in {time.time() - t0:.1f}s")
        return {'FINISHED'}


class PTL_OT_blur_surface(bpy.types.Operator):
    """Blur along the surface of the whole mesh, including what isn't visible in the view.
    Ignores seams. With Face Selection Masking on, only the selected faces change"""
    bl_idname = "painting_tools.blur_surface"
    bl_label = "Surface blur"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return context.mode == 'PAINT_TEXTURE' and obj is not None and obj.type == 'MESH'

    def execute(self, context):
        import time
        props = context.scene.painting_tools
        obj = context.active_object
        img = _paint_target_image(context)
        if img is None:
            self.report({'WARNING'}, "No image being painted")
            return {'CANCELLED'}
        _blur_backup(img)
        t0 = time.time()
        try:
            n = surface_blur(context, obj, img, props.blur_surface_radius,
                             props.blur_strength, props.blur_expand, props.blur_surface_res)
        except Exception as e:
            _BLUR_BACKUPS[img.name].pop()
            self.report({'WARNING'}, f"Surface blur: {e}")
            return {'CANCELLED'}
        _redraw_all(context)
        self.report({'INFO'}, f"Surface blur: {n:,} pixels in {time.time() - t0:.1f}s")
        return {'FINISHED'}


class PTL_OT_blur_uv(bpy.types.Operator):
    """Apply the blur to the whole texture, in UV space (seams may show)"""
    bl_idname = "painting_tools.blur_uv"
    bl_label = "Blur whole texture (UV)"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.painting_tools
        img = _paint_target_image(context)
        if img is None:
            self.report({'WARNING'}, "No image being painted")
            return {'CANCELLED'}
        if img.size[0] == 0 or img.size[1] == 0:
            self.report({'WARNING'}, "Image empty or not loaded")
            return {'CANCELLED'}
        _blur_backup(img)
        px = _backup_pixels(_BLUR_BACKUPS[img.name][-1])
        out = _blur_pixels(px, props, props.blur_tileable, props.blur_expand)
        img.pixels.foreach_set(out.ravel())
        _refresh_image(img)
        _redraw_all(context)
        return {'FINISHED'}


class PTL_OT_blur_undo(bpy.types.Operator):
    """Restore the image to before the last filter"""
    bl_idname = "painting_tools.blur_undo"
    bl_label = "Undo Blur"

    image_name: bpy.props.StringProperty(options={'SKIP_SAVE', 'HIDDEN'})

    def _image(self, context):
        if self.image_name:
            return bpy.data.images.get(self.image_name)
        return _paint_target_image(context)

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        img = self._image(context)
        if img is None or not _BLUR_BACKUPS.get(img.name):
            self.report({'INFO'}, "Nothing to undo")
            return {'CANCELLED'}
        px = _backup_pixels(_BLUR_BACKUPS[img.name].pop())
        if tuple(img.size) != (px.shape[1], px.shape[0]):
            self.report({'WARNING'}, "The image size changed, can't undo")
            return {'CANCELLED'}
        img.pixels.foreach_set(px.ravel())
        _refresh_image(img)
        _redraw_all(context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class PTL_PT_panel(bpy.types.Panel):
    bl_label = "Nakan Texturing Tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Texturing Tools"

    @classmethod
    def poll(cls, context):
        return context.mode == 'PAINT_TEXTURE'

    def draw(self, context):
        props = context.scene.painting_tools
        if _watch_needed(props) and not PTL_OT_stroke_watch._running:
            warn = self.layout.box()
            warn.label(text="Features paused (file reloaded?)", icon='ERROR')
            warn.operator("painting_tools.stroke_watch", text="Reactivate", icon='PLAY')


class _PTLSub:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Texturing Tools"
    bl_parent_id = "PTL_PT_panel"


class PTL_PT_brush(_PTLSub, bpy.types.Panel):
    bl_label = "Brush"

    def draw_header(self, context):
        self.layout.label(text="", icon='BRUSH_DATA')

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        brush = _get_brush(context)
        active = bool(brush and brush.mask_texture and brush.mask_texture.name == TIP_NAME)
        if brush:
            layout.label(text=brush.name, icon='DOT')
        col = layout.column(align=True)
        col.prop(props, "roundness", slider=True)
        col.prop(props, "hardness", slider=True)
        layout.prop(props, "angle_mode", expand=True)
        if props.angle_mode == 'FIXED':
            layout.prop(props, "angle")
        elif props.angle_mode == 'RANDOM':
            layout.prop(props, "random_range")
        sp = layout.column(align=True)
        sp.prop(props, "auto_spacing")
        sub = sp.row(align=True)
        sub.active = props.auto_spacing
        sub.prop(props, "base_spacing", slider=True)
        if props.auto_spacing:
            sub2 = sp.row()
            sub2.active = False
            sub2.label(text=f"Applied spacing: {_auto_spacing_value(props)}%")
        row = layout.row(align=True)
        if active:
            row.label(text="Elliptical tip active", icon='CHECKMARK')
            row.operator("painting_tools.tip_remove", text="", icon='X')
        else:
            row.operator("painting_tools.tip_apply", icon='PLAY')


class PTL_PT_colors(_PTLSub, bpy.types.Panel):
    bl_label = "Colors"

    def draw_header(self, context):
        self.layout.label(text="", icon='COLOR')

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        owner = _color_owner(context)
        layout.label(text="Color palette")
        if owner is not None:
            row = layout.row(align=True)
            for prop_owner, name in ((owner, "color"), (props, "mid_preview"), (owner, "secondary_color")):
                sub = row.row(align=True)
                sub.scale_x = 0.35
                sub.prop(prop_owner, name, text="")
            row.operator("painting_tools.mid_color", text="Middle color", icon='IPO_LINEAR')
            lab = layout.row(align=True)
            lab.active = False
            for t in ("primary", "middle", "secondary"):
                lab.label(text=t)
        info = layout.column(align=True)
        info.active = False
        info.label(text="Shift+X: pick color for the primary")
        info.label(text="Alt+Shift+X: pick color for the secondary")
        info.label(text="Drag color swatches between the palette and the list")


class PTL_PT_color_manager(bpy.types.Panel):
    bl_label = "Color management"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Texturing Tools"
    bl_parent_id = "PTL_PT_colors"

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        row = layout.row()
        row.template_list("PTL_UL_colors", "", props, "palette_items", props, "palette_index", rows=6)
        col = row.column(align=True)
        col.operator("painting_tools.pal_add", text="", icon='ADD')
        col.operator("painting_tools.pal_add_group", text="", icon='NEWFOLDER')
        col.operator("painting_tools.pal_remove", text="", icon='REMOVE')
        col.separator()
        col.operator("painting_tools.pal_move", text="", icon='TRIA_UP').direction = 'UP'
        col.operator("painting_tools.pal_move", text="", icon='TRIA_DOWN').direction = 'DOWN'

        row = layout.row(align=True)
        row.label(text="Drop here to save:")
        sub = row.row(align=True)
        sub.scale_x = 0.8
        sub.prop(props, "drop_slot", text="")

        row = layout.row(align=True)
        row.operator("painting_tools.pal_use", text="Use as primary").target = 'PRIMARY'
        row.operator("painting_tools.pal_use", text="Use as secondary").target = 'SECONDARY'
        row = layout.row(align=True)
        row.operator("painting_tools.pal_import", text="Import", icon='IMPORT')
        row.operator("painting_tools.pal_export", text="Export", icon='EXPORT')


class PTL_PT_color_vary(bpy.types.Panel):
    bl_label = "Variation per stroke"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Texturing Tools"
    bl_parent_id = "PTL_PT_colors"

    def draw_header(self, context):
        self.layout.prop(context.scene.painting_tools, "color_vary", text="")

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        owner = _color_owner(context)
        layout.active = props.color_vary
        box = layout
        if props.dual_enable:
            sub = box.column(align=True)
            sub.active = False
            sub.label(text="Dual painting: each color varies around itself")
            col = box.column(align=True)
            col.label(text="Layer 1 (primary)")
            col.prop(props, "color_hue_var", slider=True)
            col.prop(props, "color_sat_var", slider=True)
            col.prop(props, "color_val_var", slider=True)
            sub = col.row()
            sub.active = False
            sub.label(text=f"Hue: ±{props.color_hue_var * 180:.0f}°")
            col = box.column(align=True)
            col.prop(props, "dual_vary_b", text="Layer 2 (secondary)")
            sub = col.column(align=True)
            sub.active = props.dual_vary_b
            sub.prop(props, "color2_hue_var", slider=True)
            sub.prop(props, "color2_sat_var", slider=True)
            sub.prop(props, "color2_val_var", slider=True)
            sub2 = sub.row()
            sub2.active = False
            sub2.label(text=f"Hue: ±{props.color2_hue_var * 180:.0f}°")
            return
        box.prop(props, "color_vary_mode", expand=True)
        if props.color_vary_mode == 'BETWEEN':
            box.prop(props, "color_vary_amount", slider=True)
            if owner is not None:
                row = box.row(align=True)
                row.label(text="Range:")
                row.prop(props, "color_base", text="")
                row.prop(owner, "secondary_color", text="")
        else:
            col = box.column(align=True)
            col.prop(props, "color_hue_var", slider=True)
            col.prop(props, "color_sat_var", slider=True)
            col.prop(props, "color_val_var", slider=True)
            sub = col.row()
            sub.active = False
            sub.label(text=f"Hue: ±{props.color_hue_var * 180:.0f}°")
            row = box.row(align=True)
            row.label(text="Base color:")
            row.prop(props, "color_base", text="")


class PTL_PT_dual(_PTLSub, bpy.types.Panel):
    bl_label = "Dual painting"

    def draw_header(self, context):
        self.layout.prop(context.scene.painting_tools, "dual_enable", text="")

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        layout.active = props.dual_enable
        owner = _color_owner(context)
        col = layout.column(align=True)
        for label, img_prop, color_prop in (("1", "dual_a_image", "color"),
                                            ("2", "dual_b_image", "secondary_color")):
            row = col.row(align=True)
            row.label(text=label)
            row.prop(props, img_prop, text="")
            if owner is not None:
                sub = row.row(align=True)
                sub.scale_x = 0.4
                sub.prop(owner, color_prop, text="")
        info = layout.column(align=True)
        info.active = False
        info.label(text="1 = primary color, 2 = secondary color")
        info.label(text="Shift+X: pick color for 1 (primary)")
        info.label(text="Alt+Shift+X: pick color for 2 (secondary)")
        info.label(text="Also replays: Smear, Soften and eraser")
        layout.operator("painting_tools.dual_swap", icon='ARROW_LEFTRIGHT', text="Swap 1 and 2")
        kind = _brush_kind(context)
        brush = _get_brush(context)
        if props.dual_enable and brush is not None and kind is None:
            layout.label(text=f"Brush '{brush.name}' paints only one layer", icon='INFO')
        a, b = props.dual_a_image, props.dual_b_image
        if a is None or b is None:
            layout.label(text="Choose both images", icon='INFO')
        elif a == b:
            layout.label(text="1 and 2 must be different images", icon='ERROR')
        elif props.color_vary:
            sub = layout.row()
            sub.active = False
            sub.label(text="Color variation: " + ("on both colors" if props.dual_vary_b else "only on color 1"))


class PTL_PT_view(_PTLSub, bpy.types.Panel):
    bl_label = "View and surface"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='ORIENTATION_NORMAL')

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        ip = context.tool_settings.image_paint

        box = layout.box()
        box.label(text="Align to surface (Alt+V)")
        box.prop(props, "align_center")
        box.prop(props, "align_ortho")
        box.prop(props, "align_smooth")

        box = layout.box()
        box.label(text="Angle limit")
        box.prop(props, "region_mode")
        if props.region_mode:
            col = box.column(align=True)
            col.prop(props, "region_angle")
            col.prop(props, "region_connected")
            col.prop(props, "region_lock")
            obj = context.active_object
            if props.region_lock and obj and obj.type == 'MESH' and obj.data.use_paint_mask:
                box.operator("painting_tools.region_clear", icon='X')
            box.prop(ip, "use_occlude", text="Occlude")
        else:
            row = box.row(align=True)
            row.prop(ip, "use_normal_falloff", text="By view")
            sub = row.row(align=True)
            sub.active = ip.use_normal_falloff
            sub.prop(ip, "normal_angle", text="Angle")


class PTL_PT_filters(_PTLSub, bpy.types.Panel):
    """Category that groups the image filters (Blur, Distort and future ones)."""
    bl_label = "Image filters"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='IMAGE_DATA')

    def draw(self, context):
        layout = self.layout
        img = _paint_target_image(context)
        layout.label(text=img.name if img else "No image", icon='IMAGE_DATA')
        obj = context.active_object
        info = layout.column(align=True)
        info.active = False
        if obj and obj.type == 'MESH' and obj.data.use_paint_mask:
            info.label(text="Face Mask on: selected faces only", icon='FACESEL')
        else:
            info.label(text="Turn on Face Mask to limit it to faces")
        row = layout.row(align=True)
        row.operator("painting_tools.blur_undo", text="Undo", icon='LOOP_BACK')
        n = len(_BLUR_BACKUPS.get(img.name, [])) if img else 0
        sub = row.row(align=True)
        sub.active = False
        sub.label(text=f"{n} step(s)" if n else "")


class _PTLFilter(_PTLSub):
    bl_parent_id = "PTL_PT_filters"
    bl_options = {'DEFAULT_CLOSED'}


class PTL_PT_blur(_PTLFilter, bpy.types.Panel):
    bl_label = "Blur"

    def draw_header(self, context):
        self.layout.label(text="", icon='MATFLUID')

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        layout.prop(props, "blur_strength", slider=True)
        layout.prop(props, "blur_expand")

        box = layout.box()
        box.label(text="Surface (whole mesh)", icon='MESH_DATA')
        col = box.column(align=True)
        col.prop(props, "blur_surface_radius")
        col.prop(props, "blur_surface_res", slider=True)
        cell = props.blur_surface_radius / max(props.blur_surface_res, 1)
        sub = box.row()
        sub.active = False
        sub.label(text="Cell: " + (f"{cell * 100:.1f} cm" if cell >= 0.01 else f"{cell * 1000:.1f} mm"))
        row = box.row(align=True)
        row.scale_y = 1.4
        row.operator("painting_tools.blur_surface", icon='MATFLUID')

        _draw_uv_blur_box(layout, props)


class PTL_PT_distort(_PTLFilter, bpy.types.Panel):
    bl_label = "Distort"

    def draw_header(self, context):
        self.layout.label(text="", icon='MOD_WAVE')

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        img = _paint_target_image(context)
        layout.prop(props, "distort_type", expand=True)
        col = layout.column(align=True)
        if props.distort_type == 'TURBULENT':
            col.prop(props, "turb_amount")
            col.prop(props, "turb_size")
            col.prop(props, "turb_complexity")
            col.prop(props, "turb_seed")
        else:
            col.prop(props, "wave_amp")
            col.prop(props, "wave_length")
            col.prop(props, "wave_phase")
            layout.prop(props, "wave_axis", expand=True)
            layout.prop(props, "wave_mode", expand=True)
        layout.prop(props, "distort_strength", slider=True)

        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("painting_tools.distort_surface", icon='MOD_WAVE')
        n = len(_BLUR_BACKUPS.get(img.name, [])) if img else 0
        sub = layout.row(align=True)
        sub.enabled = n > 0
        op = sub.operator("painting_tools.distort_surface", text="Reapply (replaces the last one)",
                          icon='FILE_REFRESH')
        op.replace_last = True


class PTL_PT_normals(_PTLFilter, bpy.types.Panel):
    bl_label = "Normals from colors"

    def draw_header(self, context):
        self.layout.label(text="", icon='NORMALS_FACE')

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        col = layout.column(align=True)
        col.prop(props, "nfc_color_image")
        col.prop(props, "nfc_normal_image")
        if props.nfc_color_image is None:
            sub = layout.row()
            sub.active = False
            img = _paint_target_image(context)
            sub.label(text="Color: " + (img.name if img else "image being painted"))
        dst = props.nfc_normal_image
        if dst is not None and dst.colorspace_settings.name != 'Non-Color':
            layout.label(text="The normal map should be Non-Color", icon='ERROR')
        layout.prop(props, "nfc_mode", expand=True)
        col = layout.column(align=True)
        col.prop(props, "nfc_strength", slider=True)
        col.prop(props, "nfc_max_angle")
        if props.nfc_mode == 'CONTINUOUS':
            col = layout.column(align=True)
            col.prop(props, "nfc_cont_radius", slider=True)
            col.prop(props, "nfc_cont_sigma", slider=True)
            col.prop(props, "nfc_cont_k")
        else:
            col = layout.column(align=True)
            col.prop(props, "nfc_tolerance", slider=True)
            col.prop(props, "nfc_min_pixels")
            col.prop(props, "nfc_max_facet", slider=True)
            box = layout.box()
            box.prop(props, "nfc_smooth")
            sub = box.column(align=True)
            sub.active = props.nfc_smooth
            sub.prop(props, "nfc_smooth_size", slider=True)
            sub.prop(props, "nfc_edge", slider=True)
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("painting_tools.normals_from_colors", icon='NORMALS_FACE')
        if dst is not None:
            n = len(_BLUR_BACKUPS.get(dst.name, []))
            op = layout.operator("painting_tools.blur_undo", text=f"Undo on normal map ({n})",
                                 icon='LOOP_BACK')
            op.image_name = dst.name
        info = layout.column(align=True)
        info.active = False
        info.label(text="Output in object space (Non-Color)")


def _draw_uv_blur_box(layout, props, big=False):
    box = layout.box()
    box.label(text="Whole texture (UV)", icon='UV')
    box.prop(props, "blur_radius")
    box.prop(props, "blur_type", expand=True)
    box.prop(props, "blur_tileable")
    row = box.row(align=True)
    row.scale_y = 1.4 if big else 1.0
    row.operator("painting_tools.blur_uv", text="Blur whole texture", icon='IMAGE_DATA')
    info = box.column(align=True)
    info.active = False
    info.label(text="2D: seams may show")


class PTL_PT_blur_image_editor(bpy.types.Panel):
    bl_label = "Blur"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Nakan Texturing Tools"

    def draw_header(self, context):
        self.layout.label(text="", icon='MATFLUID')

    def draw(self, context):
        layout = self.layout
        props = context.scene.painting_tools
        img = _paint_target_image(context)
        layout.label(text=img.name if img else "No image open", icon='IMAGE_DATA')
        layout.prop(props, "blur_strength", slider=True)
        layout.prop(props, "blur_expand")
        _draw_uv_blur_box(layout, props, big=True)
        layout.operator("painting_tools.blur_undo", icon='LOOP_BACK')
        n = len(_BLUR_BACKUPS.get(img.name, [])) if img else 0
        if n:
            sub = layout.row()
            sub.active = False
            sub.label(text=f"{n} step(s) to undo")


def _auto_start_watcher():
    """Re-enable the stroke watcher if any scene uses the features that depend on it."""
    try:
        if PTL_OT_stroke_watch._running:
            return None
        needed = any(_watch_needed(sc.painting_tools) for sc in bpy.data.scenes
                     if hasattr(sc, "painting_tools"))
        if not needed:
            return None
        wm = bpy.context.window_manager
        win = wm.windows[0] if wm and len(wm.windows) else None
        if win is None:
            return 1.0  # no window yet: try again in 1 s
        with bpy.context.temp_override(window=win):
            bpy.ops.painting_tools.stroke_watch('INVOKE_DEFAULT')
    except Exception as e:
        print("Texturing Tools (reconnect):", e)
    return None


@persistent
def _on_load_post(*_args):
    # when switching files the modal operators are discarded
    PTL_OT_stroke_watch._running = False
    bpy.app.timers.register(_auto_start_watcher, first_interval=0.5)


classes = (PTL_ColorItem, PTL_Props, PTL_UL_colors, PTL_OT_pal_add, PTL_OT_pal_add_group,
           PTL_OT_pal_remove, PTL_OT_pal_move, PTL_OT_pal_use, PTL_OT_pal_export, PTL_OT_pal_import, PTL_OT_tip_apply, PTL_OT_tip_remove, PTL_OT_align_to_surface,
           PTL_OT_stroke_watch, PTL_OT_region_clear, PTL_OT_dual_recorder,
           PTL_OT_dual_swap, PTL_OT_sample_secondary, PTL_OT_mid_color,
           PTL_OT_blur_uv, PTL_OT_blur_surface, PTL_OT_blur_undo, PTL_OT_distort_surface, PTL_OT_normals_from_colors,
           PTL_PT_panel, PTL_PT_brush, PTL_PT_colors, PTL_PT_color_manager, PTL_PT_color_vary,
           PTL_PT_dual, PTL_PT_view, PTL_PT_filters, PTL_PT_blur, PTL_PT_distort, PTL_PT_normals,
           PTL_PT_blur_image_editor)
addon_keymaps = []


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.painting_tools = bpy.props.PointerProperty(type=PTL_Props)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Image Paint", space_type='EMPTY')
        kmi = km.keymap_items.new(PTL_OT_align_to_surface.bl_idname, 'V', 'PRESS', alt=True)
        addon_keymaps.append((km, kmi))
        kmi = km.keymap_items.new(PTL_OT_sample_secondary.bl_idname, 'X', 'PRESS', shift=True, alt=True)
        addon_keymaps.append((km, kmi))

    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.timers.register(_auto_start_watcher, first_interval=0.5)


def unregister():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if bpy.app.timers.is_registered(_auto_start_watcher):
        bpy.app.timers.unregister(_auto_start_watcher)
    try:
        bpy.context.scene.painting_tools.region_mode = False
        bpy.context.scene.painting_tools.color_vary = False
        bpy.context.scene.painting_tools.dual_enable = False
    except Exception:
        pass
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    del bpy.types.Scene.painting_tools
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    _BLUR_BACKUPS.clear()
    _PREP_CACHE.clear()


