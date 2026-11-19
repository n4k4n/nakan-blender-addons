bl_info = {
    "name": "Nakan GP Tools",
    "author": "Nakan + Claude",
    "version": (1, 0, 0),
    "blender": (4, 3, 0),
    "location": "View3D > Sidebar (N) > Nakan GP",
    "description": "Grease Pencil tools: flip canvas, selection by layer and Layer Mirror",
    "category": "Grease Pencil",
}

import re
import bpy
import numpy as np
from mathutils import Matrix


# ================================================================ PROPERTIES

class GPM_LayerItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()


class GPM_Settings(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Mirror enabled", default=True,
        description="Turns automatic mirroring on/off for this object")
    mirror_eraser: bpy.props.BoolProperty(
        name="Mirror eraser", default=True,
        description="Erasing on one side erases the matching point on the other side")
    axis: bpy.props.EnumProperty(
        name="Axis", default='X',
        items=[('X', "X", "Mirror on the X axis"),
               ('Y', "Y", "Mirror on the Y axis"),
               ('Z', "Z", "Mirror on the Z axis")])
    mirror_object: bpy.props.PointerProperty(
        name="Center", type=bpy.types.Object,
        description="Object (e.g. an Empty) that sets the mirror center and rotation. "
                    "Empty = origin of the Grease Pencil object")
    layers: bpy.props.CollectionProperty(type=GPM_LayerItem)


# ================================================================ LAYER MIRROR (core)

ROLE = "gpm_role"   # CURVE: 0 = new, 1 = original, 2 = mirrored copy, 3 = ignored (already existed)
PID = "gpm_pid"     # POINT: ID shared by the original point and its mirrored point
LEGACY = "gpm_done"
TIMER_INTERVAL = 0.1

ATTR_INFO = {
    'FLOAT': ('value', 1, np.float32),
    'INT': ('value', 1, np.int32),
    'INT8': ('value', 1, np.int32),
    'BOOLEAN': ('value', 1, bool),
    'FLOAT_VECTOR': ('vector', 3, np.float32),
    'FLOAT2': ('vector', 2, np.float32),
    'INT32_2D': ('value', 2, np.int32),
    'FLOAT_COLOR': ('color', 4, np.float32),
    'BYTE_COLOR': ('color', 4, np.float32),
    'QUATERNION': ('value', 4, np.float32),
    'FLOAT4X4': ('value', 16, np.float32),
}
MIRROR_VECTOR_ATTRS = {"position", "handle_left", "handle_right"}


def layer_enabled(settings, name):
    return any(it.name == name for it in settings.layers)


# ---------------------------------------------------------------- helpers

def get_attr_array(attr, n):
    key, comps, dt = ATTR_INFO[attr.data_type]
    arr = np.empty(n * comps, dtype=dt)
    if n:
        attr.data.foreach_get(key, arr)
    return arr.reshape(n, comps) if comps > 1 else arr


def set_attr_array(attr, arr):
    key, comps, dt = ATTR_INFO[attr.data_type]
    attr.data.foreach_set(key, np.ascontiguousarray(arr, dtype=dt).ravel())


def ensure_attrs(drawing):
    legacy = LEGACY in drawing.attributes
    if ROLE not in drawing.attributes:
        drawing.attributes.new(ROLE, 'INT8', 'CURVE')
        if legacy and len(drawing.strokes):
            # files from v1: treat everything that already existed as "ignored"
            drawing.attributes[ROLE].data.foreach_set(
                "value", np.full(len(drawing.strokes), 3, dtype=np.int32))
    if PID not in drawing.attributes:
        drawing.attributes.new(PID, 'INT', 'POINT')


def mark_existing_ignored(drawing):
    ensure_attrs(drawing)
    n = len(drawing.strokes)
    if not n:
        return
    role = get_attr_array(drawing.attributes[ROLE], n)
    role[role == 0] = 3
    set_attr_array(drawing.attributes[ROLE], role)


def _loc_rot(mw):
    """Location + orthonormal rotation (ignores scale, negative scale included)."""
    m3 = mw.to_3x3()
    cols = [m3.col[i].normalized() for i in range(3)]
    rot = Matrix((cols[0], cols[1], cols[2])).transposed()
    if rot.determinant() < 0:
        rot.col[0] = -rot.col[0]
    out = rot.to_4x4()
    out.translation = mw.translation
    return out


