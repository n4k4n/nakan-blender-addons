# SPDX-License-Identifier: GPL-3.0-or-later
"""Nakan Vector Texturing: Illustrator-style editable vector layers (curves + text)
rasterized into a mesh's UV image texture, live."""

bl_info = {
    "name": "Nakan Vector Texturing",
    "author": "Nakan + Claude",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar (N) > Nakan Vector Texturing",
    "description": "Illustrator-style vector layers (curves and text) projected on a mesh and rasterized "
                   "into its UV texture live: fill, stroke, Trim Path, Dashes, Taper, Wave, Blur and "
                   "Roughen Edges",
    "category": "Paint",
}

import time
import bpy
from mathutils import Matrix, Vector
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty,
    IntProperty, PointerProperty, StringProperty,
)

from . import bridge

# --------------------------------------------------------------------------
# Dirty tracking
# --------------------------------------------------------------------------

_state = {"dirty": set(), "last_change": 0.0, "busy": False}


def _set_collections():
    return [c for c in bpy.data.collections if c.vl_set.target is not None]


def tag_set(coll):
    if coll is not None:
        _state["dirty"].add(coll.name)
        _state["last_change"] = time.perf_counter()


def tag_all():
    for c in _set_collections():
        tag_set(c)


def set_objects(coll):
    """Meshes that receive this set (main target first)."""
    return [t[0] for t in bridge.set_targets(coll)]


def sets_using(obj):
    """Sets that paint onto this mesh."""
    return [c for c in _set_collections() if obj in set_objects(c)]


def _sets_for_object(obj):
    return [c for c in _set_collections() if obj.name in c.all_objects or obj in set_objects(c)]


def set_of(collection):
    """The vector set a group (sub-collection) belongs to, or the set itself."""
    for c in _set_collections():
        if c == collection or collection in c.children_recursive:
            return c
    return None


def parent_of(coll, kind, item):
    """Set or group that directly holds a layer ("L") or a group ("G")."""
    for p in [coll] + list(coll.children_recursive):
        if (kind == "L" and item.name in p.objects) or (kind == "G" and item.name in p.children):
            return p
    return None


def place_in(parent, kind, item, index):
    """Put a layer/group inside parent at stack position index (0 = bottom), renumbering."""
    sibs = [kx for kx in bridge.children(parent) if kx[1] != item]
    index = max(0, min(index, len(sibs)))
    sibs.insert(index, (kind, item))
    for n, (k, x) in enumerate(sibs, 1):
        if k == "L":
            x.vl["order"] = n
        else:
            x.vl_group["order"] = n


def move_item(coll, kind, item, dest, index):
    """Move a layer/group to another parent of the same set (keeps its place in the world)."""
    src = parent_of(coll, kind, item)
    if src is not None and src != dest:
        if kind == "L":
            src.objects.unlink(item)
            dest.objects.link(item)
        else:
            src.children.unlink(item)
            dest.children.link(item)
    place_in(dest, kind, item, index)


def _layer_tag(self, context):
    _layer_update(self, context)


def _group_update(self, context):
    tag_set(set_of(self.id_data))


def _layer_update(self, context):
    obj = self.id_data
    for c in _sets_for_object(obj):
        tag_set(c)


def _set_update(self, context):
    tag_set(self.id_data)


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------


MODIFIER_TYPES = [
    ("BLUR", "Blur", "Gaussian blur of the layer (fill and stroke)", "MOD_SMOOTH", 0),
    ("ROUGHEN", "Roughen Edges", "Noise along the outline of the stroke and fill, for a "
     "hand-drawn look", "MOD_NOISE", 1),
]


class VL_ModifierProps(bpy.types.PropertyGroup):
    type: EnumProperty(name="Type", items=MODIFIER_TYPES, default="BLUR", update=_layer_update)
    enabled: BoolProperty(name="Enabled", default=True, update=_layer_update)
    show_expanded: BoolProperty(name="Expanded", default=True)
    radius: FloatProperty(
        name="Radius", subtype="DISTANCE", unit="LENGTH", default=0.02, min=0.0, soft_max=0.5,
        precision=4, update=_layer_update,
        description="Blur size on the surface (about 2 standard deviations)",
    )
    # Roughen Edges
    noise_size: FloatProperty(
        name="Size", subtype="DISTANCE", unit="LENGTH", default=0.05, min=1e-5, soft_max=1.0,
        precision=4, update=_layer_update,
        description="Size of the noise blobs on the surface (smaller = finer, more jagged edges)",
    )
    noise_detail: FloatProperty(
        name="Detail", default=3.0, min=0.0, max=8.0, update=_layer_update,
        description="Layers of finer noise on top of the base one",
    )
    noise_roughness: FloatProperty(
        name="Roughness", subtype="FACTOR", default=0.5, min=0.0, max=1.0, update=_layer_update,
        description="How strong the finer detail layers are",
    )
    rough_inside: FloatProperty(
        name="Inside", subtype="DISTANCE", unit="LENGTH", default=0.01, min=0.0, soft_max=0.2,
        precision=4, update=_layer_update,
        description="How far the noise can eat into the line or shape",
    )
    rough_outside: FloatProperty(
        name="Outside", subtype="DISTANCE", unit="LENGTH", default=0.01, min=0.0, soft_max=0.2,
        precision=4, update=_layer_update,
        description="How far the noise can spread out from the line or shape",
    )
    rough_softness: FloatProperty(
        name="Edge Softness", subtype="DISTANCE", unit="LENGTH", default=0.0, min=0.0, soft_max=0.05,
        precision=4, update=_layer_update,
        description="Blur of the new outline (0 = crisp, just anti-aliased)",
    )
    noise_evolution: FloatProperty(
        name="Evolution", default=0.0, update=_layer_update,
        description="Morphs the noise into a new pattern every whole number. Keyframe it for a "
                    "moving edge",
    )
    noise_seed: IntProperty(name="Seed", default=0, min=0, update=_layer_update,
                            description="Another random pattern")
    use_boil: BoolProperty(
        name="Boil", default=False, update=_layer_update,
        description="New noise pattern every few frames, like a hand-drawn line redrawn on each "
                    "drawing",
    )
    boil_hold: IntProperty(
        name="Hold", default=2, min=1, soft_max=12, update=_layer_update,
        description="Frames each pattern holds (2 = animating on twos)",
    )
    noise_ramp: PointerProperty(
        name="Ramp", type=bpy.types.Texture,
        description="Color ramp applied to the noise: black pulls the edge in, white pushes it out",
    )


MASK_MODES = [
    ("NONE", "None", "No mask"),
    ("INSIDE", "Inside", "Show only where the mask layer is (like an Alpha Matte)"),
    ("OUTSIDE", "Outside", "Cut out where the mask layer is (like an Alpha Inverted Matte)"),
]


def _mask_poll(self, o):
    return o.type in {"CURVE", "FONT"} and o != self.id_data


class VL_GroupProps(bpy.types.PropertyGroup):
    """A sub-collection of a vector set acts as a group of layers."""
    enabled: BoolProperty(name="Visible", default=True, update=_group_update)
    order: IntProperty(name="Order", default=0, update=_group_update)
    opacity: FloatProperty(name="Opacity", subtype="FACTOR", default=1.0, min=0, max=1, update=_group_update)
    show_expanded: BoolProperty(name="Expanded", default=True)
    mask_mode: EnumProperty(name="Mask", items=MASK_MODES, default="NONE", update=_group_update)
    mask_object: PointerProperty(name="Mask Layer", type=bpy.types.Object, poll=_mask_poll,
                                 update=_group_update, description="Layer (curve or text) used as mask")
    mask_show: BoolProperty(name="Show Mask Layer", default=False, update=_group_update,
                            description="Also draw the mask layer itself (off = it only masks)")


class VL_RowProps(bpy.types.PropertyGroup):
    """One row of the layer list (a layer or a group), rebuilt from the collections."""
    obj: PointerProperty(type=bpy.types.Object)
    group: PointerProperty(type=bpy.types.Collection)
    depth: IntProperty()


