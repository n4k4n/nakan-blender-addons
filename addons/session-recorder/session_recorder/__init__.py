# Session Recorder: automatically records every Blender session as a timelapse.
#
# There are two ways of recording in here, and it's worth knowing why.
#
# THE WINDOW (sr_helper.py, default on macOS)
#   A separate program asks the system for the Blender window and builds the video.
#   You get exactly what you see: viewport, UV editor, Ucupaint, shader editor,
#   Material Preview, Cycles, menus, tooltips. Nothing runs inside Blender
#   (no extra drawing, no re-rendering), so this path has
#   no way to make Blender hang. If Blender closes, even by crashing, the
#   program notices and finishes the video properly.
#   Requires the Screen Recording permission for Blender, on macOS.
#   While Blender is behind another window the system refuses the
#   capture; that's not a bug, it's exactly when nothing is changing in it.
#
# THE PANELS (fallback: other systems, or without the permission)
#   The add-on reads the pixels of each panel from inside, during the drawing
#   Blender was going to do anyway. Works with no permission at all, but
#   has a known limit: Blender draws the content of the 3D viewport and the
#   image editor into textures Python can't see. For the viewport I
#   work around it by re-rendering the scene separately (which needs care with EEVEE);
#   the image editor has no workaround and comes out almost black.
#
# In both:
#   * The session starts by itself when Blender opens.
#   * Only what changed on screen becomes a frame. Idle time takes no video.
#   * On retina screens it records at the logical size of the interface (what you see),
#     with half the pixels and half the file size.
#   * Changed monitor or window size: finishes the video and starts
#     another one at the new size.
#   * Left Blender open without touching it: finishes and goes on standby; starts
#     recording to a new file when you come back.
#   * A video with too little work time is discarded instead of becoming a file.
#   * No images piling up on disk. The MP4 is written progressively
#     (fragmented), so it stays playable even after a hard shutdown.
#   * Videos go to:
#         <base folder>/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS.mp4
#
# Requires ffmpeg installed (Mac: brew install ffmpeg | Windows: winget install ffmpeg).

import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

import bpy
import gpu
import numpy as np

ADDON_ID = __package__ or __name__
TICK = 0.5  # how often the timer checks whether it's time to capture

