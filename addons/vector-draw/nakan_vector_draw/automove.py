"""Vector Mode: with it on, selecting a curve in Object Mode starts the add-on's Move right away
(pick the base point, then the destination). Esc leaves the Move; the curve stays selected and
the Move only starts again when the selection changes."""
import bpy

_last = [None]
INTERVAL = 0.12


def _sel_key(context):
    try:
        sel = context.selected_objects
    except AttributeError:
        return None
    return tuple(sorted(o.name for o in sel))


def _view3d(win):
    best = None
    for area in win.screen.areas:
        if area.type == 'VIEW_3D':
            if best is None or area.width * area.height > best.width * best.height:
                best = area
    if best is None:
        return None, None
    region = next((r for r in best.regions if r.type == 'WINDOW'), None)
    return best, region


def _tick():
    wm = bpy.context.window_manager
    scene = bpy.context.scene
    if scene is None or not mode_on():
        _last[0] = None
        return None                          # timer stops; switching the mode on starts it again
    if not hasattr(bpy.ops, "nkdraw") or not hasattr(bpy.ops.nkdraw, "move"):
        return 1.0
    for win in wm.windows:
        try:
            busy = len(win.modal_operators) > 0
        except AttributeError:
            busy = False
        if busy:
            _last[0] = _sel_key(bpy.context)   # whatever happens inside a modal is not a new selection
            return INTERVAL
    key = _sel_key(bpy.context)
    if key == _last[0]:
        return INTERVAL
    _last[0] = key
    if not key or bpy.context.mode != 'OBJECT':
        return INTERVAL
    ob = bpy.context.view_layer.objects.active
    if ob is None or ob.type != 'CURVE' or not ob.select_get():
        return INTERVAL
    for win in wm.windows:
        area, region = _view3d(win)
        if area is None or region is None:
            continue
        try:
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.nkdraw.move('INVOKE_DEFAULT')
        except (RuntimeError, TypeError):
            pass
        break
    return INTERVAL


def prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def mode_on():
    p = prefs()
    return bool(p is not None and p.vector_mode and getattr(p, "move_on_select", False))


def start():
    _last[0] = _sel_key(bpy.context)          # the current selection does not start it
    if mode_on() and not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=INTERVAL, persistent=True)


def toggle(self, context):
    start()


class NKDRAW_PT_mode(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Vector Draw"
    bl_parent_id = "NKDRAW_PT_main"
    bl_label = "Vector Mode"
    bl_order = 49

    def draw_header(self, context):
        p = prefs()
        if p is not None:
            self.layout.prop(p, "vector_mode", text="")

    def draw(self, context):
        lay = self.layout
        p = prefs()
        col = lay.column()
        col.scale_y = 0.8
        col.label(text="G / R / S on the CPlane · P = Pen")
        if p is not None:
            lay.prop(p, "move_on_select")


def register():
    bpy.app.timers.register(start, first_interval=0.5)   # prefs are ready a moment after register


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
