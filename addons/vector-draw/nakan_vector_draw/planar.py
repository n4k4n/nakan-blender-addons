"""Vector Mode keys and text on the CPlane.

- Vector Mode (Object Mode): G moves on the CPlane, R rotates around its normal, S scales in its
  plane (Blender's own transforms, locked to the CPlane: X / Y / Ctrl snapping work as usual);
  P starts the pen. The keys are active only while Vector Mode is on.
- Text: add a text object lying on the CPlane; Text -> Curves makes editable Bezier curves for the pen,
  Shape Builder and Extrude, keeping the text hidden; edit the text again and Update Curves rewrites the
  same curve object (its Extrude / Thickness solid stays linked).
"""
import bpy
from bpy.props import EnumProperty, PointerProperty
from mathutils import Matrix

from . import common


def _prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


# ---------------------------------------------------------------------------- G / R / S on the CPlane
class NKDRAW_OT_plane_transform(bpy.types.Operator):
    """Move / rotate / scale the selection on the CPlane (Blender's transform, locked to the CPlane)"""
    bl_idname = "nkdraw.plane_transform"
    bl_label = "Transform on CPlane"

    mode: EnumProperty(items=[('MOVE', "Move", ""), ('ROTATE', "Rotate", ""), ('SCALE', "Scale", "")])

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def invoke(self, context, event):
        st = getattr(context.scene, "nkdraw", None)
        po = getattr(st, "plane_object", None)
        if po is not None and po in context.selected_objects:
            # the Plane Object itself: Blender's free transforms, any angle
            op = {'MOVE': bpy.ops.transform.translate, 'ROTATE': bpy.ops.transform.rotate,
                  'SCALE': bpy.ops.transform.resize}[self.mode]
            op('INVOKE_DEFAULT')
            return {'FINISHED'}
        _o, R = common.cplane_frame(context.scene)
        M = Matrix(R)
        kw = dict(orient_matrix=M, orient_matrix_type='GLOBAL')
        if self.mode == 'MOVE':
            bpy.ops.transform.translate('INVOKE_DEFAULT', constraint_axis=(True, True, False), **kw)
        elif self.mode == 'ROTATE':
            bpy.ops.transform.rotate('INVOKE_DEFAULT', orient_axis='Z', constraint_axis=(False, False, True), **kw)
        else:
            bpy.ops.transform.resize('INVOKE_DEFAULT', constraint_axis=(True, True, False), **kw)
        return {'FINISHED'}


class NKDRAW_OT_plane_duplicate(bpy.types.Operator):
    """Duplicate the selection (curves bring their Extrude / Thickness solids) and move the copy on the
    CPlane (Esc leaves it in place)"""
    bl_idname = "nkdraw.plane_duplicate"
    bl_label = "Duplicate on CPlane"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def invoke(self, context, event):
        from mathutils import Matrix as _M
        from . import copytool
        objs = copytool.copy_set(list(context.selected_objects))
        active = context.view_layer.objects.active
        copies = copytool.make_copies(objs, _M.Identity(4))
        pair = dict(zip([o.name for o in objs], copies))
        for o in context.selected_objects:
            o.select_set(False)
        for c in copytool.roots(copies):
            c.select_set(True)
        if active is not None and active.name in pair:
            new_active = pair[active.name]
            while new_active.parent is not None and new_active.parent in copies:
                new_active = new_active.parent
            context.view_layer.objects.active = new_active
        _o, R = common.cplane_frame(context.scene)
        bpy.ops.transform.translate('INVOKE_DEFAULT', constraint_axis=(True, True, False),
                                    orient_matrix=Matrix(R), orient_matrix_type='GLOBAL')
        return {'FINISHED'}


_keymaps = []


def mode_on():
    p = _prefs()
    return bool(p is not None and p.vector_mode)


def sync_keymaps():
    on = mode_on()
    for _km, kmi in _keymaps:
        try:
            kmi.active = on
        except ReferenceError:
            pass


