bl_info = {
    "name": "Scene Switcher Menu",
    "description": "Scene menu at the mouse position, with groups as submenus",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "category": "Interface",
}

import bpy
from bpy.types import Operator, Menu, AddonPreferences, PropertyGroup
from bpy.props import StringProperty, BoolProperty, EnumProperty, CollectionProperty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def visible_scenes():
    # Scenes whose name starts with "." are internal/hidden (e.g. SceneBoost)
    return [s for s in bpy.data.scenes if not s.name.startswith(".")]


class NKSCENE_GroupItem(PropertyGroup):
    # Group registry (allows empty groups). Uses the default "name".
    pass


def group_items():
    """{name: item} for every registered group (may be empty)."""
    d = {}
    for s in bpy.data.scenes:
        for it in s.nk_groups:
            if it.name and it.name not in d:
                d[it.name] = it
    return d


def group_names():
    names = {s.nk_scene_group for s in visible_scenes() if s.nk_scene_group}
    names |= set(group_items())
    return sorted(names)


def register_group(name):
    # Stored in every scene so the group doesn't vanish if a scene is deleted
    for s in bpy.data.scenes:
        if not any(it.name == name for it in s.nk_groups):
            s.nk_groups.add().name = name


def unregister_group(name):
    for s in bpy.data.scenes:
        for i in reversed(range(len(s.nk_groups))):
            if s.nk_groups[i].name == name:
                s.nk_groups.remove(i)


def draw_scene_item(layout, context, sc):
    icon = 'CHECKMARK' if sc == context.window.scene else 'SCENE_DATA'
    if use_scene_submenus():
        layout.context_pointer_set("nk_scene_ref", sc)
        layout.menu("NKSCENE_MT_scene", text=sc.name, icon=icon)
    else:
        layout.operator("nk_scene.switch_to", text=sc.name, icon=icon).name = sc.name


def use_scene_submenus():
    try:
        return bpy.context.preferences.addons[__name__].preferences.scene_submenus
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class NKSCENE_OT_switch_to(Operator):
    bl_idname = "nk_scene.switch_to"
    bl_label = "Switch scene"
    bl_description = "Click: switch to this scene.\nShift/Cmd/Ctrl+Click: move this scene to a group"
    bl_options = {'INTERNAL'}
    name: StringProperty()

    def invoke(self, context, event):
        if event.ctrl or event.oskey or event.shift:
            context.window_manager.nk_scene_target = self.name
            bpy.ops.wm.call_menu('INVOKE_DEFAULT', name="NKSCENE_MT_move")
            return {'FINISHED'}
        return self.execute(context)

    def execute(self, context):
        sc = bpy.data.scenes.get(self.name)
        if sc:
            context.window.scene = sc
        return {'FINISHED'}


class NKSCENE_OT_set_group(Operator):
    bl_idname = "nk_scene.set_group"
    bl_label = "Move scene to group"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    scene: StringProperty()
    group: StringProperty()

    def execute(self, context):
        sc = bpy.data.scenes.get(self.scene)
        if not sc:
            return {'CANCELLED'}
        sc.nk_scene_group = self.group.strip()
        if sc.nk_scene_group:
            register_group(sc.nk_scene_group)
        return {'FINISHED'}


class NKSCENE_OT_new_group(Operator):
    bl_idname = "nk_scene.new_group"
    bl_label = "New group"
    bl_description = "Creates a new group (empty, or already with the chosen scene)"
    bl_options = {'REGISTER', 'UNDO'}
    scene: StringProperty(options={'HIDDEN'})
    name: StringProperty(name="Name", default="Group")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        name = self.name.strip()
        if not name:
            self.report({'WARNING'}, "Empty name")
            return {'CANCELLED'}
        register_group(name)
        sc = bpy.data.scenes.get(self.scene) if self.scene else None
        if sc:
            sc.nk_scene_group = name
        return {'FINISHED'}


class NKSCENE_OT_rename_group(Operator):
    bl_idname = "nk_scene.rename_group"
    bl_label = "Rename group"
    bl_options = {'REGISTER', 'UNDO'}
    old_name: StringProperty(options={'HIDDEN'})
    new_name: StringProperty(name="New name")

    def invoke(self, context, event):
        self.new_name = self.old_name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        new = self.new_name.strip()
        if not new:
            self.report({'WARNING'}, "Empty name")
            return {'CANCELLED'}
        for sc in bpy.data.scenes:
            if sc.nk_scene_group == self.old_name:
                sc.nk_scene_group = new
            for it in sc.nk_groups:
                if it.name == self.old_name:
                    it.name = new
        register_group(new)
        return {'FINISHED'}


