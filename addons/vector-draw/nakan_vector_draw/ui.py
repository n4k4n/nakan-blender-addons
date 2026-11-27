"""Modal operator, viewport drawing and the sidebar panel."""

import math

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import geom, modegate, pen

ACCENT = (0.1, 0.75, 1.0, 1.0)
SNAPC = (1.0, 0.8, 0.1, 1.0)
HANDLE = (1.0, 0.45, 0.2, 0.9)
WHITE = (1.0, 1.0, 1.0, 1.0)
DARK = (0.05, 0.05, 0.06, 0.9)

NAV = {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'WHEELINMOUSE', 'WHEELOUTMOUSE', 'TRACKPADPAN',
       'TRACKPADZOOM', 'MOUSEROTATE', 'MOUSESMARTZOOM', 'NDOF_MOTION', 'HOME', 'ACCENT_GRAVE', 'N', 'T',
       'NUMPAD_0', 'NUMPAD_1', 'NUMPAD_2', 'NUMPAD_3', 'NUMPAD_4', 'NUMPAD_5', 'NUMPAD_6', 'NUMPAD_7',
       'NUMPAD_8', 'NUMPAD_9', 'NUMPAD_PERIOD', 'NUMPAD_PLUS', 'NUMPAD_MINUS', 'NUMPAD_SLASH',
       'NUMPAD_ASTERIX', 'TIMER', 'TIMER_REPORT', 'WINDOW_DEACTIVATE'}

HELP = {
    'PEN': ("Click corner \u00b7 Drag smooth \u00b7 Alt convert/break \u00b7 Ctrl direct select \u00b7 Shift 45\u00b0 \u00b7 "
            "Shift+click a point = new path from it \u00b7 hover a snap then move = tracking \u00b7 "
            "1st point/C close \u00b7 Enter finish \u00b7 Backspace undo \u00b7 D osnap \u00b7 Type 3.5  <45  x,y  R1,0 \u00b7 "
            "P pen  A direct select  M rect  L circle  Y polygon \u00b7 Tab CPlane \u00b7 F pick CPlane (also mid-curve) \u00b7 Shift+F stand up on last segment \u00b7 Shift+M mirror \u00b7 S Osnap \u00b7 "
            "Q/Shift+Q/Alt+Q turn CPlane \u00b7 Ctrl+drag box select \u00b7 K keyframe \u00b7 \u2190\u2192 frame  \u2191\u2193 keyframe"),
    'EDIT': ("Drag anchor / handle / segment \u00b7 click anchor or handle = select \u00b7 Shift+click add \u00b7 "
             "Ctrl+drag box \u00b7 G move  R rotate  S scale the selection (type a value, Shift steps) \u00b7 "
             "Alt convert/break \u00b7 X/Del delete point \u00b7 "
             "P pen  A direct select  M rect  L circle  Y polygon \u00b7 Enter/Esc leave"),
    'RECT': ("Click/drag corner to corner \u00b7 Shift square \u00b7 Alt from centre \u00b7 Type w,h \u00b7 "
             "P pen  A direct select  M rect  L circle  Y polygon \u00b7 Tab CPlane \u00b7 Enter/Esc cancel/leave \u00b7 S Osnap"),
    'CIRCLE': ("Click/drag corner to corner (inscribed in the square) \u00b7 Alt from centre \u00b7 Type the radius \u00b7 "
               "P pen  A direct select  M rect  L circle  Y polygon \u00b7 Tab CPlane \u00b7 S Osnap"),
    'POLY': ("Click/drag corner to corner (inscribed) \u00b7 Alt from centre (Shift flat edge) \u00b7 Up/Down sides \u00b7 Type the radius \u00b7 "
             "P pen  A direct select  M rect  L circle  Y polygon \u00b7 Tab CPlane \u00b7 S Osnap"),
}


# ----------------------------------------------------------------- drawing helpers
def _lines2d(pts, col, width=1.5, strip=False):
    if len(pts) < 2:
        return
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    vp = gpu.state.viewport_get()
    sh.uniform_float("viewportSize", (vp[2], vp[3]))
    sh.uniform_float("lineWidth", width)
    sh.uniform_float("color", col)
    batch_for_shader(sh, 'LINE_STRIP' if strip else 'LINES', {"pos": [(p[0], p[1], 0) for p in pts]}).draw(sh)


def _tris2d(pts, col):
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    sh.uniform_float("color", col)
    batch_for_shader(sh, 'TRIS', {"pos": [(p[0], p[1]) for p in pts]}).draw(sh)


def _square(c, r, fill, border=DARK, bw=1.5):
    x, y = c
    _tris2d([(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y - r), (x + r, y + r), (x - r, y + r)], border)
    r2 = r - bw
    _tris2d([(x - r2, y - r2), (x + r2, y - r2), (x + r2, y + r2), (x - r2, y - r2), (x + r2, y + r2), (x - r2, y + r2)], fill)


def _circle(c, r, n=20):
    return [(c[0] + r * math.cos(2 * math.pi * k / n), c[1] + r * math.sin(2 * math.pi * k / n)) for k in range(n + 1)]


SNAP_TEXT = 15        # size of the Osnap labels (End, Mid, Vertex...)


