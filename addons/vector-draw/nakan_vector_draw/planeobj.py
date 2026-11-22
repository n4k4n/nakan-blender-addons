"""Plane Object and saved planes.

Plane Object: an empty that holds the drawing plane (plane = Object). Move and rotate it like any object, at any
angle; the grid, the pen and every tool follow it. Buttons put it on the 3D cursor, back on the world origin,
or on the plane of a selected shape.

Saved planes: named copies of the drawing plane (location + rotation), kept in the .blend. Shapes drawn on the
same plane can be combined in the Shape Builder, so a saved plane is a way to come back and keep drawing on it.
"""

import bpy
from bpy.props import FloatVectorProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector

NAME = "Drawing Plane"


def _st(context):
    return context.scene.nkdraw


def plane_matrix(context):
    """the drawing plane in use, as a clean matrix (X, Y, normal, origin)"""
    from . import cplane
    return cplane.current_matrix(context)


def _redraw():
    from . import grid
    grid.redraw()
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except (AttributeError, RuntimeError):
        pass


def ensure_object(context):
    """the Plane Object (made if missing), placed on the drawing plane in use"""
    st = _st(context)
    ob = st.plane_object
    if ob is not None and ob.name in context.scene.objects:
        return ob, False
    m = plane_matrix(context)
    ob = bpy.data.objects.new(NAME, None)
    ob.empty_display_type = 'ARROWS'
    ob.empty_display_size = 1.0
    ob.show_in_front = True
    ob.hide_render = True
    ob.matrix_world = m
    context.scene.collection.objects.link(ob)
    st.plane_object = ob
    return ob, True


def set_plane(context, m):
    """Make `m` the drawing plane, through the Plane Object when there is one in use."""
    from . import cplane
    cplane.write_matrix(context, m)
    _redraw()


def _clean(m):
    """location + rotation only (no scale / shear)"""
    loc, rot, _sca = m.decompose()
    return Matrix.LocRotScale(loc, rot, Vector((1.0, 1.0, 1.0)))


# ------------------------------------------------------------------------------------- operators
class NKDRAW_OT_plane_object(bpy.types.Operator):
    """Use a Plane Object for the drawing plane: an empty you can move and rotate at any angle. The grid and
    every tool follow it. Click again to select it"""
    bl_idname = "nkdraw.plane_object"
    bl_label = "Plane Object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        st = _st(context)
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        ob, _new = ensure_object(context)
        st.cplane_offset = (0.0, 0.0, 0.0)
        st.cplane = 'OBJECT'
        ob.hide_set(False)
        for o in context.selected_objects:
            o.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        _redraw()
        return {'FINISHED'}


class NKDRAW_OT_plane_place(bpy.types.Operator):
    """Put the drawing plane on the 3D cursor, back on the world origin, or on the plane of the active shape"""
    bl_idname = "nkdraw.plane_place"
    bl_label = "Place Drawing Plane"
    bl_options = {'REGISTER', 'UNDO'}

    where: bpy.props.EnumProperty(items=[
        ('CURSOR', "3D Cursor", "Location and rotation of the 3D cursor"),
        ('WORLD', "World Origin", "Back to the world origin, lying flat (Top)"),
        ('SELECTED', "From Shape", "The plane the active shape was drawn on (its local X / Y)"),
    ])

    @classmethod
    def description(cls, context, props):
        return {'CURSOR': "Move the drawing plane onto the 3D cursor (location and rotation)",
                'WORLD': "Move the drawing plane back to the world origin, lying flat",
                'SELECTED': "Use the plane of the active shape: draw on it again, or line up new shapes with it "
                            "for the Shape Builder"}[props.where]

    def execute(self, context):
        if self.where == 'CURSOR':
            m = _clean(context.scene.cursor.matrix)
        elif self.where == 'WORLD':
            m = Matrix.Identity(4)
        else:
            ob = context.active_object
            if ob is None or ob.type not in {'CURVE', 'FONT', 'EMPTY'}:
                self.report({'WARNING'}, "Select a shape first")
                return {'CANCELLED'}
            m = _clean(ob.matrix_world)
        st = _st(context)
        if st.plane_object is None and self.where == 'WORLD':
            from . import cplane
            cplane.set_world(context, 'TOP')
            _redraw()
            return {'FINISHED'}
        set_plane(context, m)
        return {'FINISHED'}


