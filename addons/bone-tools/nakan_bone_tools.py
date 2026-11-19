bl_info = {
    "name": "Nakan Bone Tools",
    "description": "Duplicate bone collections with their bones, prefix and rename bones in sequence",
    "version": (1, 0, 0),
    "blender": (4, 1, 0),
    "location": "Properties > Armature > Bone Collections > right-click",
    "category": "Rigging",
}

import bpy
from bpy.types import Operator, Menu
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty

TAG = "_nk_src"


def collection_tree(c):
    out = [c]
    for ch in c.children:
        out += collection_tree(ch)
    return out


SIDE_ITEMS = [
    ('NONE', "None", "No side suffix"),
    ('L', ".L", "Adds .L to all"),
    ('R', ".R", "Adds .R to all"),
    ('AUTO', "Automatic (by position)", "Positive X = .L, negative X = .R, center gets no suffix"),
]
_SIDE_ENDINGS = (".L", ".R", ".l", ".r", "_L", "_R", "_l", "_r", "-L", "-R",
                 ".Left", ".Right", "_Left", "_Right")


def strip_side(name):
    for e in _SIDE_ENDINGS:
        if name.endswith(e) and len(name) > len(e):
            return name[:-len(e)]
    return name


def side_of(bone, mode, eps=1e-4):
    if mode == 'L':
        return ".L"
    if mode == 'R':
        return ".R"
    if mode == 'AUTO':
        x = (bone.head_local.x + bone.tail_local.x) / 2
        if x > eps:
            return ".L"
        if x < -eps:
            return ".R"
    return ""


def armature_poll(context):
    obj = context.object
    return (obj is not None and obj.type == 'ARMATURE'
            and obj.data.collections.active is not None)


def run_duplicate(context):
    try:
        return bpy.ops.armature.duplicate()
    except RuntimeError:
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    region = next(r for r in area.regions if r.type == 'WINDOW')
                    with context.temp_override(window=win, area=area, region=region):
                        return bpy.ops.armature.duplicate()
        raise


# ---------------------------------------------------------------------------
# Duplicate collection
# ---------------------------------------------------------------------------

