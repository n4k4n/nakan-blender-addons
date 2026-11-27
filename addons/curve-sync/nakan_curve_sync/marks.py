"""Sync Curves: channels marked in a scene so they can be shown in the VSE."""

import bpy

from .core import action_fcurves, _split_path


AXIS_LETTERS = {
    "location": "XYZ", "delta_location": "XYZ",
    "rotation_euler": "XYZ", "delta_rotation_euler": "XYZ",
    "scale": "XYZ", "delta_scale": "XYZ",
    "rotation_quaternion": "WXYZ", "delta_rotation_quaternion": "WXYZ",
    "rotation_axis_angle": "WXYZ",
    "color": "RGBA", "default_value": "XYZW",
}


def redraw_all(self=None, context=None):
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {"GRAPH_EDITOR", "SEQUENCE_EDITOR", "DOPESHEET_EDITOR"}:
                area.tag_redraw()


class NKCS_Curve(bpy.types.PropertyGroup):
    id: bpy.props.PointerProperty(type=bpy.types.ID)
    data_path: bpy.props.StringProperty()
    array_index: bpy.props.IntProperty()
    show: bpy.props.BoolProperty(
        name="Show", default=True, update=redraw_all,
        description="Show this curve on scene strips in the Video Sequencer")
    color: bpy.props.FloatVectorProperty(
        name="Color", subtype="COLOR", size=3, min=0.0, max=1.0,
        default=(1.0, 0.6, 0.2), update=redraw_all)


def curve_label(item):
    idb = item.id
    if idb is None:
        return "(missing) " + item.data_path
    path = item.data_path
    owner_path, prop = _split_path(path)
    parts = []
    if path.startswith('pose.bones["'):
        parts.append(path[len('pose.bones["'):].split('"]', 1)[0])
    elif path.startswith('key_blocks["'):
        parts.append(path[len('key_blocks["'):].split('"]', 1)[0])
    if prop is None:
        name = path[path.rfind('["') + 2:-2]
    else:
        name = prop.replace("_", " ").title()
        try:
            owner = idb.path_resolve(owner_path) if owner_path else idb
            name = owner.bl_rna.properties[prop].name or name
            alen = getattr(owner.bl_rna.properties[prop], "array_length", 0) or 0
        except Exception:
            alen = 0
        if alen > 1:
            letters = AXIS_LETTERS.get(prop)
            if letters and item.array_index < len(letters):
                name = f"{letters[item.array_index]} {name}"
            else:
                name = f"{name} [{item.array_index}]"
    parts.append(name)
    return f"{idb.name}: " + " › ".join(parts)


def find_mark(scene, idb, path, index):
    for i, m in enumerate(scene.nkcs_curves):
        if m.id == idb and m.data_path == path and m.array_index == index:
            return i
    return -1


def _id_collections():
    d = bpy.data
    for name in ("objects", "meshes", "shape_keys", "materials", "cameras", "lights",
                 "curves", "armatures", "worlds", "node_groups", "grease_pencils",
                 "scenes", "textures", "particles", "hair_curves", "lattices",
                 "metaballs", "speakers", "volumes", "pointclouds", "linestyles",
                 "masks", "movieclips", "cache_files"):
        coll = getattr(d, name, None)
        if coll is not None:
            yield coll


def fcurve_owners(fc):
    """Datablocks animated by this F-Curve (the IDs that use its action/slot)."""
    act = fc.id_data
    if not isinstance(act, bpy.types.Action):
        return []
    slot = None
    for layer in getattr(act, "layers", []):
        for strip in layer.strips:
            for cb in strip.channelbags:
                for f in cb.fcurves:
                    if f == fc:
                        slot = cb.slot
                        break
    owners = []
    if slot is not None:
        try:
            owners = list(slot.users())
        except Exception:
            owners = []
    if not owners:
        for coll in _id_collections():
            for idb in coll:
                ad = getattr(idb, "animation_data", None)
                if ad is None:
                    continue
                if ad.action == act:
                    owners.append(idb)
                    continue
                for t in ad.nla_tracks:
                    if any(s.action == act for s in t.strips):
                        owners.append(idb)
                        break
    return owners


def selected_fcurves(context):
    fcs = getattr(context, "selected_visible_fcurves", None)
    if fcs is None:
        fcs = getattr(context, "selected_editable_fcurves", None)
    return [fc for fc in (fcs or []) if isinstance(fc.id_data, bpy.types.Action)]


