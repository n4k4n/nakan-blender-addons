# SPDX-License-Identifier: GPL-3.0-or-later
"""Nakan Extras for PenForge: Live Shapes, Align & Distribute, Vector Mode (G / R / S on the CPlane),
drawing projected on objects, Contour and Split, on top of PenForge by Enrico Biancardi.

PenForge itself is not included: buy it and install it first. These extras plug into it."""

import sys

# Reinstalling over an older version in the same session: forget the old sub-modules.
for _k in [k for k in list(sys.modules) if k.startswith(__name__ + ".")]:
    del sys.modules[_k]

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty

PENFORGE_URL = "https://4754272285663.gumroad.com/l/penforge"

# PenForge modules the extras use; they are made reachable as  from . import geom  etc.
_PF_MODULES = ("geom", "snaps", "cplane", "grid", "pen", "ui", "boolean", "shapebuilder",
               "solid", "construct", "copytool", "stroke")

_state = {"ready": False, "pf": None, "tries": 0}


def find_penforge():
    """Package name of the enabled PenForge add-on (legacy or extension, any repository)."""
    for name in bpy.context.preferences.addons.keys():
        if name == "penforge" or name.endswith(".penforge"):
            if name in sys.modules:
                return name
    return None


def _alias(pf):
    for m in _PF_MODULES:
        full = pf + "." + m
        if full not in sys.modules:
            try:
                __import__(full)
            except Exception:  # noqa: BLE001
                continue
        sys.modules[__name__ + "." + m] = sys.modules[full]
        setattr(sys.modules[__name__], m, sys.modules[full])


def prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _mode_changed(self, context):
    if not _state["ready"]:
        return
    from . import automove, planar, modegate, grid
    automove.toggle(self, context)
    planar.sync_keymaps()
    modegate._on_mode(context)
    try:
        grid.redraw()
    except Exception:  # noqa: BLE001
        pass


def _auto_move_changed(self, context):
    if _state["ready"]:
        from . import automove
        automove.toggle(self, context)


class NKPFX_Prefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_live: BoolProperty(
        name="Drawn shapes are live",
        description="Rectangles, circles and polygons drawn with the pen become live shapes by themselves",
        default=True)
    penforge_mode: BoolProperty(
        name="Vector Mode",
        description="Switches the extras on: G / R / S / Shift+D on the CPlane, P for the pen, "
                    "the target collection and drawing on objects",
        default=False, update=_mode_changed)
    move_on_select: BoolProperty(
        name="Move on select",
        description="In Vector Mode, selecting a curve starts PenForge's snapping Move (base point, then destination)",
        default=False, update=_auto_move_changed)

    def draw(self, context):
        lay = self.layout
        if not _state["ready"]:
            box = lay.box()
            box.label(text="PenForge is not installed or not enabled", icon='ERROR')
            box.label(text="These extras need PenForge by Enrico Biancardi (sold separately).")
            box.operator("wm.url_open", text="Get PenForge", icon='URL').url = PENFORGE_URL
        col = lay.column()
        col.prop(self, "auto_live")
        col.prop(self, "penforge_mode")
        col.prop(self, "move_on_select")


@persistent
def _on_depsgraph_shapes(scene, depsgraph):
    from . import shapes
    try:
        shapes.auto_live(scene, depsgraph)
    except Exception as e:  # noqa: BLE001  (never break the depsgraph for others)
        print("Nakan Extras for PenForge (live shapes):", e)


def _patch_penforge():
    """Small hooks into PenForge, all reversible."""
    from . import pen, boolean, shapebuilder, shapes
    # the pen leaves live shapes alone (their points belong to the shape until Convert to Curves)
    orig = pen.PenSession._load
    if not getattr(orig, "_nkpfx", False):
        def _load(self, ob):
            sh = getattr(ob, "nkpf_shape", None)
            if sh is not None and sh.live:
                return
            return orig(self, ob)
        _load._nkpfx = True
        _load._orig = orig
        pen.PenSession._load = _load
    # booleans / Shape Builder refuse live shapes: Convert to Curves first (like Affinity)
    shapes.guard(boolean.PENFORGE_OT_boolean, "execute")
    shapes.guard(shapebuilder.PENFORGE_OT_shape_builder, "invoke")


def _unpatch_penforge():
    try:
        from . import pen, boolean, shapebuilder
    except ImportError:
        return
    f = pen.PenSession._load
    if getattr(f, "_nkpfx", False):
        pen.PenSession._load = f._orig
    for cls, meth in ((boolean.PENFORGE_OT_boolean, "execute"),
                      (shapebuilder.PENFORGE_OT_shape_builder, "invoke")):
        f = getattr(cls, meth)
        if getattr(f, "_nkpf_guard", False) and hasattr(f, "_orig"):
            setattr(cls, meth, f._orig)


def _setup():
    """Register the extras once PenForge is loaded (it may load after us at startup)."""
    if _state["ready"]:
        return None
    pf = find_penforge()
    if pf is None or not hasattr(bpy.types, "PENFORGE_PT_main"):
        _state["tries"] += 1
        if _state["tries"] > 40:            # ~20 s: PenForge is really not there
            print("Nakan Extras for PenForge: PenForge is not installed or not enabled.")
            return None
        return 0.5
    _state["pf"] = pf
    _alias(pf)
    from . import shapes, align, automove, planar, modegate, surface, contour, split
    surface.register()
    modegate.register()
    for p in (shapes.NKPF_PT_shape, shapes.NKPF_PT_shape_data, align.NKPF_PT_align, planar.NKPF_PT_text):
        modegate.gate(p)
    automove.register()
    shapes.register()
    align.register()
    planar.register()
    contour.register()
    split.register()
    _patch_penforge()
    if _on_depsgraph_shapes not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_shapes)
    _state["ready"] = True
    p = prefs()
    if p is not None and p.penforge_mode:
        planar.sync_keymaps()
        modegate._on_mode(bpy.context)
    return None


def register():
    bpy.utils.register_class(NKPFX_Prefs)
    _state["tries"] = 0
    if _setup() is not None:
        bpy.app.timers.register(_setup, first_interval=0.5)


def unregister():
    if bpy.app.timers.is_registered(_setup):
        bpy.app.timers.unregister(_setup)
    if _state["ready"]:
        from . import shapes, align, automove, planar, modegate, surface, contour, split
        if _on_depsgraph_shapes in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_shapes)
        _unpatch_penforge()
        for m in (split, contour, planar, align, shapes, automove, modegate, surface):
            try:
                m.unregister()
            except Exception as e:  # noqa: BLE001
                print("Nakan Extras for PenForge (unregister):", e)
        _state["ready"] = False
    bpy.utils.unregister_class(NKPFX_Prefs)