# Regions Blender exposes to Python. The ones that don't exist in a given editor
# simply don't register a handler.
REGION_TYPES = (
    'WINDOW', 'HEADER', 'TOOL_HEADER', 'FOOTER', 'UI', 'TOOLS',
    'NAVIGATION_BAR', 'CHANNELS', 'PREVIEW', 'HUD', 'EXECUTE',
    'ASSET_SHELF', 'ASSET_SHELF_HEADER',
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def default_folder():
    home = Path.home()
    for sub in ("Movies", "Videos"):
        if (home / sub).is_dir():
            return str(home / sub / "Blender Sessions")
    return str(home / "Blender Sessions")


def find_ffmpeg(custom_path=""):
    if custom_path:
        p = bpy.path.abspath(custom_path)
        if os.path.isfile(p):
            return p
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Blender opened from the Dock/Start Menu doesn't always see the terminal's PATH
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def get_prefs():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def pixel_density():
    try:
        return max(1.0, float(bpy.context.preferences.system.pixel_size))
    except Exception:
        return 1.0


def biggest_window():
    """Largest open window (with several monitors, the one in use)."""
    best, best_px = None, 0
    for win in bpy.context.window_manager.windows:
        if win.screen is None:
            continue
        px = win.width * win.height
        if px > best_px:
            best, best_px = win, px
    return best


def output_size(prefs, first_w, first_h):
    if prefs.resolution_mode == 'FIXED':
        w, h = prefs.width, prefs.height
    else:
        # On retina screens Blender draws 2 pixels for each interface pixel;
        # recording at the logical size changes nothing visible and cuts the size a lot.
        dense = pixel_density()
        base_w, base_h = first_w / dense, first_h / dense
        scale = min(1.0, prefs.max_width / base_w)
        w, h = round(base_w * scale), round(base_h * scale)
    return max(2, w - w % 2), max(2, h - h % 2)


_USED_PATHS = set()


def reserve_path(folder, stamp):
    """Free name for the next video. Several videos can be born in the same second
    (quick monitor switches), and ffmpeg only creates the file on the first frame,
    so we keep the names already handed out."""
    path = folder / f"{stamp}.mp4"
    n = 2
    while str(path) in _USED_PATHS or path.exists():
        path = folder / f"{stamp}_{n}.mp4"
        n += 1
    _USED_PATHS.add(str(path))
    return path


# ---------------------------------------------------------------------------
# Collecting pixels (without forcing a redraw)
# ---------------------------------------------------------------------------
# Each drawn panel hands its pixels here, at most once per
# capture interval. An idle panel doesn't redraw, isn't read and costs nothing.

TILES = {}        # region pointer -> {"px": RGBA, "v3d": bool}
_HANDLES = []
_READ_AT = {}     # region pointer -> when it was last read
_ADIADOS = set()  # panels that redrew but were at the read limit
_GERACAO = [0]    # goes up on every new read: tells whether the screen changed
_GAP = [2.0]      # minimum interval between reads of the same panel


def _draw_cb():
    """Runs at the end of each panel's drawing, in the drawing Blender was
    going to do anyway. A panel that doesn't redraw costs nothing, and its
    stored snapshot stays valid, because it didn't change."""
    try:
        region = bpy.context.region
        if region is None:
            return
        ptr = region.as_pointer()
        agora = time.monotonic()
        if agora - _READ_AT.get(ptr, 0.0) < _GAP[0]:
            _ADIADOS.add(ptr)  # redrew too early; picked up next time
            return
        area = bpy.context.area
        if area is None:
            return
        vx, vy, vw, vh = gpu.state.viewport_get()
        if vw < 2 or vh < 2:
            return
        fb = gpu.state.active_framebuffer_get()
        buf = fb.read_color(0, 0, vw, vh, 4, 0, 'UBYTE')
        buf.dimensions = vw * vh * 4
        px = np.frombuffer(buf, dtype=np.uint8).reshape(vh, vw, 4)
        TILES[ptr] = {
            "px": px.copy(),
            # In the 3D viewport, what is read here is only the overlay layer (grid,
            # axes, outlines, text, gizmos). The shaded geometry lives in
            # another texture, which is only combined with this one when it goes to the
            # screen, and Python can't reach that combination. That's why the object
            # comes out as a black silhouette and the scene has to be rendered separately.
            "v3d": area.type == 'VIEW_3D' and region.type == 'WINDOW',
        }
        _READ_AT[ptr] = agora
        _ADIADOS.discard(ptr)
        _GERACAO[0] += 1
    except Exception:
        pass  # a failing panel must not bring down the interface drawing


def _register_handlers():
    _unregister_handlers()
    for space in bpy.types.Space.__subclasses__():
        if not hasattr(space, "draw_handler_add"):
            continue
        for rt in REGION_TYPES:
            try:
                _HANDLES.append((space, rt, space.draw_handler_add(_draw_cb, (), rt, 'POST_PIXEL')))
            except Exception:
                pass  # this editor doesn't have that region


def _unregister_handlers():
    while _HANDLES:
        space, rt, h = _HANDLES.pop()
        try:
            space.draw_handler_remove(h, rt)
        except Exception:
            pass
    TILES.clear()
    _READ_AT.clear()
    _ADIADOS.clear()
    for off, _w, _h in _OFFSCREENS.values():
        try:
            off.free()
        except Exception:
            pass
    for off in _APOSENTADOS:
        try:
            off.free()
        except Exception:
            pass
    _APOSENTADOS.clear()
    _OFFSCREENS.clear()
    _SCENE_TILES.clear()
    _V3D_NEXT.clear()
    _V3D_SIG.clear()
    _V3D_PESADO.clear()


# The 3D scene is rendered separately, in its own buffer. That forces no redraw
# of the interface, so it doesn't reopen the door to the hang.
_OFFSCREENS = {}    # region pointer -> (offscreen, width, height)
_SCENE_TILES = {}   # region pointer -> last scene snapshot (RGB)
_V3D_NEXT = {}      # region pointer -> when it may render again
_V3D_SIG = {}       # region pointer -> scene state of the last snapshot
_V3D_PESADO = {}    # region pointer -> last time the heavy engine ran
_APOSENTADOS = []   # buffers swapped out for size, kept instead of freed
_SCENE_GEN = [0]    # goes up on every scene change

# Modes where the change doesn't show up reliably in the depsgraph: there it's
# always worth rendering again.
LIVE_MODES = {"SCULPT", "SCULPT_CURVES", "PAINT_TEXTURE", "PAINT_VERTEX",
              "PAINT_WEIGHT", "PAINT_GREASE_PENCIL", "EDIT_MESH"}


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    _SCENE_GEN[0] += 1


def _v3d_signature(area, region, w, h):
    space = area.spaces.active
    rv3d = space.region_3d
    mat = tuple(round(v, 5) for row in rv3d.view_matrix for v in row)
    return (_SCENE_GEN[0], mat, round(rv3d.view_distance or 0.0, 5),
            rv3d.view_perspective, space.shading.type, w, h)


def shading_pesado(space):
    """Tell whether rendering this viewport would trigger EEVEE or Cycles.

    In Solid and Wireframe the Workbench draws, which is simple and light.
    In Material Preview EEVEE comes in; in Rendered, the scene's engine. It was
    always with these two that Blender crashed (Oct 26 to Nov 1, 2026): material
    sync, image textures, ray tracing and the denoise destructor."""
    try:
        tipo = space.shading.type
    except Exception:
        return 'proibido'
    if tipo in {'SOLID', 'WIREFRAME'}:
        return False
    if tipo == 'RENDERED':
        try:
            motor = bpy.context.scene.render.engine.upper()
        except Exception:
            return 'proibido'
        if 'EEVEE' not in motor:
            return 'proibido'  # real-time path tracer: no way
    return True


def scene_tile(area, region, now, w, h):
    """Render this viewport's 3D scene at the requested size.

    The size comes from the caller (the overlay snapshot of that same region)
    so the two layers always match. Deriving the size from DPI here was
    what made the object come out black when the two calculations disagreed.

    Rendering is the expensive part of the capture, so it only happens when the scene or
    the point of view really changed, and its own measured cost spaces out the
    next ones. In between, the last snapshot stays valid, and a
    snapshot of something that didn't change is still correct."""
    ptr = region.as_pointer()
    cache = _SCENE_TILES.get(ptr)
    space = area.spaces.active
    prefs = get_prefs()

    pesado = shading_pesado(space)
    if pesado == 'proibido':
        return cache
    if pesado:
        espera = getattr(prefs, "heavy_interval", 30.0) if prefs else 30.0
        if espera <= 0 or now - _V3D_PESADO.get(ptr, -1e9) < espera:
            return cache  # hold back the heavy engine: that's where the crashes come from

    if cache is not None and cache.shape[:2] == (h, w):
        if now < _V3D_NEXT.get(ptr, 0.0):
            return cache  # still within the time budget
        try:
            ao_vivo = bpy.context.mode in LIVE_MODES
        except Exception:
            ao_vivo = True
        if not ao_vivo and _V3D_SIG.get(ptr) == _v3d_signature(area, region, w, h):
            return cache  # nothing changed: the previous snapshot is still accurate
    try:
        off, ow, oh = _OFFSCREENS.get(ptr, (None, 0, 0))
        if off is None or (ow, oh) != (w, h):
            if off is not None:
                # Freeing a buffer that had served EEVEE crashed Blender
                # on Nov 1, 2026, inside the denoise destructor. Here it's
                # only retired; the memory is freed when the add-on is unloaded.
                _APOSENTADOS.append(off)
            off = gpu.types.GPUOffScreen(w, h)
            _OFFSCREENS[ptr] = (off, w, h)
        rv3d = space.region_3d
        if pesado:
            _V3D_PESADO[ptr] = now
        t0 = time.perf_counter()
        # With overlays visible, this render makes the overlay engine
        # sync the mesh being edited and read data that isn't valid outside
        # normal drawing; that's how Blender crashed on Oct 15, 2026
        # (edit_object_sync -> CustomData_get_offset). In Blender's code
        # that block is guarded by "in_edit_mode && !state.hide_overlays",
        # so hiding them here takes it off the path. And it costs nothing: the
        # overlays of this buffer were already discarded, because the real ones are
        # read from the region itself.
        overlays_ligados = space.overlay.show_overlays
        try:
            if overlays_ligados:
                space.overlay.show_overlays = False
            with off.bind():
                fb = gpu.state.active_framebuffer_get()
                off.draw_view3d(bpy.context.scene, bpy.context.view_layer, space, region,
                                rv3d.view_matrix, rv3d.window_matrix, do_color_management=True)
                buf = fb.read_color(0, 0, w, h, 4, 0, 'UBYTE')
        finally:
            if overlays_ligados:
                space.overlay.show_overlays = True
        custo = time.perf_counter() - t0
        buf.dimensions = w * h * 4
        tile = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()
        _SCENE_TILES[ptr] = tile
        _V3D_SIG[ptr] = _v3d_signature(area, region, w, h)
        _V3D_NEXT[ptr] = now + custo * 10.0
        return tile
    except Exception as e:
        _V3D_NEXT[ptr] = now + 30.0  # it failed: don't keep insisting
        print(f"[Session Recorder] could not render the viewport: {e}")
        return cache


def esticar(img, w, h):
    """Reuse a snapshot of another size, repeating rows and columns.

    It's a safety net: when the viewport changes size, or when the
    scene rendering is being spaced out, a stretched snapshot is much
    better than the object disappearing and staying black until the next one."""
    ih, iw = img.shape[:2]
    if (ih, iw) == (h, w):
        return img
    ys = np.clip((np.arange(h) * (ih / h)).astype(np.intp), 0, ih - 1)
    xs = np.clip((np.arange(w) * (iw / w)).astype(np.intp), 0, iw - 1)
    return np.ascontiguousarray(img[ys][:, xs])


def blend_overlay(cena, overlay):
    """Put the overlay layer on top of the rendered scene.

    In floating point this cost tens of milliseconds on a large
    viewport. In integer math, and only on rows that have ink, it costs a fraction
    of that, and the difference in the result is one color level."""
    saida = cena.copy()
    linhas = np.flatnonzero(overlay[:, :, 3].any(axis=1))
    if linhas.size:
        y0, y1 = int(linhas[0]), int(linhas[-1]) + 1
        inv = (255 - overlay[y0:y1, :, 3]).astype(np.uint16)[:, :, None]
        faixa = saida[y0:y1].astype(np.uint16)
        faixa *= inv
        faixa >>= 8                      # premultiplied alpha; >> 8 instead
        faixa += overlay[y0:y1, :, :3]   # of / 255 is off by 0.4%, which nobody sees
        np.minimum(faixa, 255, out=faixa)
        saida[y0:y1] = faixa.astype(np.uint8)
    return saida


def _tag_all():
    for win in bpy.context.window_manager.windows:
        if win.screen is None:
            continue
        for area in win.screen.areas:
            area.tag_redraw()


_ULTIMA_GERAL = 0.0


def _pedir_redesenho(win, now):
    """Request a redraw only from those who need it: a panel with no snapshot at all, and a panel
    that redrew too early and was left behind. Telling the whole interface to
    redraw on every capture would charge for work it didn't need."""
    global _ULTIMA_GERAL
    geral = now - _ULTIMA_GERAL > 30.0  # cheap safety net
    if geral:
        _ULTIMA_GERAL = now
        _garantir_indicador()
    for area in win.screen.areas:
        for r in area.regions:
            if r.width <= 1 or r.height <= 1:
                continue
            ptr = r.as_pointer()
            if geral or ptr not in TILES:
                _READ_AT.pop(ptr, None)
                r.tag_redraw()
            elif ptr in _ADIADOS:
                _READ_AT.pop(ptr, None)  # free it to be read on the next drawing
                r.tag_redraw()
    _ADIADOS.clear()


def compose(win):
    """Build the window frame from the pixels the panels handed over.
    Returns (RGB frame, (width, height)) or None."""
    regions = {}
    for area in win.screen.areas:
        for r in area.regions:
            if r.width > 1 and r.height > 1:
                regions[r.as_pointer()] = (area, r)
    for ptr in list(TILES):
        if ptr not in regions:
            del TILES[ptr]  # panel closed or layout changed
    for ptr in list(_SCENE_TILES):
        if ptr not in regions:
            del _SCENE_TILES[ptr]
            _V3D_SIG.pop(ptr, None)
            _V3D_NEXT.pop(ptr, None)
            _V3D_PESADO.pop(ptr, None)
            off = _OFFSCREENS.pop(ptr, (None, 0, 0))[0]
            if off is not None:
                _APOSENTADOS.append(off)
    usable = [(regions[p][0], regions[p][1], t) for p, t in TILES.items() if p in regions]
    if not usable:
        return None

    # The frame size comes from the EDITORS, not from the panels that happened
    # to draw now: that way it doesn't fluctuate (a floating panel that shows up
    # must not change the resolution and chop the video into several files).
    # The top bar (menus/workspaces) and the status bar are not exposed to
    # Python and naturally stay outside this rectangle.
    areas = [a for a in win.screen.areas]
    x0 = min(a.x for a in areas)
    y0 = min(a.y for a in areas)
    x1 = max(a.x + a.width for a in areas)
    y1 = max(a.y + a.height for a in areas)

    # The scale comes from the snapshots themselves, not from Blender's DPI preference.
    # With two monitors of different densities, the global pixel_size may
    # say 2 while the window draws at 1. That's what scrambled the videos
    # of Nov 2, 2026 made on the laptop screen: each panel came out at half
    # size, shrunk into the bottom corner, and the object was black because the
    # rendered scene didn't match the overlay layer.
    medidas = {}
    for _a, _r, _t in usable:
        if _r.width > 1:
            k = round(_t["px"].shape[1] / _r.width, 3)
            medidas[k] = medidas.get(k, 0) + 1
    dense = max(medidas, key=medidas.get) if medidas else pixel_density()
    cw = max(2, int(round((x1 - x0) * dense)))
    ch = max(2, int(round((y1 - y0) * dense)))
    canvas = np.zeros((ch, cw, 3), dtype=np.uint8)

    agora = time.monotonic()
    for area, r, tile in usable:
        px = tile["px"]
        if tile.get("v3d") and px.shape[2] == 4:
            # Overlays on top of the rendered scene, using the overlay's alpha.
            cena = scene_tile(area, r, agora, px.shape[1], px.shape[0])
            if cena is not None:
                px = blend_overlay(esticar(cena, px.shape[1], px.shape[0]), px)
            else:
                px = px[:, :, :3]
        else:
            px = px[:, :, :3]
        th, tw = px.shape[0], px.shape[1]
        dx = int(round((r.x - x0) * dense))
        dy = int(round((r.y - y0) * dense))
        if dx >= cw or dy >= ch or dx < 0 or dy < 0:
            continue  # panel outside the rectangle (e.g. invalid coordinate)
        tw = min(tw, cw - dx)
        th = min(th, ch - dy)
        if tw > 0 and th > 0:
            canvas[dy:dy + th, dx:dx + tw] = px[:th, :tw]

    return canvas[::-1], (cw, ch)  # OpenGL draws bottom to top


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

def _close_stream(q, thread, proc, log_path, path=None, seg=None, min_frames=0):
    """Close the ffmpeg of a video, waiting for the last frames to go in."""
    if q is not None and thread is not None:
        try:
            q.put(None, timeout=5)
        except Exception:
            pass
        thread.join(timeout=30)
    if proc is not None:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=60)
        except Exception:
            proc.kill()
    if log_path and Path(log_path).exists():
        try:
            if Path(log_path).stat().st_size == 0:
                Path(log_path).unlink()
        except Exception:
            pass
    # Lightning session: too little work time, the video is discarded.
    if path is not None and seg is not None and min_frames > 0 and seg["n"] < min_frames:
        for f in (path, log_path):
            try:
                if f is not None and Path(f).exists():
                    Path(f).unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# External engine: the window captured by the system itself