def _add_keymaps():
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.new(name="Object Mode", space_type='EMPTY')
    for key, mode in (('G', 'MOVE'), ('R', 'ROTATE'), ('S', 'SCALE')):
        kmi = km.keymap_items.new(NKDRAW_OT_plane_transform.bl_idname, key, 'PRESS')
        kmi.properties.mode = mode
        _keymaps.append((km, kmi))
    kmi = km.keymap_items.new("nkdraw.pen", 'P', 'PRESS')
    _keymaps.append((km, kmi))
    kmi = km.keymap_items.new(NKDRAW_OT_plane_duplicate.bl_idname, 'D', 'PRESS', shift=True)
    _keymaps.append((km, kmi))
    sync_keymaps()


# ------------------------------------------------------------------------------------- text
class NKDRAW_OT_add_text(bpy.types.Operator):
    """Add a text object lying on the CPlane, at its origin (Tab to type)"""
    bl_idname = "nkdraw.add_text"
    bl_label = "Add Text on CPlane"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        o, R = common.cplane_frame(context.scene)
        cu = bpy.data.curves.new("Text", 'FONT')
        cu.body = "Text"
        cu.align_x = 'CENTER'
        cu.align_y = 'CENTER'
        ob = bpy.data.objects.new("Text", cu)
        ob.matrix_world = Matrix.Translation(o) @ R.to_4x4()
        (context.collection or context.scene.collection).objects.link(ob)
        for x in context.selected_objects:
            x.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        return {'FINISHED'}


class NKDRAW_OT_to_cplane(bpy.types.Operator):
    """Lay the selected objects on the CPlane: their rotation becomes the CPlane's and their origin goes
    onto the plane (scale kept)"""
    bl_idname = "nkdraw.to_cplane"
    bl_label = "Lay on CPlane"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and bool(context.selected_objects)

    def execute(self, context):
        o, R = common.cplane_frame(context.scene)
        n = R.col[2]
        for ob in context.selected_objects:
            mw = ob.matrix_world
            loc = mw.translation
            loc = loc - n * (loc - o).dot(n)
            sca = mw.to_scale()
            ob.matrix_world = Matrix.LocRotScale(loc, R.to_quaternion(), sca)
        return {'FINISHED'}


def convert_text(context, text_ob):
    """Convert with Blender's own Convert (keeping the original) and return the new curve object."""
    view_layer = context.view_layer
    for x in context.selected_objects:
        x.select_set(False)
    hidden = text_ob.hide_get()
    if hidden:
        text_ob.hide_set(False)
    text_ob.select_set(True)
    view_layer.objects.active = text_ob
    before = set(bpy.data.objects)
    shown = [(m, m.show_viewport) for m in text_ob.modifiers]
    for m, _v in shown:                          # convert the flat text, not its projection on a surface
        m.show_viewport = False
    try:
        bpy.ops.object.convert(target='CURVE', keep_original=True)
    finally:
        for m, v in shown:
            m.show_viewport = v
    new = [o for o in bpy.data.objects if o not in before]
    for o in new:
        for m in list(o.modifiers):              # a copied projection would act on the curves twice
            o.modifiers.remove(m)
    if hidden:
        text_ob.hide_set(True)
    return new[0] if new else None