def mirror_matrix(obj, layer, settings):
    """Matrix that mirrors points in layer space."""
    s = [1.0, 1.0, 1.0]
    s["XYZ".index(settings.axis)] = -1.0
    S = Matrix.Diagonal((*s, 1.0))
    if settings.mirror_object:
        center = _loc_rot(settings.mirror_object.matrix_world)
        to_world = obj.matrix_world @ layer.matrix_local
        return to_world.inverted() @ center @ S @ center.inverted() @ to_world
    # no Empty: mirror around the object's own origin/axes
    lm = layer.matrix_local
    return lm.inverted() @ S @ lm


def split_runs(indices_mask):
    """Start/end (exclusive) indices of continuous runs of True."""
    m = np.concatenate([[False], indices_mask, [False]]).astype(np.int8)
    d = np.diff(m)
    return np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]


# ---------------------------------------------------------------- core

def sync_drawing(obj, layer, drawing, settings):
    n_c = len(drawing.strokes)
    if n_c == 0:
        return False
    ensure_attrs(drawing)
    attrs = drawing.attributes

    offsets = np.empty(n_c + 1, dtype=np.int32)
    drawing.curve_offsets.foreach_get("value", offsets)
    n_p = int(offsets[-1])
    sizes = np.diff(offsets)
    role = get_attr_array(attrs[ROLE], n_c)
    pid = get_attr_array(attrs[PID], n_p)
    point_curve = np.repeat(np.arange(n_c), sizes)
    prole = role[point_curve]

    is_o, is_c, is_new = prole == 1, prole == 2, prole == 0
    new_curves = role == 0

    keep = np.ones(n_p, dtype=bool)
    opac = get_attr_array(attrs["opacity"], n_p) if "opacity" in attrs else None
    opac_changed = False

    if settings.mirror_eraser and (is_o.any() or is_c.any()):
        # point erased on one side -> erase its pair on the other side
        keep[is_o] = np.isin(pid[is_o], pid[is_c])
        keep[is_c] = np.isin(pid[is_c], pid[is_o])
        # soft eraser -> minimum opacity between the pairs
        if opac is not None:
            paired = is_o | is_c
            if paired.any():
                m = np.full(int(pid[paired].max()) + 1, np.inf, dtype=np.float32)
                np.minimum.at(m, pid[paired], opac[paired])
                new_op = m[pid[paired]]
                opac_changed = not np.array_equal(new_op, opac[paired])
                opac[paired] = new_op

    if keep.all() and not new_curves.any() and not opac_changed:
        return False

    # IDs for the points of the new strokes
    if is_new.any():
        start = int(max(pid.max(initial=0), 0)) + 1
        pid[is_new] = np.arange(start, start + int(is_new.sum()), dtype=np.int32)

    cyclic = get_attr_array(attrs["cyclic"], n_c) if "cyclic" in attrs else None

    # build the plan: (source curve, point indices, is copy?, cyclic)
    plan_c, plan_pts, plan_mirror, plan_cyc, plan_role = [], [], [], [], []

    def emit(c, pts, mirrored, cyc, r):
        plan_c.append(c)
        plan_pts.append(pts)
        plan_mirror.append(mirrored)
        plan_cyc.append(cyc)
        plan_role.append(r)

    for c in range(n_c):
        pts = np.arange(offsets[c], offsets[c + 1])
        cyc = bool(cyclic[c]) if cyclic is not None else False
        if role[c] == 0:
            emit(c, pts, False, cyc, 1)
            emit(c, pts, True, cyc, 2)
            continue
        k = keep[pts]
        if k.all():
            emit(c, pts, False, cyc, int(role[c]))
            continue
        starts, ends = split_runs(k)
        for a, b in zip(starts, ends):
            emit(c, pts[a:b], False, False, int(role[c]))

    M = np.array(mirror_matrix(obj, layer, settings), dtype=np.float32)
    P = np.concatenate(plan_pts) if plan_pts else np.empty(0, dtype=np.int64)
    new_sizes = [len(p) for p in plan_pts]
    mirror_mask = np.repeat(np.array(plan_mirror, dtype=bool), new_sizes) if plan_pts else np.empty(0, bool)
    C = np.array(plan_c, dtype=np.int64)

    # read every attribute before rebuilding
    snapshot = []
    for attr in attrs:
        if attr.data_type not in ATTR_INFO or attr.domain not in {'POINT', 'CURVE'}:
            continue
        if attr.domain == 'POINT':
            if attr.name == PID:
                full = pid
            elif attr.name == "opacity" and opac is not None:
                full = opac
            else:
                full = get_attr_array(attr, n_p)
            out = full[P].copy()
            if attr.name in MIRROR_VECTOR_ATTRS and mirror_mask.any():
                out[mirror_mask] = out[mirror_mask] @ M[:3, :3].T + M[:3, 3]
        else:
            if attr.name == ROLE:
                out = np.array(plan_role, dtype=np.int32)
            elif attr.name == "cyclic":
                out = np.array(plan_cyc, dtype=bool)
            else:
                out = get_attr_array(attr, n_c)[C].copy()
        snapshot.append((attr.name, out))

    drawing.remove_strokes()
    if new_sizes:
        drawing.add_strokes(new_sizes)
        for name, out in snapshot:
            a = drawing.attributes.get(name)
            if a is not None:
                set_attr_array(a, out)
    drawing.tag_positions_changed()
    return True