class VL_LayerProps(bpy.types.PropertyGroup):
    enabled: BoolProperty(name="Visible", default=True, update=_layer_update)
    order: IntProperty(name="Order", default=0, update=_layer_update)
    mask_mode: EnumProperty(name="Mask", items=MASK_MODES, default="NONE", update=_layer_update)
    mask_object: PointerProperty(name="Mask Layer", type=bpy.types.Object, poll=_mask_poll,
                                 update=_layer_update, description="Layer (curve or text) used as mask")
    mask_show: BoolProperty(name="Show Mask Layer", default=False, update=_layer_update,
                            description="Also draw the mask layer itself (off = it only masks)")

    use_fill: BoolProperty(name="Fill", default=True, update=_layer_update)
    fill_type: EnumProperty(
        name="Fill Type",
        items=[
            ("SOLID", "Solid", "", "SNAP_FACE", 0),
            ("LINEAR", "Linear", "Linear gradient", "IPO_LINEAR", 1),
            ("RADIAL", "Radial", "Radial gradient", "SPHERECURVE", 2),
        ],
        default="SOLID", update=_layer_update,
    )
    fill_color: FloatVectorProperty(
        name="Fill Color", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(0.9, 0.3, 0.2, 1.0), update=_layer_update,
    )
    fill_color2: FloatVectorProperty(
        name="Gradient End", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(1.0, 0.85, 0.2, 1.0), update=_layer_update,
    )
    gradient_angle: FloatProperty(name="Angle", subtype="ANGLE", default=0.0, update=_layer_update)

    use_stroke: BoolProperty(name="Stroke", default=False, update=_layer_update)
    stroke_color: FloatVectorProperty(
        name="Stroke Color", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(0.05, 0.05, 0.05, 1.0), update=_layer_update,
    )
    stroke_width: FloatProperty(
        name="Width", subtype="DISTANCE", unit="LENGTH", default=0.02, min=0.0,
        soft_max=0.5, precision=4, update=_layer_update,
    )
    stroke_cap: EnumProperty(
        name="Line Cap",
        items=[
            ("BUTT", "Butt", "Flat end exactly at the path end"),
            ("ROUND", "Round", "Half circle past the path end"),
            ("SQUARE", "Projecting", "Square end past the path end by half the width"),
        ],
        default="BUTT", update=_layer_update,
    )
    stroke_fill_type: EnumProperty(
        name="Stroke Type",
        items=[
            ("SOLID", "Solid", "", "SNAP_FACE", 0),
            ("ALONG", "Along Path", "Gradient from the start to the end of the path", "CURVE_PATH", 1),
            ("LINEAR", "Linear", "Linear gradient across the shape", "IPO_LINEAR", 2),
            ("RADIAL", "Radial", "Radial gradient from the shape center", "SPHERECURVE", 3),
        ],
        default="SOLID", update=_layer_update,
    )
    stroke_color2: FloatVectorProperty(
        name="Stroke Gradient End", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(0.9, 0.2, 0.5, 1.0), update=_layer_update,
    )
    stroke_gradient_angle: FloatProperty(name="Angle", subtype="ANGLE", default=0.0, update=_layer_update)

    use_dashes: BoolProperty(name="Dashes", default=False, update=_layer_update)
    dash: FloatProperty(name="Dash", subtype="DISTANCE", unit="LENGTH", default=0.05, min=0.0,
                        precision=4, update=_layer_update)
    gap: FloatProperty(name="Gap", subtype="DISTANCE", unit="LENGTH", default=0.03, min=0.0,
                       precision=4, update=_layer_update)
    dash_offset: FloatProperty(name="Offset", subtype="DISTANCE", unit="LENGTH", default=0.0,
                               precision=4, update=_layer_update,
                               description="Slides the dashes along the path (animatable)")

    use_trim: BoolProperty(name="Trim Path", default=False, update=_layer_update)
    trim_start: FloatProperty(name="Start", subtype="PERCENTAGE", default=0.0, min=0.0, max=100.0,
                              update=_layer_update, description="Where the visible part of the stroke begins (animatable)")
    trim_end: FloatProperty(name="End", subtype="PERCENTAGE", default=100.0, min=0.0, max=100.0,
                            update=_layer_update, description="Where the visible part of the stroke ends (animatable)")
    trim_offset: FloatProperty(name="Offset", subtype="ANGLE", default=0.0, update=_layer_update,
                               description="Slides the visible part along the path; 360° = one full lap (animatable)")
    trim_mode: EnumProperty(
        name="Trim Multiple Paths",
        items=[("SIMULTANEOUS", "Simultaneously", "Each path of the layer is trimmed on its own, all at once"),
               ("INDIVIDUAL", "Individually", "The paths are trimmed one after another, as a single long path")],
        default="SIMULTANEOUS", update=_layer_update,
    )

    use_taper: BoolProperty(name="Taper", default=False, update=_layer_update)
    taper_start_length: FloatProperty(name="Start Length", subtype="FACTOR", default=0.0, min=0, max=1,
                                      update=_layer_update, description="Fraction of the path that tapers at the start")
    taper_end_length: FloatProperty(name="End Length", subtype="FACTOR", default=0.3, min=0, max=1,
                                    update=_layer_update, description="Fraction of the path that tapers at the end")
    taper_start_width: FloatProperty(name="Start Width", subtype="FACTOR", default=0.0, min=0, max=1,
                                     update=_layer_update, description="Width at the very start, relative to the stroke width")
    taper_end_width: FloatProperty(name="End Width", subtype="FACTOR", default=0.0, min=0, max=1,
                                   update=_layer_update, description="Width at the very end, relative to the stroke width")
    taper_start_ease: FloatProperty(name="Start Ease", subtype="FACTOR", default=0.0, min=0, max=1, update=_layer_update)
    taper_end_ease: FloatProperty(name="End Ease", subtype="FACTOR", default=0.0, min=0, max=1, update=_layer_update)

    use_wave: BoolProperty(name="Wave", default=False, update=_layer_update)
    wave_shape: EnumProperty(
        name="Shape",
        items=[("SMOOTH", "Smooth", "Sine wave"), ("ZIGZAG", "Zig Zag", "Sharp triangle wave")],
        default="SMOOTH", update=_layer_update,
    )
    wave_amount: FloatProperty(name="Amount", subtype="DISTANCE", unit="LENGTH", default=0.02, min=0.0,
                               precision=4, update=_layer_update)
    wave_length: FloatProperty(name="Wavelength", subtype="DISTANCE", unit="LENGTH", default=0.1, min=0.0001,
                               precision=4, update=_layer_update)
    modifiers: CollectionProperty(type=VL_ModifierProps)
    wave_phase: FloatProperty(name="Phase", subtype="ANGLE", default=0.0, update=_layer_update,
                              description="Shifts the wave along the path (animatable)")

    opacity: FloatProperty(name="Opacity", subtype="FACTOR", default=1.0, min=0, max=1, update=_layer_update)
    projection: EnumProperty(
        name="Projection",
        items=[
            ("AUTO", "Auto", "Flat shapes (text, 2D curves, Vector Tools shapes) project like decals; curves drawn on the surface follow it"),
            ("PLANAR", "Planar", "Project along the object's local Z axis, like a decal"),
            ("SURFACE", "Surface", "Curve lies on the surface; strokes use true 3D distance"),
        ],
        default="AUTO", update=_layer_update,
    )
    depth: FloatProperty(
        name="Depth", subtype="DISTANCE", unit="LENGTH", default=0.0, min=0.0,
        description="How far the projection reaches (0 = automatic)", update=_layer_update,
    )


class VL_TargetProps(bpy.types.PropertyGroup):
    """Another object that receives the same vector layers."""
    obj: PointerProperty(
        name="Object", type=bpy.types.Object,
        poll=lambda self, o: o.type == "MESH", update=_set_update,
    )
    uv_map: StringProperty(name="UV Map", default="", update=_set_update)
    image: PointerProperty(
        name="Image", type=bpy.types.Image, update=_set_update,
        description="Image this object receives (empty = the same image as the main object)",
    )


class VL_SetProps(bpy.types.PropertyGroup):
    target: PointerProperty(
        name="Target", type=bpy.types.Object,
        poll=lambda self, o: o.type == "MESH", update=_set_update,
        description="Mesh that receives the vectors",
    )
    uv_map: StringProperty(name="UV Map", default="", update=_set_update)
    image: PointerProperty(name="Image", type=bpy.types.Image, update=_set_update)
    extra_targets: CollectionProperty(type=VL_TargetProps)
    rows: CollectionProperty(type=VL_RowProps)
    live: BoolProperty(name="Live", default=True, update=_set_update,
                       description="Update the image automatically when a layer changes")
    margin: IntProperty(name="Margin", default=4, min=0, max=32, update=_set_update,
                        description="Pixels to bleed past UV island borders")
    keep_background: BoolProperty(name="Background", default=False, update=_set_update)
    parent_layers: BoolProperty(
        name="Parent to Target", default=True,
        description="New curves and texts in this set are parented to the target (keeping their place), "
                    "so they move, rotate and scale with it",
    )
    background_color: FloatVectorProperty(
        name="Background Color", subtype="COLOR_GAMMA", size=4, min=0, max=1,
        default=(1, 1, 1, 1), update=_set_update,
    )
    active_index: IntProperty(name="Active Layer", default=-1, update=lambda s, c: _select_active(s, c))
    last_info: StringProperty(default="")


