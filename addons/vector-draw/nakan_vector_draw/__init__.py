"""Nakan Vector Draw — vector drawing for Blender: an Illustrator-style pen, live shapes, Shape Builder,
Repeat, Align & Distribute, snaps.

Started as a fork of PenForge 1.2.1 by Enrico Biancardi (GPL-3.0-or-later), now developed independently
by Nakan. Changes from PenForge: the Solid / Quad / Boolean (mesh) tools and the construction lines were
removed; Live Shapes, Repeat, Align & Distribute, Vector Mode, Contour, Split, Text and Draw on Object were
added. Licensed under the GPL-3.0-or-later, like the original."""

bl_info = {
    "name": "Nakan Vector Draw",
    "author": "Nakan (based on PenForge by Enrico Biancardi)",
    "version": (1, 4, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Nakan Vector Draw  |  Ctrl+Shift+P in Object Mode",
    "description": "Vector drawing: an Illustrator-style pen, live shapes, Shape Builder, Repeat, snaps",
    "category": "Add Curve",
}

import math
import sys

# Reinstalling over an older version in the same Blender session: forget the old sub-modules, or
# Python would keep running their old code (new buttons and fixes would not appear until a restart).
for _k in [k for k in list(sys.modules) if k.startswith(__name__ + ".")]:
    del sys.modules[_k]

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty

from . import grid, ui, shapebuilder, copytool
from . import shapes, align, automove, planar, modegate, surface, contour, split, repeat, planeobj
_PANELS = tuple(ui.PANELS)
_OPS = (ui.NKDRAW_OT_pen, ui.NKDRAW_OT_cplane_face, ui.NKDRAW_OT_cplane_world, ui.NKDRAW_OT_key_points,
        ui.NKDRAW_OT_cplane_rotate, ui.NKDRAW_OT_cplane_apply, ui.NKDRAW_OT_mirror)
_CONFLICTS = ("nakan_vector_tools", "nakan_penforge_extras")


def _conflict():
    for a in bpy.context.preferences.addons:
        name = a.module.split(".")[-1]
        if name in _CONFLICTS:
            return name
    return None


def _mode_changed(self, context):
    automove.toggle(self, context)
    planar.sync_keymaps()
    for _km, _kmi in _keymaps:
        try:
            _kmi.active = bool(self.vector_mode)
        except ReferenceError:
            pass
    modegate._on_mode(context)
    grid.redraw()
from bpy.app.handlers import persistent


class NkDrawSettings(bpy.types.PropertyGroup):
    osnap: BoolProperty(name="Snap", default=True, description="Snapping on/off (D while drawing)")
    snap_end: BoolProperty(name="End", default=True, description="Curve end and corner points")
    snap_vert: BoolProperty(name="Vertex", default=True, description="Mesh vertices")
    snap_mid: BoolProperty(name="Mid", default=True, description="Midpoint of edges and curve segments")
    snap_cen: BoolProperty(name="Cen", default=False, description="Centre of faces and closed curves")
    snap_int: BoolProperty(name="Int", default=True, description="Intersection of edges and curves")
    snap_perp: BoolProperty(name="Perp", default=False, description="Perpendicular from the last point")
    snap_near: BoolProperty(name="Near", default=False, description="Nearest point on an edge or curve")
    snap_track: BoolProperty(name="Track", default=True,
                             description="Tracking (AutoCAD / Illustrator smart guides): hover a snap point, then move "
                                         "away: the cursor lines up with it along the CPlane X / Y, also where the "
                                         "Shift / Ortho direction meets that line")
    snap_grid: BoolProperty(name="Grid", default=False, description="Grid points of the CPlane")
    snap_axis: BoolProperty(name="Mirror axis", default=True,
                            description="The cursor catches the axes: the mirror axis with the mirror on, otherwise the "
                                        "CPlane X / Y axes through its origin (also where the Shift / Ortho "
                                        "direction meets them)")
    snap_project: BoolProperty(name="Project", default=False,
                               description="Project snapped points onto the CPlane (draw flat plans over 3D models)")
    snap_hidden: BoolProperty(name="Hidden", default=False,
                              description="Also snap to points hidden behind other objects")
    planar: BoolProperty(name="Planar", default=False,
                         description="Rhino Planar: free points stay at the elevation of the last point instead of "
                                     "going back to the CPlane")
    mirror: EnumProperty(name="Mirror", default='OFF', items=[
        ('OFF', "Off", "No mirror"),
        ("Y", "L \u2194 R", "Mirror across the CPlane Y axis (through the CPlane origin)"),
        ("X", "F \u2194 B", "Mirror across the CPlane X axis (through the CPlane origin)")],
        description="Symmetric drawing: the curve is mirrored when you finish it (Shift+M in the pen). "
                    "Start or end on the axis to get one joined curve")
    snap_radius: IntProperty(name="Radius", default=14, min=4, max=60, subtype='PIXEL',
                             description="How close (pixels) the cursor must be to catch a snap")
    grid_step: FloatProperty(name="Step", default=0.1, min=0.0001, subtype='DISTANCE', unit='LENGTH',
                             update=lambda s, c: grid.redraw(),
                             description="Grid spacing on the CPlane (used by the visible grid and Grid snap)")
    show_grid: BoolProperty(name="Show Grid", default=True, update=lambda s, c: grid.redraw(),
                            description="Draw the construction grid on the CPlane in every 3D view")
    grid_cells: IntProperty(name="Extent", default=50, min=1, max=1000, update=lambda s, c: grid.redraw(),
                            description="Grid cells on each side of the CPlane origin (the 3D cursor)")
    grid_major: IntProperty(name="Major every", default=10, min=1, max=100, update=lambda s, c: grid.redraw(),
                            description="A stronger line every N cells")
    grid_opacity: FloatProperty(name="Opacity", default=1.0, min=0.0, max=2.0, subtype='FACTOR',
                                update=lambda s, c: grid.redraw())
    grid_color: FloatVectorProperty(name="Color", size=3, subtype='COLOR', default=(1.0, 0.55, 0.15),
                                    min=0.0, max=1.0, update=lambda s, c: grid.redraw(),
                                    description="Colour of the Vector Draw grid (orange, to tell it from Blender's)")
    shift_angle: FloatProperty(name="Shift angle", default=45.0, min=1.0, max=90.0,
                               description="Shift constrains new segments and handles to steps of this angle")
    poly_sides: IntProperty(name="Sides", default=6, min=3, max=64,
                            description="Polygon sides (Up/Down arrows while drawing)")
    ortho: BoolProperty(name="Ortho", default=False,
                        description="Lock new segments to the CPlane axes (Shift inverts it while held)")
    cplane_offset: FloatVectorProperty(name="Turn", size=3, subtype='EULER', default=(0.0, 0.0, 0.0),
                                       update=lambda s, c: grid.redraw(),
                                       description="Live rotation of the CPlane around its own X / Y / Z axes "
                                                   "(seen at once on the grid); Apply makes it the CPlane and resets it to 0")
    cplane_angle: FloatProperty(name="Angle", default=math.radians(90.0), subtype='ANGLE',
                                description="How much the CPlane turns (Q / Shift+Q / Alt+Q, Shift+click on an edge)")
    cplane: EnumProperty(name="CPlane", default='TOP', items=[
        ('TOP', "Top", "World XY through the 3D cursor"),
        ('FRONT', "Front", "World XZ through the 3D cursor"),
        ('RIGHT', "Right", "World YZ through the 3D cursor"),
        ('CURSOR', "Cursor", "The 3D cursor's own plane: align the cursor to a face (Shift+right drag) "
                             "to draw on it"),
        ('VIEW', "View", "Parallel to the screen through the 3D cursor"),
        ('OBJECT', "Object", "The Plane Object: an empty you move and rotate freely (Create it in the "
                             "Drawing Plane panel)"),
    ], update=lambda s, c: grid.redraw())
    plane_object: PointerProperty(type=bpy.types.Object, name="Plane Object",
                                  poll=lambda s, o: o.type == 'EMPTY',
                                  description="The empty that holds the drawing plane when the plane is set to Object")
    saved_planes: bpy.props.CollectionProperty(type=planeobj.NKDRAW_SavedPlane)
    saved_index: IntProperty(name="Saved plane", default=0, min=0)


class NkDrawPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_live: BoolProperty(
        name="Drawn shapes are live",
        description="Rectangles, circles and polygons drawn with the pen become live shapes by themselves",
        default=True)
    vector_mode: BoolProperty(
        name="Vector Mode",
        description="Switches the whole Nakan Vector Draw on: tools, shortcuts (Ctrl+Shift+P, P, G / R / S on the "
                    "CPlane) and the target collection. Always off when Blender starts",
        default=False, update=_mode_changed)
    move_on_select: BoolProperty(
        name="Move on select",
        description="In Vector Mode, selecting a curve starts the snapping Move (base point, then destination)",
        default=False, update=automove.toggle)

    def draw(self, context):
        lay = self.layout
        col = lay.column()
        col.prop(self, "auto_live")
        col.prop(self, "vector_mode")
        col.prop(self, "move_on_select")


def prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


_keymaps = []


@persistent
def _on_depsgraph_shapes(scene, depsgraph):
    try:
        shapes.auto_live(scene, depsgraph)
    except Exception as e:  # noqa: BLE001  (never break the depsgraph for others)
        print("Nakan Vector Draw (live shapes):", e)


_registered = [False]


def register():
    other = _conflict()
    if other is not None:
        print("Nakan Vector Draw: disable '%s' first (it is the older version of this add-on), then enable "
              "Nakan Vector Draw again." % other)
        raise RuntimeError("Nakan Vector Draw: disable the add-on '%s' first (older version of this add-on)" % other)
    bpy.utils.register_class(NkDrawPreferences)
    planeobj.register_types()
    bpy.utils.register_class(NkDrawSettings)
    bpy.types.Scene.nkdraw = PointerProperty(type=NkDrawSettings)
    shapes.guard(shapebuilder.NKDRAW_OT_shape_builder, "invoke")
    shapebuilder.register()
    copytool.register()
    surface.register()
    modegate.register()
    for p in _PANELS[1:] + (shapes.NKDRAW_PT_shape, shapes.NKDRAW_PT_shape_data, align.NKDRAW_PT_align,
                        planar.NKDRAW_PT_text, repeat.NKDRAW_PT_repeat):
        modegate.gate(p)
    for c in _OPS + _PANELS:
        bpy.utils.register_class(c)
    ui.load_icons()
    grid.register()
    automove.register()
    shapes.register()
    align.register()
    repeat.register()
    planeobj.register()
    planar.register()
    contour.register()
    split.register()
    if _on_depsgraph_shapes not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_shapes)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is not None:
        km = kc.keymaps.new(name="Object Mode", space_type='EMPTY')
        kmi = km.keymap_items.new(ui.NKDRAW_OT_pen.bl_idname, 'P', 'PRESS', ctrl=True, shift=True)
        _keymaps.append((km, kmi))
        kmi.active = modegate.mode_on()
    bpy.app.timers.register(_sync_after_start, first_interval=0.5)
    _registered[0] = True