def _marker(kind, c, r=7):
    x, y = c
    col = SNAPC
    if kind in ("END", "VERT"):
        _lines2d([(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y + r), (x - r, y - r)], col, 2, True)
    elif kind == "MID":
        _lines2d([(x - r, y - r * 0.7), (x + r, y - r * 0.7), (x, y + r), (x - r, y - r * 0.7)], col, 2, True)
    elif kind == "CEN":
        _lines2d(_circle(c, r), col, 2, True)
    elif kind == "INT":
        _lines2d([(x - r, y - r), (x + r, y + r), (x - r, y + r), (x + r, y - r)], col, 2.5)
    elif kind == "PERP":
        _lines2d([(x - r, y - r), (x + r, y - r), (x, y - r), (x, y + r), (x - r, y - r + 5), (x - r + 5, y - r + 5),
                  (x - r + 5, y - r + 5), (x - r + 5, y - r)], col, 2)
    elif kind == "TRACK":
        _lines2d([(x - r, y - r), (x + r, y + r), (x - r, y + r), (x + r, y - r)], (0.3, 0.9, 0.45, 1.0), 2.0)
    elif kind == "AXIS":       # two arrows facing each other: the mirror line
        _lines2d([(x, y - r - 2), (x, y + r + 2)], col, 2)
        _lines2d([(x - r - 3, y - r * 0.6), (x - 3, y), (x - r - 3, y + r * 0.6), (x - r - 3, y - r * 0.6)], col, 2, True)
        _lines2d([(x + r + 3, y - r * 0.6), (x + 3, y), (x + r + 3, y + r * 0.6), (x + r + 3, y - r * 0.6)], col, 2, True)
    elif kind == "NEAR":
        _lines2d([(x, y - r), (x + r, y), (x, y + r), (x - r, y), (x, y - r)], col, 2, True)
    else:   # GRID
        _lines2d([(x - r, y), (x + r, y), (x, y - r), (x, y + r)], col, 1.5)


def _dashed(a, b, col, dash=6.0, gap=4.0, width=1.3):
    d = Vector((b[0] - a[0], b[1] - a[1]))
    L = d.length
    if L < 1e-6:
        return
    d /= L
    pts = []
    t = 0.0
    while t < L and len(pts) < 2000:
        e = min(t + dash, L)
        pts += [(a[0] + d.x * t, a[1] + d.y * t), (a[0] + d.x * e, a[1] + d.y * e)]
        t = e + gap
    _lines2d(pts, col, width)


def _text(txt, x, y, col=WHITE, size=12, bg=True):
    ui = bpy.context.preferences.view.ui_scale
    blf.size(0, size * ui)
    w, h = blf.dimensions(0, txt)
    if bg:
        pad = 4 * ui
        _tris2d([(x - pad, y - pad), (x + w + pad, y - pad), (x + w + pad, y + h + pad),
                 (x - pad, y - pad), (x + w + pad, y + h + pad), (x - pad, y + h + pad)], (0.05, 0.05, 0.06, 0.75))
    blf.color(0, *col)
    blf.position(0, x, y, 0)
    blf.draw(0, txt)
    return w, h


# ----------------------------------------------------------------- draw callbacks
def _shown(S):
    """Paths whose anchors/handles are drawn: the one being drawn, the one under the mouse and
    the last one edited (like Illustrator shows only the selected path)."""
    out = set()
    if S.tool not in pen.PATH_TOOLS:
        return out
    if S.active is not None:
        out.add(S.active)
    if S.hover is not None:
        out.add(S.hover[1])
    if S.drag is not None:
        if S.drag.get("kind") == "box":
            return set(range(len(S.paths)))         # while boxing, every anchor is visible
        if "path" in S.drag:
            out.add(S.drag["path"])
    out |= {pi for pi, _i in getattr(S, "sel", ())}
    if getattr(S, "focus", None) is not None:
        out.add(S.focus)
    try:
        so = set(S.ctx.selected_objects)
        out |= {pi for pi, P in enumerate(S.paths) if P.ob in so}
    except (AttributeError, ReferenceError):
        pass
    for pi, P in enumerate(S.paths):
        if P.ob is S.target and S.target is not None:
            out.add(pi)
    return out


def draw_3d(op, context):
    S = op.session
    if S is None or context.region != op.region:
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    sh.uniform_float("viewportSize", (context.region.width, context.region.height))
    pv = S.preview()
    shp, _info = S.shape_preview()
    if len(shp) > 1:
        sh.uniform_float("lineWidth", 2.0)
        sh.uniform_float("color", ACCENT)
        batch_for_shader(sh, 'LINE_STRIP', {"pos": shp}).draw(sh)
    if len(pv) > 1:
        sh.uniform_float("lineWidth", 2.0)
        sh.uniform_float("color", ACCENT)
        batch_for_shader(sh, 'LINE_STRIP', {"pos": pv}).draw(sh)
    mstrips, maxis = S.mirror_preview()
    if maxis:
        sh.uniform_float("lineWidth", 1.0)
        sh.uniform_float("color", (ACCENT[0], ACCENT[1], ACCENT[2], 0.35))
        batch_for_shader(sh, 'LINES', {"pos": maxis}).draw(sh)
    for strip in mstrips:
        if len(strip) > 1:
            sh.uniform_float("lineWidth", 1.6)
            sh.uniform_float("color", (ACCENT[0], ACCENT[1], ACCENT[2], 0.55))
            batch_for_shader(sh, 'LINE_STRIP', {"pos": strip}).draw(sh)
    lines = []
    shown = _shown(S)
    for pi, P in enumerate(S.paths):
        if pi not in shown:
            continue
        for p in P.pts:
            if p.kind == pen.CORNER:
                continue
            for h in (p.hl, p.hr):
                if (h - p.co).length > 1e-9:
                    lines += [p.co, h]
    if lines:
        sh.uniform_float("lineWidth", 1.2)
        sh.uniform_float("color", HANDLE)
        batch_for_shader(sh, 'LINES', {"pos": lines}).draw(sh)
    hv = S.hover
    if hv is not None and hv[0] == "seg" and S.state == "IDLE":
        sm = geom.bez_samples(*S.paths[hv[1]].seg(hv[2]), 24)
        sh.uniform_float("lineWidth", 4.0)
        sh.uniform_float("color", (ACCENT[0], ACCENT[1], ACCENT[2], 0.6))
        batch_for_shader(sh, 'LINE_STRIP', {"pos": sm}).draw(sh)
    gpu.state.blend_set('NONE')