def _select_active(setprops, context):
    coll = setprops.id_data
    i = setprops.active_index
    if not (0 <= i < len(setprops.rows)):
        return
    obj = setprops.rows[i].obj
    if obj is None or context.mode != "OBJECT":
        return
    try:
        for o in context.view_layer.objects.selected:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        _state["last_active"] = (context.scene.name, obj.name)
    except RuntimeError:
        pass


def _row_list(coll):
    """Visible rows, top of the stack first: [(kind, item, depth)]."""
    out = []

    def walk(parent, depth):
        for k, x in reversed(bridge.children(parent)):
            out.append((k, x, depth))
            if k == "G" and x.vl_group.show_expanded:
                walk(x, depth + 1)
    walk(coll, 0)
    return out


def refresh_rows(coll):
    """Rebuild the list rows when the layers/groups changed; keeps the active item."""
    st = coll.vl_set
    rows = _row_list(coll)
    sig = [(k, x.name, d) for k, x, d in rows]
    cur = [("L" if r.obj else "G", (r.obj or r.group).name if (r.obj or r.group) else "", r.depth)
           for r in st.rows]
    if sig == cur:
        return False
    act = None
    if 0 <= st.active_index < len(st.rows):
        r = st.rows[st.active_index]
        act = r.obj or r.group
    st.rows.clear()
    new_index = -1
    for n, (k, x, d) in enumerate(rows):
        r = st.rows.add()
        r.name = x.name
        r.depth = d
        if k == "L":
            r.obj = x
        else:
            r.group = x
        if act is not None and x == act:
            new_index = n
    st["active_index"] = new_index  # no selection side effect
    return True


def active_row(context):
    coll = active_set(context)
    if coll is None:
        return None, None
    st = coll.vl_set
    i = st.active_index
    if 0 <= i < len(st.rows):
        r = st.rows[i]
        if r.obj is not None:
            return "L", r.obj
        if r.group is not None:
            return "G", r.group
    return None, None


def active_group(context):
    k, x = active_row(context)
    return x if k == "G" else None


def insert_parent(context, coll):
    """Where new layers go: inside the active group, or next to the active layer."""
    k, x = active_row(context)
    if k == "G":
        return x
    if k == "L":
        return parent_of(coll, "L", x) or coll
    return coll


def reveal(coll, kind, item):
    """Expand the groups holding an item so it shows in the list; returns its row index."""
    p = parent_of(coll, kind, item)
    while p is not None and p != coll:
        p.vl_group.show_expanded = True
        p = parent_of(coll, "G", p)
    refresh_rows(coll)
    for n, r in enumerate(coll.vl_set.rows):
        if (r.obj if kind == "L" else r.group) == item:
            return n
    return -1


def active_set(context):
    c = context.scene.vl_active_set
    return c


def shown_object(context, coll):
    """The mesh whose settings the panel shows: the active object if it uses the set,
    else the set's main object."""
    ob = context.view_layer.objects.active if context.view_layer else None
    if ob is not None and ob.type == "MESH" and ob in set_objects(coll):
        return ob
    return coll.vl_set.target


def binding(coll, obj):
    """(property owner, object property name) holding obj's UV map / image in this set."""
    st = coll.vl_set
    if obj is None or obj == st.target:
        return st, "target"
    for t in st.extra_targets:
        if t.obj == obj:
            return t, "obj"
    return st, "target"


def nearest_set_object(context, coll, point):
    """The object of the set whose surface is closest to a world point."""
    from mathutils.bvhtree import BVHTree
    objs = set_objects(coll)
    if len(objs) < 2:
        return coll.vl_set.target
    dg = context.evaluated_depsgraph_get()
    best, best_d = objs[0], float("inf")
    for o in objs:
        mw = o.matrix_world
        hit = BVHTree.FromObject(o, dg).find_nearest(mw.inverted_safe() @ point)
        if hit[0] is not None:
            d = ((mw @ hit[0]) - point).length
            if d < best_d:
                best, best_d = o, d
    return best


def active_layer(context):
    k, x = active_row(context)
    return x if k == "L" else None


# --------------------------------------------------------------------------
# Rendering driver
# --------------------------------------------------------------------------


def _redraw():
    wm = bpy.context.window_manager
    if not wm:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {"VIEW_3D", "IMAGE_EDITOR"}:
                area.tag_redraw()


def render_collection(coll, force=False, depsgraph=None):
    ok, msg = bridge.render_set(coll, depsgraph=depsgraph, force=force)
    if not ok:
        coll.vl_set.last_info = msg
    _redraw()
    return ok, msg


def parent_layers(coll, only_unparented=True, from_obj=None):
    """Parent the set's curves/texts to its target, keeping their world placement.
    from_obj: also move layers that were parented to this object (main object changed)."""
    tgt = coll.vl_set.target
    if tgt is None:
        return 0
    n = 0
    for o in bridge.layer_objects(coll):
        if o.parent == tgt:
            continue
        if only_unparented and o.parent is not None and o.parent != from_obj:
            continue  # respect the user's own parenting
        mw = o.matrix_world.copy()
        o.parent = tgt
        o.matrix_parent_inverse = tgt.matrix_world.inverted_safe()
        o.matrix_world = mw
        n += 1
    return n


def _timer():
    if _state["dirty"] and not _state["busy"]:
        if time.perf_counter() - _state["last_change"] > 0.06:
            names = list(_state["dirty"])
            _state["dirty"].clear()
            _state["busy"] = True
            try:
                for name in names:
                    coll = bpy.data.collections.get(name)
                    if coll and coll.vl_set.live and coll.vl_set.target and coll.vl_set.image:
                        try:
                            render_collection(coll)
                        except Exception as e:
                            coll.vl_set.last_info = f"Error: {e}"
                            print("[Nakan Vector Texturing]", repr(e))
            finally:
                _state["busy"] = False
    _follow_active()
    _watch_ramps()
    # assign orders to newly added layers
    try:
        for c in _set_collections():
            objs = bridge.layer_objects(c)
            if any(o.vl.order <= 0 for o in objs) or any(g.vl_group.order <= 0 for g in c.children_recursive):
                for p in [c] + list(c.children_recursive):
                    bridge.children(p)  # assigns orders to new layers / groups
                tag_set(c)
            if refresh_rows(c):
                _redraw()
            if c.vl_set.parent_layers and bpy.context.mode == "OBJECT" and \
                    any(o.parent is None for o in objs):
                parent_layers(c)
    except Exception:
        pass
    return 0.08


_RAMP_SIGS = {}


def _ramp_sig(tex):
    cr = tex.color_ramp
    return (cr.interpolation, cr.color_mode, cr.hue_interpolation,
            tuple((round(e.position, 5), tuple(round(x, 5) for x in e.color)) for e in cr.elements))


def _watch_ramps():
    """A Roughen Edges ramp lives in a texture nothing renders, so the depsgraph never
    reports edits to it: compare the ramps here instead."""
    try:
        for c in _set_collections():
            for o in bridge.layer_objects(c):
                for m in o.vl.modifiers:
                    tex = m.noise_ramp if m.type == "ROUGHEN" else None
                    if tex is None or not tex.use_color_ramp:
                        continue
                    k = (c.name, o.name, tex.name)
                    sig = _ramp_sig(tex)
                    old = _RAMP_SIGS.get(k)
                    _RAMP_SIGS[k] = sig
                    if old is not None and old != sig:
                        tag_set(c)
    except Exception:
        pass


def _follow_active():
    """Show the vector layers of whatever the user clicks (unless the set is pinned)."""
    try:
        ctx = bpy.context
        scene, vlayer = ctx.scene, ctx.view_layer
        ob = vlayer.objects.active if vlayer else None
    except Exception:
        return
    key = (scene.name if scene else "", ob.name if ob else "")
    if key == _state.get("last_active"):
        return
    _state["last_active"] = key
    if scene is None or ob is None or scene.vl_pin_set:
        return
    cur = scene.vl_active_set
    if ob.type == "MESH":
        sets = sets_using(ob)
        if cur is None or cur not in sets:
            scene.vl_active_set = sets[0] if sets else None
    elif ob.type in {"CURVE", "FONT"}:
        sets = [c for c in _set_collections() if ob.name in c.all_objects]
        if sets and (cur is None or cur not in sets):
            scene.vl_active_set = cur = sets[0]
        if cur is not None and ob.name in cur.all_objects:
            # clicking a layer in the viewport selects it in the list
            cur.vl_set["active_index"] = reveal(cur, "L", ob)
    else:
        return
    _redraw()


