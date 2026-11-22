# SPDX-License-Identifier: GPL-3.0-or-later
bl_info = {
    "name": "Nakan Paintover PSD",
    "author": "Nakan",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "Properties > Output > Paintover PSD  |  Render > Render Paintover PSD",
    "description": "Render straight to a layered PSD with the render, flat colors, shadows and "
                   "a mask for every object and material, ready to paint over",
    "category": "Render",
}

import os
import shutil
import tempfile
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper

from . import build, exr

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

SHADOW_ITEMS = [
    ("AUTO", "Auto", "Shadow Catcher when the scene has one (Cycles), otherwise Everything"),
    ("CATCHER", "Shadow Catcher",
     "Shadows that fall on objects marked as Shadow Catcher (Cycles). One render, the catcher "
     "stays invisible in the Render layer"),
    ("FULL", "Everything",
     "Shadows on every surface. Renders twice, with and without shadows, and keeps the "
     "difference"),
    ("NONE", "None", "No shadow layer"),
]


class NKPAINT_Settings(PropertyGroup):
    use_render: BoolProperty(name="Render", default=True,
                             description="The final image, as you see it in the render window")
    use_flat: BoolProperty(name="Flat Colors", default=True,
                           description="Base colors of the materials without light and shadow "
                                       "(Diffuse Color pass). Hidden in the PSD")
    use_object_masks: BoolProperty(name="Object Masks", default=True,
                                   description="One antialiased mask per object (Cryptomatte)")
    use_material_masks: BoolProperty(name="Material Masks", default=True,
                                     description="One antialiased mask per material (Cryptomatte)")
    shadows: EnumProperty(name="Shadows", items=SHADOW_ITEMS, default="AUTO")
    colored: BoolProperty(name="Colored Masks", default=True,
                          description="Each mask gets its own color. Turn the group on and click an "
                                      "object with the Move tool (Auto-Select) to jump to its layer")
    masks_visible: BoolProperty(name="Show Masks", default=False,
                                description="Open the PSD with the Object Masks group visible on top")
    skip_names: StringProperty(name="Skip",
                               description="Masks to leave out, separated by commas. Parts of names "
                                           "or wildcards, like 'Light, Camera*, *_helper'",
                               default="")
    directory: StringProperty(name="Folder", subtype="DIR_PATH", default="//",
                              description="Where the PSD is saved. // is the folder of the .blend")
    file_name: StringProperty(name="Name", default="",
                              description="File name without extension. Empty uses the .blend name "
                                          "and the frame number")
    open_folder: BoolProperty(name="Open Folder When Done", default=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def render_result():
    for img in bpy.data.images:
        if img.type == "RENDER_RESULT":
            return img
    return None


def save_render(scene, path, kind):
    """Save the Render Result as a multilayer EXR (all passes, linear) or as
    an 8-bit PNG (with the scene's color management, like the render window)."""
    st = scene.render.image_settings
    keys = ["media_type", "file_format", "color_mode", "color_depth", "exr_codec",
            "color_management"]
    saved = {k: getattr(st, k) for k in keys if hasattr(st, k)}
    try:
        if hasattr(st, "color_management"):
            st.color_management = "FOLLOW_SCENE"
        if kind == "EXR":
            if hasattr(st, "media_type"):
                st.media_type = "MULTI_LAYER_IMAGE"
            st.file_format = "OPEN_EXR_MULTILAYER"
            st.color_depth = "32"
            st.exr_codec = "ZIP"
        else:
            if hasattr(st, "media_type"):
                st.media_type = "IMAGE"
            st.file_format = "PNG"
            st.color_mode = "RGBA"
            st.color_depth = "8"
        img = render_result()
        if img is None:
            raise RuntimeError("There is no render yet")
        img.save_render(path, scene=scene)
    finally:
        for k, v in saved.items():
            try:
                setattr(st, k, v)
            except (TypeError, AttributeError):
                pass
    if not os.path.exists(path):
        raise RuntimeError("Blender could not save the render")


def load_png_rgba(path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        buf = np.empty(w * h * 4, np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    return build.to_u8(buf.reshape(h, w, 4)[::-1])


def find_rgb(f, layer_prefix, pass_name):
    """[R, G, B] channel names of a pass, or None. An empty pass name means the
    plain R, G, B channels some apps write."""
    for case in (str.upper, str.lower):
        parts = [p for p in (layer_prefix, pass_name) if p]
        names = [".".join(parts + [case(c)]) for c in "rgb"]
        if all(n in f.index for n in names):
            return names
    return None


def find_alpha(f, layer_prefix):
    for n in ("%s.Combined.A" % layer_prefix, "%s.Combined.a" % layer_prefix, "A", "a"):
        if n in f.index:
            return n
    return None


def pass_rgb(f, names):
    return np.stack([f.channel(n) for n in names], axis=-1)


RENDERABLE = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME"}


def scene_catchers(scene):
    return [o for o in scene.objects if o.type in RENDERABLE
            and getattr(o, "is_shadow_catcher", False) and not o.hide_render]


def resolve_shadow_mode(scene, mode):
    cycles = scene.render.engine == "CYCLES"
    if mode == "AUTO":
        return "CATCHER" if cycles and scene_catchers(scene) else "FULL"
    if mode == "CATCHER" and not cycles:
        return "FULL"
    return mode


class ShadowsOff:
    """Turn every shadow off for one render: lights stop casting shadows and
    objects stop blocking shadow rays (that covers the world light too)."""

    def __init__(self, scene):
        self.scene = scene
        self.saved = []

    def __enter__(self):
        for ob in self.scene.objects:
            if ob.type == "LIGHT":
                light = ob.data
                if hasattr(light, "use_shadow"):
                    self.saved.append((light, "use_shadow", light.use_shadow))
                    light.use_shadow = False
                cyc = getattr(light, "cycles", None)
                if cyc is not None and hasattr(cyc, "cast_shadow"):
                    self.saved.append((cyc, "cast_shadow", cyc.cast_shadow))
                    cyc.cast_shadow = False
            if hasattr(ob, "visible_shadow"):
                self.saved.append((ob, "visible_shadow", ob.visible_shadow))
                ob.visible_shadow = False
        return self

    def __exit__(self, *exc):
        for owner, attr, value in reversed(self.saved):
            try:
                setattr(owner, attr, value)
            except (AttributeError, ReferenceError):
                pass
        return False


class PassesOn:
    """Enable the passes the PSD needs and put everything back afterwards."""

    def __init__(self, view_layer, flat, crypto_obj, crypto_mat, catcher):
        self.vl = view_layer
        self.want = [(view_layer, "use_pass_diffuse_color", flat),
                     (view_layer, "use_pass_cryptomatte_object", crypto_obj),
                     (view_layer, "use_pass_cryptomatte_material", crypto_mat)]
        cyc = getattr(view_layer, "cycles", None)
        if cyc is not None and hasattr(cyc, "use_pass_shadow_catcher"):
            self.want.append((cyc, "use_pass_shadow_catcher", catcher))
        self.saved = []

    def __enter__(self):
        for owner, attr, on in self.want:
            if on and hasattr(owner, attr) and not getattr(owner, attr):
                self.saved.append((owner, attr))
                setattr(owner, attr, True)
        return self

    def __exit__(self, *exc):
        for owner, attr in self.saved:
            setattr(owner, attr, False)
        return False


def output_path(context, settings):
    folder = bpy.path.abspath(settings.directory or "//")
    if not folder or (settings.directory.startswith("//") and not bpy.data.filepath):
        raise RuntimeError("Save the .blend first, or pick an output folder")
    os.makedirs(folder, exist_ok=True)
    name = settings.file_name.strip()
    if not name:
        base = bpy.path.display_name_from_filepath(bpy.data.filepath) or "untitled"
        name = "%s_paintover_%04d" % (base, context.scene.frame_current)
    return os.path.join(folder, bpy.path.clean_name(name, replace="_") + ".psd")


def assemble(settings, f, layer_prefix, render_png=None, noshadow_png=None,
             noshadow_exr=None, shadow_mode="NONE", linear_render=False, progress=None):
    """Read the EXR (and the PNGs) and write the PSD. Returns a short summary."""
    w, h = f.size
    alpha_name = find_alpha(f, layer_prefix)
    alpha = f.channel(alpha_name) if alpha_name else np.ones((h, w), np.float32)

    render = None
    if render_png:
        render = load_png_rgba(render_png)
    elif linear_render:
        rgb = find_rgb(f, layer_prefix, "Combined") or find_rgb(f, "", "")
        if rgb:
            c = pass_rgb(f, rgb)
            a = alpha[..., None]
            straight = np.where(a > 1e-6, c / np.maximum(a, 1e-6), 0.0)
            render = np.concatenate([build.to_u8(build.linear_to_srgb(straight)),
                                     build.to_u8(alpha)[..., None]], axis=-1)

    extras = []
    if settings.use_flat:
        names = find_rgb(f, layer_prefix, "Diffuse Color")
        if names:
            col = build.linear_to_srgb(pass_rgb(f, names))
            extras.append(("Flat Colors", np.concatenate(
                [build.to_u8(col), build.to_u8(alpha)[..., None]], axis=-1), False))
    if noshadow_png:
        extras.append(("Render Without Shadows", load_png_rgba(noshadow_png), False))
    if progress:
        progress(0.15)

    shadows, shadows_visible = None, True
    if shadow_mode == "CATCHER":
        names = find_rgb(f, layer_prefix, "Shadow Catcher")
        if names:
            shadows = build.shadow_alpha(build.luminance(pass_rgb(f, names)))
            if not shadows.any():
                shadows = None
    elif shadow_mode == "FULL" and noshadow_exr is not None:
        with_s = find_rgb(f, layer_prefix, "Combined")
        without = find_rgb(noshadow_exr, layer_prefix, "Combined")
        if with_s and without:
            shadows = build.shadow_alpha(build.shadow_ratio(pass_rgb(f, with_s),
                                                            pass_rgb(noshadow_exr, without)))
            shadows_visible = False  # the Render already has them
    elif shadow_mode == "EXR":
        for pname in ("Shadow Catcher", "Shadow"):
            names = find_rgb(f, layer_prefix, pname)
            if names:
                shadows = build.shadow_alpha(build.luminance(pass_rgb(f, names)))
                shadows_visible = pname == "Shadow Catcher"
                break
    if progress:
        progress(0.3)

    kinds = set()
    if settings.use_object_masks:
        kinds.add("Object")
    if settings.use_material_masks:
        kinds.add("Material")
    groups = []
    if kinds:
        sub = (lambda x: progress(0.3 + 0.6 * x)) if progress else None
        groups = build.collect_masks(f, kinds, build.parse_patterns(settings.skip_names), sub)
        if layer_prefix:
            mine = [g for g in groups if "(" not in g[0]]
            groups = mine or groups
    return w, h, render if settings.use_render else None, extras, shadows, shadows_visible, groups


def write(settings, path, parts):
    w, h, render, extras, shadows, shadows_visible, groups = parts
    n = build.write_psd(path, w, h, render, extras, shadows, shadows_visible, groups,
                        masks_visible=settings.masks_visible, colored=settings.colored)
    bits = []
    if shadows is not None:
        bits.append("shadows")
    bits.append("%d masks" % n)
    return ", ".join(bits)


def open_folder(path):
    try:
        bpy.ops.wm.path_open(filepath=os.path.dirname(path))
    except RuntimeError:
        pass


class _Progress:
    def __init__(self, context):
        self.wm = context.window_manager

    def __enter__(self):
        self.wm.progress_begin(0, 100)
        return self

    def __call__(self, x):
        self.wm.progress_update(int(max(0, min(1, x)) * 100))

    def __exit__(self, *exc):
        self.wm.progress_end()
        return False


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class NKPAINT_OT_render(Operator):
    bl_idname = "nk_paintover.render"
    bl_label = "Render Paintover PSD"
    bl_description = ("Render the current frame and save a layered PSD with the render, flat "
                      "colors, shadows and a mask per object and material")

    def execute(self, context):
        scene, vl = context.scene, context.view_layer
        st = scene.nk_paintover
        if scene.camera is None:
            self.report({"ERROR"}, "The scene has no camera")
            return {"CANCELLED"}
        try:
            path = output_path(context, st)
        except RuntimeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        mode = resolve_shadow_mode(scene, st.shadows)
        tmp = tempfile.mkdtemp(prefix="nk_paintover_")
        t0 = time.time()
        try:
            with _Progress(context) as prog, PassesOn(
                    vl, st.use_flat, st.use_object_masks, st.use_material_masks,
                    mode == "CATCHER"):
                noshadow_exr = noshadow_png = None
                if mode == "FULL":
                    with ShadowsOff(scene):
                        bpy.ops.render.render(layer=vl.name)
                        save_render(scene, os.path.join(tmp, "noshadow.exr"), "EXR")
                        save_render(scene, os.path.join(tmp, "noshadow.png"), "PNG")
                    noshadow_exr = exr.EXRFile(os.path.join(tmp, "noshadow.exr"))
                    noshadow_png = os.path.join(tmp, "noshadow.png")
                    prog(0.1)
                bpy.ops.render.render(layer=vl.name)
                save_render(scene, os.path.join(tmp, "render.exr"), "EXR")
                save_render(scene, os.path.join(tmp, "render.png"), "PNG")
            f = exr.EXRFile(os.path.join(tmp, "render.exr"))
            parts = assemble(st, f, vl.name, render_png=os.path.join(tmp, "render.png"),
                             noshadow_png=noshadow_png, noshadow_exr=noshadow_exr,
                             shadow_mode=mode)
            summary = write(st, path, parts)
        except (RuntimeError, exr.EXRError, OSError) as e:
            self.report({"ERROR"}, "Paintover PSD: %s" % e)
            return {"CANCELLED"}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.report({"INFO"}, "Saved %s (%s, %.0fs)" % (os.path.basename(path), summary,
                                                         time.time() - t0))
        if st.open_folder:
            open_folder(path)
        return {"FINISHED"}


class NKPAINT_OT_export_last(Operator):
    bl_idname = "nk_paintover.export_last"
    bl_label = "Export Last Render"
    bl_description = ("Save the render that is already in the render window as a layered PSD. "
                      "Masks, flat colors and catcher shadows only appear if those passes were on "
                      "when you rendered")

    def execute(self, context):
        scene, vl = context.scene, context.view_layer
        st = scene.nk_paintover
        try:
            path = output_path(context, st)
        except RuntimeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        tmp = tempfile.mkdtemp(prefix="nk_paintover_")
        try:
            save_render(scene, os.path.join(tmp, "render.exr"), "EXR")
            save_render(scene, os.path.join(tmp, "render.png"), "PNG")
            f = exr.EXRFile(os.path.join(tmp, "render.exr"))
            prefix = vl.name if find_alpha(f, vl.name) else f.parts[0].name.split(".")[0]
            mode = "NONE" if st.shadows == "NONE" else "CATCHER"
            parts = assemble(st, f, prefix, render_png=os.path.join(tmp, "render.png"),
                             shadow_mode=mode)
            summary = write(st, path, parts)
        except (RuntimeError, exr.EXRError, OSError) as e:
            self.report({"ERROR"}, "Paintover PSD: %s" % e)
            return {"CANCELLED"}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.report({"INFO"}, "Saved %s (%s)" % (os.path.basename(path), summary))
        if st.open_folder:
            open_folder(path)
        return {"FINISHED"}


class NKPAINT_OT_convert_exr(Operator, ImportHelper):
    bl_idname = "nk_paintover.convert_exr"
    bl_label = "Convert EXR to PSD"
    bl_description = ("Turn a multilayer EXR with Cryptomatte (from Blender or another app) into "
                      "a layered PSD next to it. The Render layer uses a plain sRGB conversion")
    filename_ext = ".exr"
    filter_glob: StringProperty(default="*.exr", options={"HIDDEN"})

    def execute(self, context):
        st = context.scene.nk_paintover
        src = self.filepath
        try:
            with _Progress(context) as prog:
                f = exr.EXRFile(src)
                prefixes = sorted({n.split(".")[0] for n in f.index if n.count(".") >= 2})
                prefix = next((p for p in prefixes if find_alpha(f, p)), prefixes[0] if prefixes else "")
                parts = assemble(st, f, prefix, linear_render=True, shadow_mode="EXR",
                                 progress=prog)
                path = os.path.splitext(src)[0] + "_paintover.psd"
                summary = write(st, path, parts)
        except (RuntimeError, exr.EXRError, OSError, KeyError, IndexError) as e:
            self.report({"ERROR"}, "Paintover PSD: %s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "Saved %s (%s)" % (os.path.basename(path), summary))
        if st.open_folder:
            open_folder(path)
        return {"FINISHED"}


class NKPAINT_OT_make_catcher(Operator):
    bl_idname = "nk_paintover.make_catcher"
    bl_label = "Selected as Shadow Catcher"
    bl_description = ("Mark the selected objects (a floor, a wall) as Shadow Catcher: they vanish "
                      "from the render and only the shadows on them stay, in the Shadows layer")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects)

    def execute(self, context):
        n = 0
        for ob in context.selected_objects:
            if ob.type in RENDERABLE and hasattr(ob, "is_shadow_catcher"):
                ob.is_shadow_catcher = True
                n += 1
        self.report({"INFO"}, "%d object(s) now catch shadows" % n)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class NKPAINT_PT_panel(Panel):
    bl_label = "Paintover PSD"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "output"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        st = scene.nk_paintover
        layout.use_property_split = True
        layout.use_property_decorate = False

        col = layout.column()
        col.scale_y = 1.4
        col.operator("nk_paintover.render", icon="RENDER_STILL")
        row = layout.row(align=True)
        row.operator("nk_paintover.export_last", icon="IMAGE_DATA")
        row.operator("nk_paintover.convert_exr", text="Convert EXR...", icon="FILE_IMAGE")

        box = layout.column(heading="Layers")
        box.prop(st, "use_render")
        box.prop(st, "use_flat")
        box.prop(st, "use_object_masks")
        box.prop(st, "use_material_masks")

        layout.prop(st, "shadows")
        mode = resolve_shadow_mode(scene, st.shadows)
        if st.shadows != "NONE":
            info = layout.column(align=True)
            if mode == "CATCHER" and not scene_catchers(scene):
                info.label(text="No Shadow Catcher in the scene", icon="ERROR")
                info.operator("nk_paintover.make_catcher", icon="SHADING_SOLID")
            elif mode == "CATCHER":
                info.label(text="Shadows on: " + ", ".join(o.name for o in scene_catchers(scene)[:3]),
                           icon="LIGHT_SUN")
            elif mode == "FULL":
                info.label(text="Renders twice: with and without shadows", icon="TIME")
                if st.shadows == "AUTO" and scene.render.engine == "CYCLES":
                    info.operator("nk_paintover.make_catcher", icon="SHADING_SOLID")

        col = layout.column(heading="Masks")
        col.active = st.use_object_masks or st.use_material_masks
        col.prop(st, "colored")
        col.prop(st, "masks_visible")
        col.prop(st, "skip_names")

        col = layout.column()
        col.prop(st, "directory")
        col.prop(st, "file_name")
        col.prop(st, "open_folder")


def menu_render(self, context):
    self.layout.separator()
    self.layout.operator("nk_paintover.render", icon="RENDER_STILL")


classes = (NKPAINT_Settings, NKPAINT_OT_render, NKPAINT_OT_export_last, NKPAINT_OT_convert_exr,
           NKPAINT_OT_make_catcher, NKPAINT_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.nk_paintover = PointerProperty(type=NKPAINT_Settings)
    bpy.types.TOPBAR_MT_render.append(menu_render)


def unregister():
    bpy.types.TOPBAR_MT_render.remove(menu_render)
    del bpy.types.Scene.nk_paintover
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