# ---------------------------------------------------------------------------
#
# Reading the panels from inside Blender has a limit: Blender draws the
# content of the 3D viewport and the image editor into textures Python can't
# see, and only hands over the interface layer on top. For the viewport I
# worked around it by re-rendering the scene, which is where the hangs came from; for
# the image editor there is no workaround at all: it came out 97% black.
#
# Asking macOS for the window gives exactly what you see: UV editor, Ucupaint,
# shader editor, Material Preview, Cycles. And it comes from a separate process, so
# none of it can crash Blender.

HELPER = Path(__file__).with_name("sr_helper.py")


def _python_do_blender():
    """The interpreter that ships with Blender: always there, nothing to install."""
    v = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for c in (Path(sys.prefix) / "bin" / v, Path(sys.prefix) / "bin" / "python3",
              Path(sys.prefix) / "python.exe"):
        if c.exists():
            return str(c)
    from shutil import which
    return which("python3") or which("python")


class MotorExterno:
    """Takes care of the program that records the window from outside Blender."""

    def __init__(self):
        self.proc = None
        self.estado = {}
        self.motivo = ""
        self.assinatura_cfg = None
        base = Path(tempfile.gettempdir())
        self.cfg = base / f"session_recorder_cfg_{os.getpid()}.json"
        self.status = base / f"session_recorder_status_{os.getpid()}.json"

    def suportado(self):
        return sys.platform == "darwin" and HELPER.exists()

    # -- configuration -----------------------------------------------------
    def _dados(self, prefs, parar=False):
        pasta = bpy.path.abspath(prefs.base_folder or default_folder())
        return {
            "pid": os.getpid(), "folder": pasta, "stop": parar,
            "enabled": bool(prefs.enabled), "interval": float(prefs.interval),
            "fps": int(prefs.fps), "crf": int(prefs.crf),
            "max_width": int(prefs.max_width),
            "idle_timeout": float(prefs.idle_timeout),
            "min_use_minutes": float(prefs.min_use_minutes),
            "split_on_resize": bool(prefs.split_on_resize),
            "resolution_mode": prefs.resolution_mode,
            "width": int(prefs.width), "height": int(prefs.height),
            "ffmpeg": prefs.ffmpeg_path or "",
        }

    def _gravar_cfg(self, prefs, parar=False):
        dados = self._dados(prefs, parar)
        assinatura = json.dumps(dados, sort_keys=True)
        if assinatura == self.assinatura_cfg and not parar:
            return                       # nothing changed: don't touch the file
        self.assinatura_cfg = assinatura
        try:
            tmp = self.cfg.with_suffix(".tmp")
            tmp.write_text(assinatura)
            tmp.replace(self.cfg)        # atomic swap
        except Exception as e:
            print(f"[Session Recorder] could not write the configuration: {e}")

    # -- lifecycle ---------------------------------------------------------
    def testar(self, prefs):
        """Run a test capture. Returns (succeeded, reason)."""
        if not self.suportado():
            return False, "only works on macOS for now"
        py = _python_do_blender()
        if not py:
            return False, "could not find Blender's Python interpreter"
        self._gravar_cfg(prefs)
        try:
            p = subprocess.run([py, str(HELPER), "--config", str(self.cfg),
                                "--status", str(self.status), "--probe"],
                               capture_output=True, text=True, timeout=40)
            saida = json.loads((p.stdout or "{}").strip().splitlines()[-1])
        except Exception as e:
            return False, f"the capture test failed: {e}"
        if saida.get("ok"):
            return True, ""
        motivo = saida.get("motivo", "unknown reason")
        if saida.get("permissao"):
            motivo = ("macOS doesn't allow Blender to capture the screen. Turn it on in System "
                      "Settings, Privacy & Security, Screen Recording, and reopen Blender")
        return False, motivo

    def iniciar(self, prefs):
        py = _python_do_blender()
        self._gravar_cfg(prefs)
        try:
            self.status.unlink()
        except Exception:
            pass
        try:
            # The program's errors go to a file next to the videos: if it
            # ever dies, you can find out why without reproducing anything.
            pasta = Path(bpy.path.abspath(prefs.base_folder or default_folder()))
            try:
                pasta.mkdir(parents=True, exist_ok=True)
                registro = open(pasta / "session_recorder.log", "ab")
            except Exception:
                registro = subprocess.DEVNULL
            self.proc = subprocess.Popen(
                [py, str(HELPER), "--config", str(self.cfg), "--status", str(self.status)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=registro, start_new_session=True)
        except Exception as e:
            self.proc = None
            self.motivo = f"could not start the capture: {e}"
            return False
        return True

    def rodando(self):
        return self.proc is not None and self.proc.poll() is None

    def atualizar(self, prefs):
        """Called by the timer: only writes the configuration if something changed, and reads the state."""
        self._gravar_cfg(prefs)
        try:
            self.estado = json.loads(self.status.read_text())
        except Exception:
            pass
        return self.estado

    def parar(self):
        if self.proc is None:
            return
        try:
            prefs = get_prefs()
            if prefs is not None:
                self._gravar_cfg(prefs, parar=True)
        except Exception:
            pass
        # I don't hold Blender's shutdown waiting: the program runs on its own and
        # notices by itself that Blender quit, so it finishes the video properly
        # even if I leave now. I only wait a moment for the common case.
        saiu = True
        try:
            self.proc.wait(timeout=8)
        except Exception:
            saiu = False
        self.proc = None
        if saiu:
            for f in (self.cfg, self.status):
                try:
                    f.unlink()
                except Exception:
                    pass


EXTERNO = MotorExterno()


class Recorder:
    def __init__(self):
        self._reset()

    def _reset(self):
        self.started_at = None
        self.externo = False
        self.proc = None
        self.queue = None
        self.thread = None
        self.video_path = None
        self.log_path = None
        self.size = (0, 0)
        self.src_size = None
        self.pending_size = None
        self.seg = None
        self.min_frames = 0
        self.last_sig = None
        self.last_mark = None
        self.last_activity = 0.0
        self.idle = False
        self.segments = 0
        self.last_capture = 0.0
        self.frames = 0
        self.failed = False
        self.status = "Stopped"

    # -- lifecycle ---------------------------------------------------------
    def start_session(self):
        if self.started_at is not None:
            return
        self._reset()
        self.started_at = datetime.now()
        prefs = get_prefs()
        modo = getattr(prefs, "backend", 'AUTO') if prefs else 'AUTO'

        # First try to record the window through the system: it's the only way to get
        # the UV editor, Ucupaint and Material Preview as they really are,
        # and it touches nothing inside Blender.
        if modo in {'AUTO', 'JANELA'} and prefs is not None and EXTERNO.suportado():
            deu, motivo = EXTERNO.testar(prefs)
            if deu and EXTERNO.iniciar(prefs):
                self.externo = True
                self.status = "Recording the window"
                if not bpy.app.timers.is_registered(_tick):
                    bpy.app.timers.register(_tick, first_interval=TICK, persistent=True)
                return
            EXTERNO.motivo = motivo or EXTERNO.motivo
            if modo == 'JANELA':
                self.failed = True
                self.status = "Could not record the window"
                print(f"[Session Recorder] {EXTERNO.motivo}")
                return
            print(f"[Session Recorder] recording through the panels: {EXTERNO.motivo}")

        self.externo = False
        self.status = "Waiting for the first frame"
        _register_handlers()
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=TICK, persistent=True)

    def stop_session(self):
        try:
            if bpy.app.timers.is_registered(_tick):
                bpy.app.timers.unregister(_tick)
        except Exception:
            pass
        if self.externo:
            EXTERNO.parar()
        else:
            _close_stream(self.queue, self.thread, self.proc, self.log_path,
                          self.video_path, self.seg, self.min_frames)
        self._reset()

    def rotate(self):
        """Finish the current video and get everything ready to open the next one."""
        threading.Thread(
            target=_close_stream,
            args=(self.queue, self.thread, self.proc, self.log_path,
                  self.video_path, self.seg, self.min_frames),
            daemon=True).start()
        self.queue = self.thread = self.proc = self.log_path = None
        self.video_path = None
        self.seg = None
        self.size = (0, 0)
        self.src_size = None
        self.segments += 1
        self.frames = 0
        self.last_sig = None
        self.last_mark = None

    def fail(self, msg):
        self.failed = True
        self.status = msg
        print(f"[Session Recorder] {msg}")

    # -- ffmpeg ------------------------------------------------------------
    def _open_ffmpeg(self, prefs, src_w, src_h):
        ffmpeg = find_ffmpeg(prefs.ffmpeg_path)
        if not ffmpeg:
            self.fail("ffmpeg not found: install it or set its path in the preferences")
            return False

        now = datetime.now()
        folder = Path(bpy.path.abspath(prefs.base_folder or default_folder())) / now.strftime("%Y-%m-%d")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.fail(f"Could not create the folder {folder}: {e}")
            return False
        path = reserve_path(folder, now.strftime("%Y-%m-%d_%H-%M-%S"))

        w, h = output_size(prefs, src_w, src_h)
        fps = prefs.fps
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=area,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{src_w}x{src_h}",
            "-framerate", str(fps), "-i", "-",
            "-vf", vf,
            # slow preset: the add-on sends ~0.5 frames per second, so there is plenty
            # of CPU left to compress better. A keyframe every 2s of video.
            "-c:v", "libx264", "-preset", "slow", "-crf", str(prefs.crf), "-g", str(fps * 2),
            # Fragmented MP4: stays playable even if Blender is force-closed
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-flush_packets", "1",
            str(path),
        ]
        self.log_path = path.with_suffix(".log")
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            with open(self.log_path, "wb") as log:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=log, **kwargs)
        except Exception as e:
            self.fail(f"Failed to start ffmpeg: {e}")
            return False

        self.video_path = path
        self.size = (w, h)
        self.src_size = (src_w, src_h)
        self.seg = {"n": 0}
        self.min_frames = 0
        try:
            if prefs.min_use_minutes > 0 and prefs.interval > 0:
                self.min_frames = int(prefs.min_use_minutes * 60 / prefs.interval)
        except Exception:
            self.min_frames = 0
        self.queue = queue.Queue(maxsize=4)
        self.thread = threading.Thread(target=self._writer,
                                       args=(self.queue, self.proc, self.seg), daemon=True)
        self.thread.start()
        self.status = "Recording"
        return True

    def _writer(self, q, proc, seg):
        # Separate thread: writing to ffmpeg doesn't freeze the interface.
        while True:
            data = q.get()
            if data is None:
                return
            try:
                proc.stdin.write(data)
                seg["n"] += 1
                self.frames = seg["n"]
            except Exception as e:
                self.failed = True
                self.status = f"ffmpeg stopped: {e}"
                return

    # -- frames ------------------------------------------------------------
    def push(self, prefs, frame, size):
        # Cheap signature: samples the frame instead of reading millions of pixels.
        sig = zlib.crc32(np.ascontiguousarray(frame[::8, ::8]).tobytes())
        if sig == self.last_sig:
            return  # screen identical to the previous one: nothing happened
        self.last_sig = sig
        self.last_activity = time.monotonic()
        if self.idle:
            self.idle = False
            self.status = "Recording"

        if self.proc is not None and prefs.split_on_resize and size != self.src_size:
            # Only switch files when the new size is confirmed on the next
            # capture, so dragging the window edge doesn't create several videos.
            if self.pending_size == size:
                self.pending_size = None
                self.rotate()
            else:
                self.pending_size = size
                return
        else:
            self.pending_size = None

        if self.proc is None and not self._open_ffmpeg(prefs, *size):
            return
        if size != self.src_size:
            return  # size doesn't match the open ffmpeg: wait for the next one
        try:
            self.queue.put_nowait(np.ascontiguousarray(frame).tobytes())
        except queue.Full:
            pass  # ffmpeg lagging: drop the frame instead of freezing Blender