# ---------------------------------------------------------------- timer

_state = {"was_running": False, "fingerprints": {}}


def paint_is_running():
    for win in bpy.context.window_manager.windows:
        ops = getattr(win, "modal_operators", None)
        if ops and len(ops):
            return True
    return False


def fingerprint(d):
    n = len(d.strokes)
    return (n, d.curve_offsets[n].value if n else 0)


def gpm_timer():
    try:
        obj = bpy.context.view_layer.objects.active
    except Exception:
        return TIMER_INTERVAL
    if (obj is None or obj.type != 'GREASEPENCIL'
            or obj.mode != 'PAINT_GREASE_PENCIL'):
        return TIMER_INTERVAL
    settings = obj.data.gpm_settings
    if not settings.enabled or not len(settings.layers):
        return TIMER_INTERVAL

    running = paint_is_running()
    just_finished = _state["was_running"] and not running
    _state["was_running"] = running
    if running:
        return TIMER_INTERVAL

    changed = False
    for layer in obj.data.layers:
        if not layer_enabled(settings, layer.name) or layer.lock:
            continue
        frame = layer.current_frame()
        if frame is None or frame.drawing is None:
            continue
        d = frame.drawing
        key = (obj.name, layer.name, frame.frame_number)
        fp = fingerprint(d)
        if not just_finished and _state["fingerprints"].get(key) == fp:
            continue
        changed |= sync_drawing(obj, layer, d, settings)
        _state["fingerprints"][key] = fingerprint(d)

    if changed:
        obj.data.update_tag()
        screen = bpy.context.screen
        if screen:
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    return TIMER_INTERVAL


# ================================================================ SELECTION BY LAYER

def _drawing_at(layer, frame):
    """Drawing visible on the layer at the frame (last keyframe <= frame)."""
    best = None
    for f in layer.frames:
        if f.frame_number <= frame and (best is None or f.frame_number > best.frame_number):
            best = f
    return best.drawing if best else None


def _sel_attr(drawing):
    return drawing.attributes.get(".selection")


def _selected_count(drawing):
    """Count the real selection from the '.selection' attribute.

    point.select is not reliable: without the attribute, Blender treats everything
    as selected (e.g. locked layers).
    """
    attr = _sel_attr(drawing)
    if attr is None:
        return 0
    n = len(attr.data)
    if n == 0:
        return 0
    if attr.data_type == 'BOOLEAN':
        arr = np.empty(n, dtype=bool)
        attr.data.foreach_get("value", arr)
        return int(arr.sum())
    arr = np.empty(n, dtype=np.float32)
    attr.data.foreach_get("value", arr)
    return int((arr > 0).sum())


