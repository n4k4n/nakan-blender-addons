"""Visible construction grid on the CPlane (always on when ticked, like Rhino's grid)."""

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import geom

_handle = None
_cache = {"key": None, "minor": [], "major": [], "axes": []}


def _lines(st, scene):
    cur = scene.cursor
    pl = geom.scene_plane(scene, None, st.cplane if st.cplane != 'VIEW' else 'TOP')
    step = max(st.grid_step, 1e-6)
    n = max(1, int(st.grid_cells))
    major = max(1, int(st.grid_major))
    key = (tuple(pl.origin), tuple(pl.x), tuple(pl.y), step, n, major)
    if _cache["key"] == key:
        return _cache
    L = n * step
    minor, maj, axes = [], [], []
    for k in range(-n, n + 1):
        off = k * step
        a1, b1 = pl.from_local(off, -L), pl.from_local(off, L)
        a2, b2 = pl.from_local(-L, off), pl.from_local(L, off)
        if k == 0:
            axes.append(((a2, b2), (0.85, 0.25, 0.3, 0.75)))     # X axis of the CPlane: red
            axes.append(((a1, b1), (0.35, 0.75, 0.3, 0.75)))     # Y axis: green
        elif k % major == 0:
            maj += [a1, b1, a2, b2]
        else:
            minor += [a1, b1, a2, b2]
    _cache.update(key=key, minor=minor, major=maj, axes=axes)
    return _cache


def draw():
    ctx = bpy.context
    st = getattr(ctx.scene, "nkdraw", None)
    if st is None or not st.show_grid:
        return
    try:
        from . import modegate
        if not modegate.mode_on():            # Nakan: the grid shows only in Vector Mode
            return
    except Exception:      # noqa: BLE001
        pass
    try:
        g = _lines(st, ctx.scene)
    except Exception:      # noqa: BLE001
        return
    region = ctx.region
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(False)
    sh.uniform_float("viewportSize", (region.width, region.height))
    a = st.grid_opacity
    r, gg, b = tuple(getattr(st, "grid_color", (1.0, 0.55, 0.15)))
    if g["minor"]:
        sh.uniform_float("lineWidth", 1.0)
        sh.uniform_float("color", (r, gg, b, 0.07 * a))
        batch_for_shader(sh, 'LINES', {"pos": g["minor"]}).draw(sh)
    if g["major"]:
        sh.uniform_float("lineWidth", 1.4)
        sh.uniform_float("color", (r, gg, b, 0.22 * a))
        batch_for_shader(sh, 'LINES', {"pos": g["major"]}).draw(sh)
    sh.uniform_float("lineWidth", 1.8)
    for (p, q), col in g["axes"]:
        sh.uniform_float("color", (col[0], col[1], col[2], col[3] * a))
        batch_for_shader(sh, 'LINES', {"pos": [p, q]}).draw(sh)
    gpu.state.depth_mask_set(True)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


def redraw(self=None, context=None):
    _cache["key"] = None
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def register():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW')


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