RECORDER = Recorder()


def _tick():
    rec = RECORDER
    if rec.started_at is None:
        return None
    if rec.failed:
        return 5.0

    # Recording through the window, Blender does no work at all: it only reads the state
    # the other program left in a small file, to show it in the interface.
    if rec.externo:
        try:
            prefs = get_prefs()
            if prefs is None:
                return 2.0
            if not EXTERNO.rodando():
                rec.failed = True
                rec.status = "Window recording stopped"
                return 5.0
            est = EXTERNO.atualizar(prefs)
            rec.status = est.get("status", rec.status)
            rec.video_path = est.get("video") or None
            rec.frames = int(est.get("frames", 0))
            rec.segments = int(est.get("segmentos", 0))
            rec.idle = bool(est.get("idle"))
            if est.get("falhou"):
                rec.failed = True
                EXTERNO.motivo = est.get("erro", "")
        except Exception as e:
            print(f"[Session Recorder] error reading the state: {e}")
        return 2.0

    try:
        prefs = get_prefs()
        if prefs is None or not prefs.enabled:
            return TICK
        _GAP[0] = max(0.5, prefs.interval * 0.9)

        now = time.monotonic()
        if rec.proc is not None and prefs.idle_timeout > 0 and rec.last_activity \
                and now - rec.last_activity > prefs.idle_timeout * 60:
            rec.rotate()  # finishes the video; a new one starts when you come back
            rec.idle = True
            rec.status = "On standby (no activity)"

        # On standby, check less often: it only needs to notice you came back.
        espera = max(prefs.interval, 5.0) if rec.idle else prefs.interval
        if now - rec.last_capture < espera:
            return TICK
        rec.last_capture = now

        win = biggest_window()
        if win is None:
            return TICK
        _pedir_redesenho(win, now)

        # No panel was re-read and the scene is the same? Then the screen didn't change,
        # and building the frame just to throw it away would be wasted work.
        marca = (_GERACAO[0], _SCENE_GEN[0], _view_marca(win))
        if marca == rec.last_mark:
            return TICK
        rec.last_mark = marca

        feito = compose(win)
        if feito is not None:
            rec.push(prefs, feito[0], feito[1])
    except Exception as e:
        print(f"[Session Recorder] erro no timer: {e}")
    return TICK