def _channel_color(fc):
    c = tuple(fc.color)
    if max(c) < 0.05:
        return (1.0, 0.6, 0.2)
    return tuple(min(1.0, v * 0.6 + 0.4) for v in c)


class NKCS_OT_mark(bpy.types.Operator):
    bl_idname = "nkcs.mark_curves"
    bl_label = "Mark as Sync Curves"
    bl_description = ("Mark the selected channels so they can be shown on this scene's "
                      "strips in the Video Sequencer")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(selected_fcurves(context))

    def execute(self, context):
        scene = context.scene
        added = 0
        for fc in selected_fcurves(context):
            for idb in fcurve_owners(fc):
                if find_mark(scene, idb, fc.data_path, fc.array_index) >= 0:
                    continue
                m = scene.nkcs_curves.add()
                m.id = idb
                m.data_path = fc.data_path
                m.array_index = fc.array_index
                m.color = _channel_color(fc)
                added += 1
        scene.nkcs_curves_index = len(scene.nkcs_curves) - 1
        self.report({"INFO"}, f"{added} sync curve(s) added to '{scene.name}'")
        redraw_all()
        return {"FINISHED"}


class NKCS_OT_unmark(bpy.types.Operator):
    bl_idname = "nkcs.unmark_curves"
    bl_label = "Unmark Sync Curves"
    bl_description = "Remove the selected channels from this scene's Sync Curves"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(selected_fcurves(context))

    def execute(self, context):
        scene = context.scene
        removed = 0
        for fc in selected_fcurves(context):
            for idb in fcurve_owners(fc):
                i = find_mark(scene, idb, fc.data_path, fc.array_index)
                if i >= 0:
                    scene.nkcs_curves.remove(i)
                    removed += 1
        scene.nkcs_curves_index = min(scene.nkcs_curves_index, len(scene.nkcs_curves) - 1)
        self.report({"INFO"}, f"{removed} sync curve(s) removed")
        redraw_all()
        return {"FINISHED"}


class NKCS_OT_remove(bpy.types.Operator):
    bl_idname = "nkcs.remove_curve"
    bl_label = "Remove Sync Curve"
    bl_description = "Remove this curve from the scene's Sync Curves"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    scene_name: bpy.props.StringProperty()
    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = bpy.data.scenes.get(self.scene_name) or context.scene
        if 0 <= self.index < len(scene.nkcs_curves):
            scene.nkcs_curves.remove(self.index)
            scene.nkcs_curves_index = min(scene.nkcs_curves_index, len(scene.nkcs_curves) - 1)
        redraw_all()
        return {"FINISHED"}


class NKCS_UL_curves(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "show", text="", emboss=False,
                 icon="HIDE_OFF" if item.show else "HIDE_ON")
        sub = row.row(align=True)
        sub.scale_x = 0.3
        sub.prop(item, "color", text="")
        row.label(text=curve_label(item), icon="NONE" if item.id else "ERROR")
        op = row.operator("nkcs.remove_curve", text="", icon="X", emboss=False)
        op.scene_name = data.name
        op.index = index


def draw_curve_list(layout, scene):
    layout.template_list("NKCS_UL_curves", "", scene, "nkcs_curves",
                         scene, "nkcs_curves_index", rows=3)


def _channel_menu(self, context):
    self.layout.separator()
    self.layout.operator("nkcs.mark_curves", icon="IPO_EASE_IN_OUT")
    self.layout.operator("nkcs.unmark_curves")


classes = (NKCS_Curve, NKCS_OT_mark, NKCS_OT_unmark, NKCS_OT_remove, NKCS_UL_curves)
_menus = ("GRAPH_MT_channel_context_menu", "DOPESHEET_MT_channel_context_menu",
          "GRAPH_MT_channel", "DOPESHEET_MT_channel")


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nkcs_curves = bpy.props.CollectionProperty(type=NKCS_Curve)
    bpy.types.Scene.nkcs_curves_index = bpy.props.IntProperty(default=-1)
    for name in _menus:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            menu.append(_channel_menu)


def unregister():
    for name in _menus:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            try:
                menu.remove(_channel_menu)
            except Exception:
                pass
    del bpy.types.Scene.nkcs_curves_index
    del bpy.types.Scene.nkcs_curves
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
