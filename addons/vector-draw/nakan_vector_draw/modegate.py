"""Vector Mode as the on/off switch of the whole add-on.

- The mode panel sits at the top of the tab: the switch and the target collection.
- Every other panel is greyed out while the mode is off (the shortcuts are off too, see __init__).
- While the mode is on, the target collection is the active collection, and every new curve / text
  (pen, shapes, Shape Builder, copies, text) is moved into it. Empty field = a collection named
  "Nakan Vector Draw", created when needed.
"""
import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty

AUTO_NAME = "Nakan Vector Draw"
KINDS = {'CURVE', 'FONT'}
_known = set()


def prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def mode_on():
    p = prefs()
    return bool(p is not None and p.vector_mode)


# ------------------------------------------------------------------------------ grey out panels
def gate(cls):
    """Wrap a panel's draw: everything inside is disabled while Vector Mode is off."""
    orig = cls.draw
    if getattr(orig, "_nkpf_gated", False):
        return

    def draw(self, context):
        self.layout.enabled = mode_on()
        orig(self, context)
    draw._nkpf_gated = True
    cls.draw = draw


# ---------------------------------------------------------------------------- target collection
def _in_scene(scene, coll):
    return coll == scene.collection or coll in scene.collection.children_recursive


def target_collection(scene, create=True):
    c = scene.nkpf_collection
    if c is None:
        if not create:
            return None
        c = bpy.data.collections.get(AUTO_NAME)
        if c is None:
            c = bpy.data.collections.new(AUTO_NAME)
    if not _in_scene(scene, c):
        scene.collection.children.link(c)
    return c


def _find_layer_collection(lc, coll):
    if lc.collection == coll:
        return lc
    for ch in lc.children:
        r = _find_layer_collection(ch, coll)
        if r is not None:
            return r
    return None


def activate(context=None):
    """Make the target collection the active one (new objects of any tool land there)."""
    context = context or bpy.context
    scene, vl = context.scene, context.view_layer
    if scene is None or vl is None:
        return
    c = target_collection(scene)
    lc = _find_layer_collection(vl.layer_collection, c)
    if lc is not None:
        if lc.exclude:
            lc.exclude = False
        vl.active_layer_collection = lc


def move_to_target(ob, coll):
    if coll in ob.users_collection and len(ob.users_collection) == 1:
        return
    if coll not in ob.users_collection:
        coll.objects.link(ob)
    for c in list(ob.users_collection):
        if c != coll:
            c.objects.unlink(ob)


def snapshot():
    try:
        uids = [o.session_uid for o in bpy.data.objects]
    except AttributeError:                       # bpy.data is not readable yet (add-on registering)
        return 0.2                               # (used as a timer: try again in a moment)
    _known.clear()
    _known.update(uids)
    return None


@persistent
def on_depsgraph(scene, depsgraph):
    if not mode_on():
        return
    new = []
    for u in depsgraph.updates:                  # a new object always shows up in the updates
        idb = u.id
        if not isinstance(idb, bpy.types.Object):
            continue
        ob = idb.original
        uid = ob.session_uid
        if uid in _known:
            continue
        _known.add(uid)
        if scene.objects.get(ob.name) == ob:
            new.append(ob)
    if not new:
        return
    try:
        from . import surface
        surface.auto_wrap(scene, new)            # Draw on: Object -> projected on the surface
    except Exception as e:  # noqa: BLE001
        print("Nakan Vector Draw (surface):", e)
    new = [o for o in new if o.type in KINDS]
    if not new:
        return
    coll = target_collection(scene)
    for o in new:
        try:
            move_to_target(o, coll)
            for ch in o.children:                 # a solid made together with its curve goes along
                if ch.type == 'MESH':
                    move_to_target(ch, coll)
        except (ReferenceError, RuntimeError):
            pass


@persistent
def on_load(*_args):
    snapshot()


def _on_mode(context=None):
    snapshot()
    if mode_on():
        try:
            activate(context)
        except (AttributeError, RuntimeError, ReferenceError):
            pass


def _coll_changed(self, context):
    if mode_on():
        activate(context)


# ------------------------------------------------------------------------------------- panel
class NKDRAW_PT_mode_top(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Vector Draw"
    bl_label = "Vector Mode"
    bl_order = 0

    def draw_header(self, context):
        p = prefs()
        if p is not None:
            self.layout.prop(p, "vector_mode", text="")

    def draw(self, context):
        lay = self.layout
        p = prefs()
        sc = context.scene
        lay.prop(sc, "nkpf_collection", text="Collection", icon='OUTLINER_COLLECTION')
        if sc.nkpf_collection is None:
            r = lay.row()
            r.scale_y = 0.7
            r.label(text="Empty = \"%s\" (created when needed)" % AUTO_NAME)
        col = lay.column()
        col.enabled = mode_on()
        from . import surface
        box = col.box()
        surface.draw_ui(box, context)
        if p is not None:
            col.prop(p, "move_on_select")
        r = col.row()
        r.scale_y = 0.7
        r.label(text="G / R / S / Shift+D on the CPlane · P = Pen")


def register():
    bpy.types.Scene.nkpf_collection = PointerProperty(
        type=bpy.types.Collection, name="Collection", update=_coll_changed,
        description="Where everything made in Vector Mode goes (empty = a \"Nakan Vector Draw\" collection)")
    bpy.utils.register_class(NKDRAW_PT_mode_top)
    bpy.app.timers.register(snapshot, first_interval=0.1)
    if on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(on_depsgraph)
    if on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_load)


def unregister():
    if bpy.app.timers.is_registered(snapshot):
        bpy.app.timers.unregister(snapshot)
    if on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load)
    if on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(on_depsgraph)
    bpy.utils.unregister_class(NKDRAW_PT_mode_top)
    del bpy.types.Scene.nkpf_collection