def _view_marca(win):
    """Summary of the 3D points of view: rotating the camera counts as a change."""
    marcas = []
    for area in win.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        rv3d = getattr(area.spaces.active, "region_3d", None)
        if rv3d is not None:
            marcas.append(tuple(round(v, 5) for row in rv3d.view_matrix for v in row))
    return tuple(marcas)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class SESSIONREC_OT_open_folder(bpy.types.Operator):
    bl_idname = "session_recorder.open_folder"
    bl_label = "Open recordings folder"

    def execute(self, context):
        prefs = get_prefs()
        folder = Path(bpy.path.abspath(prefs.base_folder or default_folder()))
        folder.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.path_open(filepath=str(folder))
        return {'FINISHED'}


class SESSIONREC_OT_toggle(bpy.types.Operator):
    bl_idname = "session_recorder.toggle"
    bl_label = "Pause / resume recording"

    def execute(self, context):
        prefs = get_prefs()
        prefs.enabled = not prefs.enabled
        return {'FINISHED'}


class SESSIONREC_Preferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    enabled: bpy.props.BoolProperty(name="Recording", default=True,
        description="Turn off to pause recording for this session")
    backend: bpy.props.EnumProperty(name="How to record", default='AUTO', items=[
        ('AUTO', "Automatic",
         "Records the window through the system when possible, and through the panels when not"),
        ('JANELA', "The window (macOS)",
         "Asks macOS for the window: captures everything you see, including UV editor, "
         "Ucupaint, shader editor and Material Preview. Requires the Screen Recording "
         "permission for Blender"),
        ('PAINEIS', "The panels (from inside)",
         "Builds the frame by reading panel by panel from inside Blender. Needs no "
         "permission, but can't see the image editor content and "
         "has to re-render the 3D scene separately"),
    ])
    base_folder: bpy.props.StringProperty(name="Base folder", subtype='DIR_PATH',
        default=default_folder())
    interval: bpy.props.FloatProperty(name="Interval (s)", default=2.0, min=1.0, max=60.0,
        description="Minimum time between captures. 2s + 30fps = 1h of work becomes 1 min of video")
    fps: bpy.props.IntProperty(name="Video FPS", default=30, min=1, max=60)
    resolution_mode: bpy.props.EnumProperty(name="Resolution", default='AUTO', items=[
        ('AUTO', "Automatic", "Uses the window size (limited by the maximum width)"),
        ('FIXED', "Fixed", "Always the same resolution, with black bars when the aspect ratio doesn't match"),
    ])
    split_on_resize: bpy.props.BoolProperty(name="New video when changing monitor", default=True,
        description="On: when the window moves to another screen or changes size, finishes the current "
                    "video and starts another at the new resolution, so each file has a single "
                    "resolution. Off: everything stays in the same file, fitting frames of another "
                    "size with bars where the aspect ratio doesn't match. Between an ultrawide and a "
                    "laptop screen the aspect ratio difference is large, so turning it off gives fewer "
                    "files, but with the laptop content small between bars")
    max_width: bpy.props.IntProperty(name="Maximum width", default=3840, min=640, max=7680,
        description="In automatic mode, the video is scaled down to fit this width. "
                    "Retina screens already come in at the interface's logical size")
    width: bpy.props.IntProperty(name="Width", default=1920, min=320, max=7680)
    height: bpy.props.IntProperty(name="Height", default=1080, min=240, max=4320)
    crf: bpy.props.IntProperty(name="Quality (CRF)", default=23, min=12, max=35,
        description="Lower = higher quality and larger file")
    idle_timeout: bpy.props.FloatProperty(name="Pause after (min)", default=5.0, min=0.0, max=120.0,
        description="With no change on screen for this long, the video is finished and recording "
                    "goes on standby. When you start working again, a new video begins. 0 disables it")
    heavy_interval: bpy.props.FloatProperty(name="Material Preview: every (s)",
        default=30.0, min=0.0, max=600.0,
        description="In Solid and Wireframe the scene is redrawn on every capture, with no risk. "
                    "Material Preview uses EEVEE, which is where Blender's hangs "
                    "came from, so there the scene is only redrawn at this interval. "
                    "0 disables it (safest: the object keeps its last known look). "
                    "In Rendered with Cycles it is never redrawn")
    min_use_minutes: bpy.props.FloatProperty(name="Discard below (min of use)", default=5.0,
        min=0.0, max=120.0,
        description="Videos with less work time than this are deleted when they are "
                    "finished, instead of becoming a file. 0 disables it")
    show_indicator: bpy.props.BoolProperty(name="Show REC in the status bar", default=True)
    ffmpeg_path: bpy.props.StringProperty(name="ffmpeg (optional)", subtype='FILE_PATH',
        description="Leave empty to detect automatically")

    def draw(self, context):
        layout = self.layout
        rec = RECORDER
        box = layout.box()
        ligado = rec.proc is not None or rec.externo
        box.label(text=f"Status: {rec.status}", icon='RECORD_ON' if ligado else 'INFO')
        box.label(text="Recording: the whole window, through the system" if rec.externo
                       else "Recording: panel by panel, from inside Blender")
        if rec.video_path:
            box.label(text=f"File: {rec.video_path}")
            box.label(text=f"Frames in this video: {rec.frames}  |  "
                           f"Videos in this session: {rec.segments + 1}")
        if not rec.externo and EXTERNO.motivo:
            aviso = box.box()
            aviso.label(text="Could not record the window:", icon='ERROR')
            for pedaco in textwrap.wrap(EXTERNO.motivo, 78):
                aviso.label(text=pedaco)
        ff = find_ffmpeg(self.ffmpeg_path)
        box.label(text=f"ffmpeg: {ff or 'NOT FOUND'}", icon='CHECKMARK' if ff else 'ERROR')

        col = layout.column()
        col.prop(self, "enabled")
        col.prop(self, "backend")
        col.label(text="Changing this takes effect the next time Blender starts.", icon='INFO')
        col.prop(self, "base_folder")
        row = col.row(align=True)
        row.prop(self, "interval")
        row.prop(self, "fps")
        col.prop(self, "split_on_resize")
        col.prop(self, "resolution_mode", expand=True)
        if self.resolution_mode == 'AUTO':
            col.prop(self, "max_width")
        else:
            row = col.row(align=True)
            row.prop(self, "width")
            row.prop(self, "height")
        col.prop(self, "crf")
        col.prop(self, "idle_timeout")
        col.prop(self, "min_use_minutes")
        if not RECORDER.externo:
            col.prop(self, "heavy_interval")
        col.prop(self, "show_indicator")
        col.prop(self, "ffmpeg_path")
        col.label(text="Folder, resolution, FPS and quality take effect from the next video.",
                  icon='INFO')
        col.operator(SESSIONREC_OT_open_folder.bl_idname, icon='FILE_FOLDER')