def _hint(S, ev_alt, ev_ctrl=False):
    if getattr(S, "face_pick", False):
        return "click a point, an edge or a face"
    if S.drag is None and getattr(S, "hover_widget", None) is not None:
        hw = S.hover_widget
        return ("drag: change the radius" if hw[2] == "arc" else "drag: round corner") + " \u00b7 Ctrl: grid steps"
    if S.tool not in pen.PATH_TOOLS:
        if S.anchor is None:
            return {'RECT': "first corner (Alt: centre)", 'CIRCLE': "first corner (Alt: centre)",
                    'POLY': "first corner (Alt: centre) \u00b7 %d sides" % S.st.poly_sides}[S.tool]
        return ""
    hv = S.hover
    if S.drag is not None:
        k = S.drag.get("kind")
        return {"seg": "bending the segment", "convert": "pulling new handles",
                "new": "Alt: break \u00b7 Shift: 45\u00b0 \u00b7 Space: move point",
                "box": "selecting (Shift: add)", "round": "rounding (Ctrl: grid steps)"}.get(k, "")
    if ev_ctrl:
        if hv is None:
            return "Ctrl: drag a box to select points"
        if hv[0] == "pt" and (hv[1], hv[2]) in getattr(S, "sel", ()) and len(S.sel) > 1:
            return "drag: move the %d selected points" % len(S.sel)
        return {"pt": "drag: move \u00b7 click: cusp \u2194 tangent", "seg": "drag: reshape segment",
                "hl": "drag handle", "hr": "drag handle"}[hv[0]]
    if ev_alt:
        if hv is None:
            return "Alt: convert"
        return {"pt": "drag: new handles \u00b7 click: corner", "seg": "drag: bend \u00b7 click: straight \u2194 curved",
                "hl": "drag: break handle", "hr": "drag: break handle"}[hv[0]]
    if S.state == "DRAW":
        if hv and hv[0] == "pt" and hv[1] == S.active:
            P = S.paths[S.active]
            if hv[2] == 0 and len(P.pts) >= 3:
                return "close"
            if hv[2] == len(P.pts) - 1:
                return "click: drop handle \u00b7 drag: new direction"
        return ""
    if hv is None:
        return "new curve"
    if hv[0] == "pt":
        P = S.paths[hv[1]]
        if not P.cyclic and hv[2] in (0, len(P.pts) - 1):
            return "click: continue \u00b7 drag: move"
        return "click: delete \u00b7 drag: move"
    if hv[0] == "seg":
        return "add point"
    return "drag handle"


def _draw_menu(S, context):
    ui = S.ui
    x0, y0, x1, y1 = S.menu_rect()
    _tris2d([(x0 - 1, y0 - 1), (x1 + 1, y0 - 1), (x1 + 1, y1 + 1), (x0 - 1, y0 - 1), (x1 + 1, y1 + 1),
             (x0 - 1, y1 + 1)], (0.0, 0.0, 0.0, 0.5))
    _tris2d([(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)], (0.13, 0.13, 0.15, 0.97))
    _text("Snap", x0 + 14 * ui, y1 - 27 * ui, SNAPC, 17, bg=False)
    _text("A all \u00b7 X none", x1 - 130 * ui, y1 - 26 * ui, (0.6, 0.6, 0.65, 1), 13, bg=False)
    _text("name = once  \u00b7  box = always", x0 + 14 * ui, y1 - 49 * ui, (0.6, 0.6, 0.65, 1), 13, bg=False)
    st = S.st
    hot, part = S.menu.get("hot"), S.menu.get("part")
    bw = pen.MENU_BOX * ui
    for it, a0, b0, a1, b1 in S.menu_rows():
        if it is None:
            ym = (b0 + b1) / 2
            _lines2d([(a0 + 10 * ui, ym), (a1 - 10 * ui, ym)], (0.32, 0.32, 0.36, 1.0), 1)
            continue
        prop, label, key = it
        on = bool(getattr(st, prop))
        if getattr(S, "once", None) == prop:
            _tris2d([(a0, b0), (a1, b0), (a1, b1), (a0, b0), (a1, b1), (a0, b1)], (0.85, 0.55, 0.15, 0.35))
        if hot == prop and part == "name":
            # the name under the mouse: a lighter band (click = once)
            n0 = a0 + bw
            _tris2d([(n0, b0), (a1, b0), (a1, b1), (n0, b0), (a1, b1), (n0, b1)], (1.0, 1.0, 1.0, 0.13))
        dim = prop.startswith("snap_") and prop not in ("snap_project", "snap_track", "snap_axis") and not st.osnap
        cx, cy, r = a0 + bw * 0.5, (b0 + b1) / 2, 9.0 * ui
        box_hot = hot == prop and part == "box"
        edge = (0.85, 0.85, 0.9, 1) if box_hot else (0.48, 0.48, 0.52, 1)
        fill = ACCENT if on else ((0.3, 0.3, 0.34, 1) if box_hot else (0.19, 0.19, 0.22, 1))
        _square((cx, cy), r, fill, edge, 1.0)
        if on:
            _lines2d([(cx - r * 0.5, cy), (cx - r * 0.12, cy - r * 0.42), (cx + r * 0.55, cy + r * 0.45)],
                     (1.0, 1.0, 1.0, 1.0), 2.0, True)
        col = (0.55, 0.55, 0.58, 1) if dim else WHITE
        _text(label, a0 + bw + 6 * ui, cy - 7 * ui, col, 17, bg=False)
        _text(key, a1 - 28 * ui, cy - 7 * ui, SNAPC if on else (0.6, 0.6, 0.65, 1), 15, bg=False)