def _set_selection(drawing, value):
    """Select/deselect everything in a drawing."""
    if len(drawing.strokes) == 0:
        return 0
    attr = _sel_attr(drawing)
    if attr is None:
        # make Blender create the attribute by touching one point
        drawing.strokes[0].points[0].select = value
        attr = _sel_attr(drawing)
    if attr is None:  # slow fallback
        n = 0
        for s in drawing.strokes:
            for p in s.points:
                p.select = value
                n += 1
        return n
    n = len(attr.data)
    if attr.data_type == 'BOOLEAN':
        attr.data.foreach_set("value", np.full(n, value, dtype=bool))
    else:
        attr.data.foreach_set("value", np.full(n, 1.0 if value else 0.0, dtype=np.float32))
    if attr.domain == 'POINT':
        return n
    offs = np.empty(len(drawing.strokes) + 1, dtype=np.int32)
    drawing.curve_offsets.foreach_get("value", offs)
    return int(offs[-1])


def _find_selected_layer(gp, frame):
    """Editable layer with the most selected points."""
    best_layer, best_count = None, 0
    for layer in gp.layers:
        if getattr(layer, "lock", False) or getattr(layer, "hide", False):
            continue
        drw = _drawing_at(layer, frame)
        if not drw:
            continue
        c = _selected_count(drw)
        if c > best_count:
            best_layer, best_count = layer, c
    return best_layer, best_count


def _select_only_layer(context, layer):
    gp = context.object.data
    frame = context.scene.frame_current
    for l in gp.layers:
        drw = _drawing_at(l, frame)
        if drw:
            _set_selection(drw, False)
    drw = _drawing_at(layer, frame)
    n = _set_selection(drw, True) if drw else 0
    gp.update_tag()
    if context.screen:
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    return n


def _gp_poll(context):
    ob = context.object
    return ob is not None and ob.type == 'GREASEPENCIL'


class NAKANGP_OT_reveal_in_layer(bpy.types.Operator):
    """Activate the layer that contains the selected points"""
    bl_idname = "nakan_gp.reveal_in_layer"
    bl_label = "Reveal in Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _gp_poll(context) and context.object.mode == 'EDIT'

    def execute(self, context):
        gp = context.object.data
        layer, count = _find_selected_layer(gp, context.scene.frame_current)
        if layer is None:
            self.report({'WARNING'}, "No selection on an editable layer")
            return {'CANCELLED'}
        gp.layers.active = layer
        self.report({'INFO'}, "Active layer: %s (%d pts)" % (layer.name, count))
        return {'FINISHED'}


class NAKANGP_OT_select_layer_vertices(bpy.types.Operator):
    """Select every point of the active layer (current frame), clearing the rest"""
    bl_idname = "nakan_gp.select_layer_vertices"
    bl_label = "Select All Vertices of Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _gp_poll(context) and context.object.data.layers.active is not None

    def execute(self, context):
        gp = context.object.data
        active = gp.layers.active
        if getattr(active, "lock", False):
            self.report({'WARNING'}, "Layer '%s' is locked" % active.name)
        if getattr(active, "hide", False):
            self.report({'WARNING'}, "Layer '%s' is hidden" % active.name)
        n = _select_only_layer(context, active)
        if n == 0:
            self.report({'WARNING'}, "No points on '%s' in this frame" % active.name)
            return {'CANCELLED'}
        if context.object.mode == 'EDIT':
            self.report({'INFO'}, "%d points selected on '%s'" % (n, active.name))
        else:
            self.report({'INFO'}, "%d points selected on '%s' (enter Edit Mode to see them)"
                        % (n, active.name))
        return {'FINISHED'}


class NAKANGP_OT_select_layer_of_selection(bpy.types.Operator):
    """Select every point of the layer of the selected points (and activate that layer)"""
    bl_idname = "nakan_gp.select_layer_of_selection"
    bl_label = "Select Whole Layer of Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _gp_poll(context) and context.object.mode == 'EDIT'

    def execute(self, context):
        gp = context.object.data
        layer, _ = _find_selected_layer(gp, context.scene.frame_current)
        if layer is None:
            self.report({'WARNING'}, "No selection on an editable layer")
            return {'CANCELLED'}
        gp.layers.active = layer
        n = _select_only_layer(context, layer)
        self.report({'INFO'}, "%d points selected on '%s'" % (n, layer.name))
        return {'FINISHED'}


# ================================================================ FLIP

class NAKANGP_OT_flip_canvas(bpy.types.Operator):
    """Flip the canvas: multiplies the active Grease Pencil's Scale X by -1"""
    bl_idname = "nakan_gp.flip_canvas"
    bl_label = "Flip Canvas"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _gp_poll(context)

    def execute(self, context):
        context.object.scale.x *= -1
        return {'FINISHED'}