class NKDRAW_OT_text_to_curves(bpy.types.Operator):
    """Text -> Bezier curves for the pen, Shape Builder and Extrude. The text is kept, hidden: edit it
    again and press Update Curves (it rewrites the same curve object, so its Extrude stays linked;
    point edits made on the curves are replaced)"""
    bl_idname = "nkdraw.text_to_curves"
    bl_label = "Text → Curves"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(o.type == 'FONT' for o in context.selected_objects)

    def execute(self, context):
        texts = [o for o in context.selected_objects if o.type == 'FONT']
        made = []
        for t in texts:
            target = t.nkpf_text_curves
            if target is not None and target.name not in context.view_layer.objects:
                target = None
            new = convert_text(context, t)
            if new is None:
                continue
            for s in new.data.splines:              # the pen and Shape Builder use plain Bezier splines
                s.use_smooth = True
            new.data.dimensions = '3D'               # like the pen's own curves (no fill)
            if target is not None:
                # update: the old curve object keeps its place in the scene, its solid and its name;
                # only its curve data is replaced
                old = target.data
                new_data = new.data
                mats = [m for m in old.materials]
                target.data = new_data
                new_data.materials.clear()
                for m in mats:
                    new_data.materials.append(m)
                target.matrix_world = t.matrix_world.copy()
                new.data = old
                bpy.data.objects.remove(new)
                if old.users == 0:
                    bpy.data.curves.remove(old)
                new_data.name = target.name
                target.hide_set(False)
                made.append(target)
            else:
                new.name = t.name + " Curves"
                new.data.name = new.name
                t.nkpf_text_curves = new
                made.append(new)
            from . import surface
            if surface.get_mod(t) is not None:   # the text was on a surface: its curves go there too
                surface.wrap(made[-1], context.scene.nkpf_surface)
            t.hide_set(True)
            t.hide_render = True
        for x in context.selected_objects:
            x.select_set(False)
        for o in made:
            o.select_set(True)
        if made:
            context.view_layer.objects.active = made[-1]
        self.report({'INFO'}, "%d text(s) -> curves" % len(made))
        return {'FINISHED'} if made else {'CANCELLED'}


def _text_of(context, curve_ob):
    for o in context.scene.objects:
        if o.type == 'FONT' and o.nkpf_text_curves == curve_ob:
            return o
    return None


class NKDRAW_OT_edit_text(bpy.types.Operator):
    """Show the text these curves were made from and select it, to change what it says
    (then press Update Curves)"""
    bl_idname = "nkdraw.edit_text"
    bl_label = "Edit Text Again"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return context.mode == 'OBJECT' and ob is not None and ob.type == 'CURVE' and _text_of(context, ob) is not None

    def execute(self, context):
        cur = context.active_object
        t = _text_of(context, cur)
        t.hide_set(False)
        t.hide_render = False
        cur.hide_set(True)
        for x in context.selected_objects:
            x.select_set(False)
        t.select_set(True)
        context.view_layer.objects.active = t
        return {'FINISHED'}


def draw_text_ui(layout, context):
    ob = context.active_object
    col = layout.column(align=True)
    row = col.row(align=True)
    row.operator(NKDRAW_OT_add_text.bl_idname, text="Add Text", icon='OUTLINER_OB_FONT')
    row.operator(NKDRAW_OT_to_cplane.bl_idname, text="Lay on CPlane", icon='ORIENTATION_CURSOR')
    if ob is not None and ob.type == 'FONT':
        has = ob.nkpf_text_curves is not None and ob.nkpf_text_curves.name in context.view_layer.objects
        col.operator(NKDRAW_OT_text_to_curves.bl_idname, icon='CURVE_BEZCURVE',
                     text="Update Curves" if has else "Text → Curves")
    elif ob is not None and ob.type == 'CURVE' and _text_of(context, ob) is not None:
        col.operator(NKDRAW_OT_edit_text.bl_idname, icon='GREASEPENCIL')


class NKDRAW_PT_text(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Vector Draw"
    bl_parent_id = "NKDRAW_PT_main"
    bl_label = "Text"
    bl_order = 52

    def draw(self, context):
        draw_text_ui(self.layout, context)


classes = (NKDRAW_OT_plane_transform, NKDRAW_OT_plane_duplicate, NKDRAW_OT_add_text, NKDRAW_OT_to_cplane, NKDRAW_OT_text_to_curves,
           NKDRAW_OT_edit_text, NKDRAW_PT_text)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.nkpf_text_curves = PointerProperty(
        type=bpy.types.Object, name="Curves", description="Curve object made from this text")
    _add_keymaps()


def unregister():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    del bpy.types.Object.nkpf_text_curves
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