def draw_2d(op, context):
    S = op.session
    if S is None or context.region != op.region or S.view is None:
        return
    gpu.state.blend_set('ALPHA')
    # project with the view as it is right now (not the one of the last mouse event), so the points
    # stay on the curve while the view is orbited, panned or zoomed
    rv3d = context.region_data
    V = geom.View.from_context(context.region, rv3d) if rv3d is not None else S.view
    hv = S.hover
    shown = _shown(S)
    for pi, P in enumerate(S.paths):
        if pi not in shown:
            continue
        for i, p in enumerate(P.pts):
            s = V.project(p.co)
            if s is None:
                continue
            hot = hv is not None and hv[0] == "pt" and hv[1] == pi and hv[2] == i
            last = S.state == "DRAW" and pi == S.active and i == len(P.pts) - 1
            if (pi, i) in getattr(S, "sel", ()):
                _square(s, 7.5, (1.0, 0.62, 0.15, 1.0))           # selected: big orange
            else:
                _square(s, 6 if hot else 4.5, ACCENT if (hot or last) else WHITE)
            if p.kind != pen.CORNER:
                for side, h in (("hl", p.hl), ("hr", p.hr)):
                    if (h - p.co).length > 1e-9:
                        hs = V.project(h)
                        if hs is not None:
                            hh = hv is not None and hv[0] == side and hv[1] == pi and hv[2] == i
                            if getattr(S, "sel_handle", None) == (pi, i, side):
                                _square(hs, 5.0, (1.0, 0.62, 0.15, 1.0))   # selected handle
                            _lines2d(_circle(hs, 4.5 if hh else 3.2, 12), HANDLE, 2.5 if hh else 1.8, True)
    if S.drag is None and hasattr(S, "corner_widgets"):
        hw = getattr(S, "hover_widget", None)
        for key, w in S.corner_widgets():
            q = V.project(w)
            if q is None:
                continue
            hot = hw == key
            _lines2d(_circle(q, 5.0 if hot else 4.2, 14), ACCENT if hot else WHITE, 1.6, True)
            _square(q, 1.6, ACCENT if hot else WHITE, ACCENT if hot else WHITE)
    if S.drag is not None and S.drag.get("kind") == "round":
        q = V.project(S.drag["c"])
        if q is not None:
            _text("R %.4g" % S.drag.get("r", 0.0), q.x + 14, q.y - 24, SNAPC, SNAP_TEXT)
    if getattr(S, "box", None) is not None:
        (ax, ay), (bx, by) = S.box
        _tris2d([(ax, ay), (bx, ay), (bx, by), (ax, ay), (bx, by), (ax, by)], (0.3, 0.55, 0.95, 0.12))
        _lines2d([(ax, ay), (bx, ay), (bx, by), (ax, by), (ax, ay)], (0.45, 0.7, 1.0, 0.9), 1.2, True)
    mx, my = op.mouse
    pp = getattr(S, "pick_pts", None)
    if S.face_pick and pp:
        scr = [V.project(p) for p in pp]
        if S.cursor is not None:
            scr.append(V.project(S.cursor))
        scr = [q for q in scr if q is not None]
        if len(scr) >= 2:
            _lines2d([(q.x, q.y) for q in scr], (0.95, 0.35, 0.35, 0.95), 2.0, True)
        for k, q in enumerate(scr[:len(pp)]):
            _square(q, 5.5, (0.95, 0.35, 0.35, 1.0))
            _text(("origin", "X", "plane")[k], q.x + 10, q.y - 18, (0.95, 0.5, 0.5, 1.0), SNAP_TEXT, False)
    # tracking: acquired points (small crosses) and the guides lining the cursor up with them
    for a in getattr(S, "track_pts", ()):
        q = V.project(a)
        if q is not None:
            _lines2d([(q.x - 5, q.y), (q.x + 5, q.y), (q.x, q.y - 5), (q.x, q.y + 5)], (0.3, 0.9, 0.45, 0.9), 1.6)
    for a, b in getattr(S, "track_draw", ()):
        qa, qb = V.project(a), V.project(b)
        if qa is not None and qb is not None:
            _dashed(qa, qb, (0.3, 0.9, 0.45, 0.85))
    if S.snap is not None:
        _marker(S.snap.kind, S.snap.screen, 9)
        _text(S.snap.label + (" (once)" if getattr(S, "once", None) else ""), S.snap.screen.x + 14,
              S.snap.screen.y + 11, SNAPC, SNAP_TEXT)
    elif getattr(S, "once", None):
        _text("Once: " + S.once_label(), mx + 18 * context.preferences.view.ui_scale, my + 16, SNAPC, SNAP_TEXT)
    ui = context.preferences.view.ui_scale
    y = my - 30 * ui
    info = "" if S.menu is not None else S.readout()
    _shp, sinfo = S.shape_preview()
    if sinfo:
        info = sinfo
    locks = []
    if S.len_lock is not None:
        locks.append("L=%.4g" % S.len_lock)
    if S.ang_lock is not None:
        locks.append("∠=%.4g°" % S.ang_lock)
    if locks:
        info = ("%s   [%s]" % (info, " ".join(locks))).strip()
    if info:
        _text(info, mx + 18 * ui, y, WHITE, 12)
        y -= 20 * ui
    if S.buffer:
        _text("> " + S.buffer + "_", mx + 18 * ui, y, SNAPC, 13)
        y -= 20 * ui
    hint = "" if S.menu is not None else _hint(S, op.alt, getattr(op, 'ctrl', False))
    if hint:
        hy = my + (42 if S.snap is not None else 14) * ui     # above the Osnap label, never on it
        _text(hint, mx + 18 * ui, hy, ACCENT, 11)
    st = context.scene.nkdraw
    tag = "Vector Draw %s  ·  CPlane %s  ·  Ortho %s  ·  Osnap %s" % (
        pen.TOOL_NAMES[S.tool], st.cplane.title(), "ON" if st.ortho else "off", "ON" if st.osnap else "off")
    if getattr(op, "_tool_shown", None) != S.tool:
        op._tool_shown = S.tool
        try:
            context.workspace.status_text_set(HELP[S.tool])
        except Exception:      # noqa: BLE001
            pass
    if S.message:
        tag += "  ·  " + S.message
    _text(tag, 14 * ui, 14 * ui, WHITE, 12)
    if S.menu is not None:
        _draw_menu(S, context)
    gpu.state.blend_set('NONE')