class WM_OT_flip_property(bpy.types.Operator):
    """Multiply the property value by -1"""
    bl_idname = "wm.flip_property"
    bl_label = "Flip"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        prop = getattr(context, "button_prop", None)
        return (prop is not None and prop.type in {'FLOAT', 'INT'}
                and not getattr(prop, "is_readonly", False))

    def execute(self, context):
        ptr = getattr(context, "button_pointer", None)
        prop = getattr(context, "button_prop", None)
        if ptr is None or prop is None:
            self.report({'WARNING'}, "No property found")
            return {'CANCELLED'}
        prop_id = prop.identifier

        # find which array component was clicked (X, Y or Z)
        index = -1
        wm = context.window_manager
        clipboard_backup = wm.clipboard
        try:
            wm.clipboard = ""
            bpy.ops.ui.copy_data_path_button(full_path=True)
            match = re.search(r"\[(\d+)\]$", wm.clipboard)
            if match:
                index = int(match.group(1))
        except Exception:
            pass
        finally:
            wm.clipboard = clipboard_backup

        try:
            value = getattr(ptr, prop_id)
        except AttributeError:
            self.report({'WARNING'}, "Could not read the property")
            return {'CANCELLED'}

        try:
            if hasattr(value, "__len__") and not isinstance(value, str):
                values = list(value)
                if 0 <= index < len(values):
                    values[index] = -values[index]
                else:
                    values = [-v for v in values]
                setattr(ptr, prop_id, values)
            else:
                setattr(ptr, prop_id, -value)
        except Exception as e:
            self.report({'WARNING'}, "Could not edit: %s" % e)
            return {'CANCELLED'}
        return {'FINISHED'}


# ================================================================ LAYER MIRROR (operators)

class GPM_OT_toggle_layer(bpy.types.Operator):
    bl_idname = "nakan_gp.mirror_toggle_layer"
    bl_label = "Mirror on layer"
    bl_description = "Turns mirroring on/off for this layer"
    bl_options = {'REGISTER', 'UNDO'}

    layer_name: bpy.props.StringProperty()

    def execute(self, context):
        gp = context.active_object.data
        st = gp.gpm_settings
        for i, it in enumerate(st.layers):
            if it.name == self.layer_name:
                st.layers.remove(i)
                return {'FINISHED'}
        st.layers.add().name = self.layer_name
        # what was already drawn is not mirrored
        layer = gp.layers.get(self.layer_name)
        if layer:
            for fr in layer.frames:
                if fr.drawing:
                    mark_existing_ignored(fr.drawing)
        _state["fingerprints"].clear()
        return {'FINISHED'}


class GPM_OT_add_empty(bpy.types.Operator):
    bl_idname = "nakan_gp.mirror_add_empty"
    bl_label = "Create Center Empty"
    bl_description = "Creates an Empty at the 3D cursor and uses it as the mirror center/rotation"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        emp = bpy.data.objects.new(f"{obj.name}_MirrorCenter", None)
        emp.empty_display_type = 'ARROWS'
        emp.empty_display_size = 0.5
        emp.show_name = True
        emp.location = context.scene.cursor.location
        emp.rotation_euler = obj.matrix_world.to_euler()
        context.collection.objects.link(emp)
        obj.data.gpm_settings.mirror_object = emp
        return {'FINISHED'}


# ================================================================ UI