@persistent
def _on_depsgraph(scene, depsgraph):
    if _state["busy"]:
        return
    sets = _set_collections()
    if not sets:
        return
    for upd in depsgraph.updates:
        idb = upd.id
        if isinstance(idb, bpy.types.Image):
            continue
        orig = getattr(idb, "original", idb)
        if isinstance(orig, bpy.types.Object):
            for c in sets:
                if orig.name in c.all_objects or orig in set_objects(c):
                    tag_set(c)
        elif isinstance(orig, bpy.types.Curve):  # Curve and TextCurve data
            for c in sets:
                if any(o.data == orig for o in c.all_objects):
                    tag_set(c)
        elif isinstance(orig, bpy.types.Collection):
            c = set_of(orig)
            if c is not None:
                tag_set(c)
        elif isinstance(orig, bpy.types.Texture):  # Roughen Edges color ramp
            for c in sets:
                if any(m.noise_ramp == orig for o in bridge.layer_objects(c)
                       for m in o.vl.modifiers):
                    tag_set(c)
        elif isinstance(orig, bpy.types.Mesh):
            for c in sets:
                if any(o.data == orig for o in set_objects(c)):
                    tag_set(c)


def _is_animated(idb):
    ad = getattr(idb, "animation_data", None) if idb is not None else None
    return bool(ad and (ad.action or len(ad.drivers) or len(ad.nla_tracks)))


def _set_is_animated(coll):
    s = coll.vl_set
    for o in bridge.layer_objects(coll) + [s.target]:
        if o is None:
            continue
        if o.type in {"CURVE", "FONT"} and any(
                m.enabled and m.type == "ROUGHEN" and m.use_boil for m in o.vl.modifiers):
            return True
        if _is_animated(o) or _is_animated(o.data):
            return True
        par = o.parent
        while par is not None:
            if _is_animated(par):
                return True
            par = par.parent
    return any(_is_animated(c) for c in [coll] + list(coll.children_recursive))


@persistent
def _on_frame(scene, depsgraph=None):
    """Keyframed layers (Trim Path, dash offset, wave phase, colors, transforms...) redraw on
    frame change. While the timeline plays the redraw is queued (playback keeps its speed);
    when stepping frames or rendering it happens right away, so every frame is correct."""
    if _state["busy"]:
        return
    try:
        screen = bpy.context.screen
        playing = bool(screen and screen.is_animation_playing)
    except Exception:
        playing = False
    for c in _set_collections():
        s = c.vl_set
        if not (s.live and s.target and s.image) or not _set_is_animated(c):
            continue
        if playing:
            tag_set(c)
            continue
        _state["busy"] = True
        try:
            rendering = depsgraph is not None and getattr(depsgraph, "mode", "") == "RENDER"
            render_collection(c, depsgraph=depsgraph if rendering else None)
            _state["dirty"].discard(c.name)
        except Exception as e:
            s.last_info = f"Error: {e}"
            print("[Nakan Vector Texturing]", repr(e))
        finally:
            _state["busy"] = False


@persistent
def _on_load(*_):
    bridge.clear_cache()
    tag_all()


@persistent
def _on_save(*_):
    # generated images live only in memory: pack them so the .blend keeps them
    for c in _set_collections():
        for _o, _uv, img in bridge.set_targets(c):
            if img and (img.source == "GENERATED" or (img.is_dirty and (img.packed_file or not img.filepath))):
                try:
                    img.pack()
                except Exception as e:
                    print("[Nakan Vector Texturing] pack failed:", e)


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------


def _move_to_collection(obj, coll):
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)


def _parent_collection(context, obj):
    """The collection the object lives in (within this scene), else the scene's."""
    scene_root = context.scene.collection
    if obj is not None:
        in_scene = set(scene_root.children_recursive)
        for c in obj.users_collection:
            if c == scene_root or c in in_scene:
                return c
    return context.collection or scene_root


class VL_OT_set_new(bpy.types.Operator):
    bl_idname = "vector_layers.set_new"
    bl_label = "New Vector Set"
    bl_description = "Create a collection that holds vector layers for the active mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target = context.active_object if context.active_object and context.active_object.type == "MESH" else None
        name = f"{target.name} Vector Layers" if target else "Vector Layers"
        coll = bpy.data.collections.new(name)
        _parent_collection(context, target).children.link(coll)
        s = coll.vl_set
        if target:
            s.target = target
            if target.data.uv_layers.active:
                s.uv_map = target.data.uv_layers.active.name
        context.scene.vl_active_set = coll
        return {"FINISHED"}


_SET_ITEMS = []  # keep enum strings alive (Blender requirement for dynamic enums)


def _set_items(self, context):
    _SET_ITEMS[:] = [(c.name, c.name, f"Vector layers painted onto {', '.join(o.name for o in set_objects(c))}",
                      "OUTLINER_COLLECTION", i) for i, c in enumerate(_set_collections())]
    return _SET_ITEMS or [("", "No vector sets", "")]


def _meshes_to_link(context, coll):
    objs = [o for o in context.selected_objects if o.type == "MESH"]
    ob = context.view_layer.objects.active
    if ob is not None and ob.type == "MESH" and ob not in objs:
        objs.append(ob)
    used = set_objects(coll)
    return [o for o in objs if o not in used]


class VL_OT_set_link(bpy.types.Operator):
    bl_idname = "vector_layers.set_link"
    bl_label = "Use Existing Vector Set"
    bl_description = ("Paint an existing set of vector layers onto the selected mesh(es) too: "
                      "the same curves and texts, projected onto each object")
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "set_name"

    set_name: EnumProperty(name="Vector Set", items=_set_items)

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and any(o.type == "MESH" for o in context.selected_objects + (
            [context.view_layer.objects.active] if context.view_layer.objects.active else []))

    def invoke(self, context, event):
        if self.properties.is_property_set("set_name"):
            return self.execute(context)
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        coll = bpy.data.collections.get(self.set_name)
        if coll is None or coll.vl_set.target is None:
            return {"CANCELLED"}
        objs = _meshes_to_link(context, coll)
        if not objs:
            self.report({"INFO"}, "The selected objects already use this set")
        for o in objs:
            t = coll.vl_set.extra_targets.add()
            t.obj = o
            if o.data.uv_layers.active:
                t.uv_map = o.data.uv_layers.active.name
            # reuse the main image when this object's material already uses it, else leave it
            # empty (= main image); the user can give it its own image in the panel
        context.scene.vl_active_set = coll
        tag_set(coll)
        if objs:
            self.report({"INFO"}, f"{', '.join(o.name for o in objs)} now use {coll.name}")
        return {"FINISHED"}


class VL_OT_set_unlink(bpy.types.Operator):
    bl_idname = "vector_layers.set_unlink"
    bl_label = "Stop Using This Set"
    bl_description = ("This object stops receiving the set's vector layers "
                      "(the curves and the other objects are not touched)")
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty()

    def execute(self, context):
        coll = active_set(context)
        obj = bpy.data.objects.get(self.object_name)
        if coll is None or obj is None:
            return {"CANCELLED"}
        st = coll.vl_set
        if obj == st.target:
            # the main object leaves: the first other object becomes the main one
            if not len(st.extra_targets):
                self.report({"WARNING"}, "This is the only object of the set; delete the collection instead")
                return {"CANCELLED"}
            first = st.extra_targets[0]
            new_main, uv, img = first.obj, first.uv_map, first.image
            st.extra_targets.remove(0)
            st.target, st.uv_map = new_main, uv
            if img is not None:
                st.image = img
            parent_layers(coll, from_obj=obj)
        else:
            for i, t in enumerate(st.extra_targets):
                if t.obj == obj:
                    st.extra_targets.remove(i)
                    break
        bridge.release_set(coll)
        tag_set(coll)
        return {"FINISHED"}


class VL_OT_set_make_main(bpy.types.Operator):
    bl_idname = "vector_layers.set_make_main"
    bl_label = "Make Main Object"
    bl_description = ("The curves and texts follow the main object (they are parented to it); "
                      "make this object the main one")
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty()

    def execute(self, context):
        coll = active_set(context)
        obj = bpy.data.objects.get(self.object_name)
        if coll is None or obj is None or obj == coll.vl_set.target:
            return {"CANCELLED"}
        st = coll.vl_set
        old_main = st.target
        for i, t in enumerate(st.extra_targets):
            if t.obj == obj:
                old, old_uv, old_img = st.target, st.uv_map, st.image
                uv, img = t.uv_map, t.image or st.image
                t.obj, t.uv_map, t.image = old, old_uv, old_img
                st.target, st.uv_map, st.image = obj, uv, img
                break
        parent_layers(coll, from_obj=old_main)
        tag_set(coll)
        return {"FINISHED"}