# ----------------------------------------------------------------- operator
class NKDRAW_OT_pen(bpy.types.Operator):
    """Draw and edit Bezier curves like Rhino and Illustrator: osnaps, typed lengths and angles,
    ortho, CPlanes; click = corner, drag = smooth, Alt = break tangent"""
    bl_idname = "nkdraw.pen"
    bl_label = "Vector Pen"
    bl_options = {'REGISTER', 'UNDO'}

    tool: bpy.props.EnumProperty(name="Tool", default='PEN', items=[
        ('PEN', "Pen", "Draw and edit curves"),
        ('EDIT', "Direct Selection", "Edit the points of the curves on the plane without adding new ones"),
        ('RECT', "Rectangle", "Rectangle / square"),
        ('CIRCLE', "Circle", "Circle"), ('POLY', "Polygon", "Regular polygon")])

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                self.report({'WARNING'}, "Switch to Object Mode first")
                return {'CANCELLED'}
        self.region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if context.region is not None and context.region.type == 'WINDOW':
            self.region = context.region
        self.rv3d = context.area.spaces.active.region_3d
        self.session = pen.PenSession(context, context.scene.nkdraw, None, self.tool)
        self.session.view = geom.View.from_context(self.region, self.rv3d)
        self.session.ui = context.preferences.view.ui_scale
        self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
        self.alt = False
        args = (self, context)
        self._h3 = bpy.types.SpaceView3D.draw_handler_add(draw_3d, args, 'WINDOW', 'POST_VIEW')
        self._h2 = bpy.types.SpaceView3D.draw_handler_add(draw_2d, args, 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('CROSSHAIR')
        context.workspace.status_text_set(HELP[self.tool])
        self._tool_shown = self.tool
        self.session.message = ("Click to draw, or click any curve to edit it" if self.tool == 'PEN' else
                                "Drag points, handles or segments \u00b7 Ctrl+drag a box to select points"
                                if self.tool == 'EDIT' else pen.SHAPE_HINT[self.tool])
        return {'RUNNING_MODAL'}

    def _cleanup(self, context):
        for h in (getattr(self, "_h3", None), getattr(self, "_h2", None)):
            if h is not None:
                bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        self._h3 = self._h2 = None
        try:
            context.window.cursor_modal_restore()
            context.workspace.status_text_set(None)
        except (AttributeError, RuntimeError):
            pass
        if context.area:
            context.area.tag_redraw()

    def _inside(self, event):
        r = self.region
        return r.x <= event.mouse_x < r.x + r.width and r.y <= event.mouse_y < r.y + r.height

    def modal(self, context, event):
        S = self.session
        fr = context.scene.frame_current
        if getattr(self, "_frame", fr) != fr and S.drag is None:
            S.reload()                      # the timeline moved: the animation changed the points
        self._frame = fr
        if event.type in NAV or event.type.startswith("NDOF") or event.type.startswith("TRACKPAD"):
            return {'PASS_THROUGH'}
        if not self._inside(event) and S.drag is None:
            return {'PASS_THROUGH'}        # sidebar, header: the Osnap buttons stay usable
        self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
        self.alt = event.alt
        self.ctrl = event.ctrl
        view = geom.View.from_context(self.region, self.rv3d)
        ev = _Ev(event, self.mouse)
        try:
            r = S.handle(ev, view)
        except Exception as e:      # noqa: BLE001 — never leave the user stuck in a modal
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, "Vector Draw: %s" % e)
            r = 'FINISHED'
        if getattr(S, "warp", None) is not None:
            # G / R / S on a shape being dragged ended: put the mouse on the corner being drawn
            x, y = S.warp
            S.warp = None
            try:
                context.window.cursor_warp(self.region.x + x, self.region.y + y)
            except (AttributeError, RuntimeError, TypeError):
                pass
        if context.area:
            context.area.tag_redraw()
        if r == 'FINISHED':
            S.finish()
            self._cleanup(context)
            if S.target is not None:
                for o in context.selected_objects:
                    o.select_set(False)
                S.target.select_set(True)
                context.view_layer.objects.active = S.target
            return {'FINISHED'}
        if r == 'PASS':
            return {'RUNNING_MODAL'}       # swallow other keys: no accidental G/R/S on the scene
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        if self.session is not None:
            self.session.finish()
        self._cleanup(context)






def _draw_pick(op, context):
    if context.region != op.region:
        return
    gpu.state.blend_set('ALPHA')
    V = getattr(op, "view", None)
    if V is not None and context.region_data is not None:
        V = geom.View.from_context(context.region, context.region_data)
    if V is not None and op.pts:
        scr = [V.project(p) for p in op.pts + ([op.cur] if op.cur is not None else [])]
        scr = [q for q in scr if q is not None]
        if len(scr) >= 2:
            _lines2d([(q.x, q.y) for q in scr], (0.95, 0.35, 0.35, 0.95), 2.0, True)
        for k, q in enumerate(scr[:len(op.pts)]):
            _square(q, 5.5, (0.95, 0.35, 0.35, 1.0))
            _text(("origin", "X", "plane")[k], q.x + 10, q.y - 18, (0.95, 0.5, 0.5, 1.0), SNAP_TEXT, False)
    sn = getattr(op, "snap", None)
    if sn is not None:
        _marker(sn.kind, sn.screen, 9)
        _text(sn.label, sn.screen.x + 14, sn.screen.y + 11, SNAPC, SNAP_TEXT)
    gpu.state.blend_set('NONE')