class NKSCENE_OT_ungroup(Operator):
    bl_idname = "nk_scene.ungroup"
    bl_label = "Delete group"
    bl_description = "Deletes the group and releases its scenes (the scenes are not deleted)"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    group: StringProperty()

    def execute(self, context):
        for sc in bpy.data.scenes:
            if sc.nk_scene_group == self.group:
                sc.nk_scene_group = ""
        unregister_group(self.group)
        return {'FINISHED'}


class NKSCENE_OT_new_scene_in_group(Operator):
    bl_idname = "nk_scene.new_scene_in_group"
    bl_label = "New scene in group"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    group: StringProperty()

    def execute(self, context):
        bpy.ops.scene.new(type='NEW')
        context.window.scene.nk_scene_group = self.group
        register_group(self.group)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------

class NKSCENE_MT_group(Menu):
    bl_idname = "NKSCENE_MT_group"
    bl_label = "Group"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        item = getattr(context, "nk_group_item", None)
        ref = getattr(context, "nk_group_ref", None)
        if item is not None:
            group = item.name
        elif ref is not None:
            group = ref.nk_scene_group
        else:
            layout.label(text="Group unavailable")
            return

        members = [sc for sc in visible_scenes() if sc.nk_scene_group == group]
        for sc in members:
            draw_scene_item(layout, context, sc)
        if not members:
            layout.label(text="(empty group)")

        layout.separator()
        layout.operator("nk_scene.new_scene_in_group", text="New scene here", icon='ADD').group = group
        layout.operator("nk_scene.rename_group", text="Rename group...").old_name = group
        layout.operator("nk_scene.ungroup", text="Delete group", icon='X').group = group


class NKSCENE_MT_scene(Menu):
    bl_idname = "NKSCENE_MT_scene"
    bl_label = "Scene"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        sc = getattr(context, "nk_scene_ref", None)
        if sc is None:
            layout.label(text="Scene unavailable")
            return
        layout.operator("nk_scene.switch_to", text="Open scene", icon='FORWARD').name = sc.name
        layout.separator()

        cur = sc.nk_scene_group
        groups = group_names()
        for g in groups:
            icon = 'CHECKMARK' if g == cur else 'FILE_FOLDER'
            op = layout.operator("nk_scene.set_group", text=g, icon=icon)
            op.scene = sc.name
            op.group = g
        if groups:
            layout.separator()
        layout.operator("nk_scene.new_group", text="New group...", icon='NEWFOLDER').scene = sc.name
        if cur:
            op = layout.operator("nk_scene.set_group", text="Remove from group", icon='REMOVE')
            op.scene = sc.name
            op.group = ""


class NKSCENE_MT_move(Menu):
    bl_idname = "NKSCENE_MT_move"
    bl_label = "Move to group"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        sc = bpy.data.scenes.get(context.window_manager.nk_scene_target)
        if sc is None:
            layout.label(text="Scene unavailable")
            return
        layout.label(text=sc.name, icon='SCENE_DATA')
        layout.separator()
        cur = sc.nk_scene_group
        groups = group_names()
        for g in groups:
            icon = 'CHECKMARK' if g == cur else 'FILE_FOLDER'
            op = layout.operator("nk_scene.set_group", text=g, icon=icon)
            op.scene = sc.name
            op.group = g
        if groups:
            layout.separator()
        layout.operator("nk_scene.new_group", text="New group...", icon='NEWFOLDER').scene = sc.name
        if cur:
            op = layout.operator("nk_scene.set_group", text="Remove from group", icon='REMOVE')
            op.scene = sc.name
            op.group = ""


class NKSCENE_MT_switcher(Menu):
    bl_idname = "NKSCENE_MT_switcher"
    bl_label = "Scenes"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        scenes = visible_scenes()
        active = context.window.scene
        groups = group_names()

        # Groups: each one opens a side submenu
        items = group_items()
        for g in groups:
            members = [s for s in scenes if s.nk_scene_group == g]
            icon = 'CHECKMARK' if active in members else 'FILE_FOLDER'
            if g in items:
                layout.context_pointer_set("nk_group_item", items[g])
            else:
                layout.context_pointer_set("nk_group_ref", members[0])
            layout.menu(NKSCENE_MT_group.bl_idname, text=f"{g}  ({len(members)})", icon=icon)

        ungrouped = [s for s in scenes if not s.nk_scene_group]
        if groups and ungrouped:
            layout.separator()
        for sc in ungrouped:
            draw_scene_item(layout, context, sc)

        layout.separator()
        layout.operator_menu_enum("scene.new", "type", text="New Scene", icon='ADD')
        layout.operator("nk_scene.new_group", text="New Group...", icon='NEWFOLDER').scene = ""