class NKDRAW_OT_plane_save(bpy.types.Operator):
    """Save the drawing plane as it is now (location and rotation), to come back to it later"""
    bl_idname = "nkdraw.plane_save"
    bl_label = "Save Drawing Plane"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(name="Name", default="")

    def invoke(self, context, event):
        self.name = "Plane %d" % (len(_st(context).saved_planes) + 1)
        return context.window_manager.invoke_props_dialog(self, width=220)

    def execute(self, context):
        st = _st(context)
        it = st.saved_planes.add()
        it.name = self.name or "Plane %d" % len(st.saved_planes)
        it.matrix = [v for row in plane_matrix(context) for v in row]
        st.saved_index = len(st.saved_planes) - 1
        return {'FINISHED'}


class NKDRAW_OT_plane_load(bpy.types.Operator):
    """Go back to the selected saved plane"""
    bl_idname = "nkdraw.plane_load"
    bl_label = "Use Saved Plane"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return len(_st(context).saved_planes) > 0

    def execute(self, context):
        st = _st(context)
        i = self.index if self.index >= 0 else st.saved_index
        if not 0 <= i < len(st.saved_planes):
            return {'CANCELLED'}
        v = list(st.saved_planes[i].matrix)
        m = Matrix([v[0:4], v[4:8], v[8:12], v[12:16]])
        set_plane(context, m)
        return {'FINISHED'}


class NKDRAW_OT_plane_update(bpy.types.Operator):
    """Overwrite the selected saved plane with the drawing plane as it is now"""
    bl_idname = "nkdraw.plane_update"
    bl_label = "Update Saved Plane"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(_st(context).saved_planes) > 0

    def execute(self, context):
        st = _st(context)
        if 0 <= st.saved_index < len(st.saved_planes):
            st.saved_planes[st.saved_index].matrix = [v for row in plane_matrix(context) for v in row]
        return {'FINISHED'}


class NKDRAW_OT_plane_remove(bpy.types.Operator):
    """Delete the selected saved plane"""
    bl_idname = "nkdraw.plane_remove"
    bl_label = "Delete Saved Plane"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(_st(context).saved_planes) > 0

    def execute(self, context):
        st = _st(context)
        st.saved_planes.remove(st.saved_index)
        st.saved_index = max(0, min(st.saved_index, len(st.saved_planes) - 1))
        return {'FINISHED'}


class NKDRAW_UL_planes(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='MESH_PLANE')
        row.operator(NKDRAW_OT_plane_load.bl_idname, text="", icon='CHECKMARK').index = index


def draw_panel(lay, context):
    st = _st(context)
    ob = st.plane_object
    col = lay.column(align=True)
    row = col.row(align=True)
    row.scale_y = 1.2
    on = st.cplane == 'OBJECT' and ob is not None
    row.operator(NKDRAW_OT_plane_object.bl_idname, text="Select Plane Object" if on else "Plane Object",
                 icon='EMPTY_ARROWS', depress=on)
    row = col.row(align=True)
    row.operator(NKDRAW_OT_plane_place.bl_idname, text="3D Cursor", icon='PIVOT_CURSOR').where = 'CURSOR'
    row.operator(NKDRAW_OT_plane_place.bl_idname, text="World Origin", icon='WORLD').where = 'WORLD'
    row.operator(NKDRAW_OT_plane_place.bl_idname, text="From Shape", icon='OUTLINER_OB_CURVE').where = 'SELECTED'
    col = lay.column(align=True)
    col.label(text="Saved planes")
    row = col.row()
    row.template_list("NKDRAW_UL_planes", "", st, "saved_planes", st, "saved_index", rows=3)
    side = row.column(align=True)
    side.operator(NKDRAW_OT_plane_save.bl_idname, text="", icon='ADD')
    side.operator(NKDRAW_OT_plane_remove.bl_idname, text="", icon='REMOVE')
    side.separator()
    side.operator(NKDRAW_OT_plane_update.bl_idname, text="", icon='FILE_REFRESH')


# ----------------------------------------------------------------------------------- registration
class NKDRAW_SavedPlane(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", default="Plane")
    matrix: FloatVectorProperty(size=16, default=(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))


CLASSES = (NKDRAW_OT_plane_object, NKDRAW_OT_plane_place, NKDRAW_OT_plane_save, NKDRAW_OT_plane_load,
           NKDRAW_OT_plane_update, NKDRAW_OT_plane_remove, NKDRAW_UL_planes)


def register_types():
    bpy.utils.register_class(NKDRAW_SavedPlane)


def unregister_types():
    bpy.utils.unregister_class(NKDRAW_SavedPlane)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