class NKDRAW_OT_cplane_face(bpy.types.Operator):
    """Pick for the CPlane (and the grid): a point or an edge = origin there, rotation kept;
Shift+click an edge = turn the CPlane around it by the CPlane angle; a face (or Alt+click) = lie on it"""
    bl_idname = "nkdraw.cplane_face"
    bl_label = "CPlane Pick"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        from . import snaps
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}
        self.region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        self.rv3d = context.area.spaces.active.region_3d
        self.cache = snaps.SnapCache()
        self.cache.gather(context)
        self.snap = None
        self.pts = []
        self.cur = None
        self.view = None
        self._draw = bpy.types.SpaceView3D.draw_handler_add(_draw_pick, (self, context), 'WINDOW', 'POST_PIXEL')
        context.window.cursor_modal_set('EYEDROPPER')
        context.workspace.status_text_set("Click a point or an edge: CPlane origin there (rotation kept)  \u00b7  "
                                          "Shift+click an edge: turn around it  \u00b7  face / Alt+click: lie on it  \u00b7  "
                                          "Ctrl+click: by 2-3 points  \u00b7  Esc / right click to cancel")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _end(self, context):
        if getattr(self, "_draw", None) is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw, 'WINDOW')
            self._draw = None
        context.window.cursor_modal_restore()
        context.workspace.status_text_set(None)
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        from . import cplane, grid as G, snaps
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        if event.type in {'ESC', 'RIGHTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._end(context)
            if len(self.pts) >= 2:
                self.report({'INFO'}, "CPlane by 2 points")
                return {'FINISHED'}
            return {'CANCELLED'}
        r = self.region
        mx, my = event.mouse_x - r.x, event.mouse_y - r.y
        view = geom.View.from_context(r, self.rv3d)
        self.view = view
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and (self.pts or event.ctrl):
            p = cplane.point_under_mouse(context, view, self.cache, mx, my)
            self.pts.append(p)
            if len(self.pts) == 2:
                if not cplane.frame_from_points(context, self.pts[0], self.pts[1], None, view.vm):
                    self.pts.pop()
            elif len(self.pts) == 3:
                cplane.frame_from_points(context, *self.pts, view.vm)
                G.redraw()
                self._end(context)
                self.report({'INFO'}, "CPlane through 3 points")
                return {'FINISHED'}
            G.redraw()
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'MOUSEMOVE' and self.pts:
            self.cur = cplane.point_under_mouse(context, view, self.cache, mx, my)
        if event.type == 'MOUSEMOVE':
            hit = None if event.alt else cplane.pick(context, view, self.cache, mx, my)
            self.snap = None
            if hit is not None:
                s_ = view.project(hit[1])
                if s_ is not None:
                    self.snap = snaps.Snap("NEAR" if hit[0] == "edge" else "VERT", hit[1], s_)
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            msg = cplane.apply_pick(context, view, self.cache, mx, my, hinge=event.shift, force_face=event.alt)
            if msg is None:
                self.report({'INFO'}, "Nothing there")
                return {'RUNNING_MODAL'}
            G.redraw()
            self._end(context)
            self.report({'INFO'}, msg)
            return {'FINISHED'}
        return {'RUNNING_MODAL'}


class NKDRAW_OT_cplane_apply(bpy.types.Operator):
    """Apply: the live X / Y / Z turn becomes the CPlane and the values go back to 0.
Reset: throw the live turn away"""
    bl_idname = "nkdraw.cplane_apply"
    bl_label = "Apply CPlane Turn"
    bl_options = {'REGISTER', 'UNDO'}

    reset: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        from . import cplane, grid as G
        if self.reset:
            context.scene.nkdraw.cplane_offset = (0.0, 0.0, 0.0)
        else:
            vm = None
            try:
                vm = context.space_data.region_3d.view_matrix
            except AttributeError:
                pass
            cplane.apply_turn(context, vm)
        G.redraw()
        return {'FINISHED'}


class NKDRAW_OT_cplane_rotate(bpy.types.Operator):
    """Add the Step angle to the live turn of the CPlane around its X, Y or Z axis"""
    bl_idname = "nkdraw.cplane_rotate"
    bl_label = "Rotate CPlane"
    bl_options = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")], default='X')
    sign: bpy.props.FloatProperty(default=1.0)

    def execute(self, context):
        from . import cplane, grid as G
        vm = None
        try:
            vm = context.space_data.region_3d.view_matrix
        except AttributeError:
            pass
        cplane.rotate_local(context, self.axis, context.scene.nkdraw.cplane_angle * self.sign, vm)
        G.redraw()
        return {'FINISHED'}


class NKDRAW_OT_key_points(bpy.types.Operator):
    """Insert a keyframe on every point (position and handles) of the selected curves at the
    current frame. Move the points at another frame and key again: the curve animates"""
    bl_idname = "nkdraw.key_points"
    bl_label = "Keyframe Curve Points"
    bl_options = {'REGISTER', 'UNDO'}

    clear: bpy.props.BoolProperty(name="Clear", default=False,
                                  description="Remove the point animation of the selected curves instead")

    @classmethod
    def poll(cls, context):
        return any(o.type == 'CURVE' for o in context.selected_objects)

    def execute(self, context):
        f = context.scene.frame_current
        n = 0
        for o in context.selected_objects:
            if o.type != 'CURVE':
                continue
            if self.clear:
                if o.data.animation_data is not None:
                    o.data.animation_data_clear()
                    n += 1
                continue
            for s in o.data.splines:
                pts = s.bezier_points if s.type == 'BEZIER' else s.points
                for bp in pts:
                    for prop in (("co", "handle_left", "handle_right") if s.type == 'BEZIER' else ("co",)):
                        bp.keyframe_insert(prop, frame=f, group="Vector points")
                    n += 1
        self.report({'INFO'}, ("Point animation removed from %d curve(s)" % n) if self.clear
                    else "Keyframe on %d point(s) at frame %d" % (n, f))
        return {'FINISHED'}


class NKDRAW_OT_cplane_world(bpy.types.Operator):
    """Back to the world CPlane (Top, through the origin)"""
    bl_idname = "nkdraw.cplane_world"
    bl_label = "CPlane World"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import cplane, grid as G
        cplane.set_world(context, 'TOP')
        G.redraw()
        return {'FINISHED'}


class _Ev:
    """Event copy with region-relative mouse coordinates."""
    __slots__ = ("type", "value", "mouse_region_x", "mouse_region_y", "shift", "ctrl", "alt", "unicode")

    def __init__(self, e, mouse):
        self.type, self.value = e.type, e.value
        self.mouse_region_x, self.mouse_region_y = mouse
        self.shift, self.ctrl, self.alt = e.shift, e.ctrl, e.alt
        self.unicode = e.unicode


# ----------------------------------------------------------------- panel
_icons = None


def load_icons():
    """the Vector Draw icons (icons/*.png), loaded once at register - never while the UI draws"""
    global _icons
    if _icons is not None:
        return
    try:
        import os
        import bpy.utils.previews
        _icons = bpy.utils.previews.new()
        d = os.path.join(os.path.dirname(__file__), "icons")
        for f in sorted(os.listdir(d)):
            if f.endswith(".png"):
                _icons.load(f[:-4], os.path.join(d, f), 'IMAGE', force_reload=True)
                _icons[f[:-4]].icon_size[:]             # make the preview now, not from a background job
    except Exception:      # noqa: BLE001
        pass


def icon(name):
    """id of a Vector Draw icon, 0 if missing"""
    try:
        return _icons[name].icon_id if _icons is not None and name in _icons else 0
    except Exception:      # noqa: BLE001
        return 0


def free_icons():
    global _icons
    if _icons is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_icons)
        _icons = None