class VL_OT_image_new(bpy.types.Operator):
    bl_idname = "vector_layers.image_new"
    bl_label = "New Image"
    bl_description = "Create a transparent image for this vector set"
    bl_options = {"REGISTER", "UNDO"}

    size: EnumProperty(
        name="Size",
        items=[("1024", "1024", ""), ("2048", "2048", ""), ("4096", "4096", "")],
        default="2048",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        coll = active_set(context)
        if coll is None:
            return {"CANCELLED"}
        n = int(self.size)
        obj = shown_object(context, coll)
        owner, _prop = binding(coll, obj)
        name = coll.name if owner == coll.vl_set else f"{obj.name} Vector Layers"
        img = bpy.data.images.new(name, n, n, alpha=True)
        img.generated_color = (0, 0, 0, 0)
        owner.image = img
        tag_set(coll)
        return {"FINISHED"}


class VL_OT_update(bpy.types.Operator):
    bl_idname = "vector_layers.update"
    bl_label = "Update"
    bl_description = "Re-render the vector layers into the image now"

    def execute(self, context):
        coll = active_set(context)
        if coll is None:
            return {"CANCELLED"}
        bridge.clear_cache()
        ok, msg = render_collection(coll, force=True)
        self.report({"INFO"} if ok else {"WARNING"}, coll.vl_set.last_info if ok else msg)
        return {"FINISHED"} if ok else {"CANCELLED"}


class VL_OT_bake(bpy.types.Operator):
    bl_idname = "vector_layers.bake"
    bl_label = "Bake"
    bl_description = ("Freeze the current result in the image, packed inside the .blend: render once more "
                      "and turn Live off so nothing overwrites it")
    bl_options = {"REGISTER", "UNDO"}

    save_png: BoolProperty(
        name="Also Save PNG Copy", default=False,
        description="Also write a copy to a 'textures' folder next to the .blend (the image itself stays packed)",
    )
    hide_layers: BoolProperty(name="Hide Vector Objects", default=False,
                              description="Hide the curves and texts of this set in the viewport")

    @classmethod
    def poll(cls, context):
        c = context.scene.vl_active_set
        return c is not None and c.vl_set.image is not None and c.vl_set.target is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        import os
        coll = active_set(context)
        s = coll.vl_set
        ok, msg = render_collection(coll, force=True)
        if not ok:
            self.report({"WARNING"}, msg)
            return {"CANCELLED"}
        s.live = False
        _state["dirty"].discard(coll.name)
        images = []
        for _o, _uv, im in bridge.set_targets(coll):
            if im is not None and im not in images:
                images.append(im)
        # pack the current pixels into the .blend (works for generated and file images)
        for img in images:
            img.pack()
        where = "packed in the .blend" if len(images) == 1 else f"{len(images)} images packed in the .blend"
        if self.save_png:
            if not bpy.data.filepath:
                self.report({"WARNING"}, "Save the .blend first to write a PNG copy")
            else:
                folder = bpy.path.abspath("//textures")
                os.makedirs(folder, exist_ok=True)
                for img in images:
                    path = os.path.join(folder, (bpy.path.clean_name(img.name) or "vectors") + ".png")
                    cp = img.copy()
                    try:
                        cp.filepath_raw = path
                        cp.file_format = "PNG"
                        cp.save()
                    finally:
                        bpy.data.images.remove(cp)
                where += " + PNG copy in //textures"
        # the cache only speeds up live updates: free it, a bake is usually the end of the work
        bridge.release_set(coll)
        if self.hide_layers:
            for o in bridge.layer_objects(coll):
                o.hide_set(True)
        s.last_info = f"Baked ({where}) · Live off · cache freed"
        self.report({"INFO"}, s.last_info)
        return {"FINISHED"}


class VL_OT_parent_all(bpy.types.Operator):
    bl_idname = "vector_layers.parent_all"
    bl_label = "Parent All to Target"
    bl_description = "Parent every curve and text of this set to the target, keeping them in place"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        coll = active_set(context)
        if coll is None or coll.vl_set.target is None:
            return {"CANCELLED"}
        n = parent_layers(coll, only_unparented=False)
        self.report({"INFO"}, f"Parented {n} objects to {coll.vl_set.target.name}")
        return {"FINISHED"}


class VL_OT_layer_move(bpy.types.Operator):
    bl_idname = "vector_layers.layer_move"
    bl_label = "Move Layer"
    bl_description = ("Move the active layer or group up or down in the stack. A layer moving onto a "
                      "group goes into it; at the edge of a group it comes out")
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        coll = active_set(context)
        kind, item = active_row(context)
        if not coll or item is None:
            return {"CANCELLED"}
        parent = parent_of(coll, kind, item)
        sibs = bridge.children(parent)  # bottom -> top
        i = next(n for n, kx in enumerate(sibs) if kx[1] == item)
        up = self.direction == "UP"
        j = i + 1 if up else i - 1
        if 0 <= j < len(sibs):
            nk, nb = sibs[j]
            if kind == "L" and nk == "G":
                # into the group: at its bottom when moving up, at its top when moving down
                inside = bridge.children(nb)
                move_item(coll, kind, item, nb, 0 if up else len(inside))
                nb.vl_group.show_expanded = True
            else:
                place_in(parent, kind, item, j)
        elif parent != coll:
            # out of the group, just above / below it
            gp = parent_of(coll, "G", parent)
            gs = bridge.children(gp)
            gi = next(n for n, kx in enumerate(gs) if kx[1] == parent)
            move_item(coll, kind, item, gp, gi + 1 if up else gi)
        else:
            return {"CANCELLED"}
        coll.vl_set["active_index"] = reveal(coll, kind, item)
        tag_set(coll)
        return {"FINISHED"}


class VL_OT_group_new(bpy.types.Operator):
    bl_idname = "vector_layers.group_new"
    bl_label = "New Group"
    bl_description = ("Create a group with the active layer and the layers selected in the viewport "
                      "(or around the active group). Groups are sub-collections: you can also drag "
                      "layers in and out of them in the Outliner")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        coll = active_set(context)
        if coll is None:
            return {"CANCELLED"}
        kind, item = active_row(context)
        if item is not None:
            parent = parent_of(coll, kind, item)
            pos = next(n for n, kx in enumerate(bridge.children(parent)) if kx[1] == item) + 1
        else:
            parent, pos = coll, len(bridge.children(coll))
        g = bpy.data.collections.new("Group")
        parent.children.link(g)
        place_in(parent, "G", g, pos)
        if kind == "G":
            # active row is a group: wrap it in the new group (nested groups)
            move_item(coll, "G", item, g, 0)
        else:
            # the selected layers (or the active one) go into the group, keeping their order
            layers = bridge.layer_objects(coll)
            chosen = [o for o in context.selected_objects if o in layers]
            if kind == "L" and item not in chosen:
                chosen.append(item)
            flat = [x for k, x, d in _row_list(coll) if k == "L"]
            chosen.sort(key=lambda o: -flat.index(o) if o in flat else 0)
            for n, o in enumerate(chosen):
                move_item(coll, "L", o, g, n)
        coll.vl_set["active_index"] = reveal(coll, "G", g)
        tag_set(coll)
        return {"FINISHED"}


class VL_OT_group_ungroup(bpy.types.Operator):
    bl_idname = "vector_layers.group_ungroup"
    bl_label = "Ungroup"
    bl_description = "Remove the active group; its layers stay where they are in the stack"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return active_group(context) is not None

    def execute(self, context):
        coll = active_set(context)
        g = active_group(context)
        parent = parent_of(coll, "G", g)
        pos = next(n for n, kx in enumerate(bridge.children(parent)) if kx[1] == g)
        kids = bridge.children(g)
        for n, (k, x) in enumerate(kids):
            move_item(coll, k, x, parent, pos + n)
        bpy.data.collections.remove(g)
        refresh_rows(coll)
        tag_set(coll)
        return {"FINISHED"}


def _new_layer_order(parent):
    return len(bridge.children(parent)) + 1


def _activate_in_list(coll, obj):
    coll.vl_set["active_index"] = reveal(coll, "L", obj)


def _add_to_stack(context, coll, obj):
    """Put a new layer in the active group (or next to the active layer), on top."""
    dest = insert_parent(context, coll)
    _move_to_collection(obj, dest)
    place_in(dest, "L", obj, len(bridge.children(dest)))


class VL_OT_add_curve(bpy.types.Operator):
    bl_idname = "vector_layers.add_curve"
    bl_label = "Draw Curve"
    bl_description = "Add a curve layer and start drawing it on the surface"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        coll = active_set(context)
        if coll is None:
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.curve.primitive_bezier_curve_add(enter_editmode=False, location=(0, 0, 0))
        obj = context.active_object
        obj.name = "Vector Curve"
        obj.data.splines.clear()
        obj.data.dimensions = "3D"
        _add_to_stack(context, coll, obj)
        obj.vl.use_fill = False
        obj.vl.use_stroke = True
        if coll.vl_set.parent_layers:
            parent_layers(coll)
        _activate_in_list(coll, obj)
        # draw on the surface
        ts = context.scene.tool_settings
        cps = ts.curve_paint_settings
        cps.depth_mode = "SURFACE"
        cps.surface_offset = 0.0
        cps.curve_type = "BEZIER"
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.wm.tool_set_by_id(name="builtin.draw")
        except Exception:
            pass
        tag_set(coll)
        return {"FINISHED"}


def _surface_frame(context, target, point, view_up=None, max_dist=None):
    """World matrix sitting on the target surface nearest to `point`:
    Z = face normal, Y = up (view up, else world Z, else world Y) projected on the face."""
    if target is None or target.type != "MESH":
        return None
    from mathutils.bvhtree import BVHTree
    dg = context.evaluated_depsgraph_get()
    bvh = BVHTree.FromObject(target, dg)
    mw = target.matrix_world
    loc, nrm, _, dist = bvh.find_nearest(mw.inverted_safe() @ point)
    if loc is None:
        return None
    loc_w = mw @ loc
    if max_dist is None:
        max_dist = max(target.dimensions) * 0.25
    if (loc_w - point).length > max_dist:
        return None
    n = (mw.inverted_safe().transposed().to_3x3() @ nrm).normalized()
    up = None
    for cand in (view_up, Vector((0, 0, 1)), Vector((0, 1, 0))):
        if cand is None:
            continue
        u = cand - n * cand.dot(n)
        if u.length > 0.2:
            up = u.normalized()
            break
    x = up.cross(n).normalized()
    y = n.cross(x)
    m = Matrix((x, y, n)).transposed().to_4x4()
    m.translation = loc_w + n * 0.001
    return m


class VL_OT_add_text(bpy.types.Operator):
    bl_idname = "vector_layers.add_text"
    bl_label = "Add Text"
    bl_description = ("Add a text layer at the 3D cursor, lying on the target's surface "
                      "(aligned to its face normal, upright for the current view)")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        coll = active_set(context)
        if coll is None:
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        cursor = context.scene.cursor.location.copy()
        view_up = None
        rv3d = getattr(context.space_data, "region_3d", None) if context.space_data else None
        if rv3d is not None:
            view_up = rv3d.view_rotation @ Vector((0, 1, 0))
        frame = _surface_frame(context, nearest_set_object(context, coll, cursor), cursor, view_up)
        if frame is None:
            align = "VIEW" if rv3d is not None else "WORLD"
            bpy.ops.object.text_add(align=align, location=cursor)
        else:
            bpy.ops.object.text_add(align="WORLD", location=cursor)
            context.active_object.matrix_world = frame
        obj = context.active_object
        obj.name = "Vector Text"
        obj.data.body = "Text"
        obj.data.size = 0.2
        obj.data.align_x = "CENTER"
        obj.data.align_y = "CENTER"
        _add_to_stack(context, coll, obj)
        bridge.init_layer(obj)
        obj.vl.use_fill = True
        obj.vl.use_stroke = False
        if coll.vl_set.parent_layers:
            parent_layers(coll)
        _activate_in_list(coll, obj)
        tag_set(coll)
        return {"FINISHED"}


def ensure_noise_ramp(m):
    """Roughen Edges keeps its color ramp in a small hidden texture (the only place Blender
    can show a color ramp widget outside a node)."""
    tex = m.noise_ramp
    if tex is None:
        tex = bpy.data.textures.new(".VL Roughen Ramp", type="BLEND")
        tex.use_color_ramp = True
        m.noise_ramp = tex
    elif not tex.use_color_ramp:
        tex.use_color_ramp = True
    return tex


class VL_OT_modifier_ramp_fix(bpy.types.Operator):
    bl_idname = "vector_layers.modifier_ramp_fix"
    bl_label = "Add Ramp"
    bl_description = "Give this modifier its own color ramp"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()
    single: BoolProperty(default=False)

    def execute(self, context):
        obj = active_layer(context)
        if not obj or not (0 <= self.index < len(obj.vl.modifiers)):
            return {"CANCELLED"}
        m = obj.vl.modifiers[self.index]
        if self.single and m.noise_ramp is not None:
            m.noise_ramp = m.noise_ramp.copy()
        ensure_noise_ramp(m)
        _layer_update(obj.vl, context)
        return {"FINISHED"}


class VL_OT_modifier_add(bpy.types.Operator):
    bl_idname = "vector_layers.modifier_add"
    bl_label = "Add Modifier"
    bl_description = "Add a modifier to the active layer"
    bl_options = {"REGISTER", "UNDO"}

    type: EnumProperty(name="Type", items=MODIFIER_TYPES)

    def execute(self, context):
        obj = active_layer(context)
        if not obj:
            return {"CANCELLED"}
        m = obj.vl.modifiers.add()
        m.type = self.type
        m.name = dict((i[0], i[1]) for i in MODIFIER_TYPES)[self.type]
        if self.type == "ROUGHEN":
            ensure_noise_ramp(m)
        _layer_update(obj.vl, context)
        return {"FINISHED"}


class VL_OT_modifier_remove(bpy.types.Operator):
    bl_idname = "vector_layers.modifier_remove"
    bl_label = "Remove Modifier"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()

    def execute(self, context):
        obj = active_layer(context)
        if not obj or not (0 <= self.index < len(obj.vl.modifiers)):
            return {"CANCELLED"}
        obj.vl.modifiers.remove(self.index)
        _layer_update(obj.vl, context)
        return {"FINISHED"}


class VL_OT_modifier_move(bpy.types.Operator):
    bl_idname = "vector_layers.modifier_move"
    bl_label = "Move Modifier"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty()
    direction: EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        obj = active_layer(context)
        if not obj:
            return {"CANCELLED"}
        j = self.index - 1 if self.direction == "UP" else self.index + 1
        if not (0 <= j < len(obj.vl.modifiers)):
            return {"CANCELLED"}
        obj.vl.modifiers.move(self.index, j)
        _layer_update(obj.vl, context)
        return {"FINISHED"}


class VL_OT_place_on_surface(bpy.types.Operator):
    bl_idname = "vector_layers.place_on_surface"
    bl_label = "Place Selected on Surface"
    bl_description = ("Bring the selected curves/texts into this set and lay them on the target "
                      "at the 3D cursor, flat on that face (upright for the current view)")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        c = context.scene.vl_active_set
        return c is not None and c.vl_set.target is not None and context.mode == "OBJECT"

    def execute(self, context):
        coll = active_set(context)
        target = nearest_set_object(context, coll, context.scene.cursor.location.copy())
        objs = [o for o in context.selected_objects if o.type in {"CURVE", "FONT"} and o != target]
        if not objs:
            self.report({"WARNING"}, "Select one or more curves or texts first")
            return {"CANCELLED"}
        rv3d = getattr(context.space_data, "region_3d", None) if context.space_data else None
        view_up = rv3d.view_rotation @ Vector((0, 1, 0)) if rv3d is not None else None
        frame = _surface_frame(context, target, context.scene.cursor.location.copy(), view_up,
                               max_dist=float("inf"))
        if frame is None:
            self.report({"WARNING"}, "Could not find the target surface")
            return {"CANCELLED"}
        pivot = context.active_object if context.active_object in objs else objs[0]
        # rigid move: the pivot lands on the surface, the others keep their place relative to it
        loc, rot, scl = pivot.matrix_world.decompose()
        old_frame = Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
        delta = frame @ old_frame.inverted()
        dest = insert_parent(context, coll)
        order = _new_layer_order(dest)
        for o in objs:
            mw = delta @ o.matrix_world
            if o.parent is not None and o.parent not in objs:
                o.parent = None
            if o.name not in coll.all_objects:
                for c in list(o.users_collection):
                    c.objects.unlink(o)
                dest.objects.link(o)
                o.vl.order = 0
            o.matrix_world = mw
            if o.vl.order <= 0:
                o.vl.order = order
                order += 1
                bridge.init_layer(o)
                closed = o.type == "FONT" or any(sp.use_cyclic_u for sp in o.data.splines)
                o.vl.use_fill = closed
                o.vl.use_stroke = not closed
        context.view_layer.update()
        if coll.vl_set.parent_layers:
            parent_layers(coll)
        _activate_in_list(coll, pivot)
        tag_set(coll)
        self.report({"INFO"}, f"Placed {len(objs)} object(s) on {target.name}")
        return {"FINISHED"}


class VL_OT_swap_gradient(bpy.types.Operator):
    bl_idname = "vector_layers.swap_gradient"
    bl_label = "Swap Colors"
    bl_description = "Swap the gradient start and end colors"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_layer(context)
        if not obj:
            return {"CANCELLED"}
        p = obj.vl
        a, b = tuple(p.fill_color), tuple(p.fill_color2)
        p.fill_color, p.fill_color2 = b, a
        return {"FINISHED"}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------


class VL_UL_layers(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        for _ in range(item.depth):
            row.label(text="", icon="BLANK1")
        if item.group is not None:
            g = item.group
            gp = g.vl_group
            row.prop(gp, "show_expanded", text="", emboss=False,
                     icon="DOWNARROW_HLT" if gp.show_expanded else "RIGHTARROW")
            row.prop(gp, "enabled", text="", emboss=False, icon="HIDE_OFF" if gp.enabled else "HIDE_ON")
            row.prop(g, "name", text="", emboss=False, icon="FILE_FOLDER")
            if gp.mask_mode != "NONE" and gp.mask_object is not None:
                row.label(text="", icon="MOD_MASK")
            if gp.opacity < 1:
                row.label(text=f"{int(round(gp.opacity * 100))}%")
            return
        obj = item.obj
        if obj is None:
            row.label(text="(removed)")
            return
        p = obj.vl
        row.prop(p, "enabled", text="", emboss=False, icon="HIDE_OFF" if p.enabled else "HIDE_ON")
        row.prop(obj, "name", text="", emboss=False,
                 icon="OUTLINER_OB_FONT" if obj.type == "FONT" else "OUTLINER_OB_CURVE")
        if p.mask_mode != "NONE" and p.mask_object is not None:
            row.label(text="", icon="MOD_MASK")
        if obj.name in _mask_hidden_names(data.id_data):
            row.label(text="", icon="CLIPUV_HLT")  # only used as a mask
        sw = row.row(align=True)
        sw.scale_x = 0.35
        if p.use_fill:
            sw.prop(p, "fill_color", text="")
        if p.use_stroke:
            sw.prop(p, "stroke_color", text="")


def _mask_hidden_names(coll):
    try:
        return bridge._mask_sources(coll)[1]
    except Exception:
        return set()


def draw_mask(layout, owner, coll):
    box = layout.box()
    box.use_property_split = False
    row = box.row(align=True)
    row.label(text="Mask", icon="MOD_MASK")
    row.prop(owner, "mask_mode", expand=True)
    if owner.mask_mode != "NONE":
        col = box.column()
        col.prop_search(owner, "mask_object", coll, "all_objects", text="", icon="OUTLINER_OB_CURVE")
        col.prop(owner, "mask_show")
        if owner.mask_object is None:
            col.label(text="Pick a curve or text of this set", icon="INFO")


class VL_PT_main(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nakan Vector Texturing"
    bl_label = "Vector Layers"
    bl_order = 10  # below the painting panel

    def draw_header(self, context):
        self.layout.label(text="", icon='CURVE_BEZCURVE')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        row = layout.row(align=True)
        row.prop(scene, "vl_pin_set", text="", emboss=False,
                 icon="PINNED" if scene.vl_pin_set else "UNPINNED")
        row.prop_search(scene, "vl_active_set", bpy.data, "collections", text="")
        row.operator("vector_layers.set_new", text="", icon="ADD")
        coll = scene.vl_active_set
        ob = context.view_layer.objects.active
        if coll is None:
            if ob is not None and ob.type == "MESH":
                box = layout.box()
                box.label(text=f"{ob.name} has no vector layers", icon="INFO")
                r = box.row(align=True)
                r.operator("vector_layers.set_new", text="New Set", icon="ADD")
                r.operator("vector_layers.set_link", text="Use Existing", icon="LINKED")
            else:
                layout.label(text="Select a mesh and create a set.", icon="INFO")
            return
        s = coll.vl_set

        # objects that receive this set
        objs = set_objects(coll)
        shown = shown_object(context, coll)
        box = layout.box()
        head = box.row(align=True)
        head.label(text=f"Objects ({len(objs)})", icon="OBJECT_DATA")
        head.operator("vector_layers.set_link", text="", icon="ADD")
        for o in objs:
            r = box.row(align=True)
            is_main = o == s.target
            r.label(text=o.name, icon="LAYER_ACTIVE" if o == shown else "LAYER_USED")
            if is_main:
                r.label(text="", icon="SOLO_ON")
            else:
                r.operator("vector_layers.set_make_main", text="", icon="SOLO_OFF",
                           emboss=False).object_name = o.name
            r.operator("vector_layers.set_unlink", text="", icon="X", emboss=False).object_name = o.name

        if shown is not None:
            owner, prop = binding(coll, shown)
            col = layout.column(align=True)
            if len(objs) > 1:
                col.label(text=shown.name, icon="MESH_DATA")
            col.prop_search(owner, "uv_map", shown.data, "uv_layers", icon="GROUP_UVS")
            col.template_ID(owner, "image", new="vector_layers.image_new")
            if owner != s and owner.image is None:
                col.label(text=f"Same image as {s.target.name}", icon="INFO")
        if ob is not None and ob.type == "MESH" and ob not in objs:
            r = layout.row()
            r.operator("vector_layers.set_link", text=f"Also paint on {ob.name}",
                       icon="LINKED").set_name = coll.name

        row = layout.row(align=True)
        row.prop(s, "live", toggle=True, icon="REC" if s.live else "PAUSE")
        row.operator("vector_layers.update", icon="FILE_REFRESH")
        row.operator("vector_layers.bake", icon="RENDER_STILL")
        if s.last_info:
            layout.label(text=s.last_info)

        row = layout.row()
        row.template_list("VL_UL_layers", "", s, "rows", s, "active_index", rows=6)
        side = row.column(align=True)
        side.operator("vector_layers.add_curve", text="", icon="CURVE_BEZCURVE")
        side.operator("vector_layers.add_text", text="", icon="FONT_DATA")
        side.operator("vector_layers.place_on_surface", text="", icon="PIVOT_CURSOR")
        side.separator()
        side.operator("vector_layers.group_new", text="", icon="NEWFOLDER")
        side.operator("vector_layers.group_ungroup", text="", icon="FOLDER_REDIRECT")
        side.separator()
        side.operator("vector_layers.layer_move", text="", icon="TRIA_UP").direction = "UP"
        side.operator("vector_layers.layer_move", text="", icon="TRIA_DOWN").direction = "DOWN"


class VL_PT_layer(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nakan Vector Texturing"
    bl_label = "Layer"
    bl_parent_id = "VL_PT_main"

    @classmethod
    def poll(cls, context):
        return active_layer(context) is not None

    def draw(self, context):
        obj = active_layer(context)
        p = obj.vl
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.prop(p, "opacity", slider=True)

        box = layout.box()
        box.prop(p, "use_fill")
        if p.use_fill:
            col = box.column()
            col.prop(p, "fill_type", text="Type")
            if p.fill_type == "SOLID":
                col.prop(p, "fill_color", text="Color")
            else:
                col.prop(p, "fill_color", text="Start")
                col.prop(p, "fill_color2", text="End")
                if p.fill_type == "LINEAR":
                    col.prop(p, "gradient_angle")
                col.operator("vector_layers.swap_gradient", icon="ARROW_LEFTRIGHT")

        box = layout.box()
        box.prop(p, "use_stroke")
        if p.use_stroke:
            col = box.column()
            col.prop(p, "stroke_fill_type", text="Type")
            if p.stroke_fill_type == "SOLID":
                col.prop(p, "stroke_color", text="Color")
            else:
                col.prop(p, "stroke_color", text="Start")
                col.prop(p, "stroke_color2", text="End")
                if p.stroke_fill_type == "LINEAR":
                    col.prop(p, "stroke_gradient_angle")
            col.prop(p, "stroke_width")
            col.prop(p, "stroke_cap")

        draw_mask(layout, p, active_set(context))

        if obj.type == "FONT":
            layout.prop(obj.data, "body", text="Text")


class VL_PT_group(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nakan Vector Texturing"
    bl_label = "Group"
    bl_parent_id = "VL_PT_main"

    @classmethod
    def poll(cls, context):
        return active_group(context) is not None

    def draw_header(self, context):
        self.layout.label(text="", icon="FILE_FOLDER")

    def draw(self, context):
        g = active_group(context)
        gp = g.vl_group
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(g, "name")
        layout.prop(gp, "opacity", slider=True)
        draw_mask(layout, gp, active_set(context))
        n = len([o for o in g.all_objects if o.type in {"CURVE", "FONT"}])
        layout.label(text=f"{n} layers inside", icon="INFO")


class _StrokeSubPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nakan Vector Texturing"
    bl_parent_id = "VL_PT_layer"
    bl_options = {"DEFAULT_CLOSED"}
    toggle = ""

    @classmethod
    def poll(cls, context):
        obj = active_layer(context)
        return obj is not None and obj.vl.use_stroke

    def draw_header(self, context):
        self.layout.prop(active_layer(context).vl, self.toggle, text="")

    def draw(self, context):
        p = active_layer(context).vl
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = True
        layout.active = getattr(p, self.toggle)
        self.draw_props(layout, p)


class VL_PT_stroke_trim(_StrokeSubPanel, bpy.types.Panel):
    bl_label = "Trim Path"
    toggle = "use_trim"

    def draw_props(self, layout, p):
        col = layout.column(align=True)
        col.prop(p, "trim_start", slider=True)
        col.prop(p, "trim_end", slider=True)
        layout.prop(p, "trim_offset")
        layout.prop(p, "trim_mode", text="Multiple Paths")
        layout.label(text="Keyframe Start/End to draw the line on", icon="INFO")


class VL_PT_stroke_dashes(_StrokeSubPanel, bpy.types.Panel):
    bl_label = "Dashes"
    toggle = "use_dashes"

    def draw_props(self, layout, p):
        layout.prop(p, "dash")
        layout.prop(p, "gap")
        layout.prop(p, "dash_offset")
        if p.stroke_cap == "BUTT":
            layout.label(text="Tip: Round cap = dots (dash 0)", icon="INFO")


class VL_PT_stroke_taper(_StrokeSubPanel, bpy.types.Panel):
    bl_label = "Taper"
    toggle = "use_taper"

    def draw_props(self, layout, p):
        col = layout.column(align=True)
        col.prop(p, "taper_start_length")
        col.prop(p, "taper_start_width")
        col.prop(p, "taper_start_ease")
        col = layout.column(align=True)
        col.prop(p, "taper_end_length")
        col.prop(p, "taper_end_width")
        col.prop(p, "taper_end_ease")


class VL_PT_stroke_wave(_StrokeSubPanel, bpy.types.Panel):
    bl_label = "Wave"
    toggle = "use_wave"

    def draw_props(self, layout, p):
        layout.prop(p, "wave_shape")
        layout.prop(p, "wave_amount")
        layout.prop(p, "wave_length")
        layout.prop(p, "wave_phase")


class VL_MT_modifier_add(bpy.types.Menu):
    bl_label = "Add Modifier"

    def draw(self, context):
        for ident, name, _d, icon, _n in MODIFIER_TYPES:
            self.layout.operator("vector_layers.modifier_add", text=name, icon=icon).type = ident


class VL_PT_layer_modifiers(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nakan Vector Texturing"
    bl_label = "Modifiers"
    bl_parent_id = "VL_PT_layer"

    @classmethod
    def poll(cls, context):
        return active_layer(context) is not None

    def draw(self, context):
        p = active_layer(context).vl
        layout = self.layout
        layout.menu("VL_MT_modifier_add", icon="ADD")
        icons = dict((i[0], i[3]) for i in MODIFIER_TYPES)
        for i, m in enumerate(p.modifiers):
            box = layout.box()
            row = box.row(align=True)
            row.prop(m, "show_expanded", text="", emboss=False,
                     icon="DOWNARROW_HLT" if m.show_expanded else "RIGHTARROW")
            row.label(text="", icon=icons.get(m.type, "MODIFIER"))
            row.prop(m, "name", text="")
            row.prop(m, "enabled", text="", icon="HIDE_OFF" if m.enabled else "HIDE_ON", emboss=False)
            sub = row.row(align=True)
            op = sub.operator("vector_layers.modifier_move", text="", icon="TRIA_UP")
            op.index, op.direction = i, "UP"
            op = sub.operator("vector_layers.modifier_move", text="", icon="TRIA_DOWN")
            op.index, op.direction = i, "DOWN"
            row.operator("vector_layers.modifier_remove", text="", icon="X", emboss=False).index = i
            if m.show_expanded:
                col = box.column()
                col.use_property_split = True
                col.use_property_decorate = True
                col.active = m.enabled
                if m.type == "BLUR":
                    col.prop(m, "radius")
                elif m.type == "ROUGHEN":
                    col.prop(m, "rough_inside")
                    col.prop(m, "rough_outside")
                    col.prop(m, "rough_softness")
                    col.separator()
                    col.prop(m, "noise_size")
                    col.prop(m, "noise_detail")
                    col.prop(m, "noise_roughness")
                    col.prop(m, "noise_seed")
                    col.prop(m, "noise_evolution")
                    col.prop(m, "use_boil")
                    sub = col.column()
                    sub.active = m.use_boil
                    sub.prop(m, "boil_hold")
                    rb = box.column()
                    rb.active = m.enabled
                    if m.noise_ramp is not None and m.noise_ramp.use_color_ramp:
                        row = rb.row()
                        row.label(text="Noise Ramp")
                        if m.noise_ramp.users > 1:
                            op = row.operator("vector_layers.modifier_ramp_fix", text="",
                                              icon="DUPLICATE")
                            op.index, op.single = i, True
                        rb.template_color_ramp(m.noise_ramp, "color_ramp", expand=True)
                    else:
                        rb.operator("vector_layers.modifier_ramp_fix", icon="ADD").index = i


class VL_PT_layer_projection(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nakan Vector Texturing"
    bl_label = "Projection"
    bl_parent_id = "VL_PT_layer"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        p = active_layer(context).vl
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(p, "projection")
        layout.prop(p, "depth")


class VL_PT_options(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nakan Vector Texturing"
    bl_label = "Options"
    bl_parent_id = "VL_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.scene.vl_active_set is not None

    def draw(self, context):
        s = context.scene.vl_active_set.vl_set
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(s, "margin")
        row = layout.row(align=True)
        row.prop(s, "parent_layers")
        row.operator("vector_layers.parent_all", text="", icon="LINKED")
        row = layout.row(heading="Background")
        row.prop(s, "keep_background", text="")
        sub = row.row()
        sub.active = s.keep_background
        sub.prop(s, "background_color", text="")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

classes = (
    VL_ModifierProps, VL_GroupProps, VL_RowProps, VL_LayerProps, VL_TargetProps, VL_SetProps,
    VL_OT_group_new, VL_OT_group_ungroup,
    VL_OT_set_link, VL_OT_set_unlink, VL_OT_set_make_main,
    VL_OT_set_new, VL_OT_image_new, VL_OT_update, VL_OT_bake, VL_OT_parent_all, VL_OT_layer_move,
    VL_OT_add_curve, VL_OT_add_text, VL_OT_swap_gradient,
    VL_OT_modifier_add, VL_OT_modifier_ramp_fix, VL_OT_modifier_remove, VL_OT_modifier_move, VL_OT_place_on_surface,
    VL_MT_modifier_add,
    VL_UL_layers, VL_PT_main, VL_PT_layer, VL_PT_group,
    VL_PT_stroke_trim, VL_PT_stroke_dashes, VL_PT_stroke_taper, VL_PT_stroke_wave,
    VL_PT_layer_modifiers, VL_PT_layer_projection, VL_PT_options,
)


def _old_bundle_loaded():
    """Earlier builds of Nakan Texturing Tools shipped Vector Layers inside it."""
    import sys
    return any(k.endswith(".vector_layers") and k.startswith("nakan_texturing_tools")
               for k in sys.modules) and hasattr(bpy.types, "VL_PT_layer")


def register():
    if _old_bundle_loaded():
        raise RuntimeError(
            "Nakan Vector Texturing: an older Nakan Texturing Tools that already includes Vector "
            "Layers is enabled. Install the latest Nakan Texturing Tools (without Vector Layers) or disable it first.")
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.vl = PointerProperty(type=VL_LayerProps)
    bpy.types.Collection.vl_set = PointerProperty(type=VL_SetProps)
    bpy.types.Collection.vl_group = PointerProperty(type=VL_GroupProps)
    bpy.types.Scene.vl_active_set = PointerProperty(
        name="Vector Set", type=bpy.types.Collection,
        description="Collection whose curves and texts are the vector layers",
    )
    bpy.types.Scene.vl_pin_set = BoolProperty(
        name="Pin Set", default=False,
        description="Keep showing this set; when off, the panel follows the object you click",
    )
    for lst, fn in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
        (bpy.app.handlers.frame_change_post, _on_frame),
        (bpy.app.handlers.load_post, _on_load),
        (bpy.app.handlers.save_pre, _on_save),
    ):
        # drop stale copies left by an add-on reload, so nothing runs twice
        for h in list(lst):
            if getattr(h, "__name__", "") == fn.__name__ and \
                    getattr(h, "__module__", "") == fn.__module__:
                lst.remove(h)
        lst.append(fn)
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=0.5, persistent=True)
    tag_all_later()


def tag_all_later():
    def _f():
        try:
            tag_all()
        except Exception:
            pass
        return None
    bpy.app.timers.register(_f, first_interval=0.3)


def unregister():
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    for lst, fn in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
        (bpy.app.handlers.frame_change_post, _on_frame),
        (bpy.app.handlers.load_post, _on_load),
        (bpy.app.handlers.save_pre, _on_save),
    ):
        for h in list(lst):
            if h is fn or (getattr(h, "__name__", "") == fn.__name__ and
                           getattr(h, "__module__", "") == fn.__module__):
                lst.remove(h)
    del bpy.types.Scene.vl_pin_set
    del bpy.types.Scene.vl_active_set
    del bpy.types.Collection.vl_group
    del bpy.types.Collection.vl_set
    del bpy.types.Object.vl
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    bridge.clear_cache()