def _draw_status(self, context):
    prefs = get_prefs()
    if prefs is None or not prefs.show_indicator or RECORDER.started_at is None:
        return
    if RECORDER.failed:
        self.layout.label(text="REC error", icon='ERROR')
    elif not prefs.enabled:
        self.layout.operator(SESSIONREC_OT_toggle.bl_idname, text="REC paused",
                             icon='PAUSE', emboss=False)
    elif RECORDER.idle:
        self.layout.operator(SESSIONREC_OT_toggle.bl_idname, text="REC on standby",
                             icon='SORTTIME', emboss=False)
    else:
        self.layout.operator(SESSIONREC_OT_toggle.bl_idname, text="REC",
                             icon='RECORD_ON', emboss=False)


def _garantir_indicador():
    """Hang the REC indicator on the status bar, if it isn't there yet.

    Calling this only in register() isn't enough: on a cold Blender start the
    extension is registered before the status bar finishes building, and
    whatever was hung there gets lost. So the check is redone after
    Blender has opened and, from time to time, during the session."""
    try:
        funcs = getattr(bpy.types.STATUSBAR_HT_header.draw, "_draw_funcs", None)
        if funcs is None or _draw_status not in funcs:
            bpy.types.STATUSBAR_HT_header.append(_draw_status)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (SESSIONREC_OT_open_folder, SESSIONREC_OT_toggle, SESSIONREC_Preferences)
_atexit_registered = False


def _deferred_start():
    _garantir_indicador()
    if not bpy.app.background:  # don't record on a render farm / command line
        RECORDER.start_session()
    return None


def register():
    global _atexit_registered
    for c in classes:
        bpy.utils.register_class(c)
    _garantir_indicador()
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    bpy.app.timers.register(_deferred_start, first_interval=1.0, persistent=True)
    if not _atexit_registered:
        atexit.register(RECORDER.stop_session)  # finishes the video when Blender closes
        _atexit_registered = True


def unregister():
    RECORDER.stop_session()
    _unregister_handlers()
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    try:
        bpy.types.STATUSBAR_HT_header.remove(_draw_status)
    except Exception:
        pass
    if bpy.app.timers.is_registered(_deferred_start):
        bpy.app.timers.unregister(_deferred_start)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