class NAKANGP_PT_main(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan GP"
    bl_label = "Nakan GP Tools"

    @classmethod
    def poll(cls, context):
        return _gp_poll(context)

    def draw(self, context):
        ob = context.object
        layer = ob.data.layers.active
        self.layout.label(text="Active layer: %s" % (layer.name if layer else "—"),
                          icon='OUTLINER_DATA_GP_LAYER')


class NAKANGP_PT_canvas(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan GP"
    bl_label = "Canvas"
    bl_parent_id = "NAKANGP_PT_main"

    def draw(self, context):
        ob = context.object
        flipped = ob.scale.x < 0
        col = self.layout.column()
        col.operator("nakan_gp.flip_canvas", icon='ARROW_LEFTRIGHT',
                     text="Flip Canvas (flipped)" if flipped else "Flip Canvas",
                     depress=flipped)


class NAKANGP_PT_selection(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan GP"
    bl_label = "Selection by Layer"
    bl_parent_id = "NAKANGP_PT_main"

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("nakan_gp.reveal_in_layer", text="Activate layer of selection",
                     icon='LAYER_ACTIVE')
        col.operator("nakan_gp.select_layer_vertices", text="Select points of active layer",
                     icon='VERTEXSEL')
        col.operator("nakan_gp.select_layer_of_selection", text="Select whole layer of selection",
                     icon='SELECT_EXTEND')
        if context.object.mode != 'EDIT':
            self.layout.label(text="Use in Edit Mode", icon='INFO')


class NAKANGP_PT_mirror(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan GP"
    bl_label = "Layer Mirror"
    bl_parent_id = "NAKANGP_PT_main"

    def draw_header(self, context):
        self.layout.prop(context.object.data.gpm_settings, "enabled", text="")

    def draw(self, context):
        obj = context.object
        st = obj.data.gpm_settings
        col = self.layout.column()
        col.active = st.enabled
        col.prop(st, "mirror_eraser")

        col.separator()
        col.label(text="Mirrored layers:")
        box = col.box()
        for layer in reversed(list(obj.data.layers)):
            on = layer_enabled(st, layer.name)
            op = box.row().operator("nakan_gp.mirror_toggle_layer", text=layer.name, depress=on,
                                    icon='CHECKBOX_HLT' if on else 'CHECKBOX_DEHLT')
            op.layer_name = layer.name

        col.separator()
        col.row().prop(st, "axis", expand=True)
        row = col.row(align=True)
        row.prop(st, "mirror_object", text="Center")
        row.operator("nakan_gp.mirror_add_empty", text="", icon='EMPTY_AXIS')
        if not st.mirror_object:
            col.label(text="Center = object origin", icon='INFO')
        if obj.mode != 'PAINT_GREASE_PENCIL':
            col.label(text="Mirrors in Draw Mode", icon='INFO')


# ---------------------------------------------------------------- context menus

def _flip_menu_draw(self, context):
    prop = getattr(context, "button_prop", None)
    if prop and prop.type in {'FLOAT', 'INT'} and not getattr(prop, "is_readonly", False):
        self.layout.separator()
        self.layout.operator(WM_OT_flip_property.bl_idname, text="Flip", icon='ARROW_LEFTRIGHT')


def _edit_menu_draw(self, context):
    layout = self.layout
    layout.separator()
    layout.operator("nakan_gp.reveal_in_layer", text="Reveal in Layer", icon='LAYER_ACTIVE')
    layout.operator("nakan_gp.select_layer_of_selection", text="Select Whole Layer",
                    icon='SELECT_EXTEND')


def _layer_extra_menu_draw(self, context):
    self.layout.separator()
    self.layout.operator("nakan_gp.select_layer_vertices",
                         text="Select All Vertices of Layer", icon='VERTEXSEL')


MENUS = [
    ("UI_MT_button_context_menu", _flip_menu_draw),
    ("VIEW3D_MT_greasepencil_edit_context_menu", _edit_menu_draw),
    ("GREASE_PENCIL_MT_grease_pencil_add_layer_extra", _layer_extra_menu_draw),
]


# ================================================================ REGISTRATION

classes = (
    GPM_LayerItem, GPM_Settings,
    NAKANGP_OT_reveal_in_layer, NAKANGP_OT_select_layer_vertices,
    NAKANGP_OT_select_layer_of_selection, NAKANGP_OT_flip_canvas, WM_OT_flip_property,
    GPM_OT_toggle_layer, GPM_OT_add_empty,
    NAKANGP_PT_main, NAKANGP_PT_canvas, NAKANGP_PT_selection, NAKANGP_PT_mirror,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.GreasePencil.gpm_settings = bpy.props.PointerProperty(type=GPM_Settings)
    for menu_name, fn in MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu:
            menu.append(fn)
    if not bpy.app.timers.is_registered(gpm_timer):
        bpy.app.timers.register(gpm_timer, first_interval=TIMER_INTERVAL, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(gpm_timer):
        bpy.app.timers.unregister(gpm_timer)
    for menu_name, fn in MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu:
            try:
                menu.remove(fn)
            except Exception:
                pass
    del bpy.types.GreasePencil.gpm_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