class NKBC_OT_duplicate(Operator):
    bl_idname = "armature.nk_duplicate_bone_collection"
    bl_label = "Duplicate Collection + Bones"
    bl_description = ("Duplicates the active bone collection, all its sub-collections and every bone "
                      "assigned to them, keeping the hierarchy")
    bl_options = {'REGISTER', 'UNDO'}

    new_name: StringProperty(name="New collection name", default="",
                             description="Empty: Blender uses .001, .002...")
    suffix: StringProperty(name="Suffix (bones and sub-collections)", default="",
                           description="E.g. _R or _copy. Empty: Blender uses .001, .002...")
    replace_prefix: BoolProperty(
        name="Replace prefix in bone names",
        description="Bones whose name starts with the source collection name get the new collection name",
        default=True)

    @classmethod
    def poll(cls, context):
        return armature_poll(context)

    def invoke(self, context, event):
        self.new_name = ""
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "new_name")
        layout.prop(self, "suffix")
        layout.prop(self, "replace_prefix")

    def execute(self, context):
        obj = context.object
        arm = obj.data
        src = arm.collections.active

        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = obj
        orig_mode = obj.mode
        if orig_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        src_tree = collection_tree(src)
        src_names = [c.name for c in src_tree]
        bone_names = set()
        for c in src_tree:
            bone_names |= {b.name for b in c.bones}

        # Temporary visibility: Blender does not duplicate hidden bones
        vis = {c.name: (c.is_visible, c.is_solo) for c in arm.collections_all}
        for c in arm.collections_all:
            c.is_solo = False
            c.is_visible = True

        # Rebuild the collection hierarchy
        mapping = {}

        def make(c, parent):
            if c == src and self.new_name.strip():
                name = self.new_name.strip()
            else:
                name = c.name + self.suffix if self.suffix else c.name
            n = arm.collections.new(name, parent=parent)
            for k in c.keys():
                try:
                    n[k] = c[k]
                except Exception:
                    pass
            mapping[c.name] = n
            for ch in list(c.children):
                make(ch, n)

        make(src, src.parent)
        try:
            mapping[src.name].child_number = src.child_number + 1
        except Exception:
            pass

        new_bone_count = 0
        if bone_names:
            bpy.ops.object.mode_set(mode='EDIT')
            ebs = arm.edit_bones
            hide_state = {}
            for eb in ebs:
                hide_state[eb.name] = eb.hide
                eb.hide = False
                sel = eb.name in bone_names
                eb.select = eb.select_head = eb.select_tail = sel
                if sel:
                    eb[TAG] = eb.name

            run_duplicate(context)

            new_bones = [eb for eb in ebs if TAG in eb and eb.name != eb[TAG]]
            new_bone_count = len(new_bones)
            src_top = src.name
            dst_top = mapping[src.name].name

            for eb in new_bones:
                orig = eb[TAG]
                # Swap collections: source tree -> new ones
                member = {c.name for c in eb.collections}
                for cname in src_names:
                    if cname in member:
                        arm.collections_all[cname].unassign(eb)
                        mapping[cname].assign(eb)
                # Name
                if self.replace_prefix and orig.startswith(src_top) and dst_top != src_top:
                    eb.name = dst_top + orig[len(src_top):]
                elif self.suffix:
                    eb.name = orig + self.suffix
                eb.hide = hide_state.get(orig, False)

            for eb in ebs:
                if TAG in eb:
                    del eb[TAG]
            new_set = set(new_bones)
            for name, h in hide_state.items():
                eb = ebs.get(name)
                if eb is not None and eb not in new_set:
                    eb.hide = h

        bpy.ops.object.mode_set(mode=orig_mode if orig_mode != 'EDIT_ARMATURE' else 'EDIT')

        # Restore visibility; new collections copy the source's
        for c in arm.collections_all:
            if c.name in vis:
                c.is_visible, c.is_solo = vis[c.name]
        for sname, n in mapping.items():
            n.is_visible = vis[sname][0]

        arm.collections.active = mapping[src.name]
        context.view_layer.objects.active = prev_active
        self.report({'INFO'}, f"'{mapping[src.name].name}' created with "
                              f"{len(mapping)} collection(s) and {new_bone_count} bone(s)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Prefix bones
# ---------------------------------------------------------------------------

class NKBC_OT_prefix(Operator):
    bl_idname = "armature.nk_prefix_bones_with_collection"
    bl_label = "Prefix Bones with Collection Name"
    bl_description = "Renames the bones of the active collection, adding the collection name as a prefix"
    bl_options = {'REGISTER', 'UNDO'}

    separator: StringProperty(name="Separator", default="_")
    include_children: BoolProperty(
        name="Include sub-collections",
        description="Bones in sub-collections are renamed too, each one with the name of "
                    "its own collection",
        default=False)
    side: EnumProperty(name="Side suffix", items=SIDE_ITEMS, default='NONE',
                       description="For Symmetrize: Blender mirrors .L to .R")

    @classmethod
    def poll(cls, context):
        return armature_poll(context)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        obj = context.object
        arm = obj.data
        src = arm.collections.active

        orig_mode = obj.mode
        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = obj
        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')

        cols = collection_tree(src) if self.include_children else [src]
        # Bone -> deepest collection in the tree (sub-collection wins)
        target = {}
        for c in cols:
            for b in c.bones:
                target[b.name] = c.name

        count = 0
        for bname, cname in target.items():
            bone = arm.bones[bname]
            prefix = cname + self.separator
            new = bname if bname.startswith(prefix) else prefix + bname
            if self.side != 'NONE':
                new = strip_side(new) + side_of(bone, self.side)
            if new != bname:
                bone.name = new
                count += 1

        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        context.view_layer.objects.active = prev_active
        self.report({'INFO'}, f"{count} bone(s) renamed")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Rename in sequence
# ---------------------------------------------------------------------------

def order_bones(arm, names, mode, axis, reverse):
    names = set(names)
    index = {b.name: i for i, b in enumerate(arm.bones)}  # Outliner order

    if mode == 'CREATION':
        out = sorted(names, key=index.get)
    elif mode == 'POSITION':
        ax = "XYZ".index(axis)
        out = sorted(names, key=lambda n: (arm.bones[n].head_local[ax], index[n]))
    else:  # HIERARCHY: walk each chain from root to tip
        def first_pos(n):
            return index[n]
        roots = [n for n in names
                 if arm.bones[n].parent is None or arm.bones[n].parent.name not in names]
        roots.sort(key=first_pos)
        out = []

        def walk(n):
            out.append(n)
            kids = [c.name for c in arm.bones[n].children if c.name in names]
            for k in sorted(kids, key=first_pos):
                walk(k)

        for r in roots:
            walk(r)
    if reverse:
        out.reverse()
    return out


class NKBC_OT_rename_sequence(Operator):
    bl_idname = "armature.nk_rename_bones_sequence"
    bl_label = "Rename Bones in Sequence"
    bl_description = ("Renames the bones of the active collection to "
                      "CollectionName_001, _002... in the chosen order")
    bl_options = {'REGISTER', 'UNDO'}

    order: EnumProperty(
        name="Order",
        items=[
            ('HIERARCHY', "Hierarchy", "From root to tip of each chain; separate chains in Outliner order"),
            ('POSITION', "Position", "By the bone head position along an axis"),
            ('CREATION', "Outliner Order", "In the order the bones appear in the Outliner"),
        ],
        default='HIERARCHY')
    axis: EnumProperty(name="Axis", items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")], default='Z')
    reverse: BoolProperty(name="Reverse order", default=False)
    separator: StringProperty(name="Separator", default="_")
    start: IntProperty(name="Start at", default=1, min=0)
    digits: IntProperty(name="Digits", default=3, min=1, max=6)
    include_children: BoolProperty(
        name="Include sub-collections",
        description="Each sub-collection is numbered separately, with its own name",
        default=False)
    side: EnumProperty(name="Side suffix", items=SIDE_ITEMS, default='NONE',
                       description="For Symmetrize: Blender mirrors .L to .R. "
                                   "In Automatic mode each side is numbered separately")

    @classmethod
    def poll(cls, context):
        return armature_poll(context)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "order")
        if self.order == 'POSITION':
            layout.prop(self, "axis", expand=True)
        layout.prop(self, "reverse")
        layout.separator()
        row = layout.row(align=True)
        row.prop(self, "separator")
        row.prop(self, "start")
        row.prop(self, "digits")
        layout.prop(self, "include_children")
        layout.prop(self, "side")
        src = context.object.data.collections.active
        ex = {'NONE': "", 'L': ".L", 'R': ".R", 'AUTO': ".L"}[self.side]
        layout.label(text=f"E.g. {src.name}{self.separator}{self.start:0{self.digits}d}{ex}", icon='INFO')

    def execute(self, context):
        obj = context.object
        arm = obj.data
        src = arm.collections.active

        orig_mode = obj.mode
        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = obj
        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')

        cols = collection_tree(src) if self.include_children else [src]
        # Each bone goes to the deepest collection in the tree
        target = {}
        for c in cols:
            for b in c.bones:
                target[b.name] = c.name
        groups = {}
        for bname, cname in target.items():
            groups.setdefault(cname, []).append(bname)

        plan = []  # (current name, new name)
        for cname in [c.name for c in cols]:
            if cname not in groups:
                continue
            # Split by side (in Automatic each side has its own count,
            # so Middle_001.L mirrors Middle_001.R)
            by_side = {}
            for bname in groups[cname]:
                by_side.setdefault(side_of(arm.bones[bname], self.side), []).append(bname)
            for sfx in sorted(by_side):
                ordered = order_bones(arm, by_side[sfx], self.order, self.axis, self.reverse)
                for i, bname in enumerate(ordered):
                    plan.append((bname, f"{cname}{self.separator}{self.start + i:0{self.digits}d}{sfx}"))

        # Two passes to avoid name clashes between the bones themselves
        tmp = []
        for i, (old, new) in enumerate(plan):
            b = arm.bones[old]
            b.name = f"__nk_tmp_{i}"
            tmp.append((b.name, new))
        clashes = 0
        for t, new in tmp:
            b = arm.bones[t]
            b.name = new
            if b.name != new:
                clashes += 1

        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        context.view_layer.objects.active = prev_active
        msg = f"{len(plan)} bone(s) renamed"
        if clashes:
            msg += f" ({clashes} already existed outside the collection and got .001)"
        self.report({'WARNING' if clashes else 'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Deform
# ---------------------------------------------------------------------------

class NKBC_OT_set_deform(Operator):
    bl_idname = "armature.nk_set_deform"
    bl_label = "Bone Deform"
    bl_description = "Turns Deform on or off for every bone in the active collection"
    bl_options = {'REGISTER', 'UNDO'}

    enable: BoolProperty(name="Deform", default=False)
    include_children: BoolProperty(name="Include sub-collections", default=True)

    @classmethod
    def poll(cls, context):
        return armature_poll(context)

    @classmethod
    def description(cls, context, props):
        return ("Turns on" if props.enable else "Turns off") + \
            " Deform for every bone in the active collection (and sub-collections)"

    def execute(self, context):
        obj = context.object
        arm = obj.data
        src = arm.collections.active

        orig_mode = obj.mode
        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = obj
        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')

        cols = collection_tree(src) if self.include_children else [src]
        names = set()
        for c in cols:
            names |= {b.name for b in c.bones}
        changed = 0
        for n in names:
            b = arm.bones[n]
            if b.use_deform != self.enable:
                b.use_deform = self.enable
                changed += 1

        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        context.view_layer.objects.active = prev_active
        state = "on" if self.enable else "off"
        self.report({'INFO'}, f"Deform {state} for {changed} bone(s) ({len(names)} in the collection)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Side suffix
# ---------------------------------------------------------------------------

class NKBC_OT_set_side(Operator):
    bl_idname = "armature.nk_set_side_suffix"
    bl_label = "Side Suffix"
    bl_description = "Changes the side suffix (.L/.R) of the bones in the active collection, keeping the rest of the name"
    bl_options = {'REGISTER', 'UNDO'}

    side: EnumProperty(name="Side", items=SIDE_ITEMS, default='AUTO')
    include_children: BoolProperty(name="Include sub-collections", default=True)

    @classmethod
    def poll(cls, context):
        return armature_poll(context)

    def execute(self, context):
        obj = context.object
        arm = obj.data
        src = arm.collections.active

        orig_mode = obj.mode
        prev_active = context.view_layer.objects.active
        context.view_layer.objects.active = obj
        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')

        cols = collection_tree(src) if self.include_children else [src]
        names = set()
        for c in cols:
            names |= {b.name for b in c.bones}

        count = 0
        for n in sorted(names):
            b = arm.bones[n]
            new = strip_side(n) + side_of(b, self.side)
            if new != n:
                b.name = new
                count += 1

        if orig_mode == 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
        context.view_layer.objects.active = prev_active
        self.report({'INFO'}, f"{count} bone(s) renamed")
        return {'FINISHED'}


class NKBC_MT_side(Menu):
    bl_idname = "NKBC_MT_side"
    bl_label = "Side Suffix"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'EXEC_DEFAULT'
        op = layout.operator(NKBC_OT_set_side.bl_idname, text=".L")
        op.side = 'L'
        op = layout.operator(NKBC_OT_set_side.bl_idname, text=".R")
        op.side = 'R'
        op = layout.operator(NKBC_OT_set_side.bl_idname, text="Automatic (by position)")
        op.side = 'AUTO'
        layout.separator()
        op = layout.operator(NKBC_OT_set_side.bl_idname, text="Remove suffix", icon='X')
        op.side = 'NONE'


# ---------------------------------------------------------------------------

def menu_func(self, context):
    layout = self.layout
    layout.separator()
    layout.operator_context = 'INVOKE_DEFAULT'
    layout.operator(NKBC_OT_duplicate.bl_idname, icon='DUPLICATE')
    layout.operator(NKBC_OT_prefix.bl_idname, icon='SORTALPHA')
    layout.operator(NKBC_OT_rename_sequence.bl_idname, icon='LINENUMBERS_ON')
    layout.menu(NKBC_MT_side.bl_idname, icon='MOD_MIRROR')
    layout.separator()
    layout.operator_context = 'EXEC_DEFAULT'
    layout.operator(NKBC_OT_set_deform.bl_idname, text="Deform Off", icon='MOD_ARMATURE').enable = False
    layout.operator(NKBC_OT_set_deform.bl_idname, text="Deform On", icon='OUTLINER_DATA_ARMATURE').enable = True


MENUS = ("ARMATURE_MT_collection_tree_context_menu", "ARMATURE_MT_collection_context_menu")
classes = (NKBC_OT_duplicate, NKBC_OT_prefix, NKBC_OT_rename_sequence, NKBC_OT_set_deform,
           NKBC_OT_set_side, NKBC_MT_side)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    for m in MENUS:
        menu = getattr(bpy.types, m, None)
        if menu:
            menu.append(menu_func)


def unregister():
    for m in MENUS:
        menu = getattr(bpy.types, m, None)
        if menu:
            try:
                menu.remove(menu_func)
            except Exception:
                pass
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