class NKDRAW_OT_mirror(bpy.types.Operator):
    """Symmetric drawing across this CPlane axis (through its origin): click again to switch it off.
The mirrored half is previewed while you draw and made when you finish the curve"""
    bl_idname = "nkdraw.mirror"
    bl_label = "Mirror"
    bl_options = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(items=[('Y', "Left \u2194 Right", ""), ('X', "Front \u2194 Back", "")], default='Y')

    @classmethod
    def description(cls, context, props):
        return ("Mirror left \u2194 right (axis = CPlane Y). Click again to switch it off" if props.axis == 'Y'
                else "Mirror front \u2194 back (axis = CPlane X). Click again to switch it off")

    def execute(self, context):
        st = context.scene.nkdraw
        st.mirror = 'OFF' if st.mirror == self.axis else self.axis
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                a.tag_redraw()
        return {'FINISHED'}


class _Sub:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Vector Draw"
    bl_parent_id = "NKDRAW_PT_main"


class NKDRAW_PT_main(bpy.types.Panel):
    bl_label = "Nakan Vector Draw"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Nakan Vector Draw"
    bl_order = 0

    def draw_header(self, context):
        p = modegate.prefs()
        if p is not None:
            self.layout.prop(p, "vector_mode", text="")

    def draw(self, context):
        st = context.scene.nkdraw
        modegate.draw_mode(self.layout, context)    # Vector Mode first: always usable
        lay = self.layout.column()
        lay.enabled = modegate.mode_on()            # the tools only work in Vector Mode
        col = lay.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.6
        row.operator(NKDRAW_OT_pen.bl_idname, text="Pen", icon='GREASEPENCIL').tool = 'PEN'
        row.operator(NKDRAW_OT_pen.bl_idname, text="Direct Select", icon='RESTRICT_SELECT_OFF').tool = 'EDIT'
        row = col.row(align=True)
        row.scale_y = 1.2
        row.operator(NKDRAW_OT_pen.bl_idname, text="Rect", icon='MESH_PLANE').tool = 'RECT'
        row.operator(NKDRAW_OT_pen.bl_idname, text="Circle", icon='MESH_CIRCLE').tool = 'CIRCLE'
        row.operator(NKDRAW_OT_pen.bl_idname, text="Polygon", icon='SEQ_CHROMA_SCOPE').tool = 'POLY'
        col.prop(st, "poly_sides")
        row = lay.row(align=True)
        row.scale_y = 1.2
        row.prop(st, "osnap", text="Snap", toggle=True, icon='SNAP_ON' if st.osnap else 'SNAP_OFF')
        sub = row.row(align=True)
        sub.active = st.osnap
        sub.prop(st, "snap_grid", text="Grid", toggle=True)
        sub.prop(st, "snap_track", text="Guides", toggle=True)
        col = lay.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.2
        row.operator("nkdraw.shape_builder", text="Shape Builder", icon='MOD_BOOLEAN')
        row = col.row(align=True)
        row.operator("nkdraw.contour", text="Contour", icon='MOD_SKIN')
        row.operator("nkdraw.split", text="Split", icon='MOD_EXPLODE')
        row = col.row(align=True)
        row.operator("nkdraw.move", text="Move", icon='VIEW_PAN')
        row.operator("nkdraw.copy", text="Copy", icon='DUPLICATE')
        row.operator("nkdraw.rotate", text="Rotate", icon='DRIVER_ROTATIONAL_DIFFERENCE')
        row = lay.row(align=True)
        row.scale_y = 1.2
        row.prop(st, "ortho", toggle=True)
        row.prop(st, "planar", toggle=True)
        sub = row.row(align=True)
        sub.scale_x = 1.15
        op = sub.operator(NKDRAW_OT_mirror.bl_idname, text="", icon_value=icon("mirror_lr"),
                          depress=st.mirror == 'Y')
        op.axis = 'Y'
        op = sub.operator(NKDRAW_OT_mirror.bl_idname, text="", icon_value=icon("mirror_fb"),
                          depress=st.mirror == 'X')
        op.axis = 'X'