# ---------------------------------------------------------------------------
# Shortcut (registered in every mode so it isn't "swallowed")
# ---------------------------------------------------------------------------

KEYMAPS = [
    ("Window", 'EMPTY'),
    ("Screen", 'EMPTY'),
    ("3D View", 'VIEW_3D'),
    ("Object Mode", 'EMPTY'),
    ("Mesh", 'EMPTY'),
    ("Curve", 'EMPTY'),
    ("Curves", 'EMPTY'),
    ("Armature", 'EMPTY'),
    ("Pose", 'EMPTY'),
    ("Lattice", 'EMPTY'),
    ("Metaball", 'EMPTY'),
    ("Font", 'EMPTY'),
    ("Sculpt", 'EMPTY'),
    ("Sculpt Curves", 'EMPTY'),
    ("Weight Paint", 'EMPTY'),
    ("Vertex Paint", 'EMPTY'),
    ("Image Paint", 'EMPTY'),
    ("Particle", 'EMPTY'),
    ("Grease Pencil", 'EMPTY'),
    ("Grease Pencil Edit Mode", 'EMPTY'),
    ("Grease Pencil Paint Mode", 'EMPTY'),
    ("Grease Pencil Sculpt Mode", 'EMPTY'),
    ("Grease Pencil Weight Paint", 'EMPTY'),
    ("Node Editor", 'NODE_EDITOR'),
    ("Graph Editor", 'GRAPH_EDITOR'),
    ("Dopesheet", 'DOPESHEET_EDITOR'),
    ("Image", 'IMAGE_EDITOR'),
    ("UV Editor", 'EMPTY'),
    ("Sequencer", 'SEQUENCE_EDITOR'),
    ("Outliner", 'OUTLINER'),
]

KEY_ITEMS = [(k, k, "") for k in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"] + \
            [(f"F{i}", f"F{i}", "") for i in range(1, 13)]

addon_keymaps = []


def register_keymaps():
    kc = bpy.context.window_manager.keyconfigs.addon
    if not kc:
        return
    try:
        prefs = bpy.context.preferences.addons[__name__].preferences
        key, ctrl, alt, shift = prefs.key, prefs.ctrl, prefs.alt, prefs.shift
    except Exception:
        key, ctrl, alt, shift = 'S', True, True, True

    for name, space in KEYMAPS:
        km = kc.keymaps.new(name=name, space_type=space)
        kmi = km.keymap_items.new("wm.call_menu", key, 'PRESS', ctrl=ctrl, alt=alt, shift=shift)
        kmi.properties.name = NKSCENE_MT_switcher.bl_idname
        addon_keymaps.append((km, kmi))


def unregister_keymaps():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


def _update_keymap(self, context):
    unregister_keymaps()
    register_keymaps()


class NKSCENE_Prefs(AddonPreferences):
    bl_idname = __name__

    key: EnumProperty(name="Key", items=KEY_ITEMS, default='S', update=_update_keymap)
    ctrl: BoolProperty(name="Ctrl", default=True, update=_update_keymap)
    alt: BoolProperty(name="Alt", default=True, update=_update_keymap)
    shift: BoolProperty(name="Shift", default=True, update=_update_keymap)
    scene_submenus: BoolProperty(
        name="Each scene as a submenu",
        description="On: hovering a scene opens Open scene + group options. "
                    "Off: clicking switches scene directly (Shift/Cmd+Click for groups)",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        layout.prop(self, "scene_submenus")
        layout.separator()
        layout.label(text="Scene menu shortcut (works in every mode):")
        row = layout.row(align=True)
        row.prop(self, "ctrl", toggle=True)
        row.prop(self, "alt", toggle=True)
        row.prop(self, "shift", toggle=True)
        row.prop(self, "key", text="")


# ---------------------------------------------------------------------------

classes = (
    NKSCENE_GroupItem,
    NKSCENE_OT_switch_to,
    NKSCENE_OT_set_group,
    NKSCENE_OT_new_group,
    NKSCENE_OT_rename_group,
    NKSCENE_OT_ungroup,
    NKSCENE_OT_new_scene_in_group,
    NKSCENE_MT_group,
    NKSCENE_MT_scene,
    NKSCENE_MT_move,
    NKSCENE_MT_switcher,
    NKSCENE_Prefs,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nk_scene_group = StringProperty(name="Scene group", default="")
    bpy.types.Scene.nk_groups = CollectionProperty(type=NKSCENE_GroupItem)
    bpy.types.WindowManager.nk_scene_target = StringProperty(default="")
    register_keymaps()


def unregister():
    unregister_keymaps()
    del bpy.types.Scene.nk_scene_group
    del bpy.types.Scene.nk_groups
    del bpy.types.WindowManager.nk_scene_target
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