def _sync_after_start():
    """The add-on preferences are read a moment after register: switch the shortcuts on / off then."""
    p = prefs()
    if p is None:
        return 0.5
    if p.vector_mode:
        try:
            p.vector_mode = False                # Vector Mode always starts off: turn it on when you need it
        except (AttributeError, RuntimeError):
            pass
    on = bool(p.vector_mode)
    for _km, _kmi in _keymaps:
        try:
            _kmi.active = on
        except ReferenceError:
            pass
    planar.sync_keymaps()
    if on:
        modegate._on_mode(bpy.context)
    return None


def unregister():
    if not _registered[0]:
        return
    _registered[0] = False
    if bpy.app.timers.is_registered(_sync_after_start):
        bpy.app.timers.unregister(_sync_after_start)
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    if _on_depsgraph_shapes in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_shapes)
    split.unregister()
    contour.unregister()
    planar.unregister()
    repeat.unregister()
    planeobj.unregister()
    align.unregister()
    shapes.unregister()
    automove.unregister()
    grid.unregister()
    ui.free_icons()
    for c in tuple(reversed(_PANELS)) + tuple(reversed(_OPS)):
        bpy.utils.unregister_class(c)
    modegate.unregister()
    surface.unregister()
    copytool.unregister()
    shapebuilder.unregister()
    del bpy.types.Scene.nkdraw
    bpy.utils.unregister_class(NkDrawSettings)
    planeobj.unregister_types()
    bpy.utils.unregister_class(NkDrawPreferences)