class NKDRAW_PT_osnap(_Sub, bpy.types.Panel):
    bl_label = "Snap"

    def draw_header(self, context):
        self.layout.prop(context.scene.nkdraw, "osnap", text="")

    def draw(self, context):
        st = context.scene.nkdraw
        lay = self.layout
        lay.active = st.osnap
        grid = lay.grid_flow(columns=2, align=True)
        for prop, label in (("snap_end", "Anchors"), ("snap_vert", "Vertices"), ("snap_mid", "Midpoints"),
                            ("snap_cen", "Centers"), ("snap_int", "Intersections"), ("snap_perp", "Perpendicular"),
                            ("snap_near", "On Curve"), ("snap_grid", "Grid")):
            grid.prop(st, prop, text=label, toggle=True)
        grid = lay.grid_flow(columns=2, align=True)
        grid.prop(st, "snap_track", text="Smart Guides", toggle=True)
        grid.prop(st, "snap_axis", text="Axes", toggle=True)
        grid.prop(st, "snap_project", text="Project", toggle=True)
        grid.prop(st, "snap_hidden", text="Behind Objects", toggle=True)
        lay.prop(st, "snap_radius")
        lay.label(text="D while drawing: snap on/off \u00b7 S: snap menu", icon='INFO')


class NKDRAW_PT_cplane(_Sub, bpy.types.Panel):
    bl_label = "Drawing Plane & Grid"

    def draw(self, context):
        st = context.scene.nkdraw
        lay = self.layout
        lay.row(align=True).prop(st, "cplane", expand=True)
        from . import planeobj
        planeobj.draw_panel(lay, context)
        lay.separator()
        row = lay.row(align=True)
        row.operator(NKDRAW_OT_cplane_face.bl_idname, text="Pick a Face", icon='EYEDROPPER')
        col = lay.column(align=True)
        col.label(text="Turn the CPlane (live)")
        col.row(align=True).prop(st, "cplane_offset", text="")
        row = col.row(align=True)
        row.operator(NKDRAW_OT_cplane_apply.bl_idname, text="Apply", icon='CHECKMARK').reset = False
        row.operator(NKDRAW_OT_cplane_apply.bl_idname, text="Reset", icon='LOOP_BACK').reset = True
        row = col.row(align=True)
        row.prop(st, "cplane_angle", text="Step")
        for ax in ('X', 'Y', 'Z'):
            op = row.operator(NKDRAW_OT_cplane_rotate.bl_idname, text=ax)
            op.axis, op.sign = ax, 1.0
        lay.separator()
        row = lay.row(align=True)
        row.prop(st, "show_grid", text="Grid", toggle=True, icon='GRID')
        row.prop(st, "grid_step", text="Step")
        if st.show_grid:
            row = lay.row(align=True)
            row.prop(st, "grid_cells")
            row.prop(st, "grid_major")
            row = lay.row(align=True)
            row.prop(st, "grid_opacity", slider=True)
            row.prop(st, "grid_color", text="")







class NKDRAW_PT_curve(_Sub, bpy.types.Panel):
    bl_label = "Curve"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        lay = self.layout
        box = lay
        ob = context.active_object
        if ob is None or ob.type != 'CURVE':
            box.label(text="Select a curve", icon='INFO')
        else:
            cu = ob.data
            col = box.column(align=True)
            col.label(text="Round bevel")
            row = col.row(align=True)
            row.prop(cu, "bevel_depth", text="Radius")
            row.prop(cu, "bevel_resolution", text="Res")
            row = col.row(align=True)
            row.prop(cu, "use_fill_caps", text="Caps", toggle=True)
            row.prop(cu, "resolution_u", text="Curve res")



class NKDRAW_PT_keys(_Sub, bpy.types.Panel):
    bl_label = "Shortcuts"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.scale_y = 0.8
        for t in ("Click = corner \u00b7 Drag = smooth",
                  "Alt = convert (drag point/segment/handle)",
                  "Ctrl = direct select \u00b7 Ctrl+click cusp\u2194tangent",
                  "Shift+click a point = new path from it",
                  "Space while dragging = move point",
                  "Type 3.5 \u00b7 <45 \u00b7 3.5<45 \u00b7 x,y \u00b7 R1,0",
                  "Shift 45\u00b0 \u00b7 O ortho \u00b7 D osnap \u00b7 Tab CPlane",
                  "S = Osnap menu: name = once, box = always",
                  "Hover a snap, move away = tracking",
                  "F = pick CPlane, also mid-curve",
                  "Shift+F = stand up on the last segment",
                  "Shift+M = mirror L\u2194R / F\u2194B / off (the axis snaps)",
                  "Enter/RMB finish \u00b7 C close",
                  "P pen \u00b7 M rect \u00b7 L circle \u00b7 Y polygon",
                  "Rect: Shift square \u00b7 Alt from centre"):
            col.label(text=t)


PANELS = (NKDRAW_PT_main, NKDRAW_PT_osnap, NKDRAW_PT_cplane,
          NKDRAW_PT_curve, NKDRAW_PT_keys)
