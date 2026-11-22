"""Contour (Affinity's Contour Tool / Illustrator's Offset Path), interactive.

Select closed curves, start Contour, move the mouse: right = the outline grows outwards, left = it
goes inwards. The offset never crosses itself (Clipper): corners round off, thin parts vanish,
holes close. The result is fitted back into clean Bezier curves.
Keys: J corners Round / Miter / Bevel · K keep the original (new curve) or change it ·
Ctrl snaps to the grid step · Shift = fine · type a value + Enter · click / Enter OK · Esc cancel.
"""
import math

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

try:
    import numpy as np
    import pyclipr
except ImportError:          # the bundled wheel is missing on this platform
    pyclipr = None

SCALE = 100000.0
JOINS = ('ROUND', 'MITER', 'BEVEL')


# --------------------------------------------------------------------------------- sampling
def _bez(p0, p1, p2, p3, t):
    u = 1 - t
    return p0 * u ** 3 + p1 * 3 * u * u * t + p2 * 3 * u * t * t + p3 * t ** 3


def _straight(p0, p1, p2, p3):
    d = p3 - p0
    L = d.length
    if L < 1e-12:
        return True
    for h in (p1, p2):
        v = h - p0
        if (v - d * (v.dot(d) / (L * L))).length > 1e-6 * max(1.0, L):
            return False
    return True


def loops_of(cu, res=32):
    """Closed splines of a curve as 2D loops (local XY)."""
    out = []
    for s in cu.splines:
        if not s.use_cyclic_u:
            continue
        if s.type == 'BEZIER':
            bp = s.bezier_points
            n = len(bp)
            if n < 2:
                continue
            pts = []
            for i in range(n):
                a, b = bp[i], bp[(i + 1) % n]
                p0, p1, p2, p3 = a.co, a.handle_right, b.handle_left, b.co
                if _straight(p0, p1, p2, p3):
                    pts.append(p0.copy())
                else:
                    for k in range(res):
                        pts.append(_bez(p0, p1, p2, p3, k / res))
        else:
            pts = [p.co.xyz for p in s.points]
        loop = [(p.x, p.y) for p in pts]
        if len(loop) >= 3:
            out.append(loop)
    return out


# ---------------------------------------------------------------------------------- offset
def normalized(loops):
    """Union with the even-odd rule: outer loops CCW, holes CW (what the offset expects)."""
    c = pyclipr.Clipper()
    c.scaleFactor = SCALE
    c.addPaths([np.asarray(lp, dtype=float) for lp in loops], pyclipr.Subject)
    r = c.execute(pyclipr.Union, pyclipr.FillRule.EvenOdd)
    return [np.asarray(p, dtype=float) for p in r if len(p) >= 3]


def offset(paths, delta, join='ROUND'):
    if abs(delta) < 1e-12:
        return [[(float(x), float(y)) for x, y in p] for p in paths]
    o = pyclipr.ClipperOffset()
    o.scaleFactor = SCALE
    o.miterLimit = 4.0
    try:
        o.arcTolerance = max(abs(delta) * 0.002, 1e-6)
    except AttributeError:
        pass
    jt = {'ROUND': pyclipr.JoinType.Round, 'MITER': pyclipr.JoinType.Miter,
          'BEVEL': pyclipr.JoinType.Square}[join]
    o.addPaths(paths, jt, pyclipr.EndType.Polygon)
    r = o.execute(delta)
    return [[(float(x), float(y)) for x, y in np.asarray(p)] for p in r if len(p) >= 3]


# -------------------------------------------------------------------------- bezier fitting
def _corners(pts, limit_deg=35.0):
    n = len(pts)
    lim = math.radians(limit_deg)
    out = []
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        u, v = b - a, c - b
        if u.length < 1e-12 or v.length < 1e-12:
            continue
        if u.angle(v) > lim:
            out.append(i)
    return out


def _chord_params(pts):
    d = [0.0]
    for i in range(1, len(pts)):
        d.append(d[-1] + (pts[i] - pts[i - 1]).length)
    L = d[-1] or 1.0
    return [x / L for x in d]


def _gen_bezier(pts, u, t1, t2):
    p0, p3 = pts[0], pts[-1]
    C = [[0.0, 0.0], [0.0, 0.0]]
    X = [0.0, 0.0]
    for i, s in enumerate(u):
        b0, b1, b2, b3 = (1 - s) ** 3, 3 * s * (1 - s) ** 2, 3 * s * s * (1 - s), s ** 3
        A1, A2 = t1 * b1, t2 * b2
        C[0][0] += A1.dot(A1)
        C[0][1] += A1.dot(A2)
        C[1][1] += A2.dot(A2)
        tmp = pts[i] - (p0 * (b0 + b1) + p3 * (b2 + b3))
        X[0] += A1.dot(tmp)
        X[1] += A2.dot(tmp)
    C[1][0] = C[0][1]
    det = C[0][0] * C[1][1] - C[0][1] * C[1][0]
    seg = (p3 - p0).length
    if abs(det) > 1e-18:
        a1 = (X[0] * C[1][1] - X[1] * C[0][1]) / det
        a2 = (C[0][0] * X[1] - C[1][0] * X[0]) / det
    else:
        a1 = a2 = seg / 3
    if a1 < 1e-6 * seg or a2 < 1e-6 * seg:
        a1 = a2 = seg / 3
    return [p0, p0 + t1 * a1, p3 + t2 * a2, p3]


def _max_error(pts, bez, u):
    worst, idx = 0.0, len(pts) // 2
    for i in range(1, len(pts) - 1):
        d = (_bez(*bez, u[i]) - pts[i]).length
        if d > worst:
            worst, idx = d, i
    return worst, idx


def _reparam(pts, bez, u):
    q1 = [(bez[1] - bez[0]) * 3, (bez[2] - bez[1]) * 3, (bez[3] - bez[2]) * 3]
    q2 = [(q1[1] - q1[0]) * 2, (q1[2] - q1[1]) * 2]
    out = []
    for p, s in zip(pts, u):
        q = _bez(*bez, s)
        d1 = q1[0] * (1 - s) ** 2 + q1[1] * 2 * s * (1 - s) + q1[2] * s * s
        d2 = q2[0] * (1 - s) + q2[1] * s
        num = (q - p).dot(d1)
        den = d1.dot(d1) + (q - p).dot(d2)
        out.append(s - num / den if abs(den) > 1e-18 else s)
    return out


def fit_cubic(pts, t1, t2, err, depth=0):
    """Schneider's algorithm: a list of [p0, h0, h1, p3] for the open run of points."""
    if len(pts) == 2:
        d = (pts[1] - pts[0]).length / 3
        return [[pts[0], pts[0] + t1 * d, pts[1] + t2 * d, pts[1]]]
    u = _chord_params(pts)
    bez = _gen_bezier(pts, u, t1, t2)
    e, split = _max_error(pts, bez, u)
    if e < err:
        return [bez]
    if e < err * 4:
        for _ in range(4):
            u = _reparam(pts, bez, u)
            bez = _gen_bezier(pts, u, t1, t2)
            e, split = _max_error(pts, bez, u)
            if e < err:
                return [bez]
    if depth > 12:
        return [bez]
    tc = pts[split - 1] - pts[split + 1]
    tc = tc.normalized() if tc.length > 1e-12 else (pts[split - 1] - pts[split]).normalized()
    return (fit_cubic(pts[:split + 1], t1, tc, err, depth + 1) +
            fit_cubic(pts[split:], -tc, t2, err, depth + 1))


def fit_loop(loop, err):
    """A closed 2D polyline -> list of (co, handle_left, handle_right, smooth) (3D, z = 0)."""
    pts = [Vector((x, y)) for x, y in loop]
    clean = []
    for p in pts:
        if not clean or (p - clean[-1]).length > err * 0.05:
            clean.append(p)
    if len(clean) > 2 and (clean[0] - clean[-1]).length <= err * 0.05:
        clean.pop()
    pts = clean
    n = len(pts)
    if n < 3:
        return []
    cs = _corners(pts)
    if not cs:
        # smooth loop: open it at point 0 with the same tangent on both ends
        t = pts[1] - pts[-1]
        t = t.normalized() if t.length > 1e-12 else Vector((1, 0))
        run = pts + [pts[0]]
        segs = fit_cubic(run, t, -t, err)
    else:
        k = cs[0]
        pts = pts[k:] + pts[:k]                       # start on a corner
        cs = sorted(((c - k) % n) for c in cs) + [n]
        segs = []
        for a, b in zip(cs, cs[1:]):
            run = pts[a:b + 1] if b < n else pts[a:] + [pts[0]]
            if len(run) < 2:
                continue
            t1 = (run[1] - run[0]).normalized()
            t2 = (run[-2] - run[-1]).normalized()
            segs += fit_cubic(run, t1, t2, err)
    m = len(segs)
    out = []
    for i, s in enumerate(segs):
        prev = segs[i - 1]
        co = s[0]
        hl, hr = prev[2], s[1]
        a, b = (co - hl), (hr - co)
        smooth = a.length > 1e-12 and b.length > 1e-12 and a.angle(b) < math.radians(2)
        out.append((co.to_3d(), hl.to_3d(), hr.to_3d(), smooth))
    return out if m else []


# ------------------------------------------------------------------------------- writing
def write_curve(cu, loops_pts):
    old = next(iter(cu.splines), None)
    attrs = {}
    if old is not None:
        attrs = {"material_index": old.material_index, "resolution_u": old.resolution_u,
                 "use_smooth": old.use_smooth}
    for s in list(cu.splines):
        cu.splines.remove(s)
    for pts in loops_pts:
        if len(pts) < 2:
            continue
        s = cu.splines.new('BEZIER')
        s.bezier_points.add(len(pts) - 1)
        for bp, (co, hl, hr, _sm) in zip(s.bezier_points, pts):
            bp.handle_left_type = bp.handle_right_type = 'FREE'
            bp.co, bp.handle_left, bp.handle_right = co, hl, hr
        s.use_cyclic_u = True
        for k, v in attrs.items():
            setattr(s, k, v)
        for bp, (_c, _l, _r, sm) in zip(s.bezier_points, pts):
            if sm:
                bp.handle_left_type = bp.handle_right_type = 'ALIGNED'
    cu.update_tag()


def contour_object(ob, delta, join, err=None):
    """Offset loops of the curve object ob (local space) -> fitted bezier loops."""
    base = normalized(loops_of(ob.data))
    res = offset(base, delta, join)
    if err is None:
        xs = [x for lp in res for x, _ in lp] or [0, 1]
        ys = [y for lp in res for _, y in lp] or [0, 1]
        err = max(1e-6, 0.0008 * math.hypot(max(xs) - min(xs), max(ys) - min(ys)))
    return [fit_loop(lp, err) for lp in res]


# ---------------------------------------------------------------------------------- modal
def _draw(op, context):
    if not op.preview:
        return
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    sh.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    sh.uniform_float("lineWidth", 2.0)
    sh.uniform_float("color", (0.25, 0.65, 1.0, 1.0))
    for pts in op.preview:
        b = batch_for_shader(sh, 'LINE_STRIP', {"pos": pts})
        b.draw(sh)
    gpu.state.blend_set('NONE')


class NKDRAW_OT_contour(bpy.types.Operator):
    """Contour (offset path): move the mouse right = outwards, left = inwards; never crosses itself.
    J corners Round / Miter / Bevel · K new curve / change the original · Ctrl grid · Shift fine ·
    type a value + Enter"""
    bl_idname = "nkdraw.contour"
    bl_label = "Contour"
    bl_options = {'REGISTER', 'UNDO'}

    distance: bpy.props.FloatProperty(name="Distance", unit='LENGTH', subtype='DISTANCE')
    join: bpy.props.EnumProperty(name="Corners", items=[(j, j.title(), "") for j in JOINS], default='ROUND')
    keep: bpy.props.BoolProperty(name="Keep Original", default=True,
                                 description="On: the contour is a new curve. Off: the original curve changes")

    @classmethod
    def poll(cls, context):
        return (pyclipr is not None and context.mode == 'OBJECT' and
                any(o.type == 'CURVE' for o in context.selected_objects))

    def _objs(self, context):
        return [o for o in context.selected_objects if o.type == 'CURVE' and loops_of(o.data)]

    def invoke(self, context, event):
        self.objs = self._objs(context)
        if not self.objs:
            self.report({'WARNING'}, "Select closed curves")
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            return self.execute(context)
        self.region, self.rv3d = context.region, context.region_data
        self.start_x = event.mouse_region_x
        self.buffer = ""
        self.distance = 0.0
        self.preview = []
        self.bases = {o.name: normalized(loops_of(o.data)) for o in self.objs}
        o = self.objs[0]
        p0 = view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, (event.mouse_region_x,
                                                                           event.mouse_region_y),
                                                   o.matrix_world.translation)
        p1 = view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, (event.mouse_region_x + 100,
                                                                           event.mouse_region_y),
                                                   o.matrix_world.translation)
        sc = max(1e-9, o.matrix_world.to_scale().x)
        self.upp = (p1 - p0).length / 100.0 / sc           # local units per pixel
        self._h = bpy.types.SpaceView3D.draw_handler_add(_draw, (self, context), 'WINDOW', 'POST_VIEW')
        context.window_manager.modal_handler_add(self)
        self._update(context)
        return {'RUNNING_MODAL'}

    def _update(self, context):
        self.preview = []
        for o in self.objs:
            mw = o.matrix_world
            for lp in offset(self.bases[o.name], self.distance, self.join):
                pts = [mw @ Vector((x, y, 0.0)) for x, y in lp]
                if pts:
                    pts.append(pts[0])
                    self.preview.append([tuple(p) for p in pts])
        try:
            us = context.scene.unit_settings
            d = bpy.utils.units.to_string(us.system, 'LENGTH', self.distance * (us.scale_length or 1.0),
                                          precision=4)
        except Exception:  # noqa: BLE001
            d = "%.4f" % self.distance
        txt = "Contour  %s%s   ·   corners %s (J)   ·   %s (K)   ·   right = out, left = in   ·   " \
              "Ctrl grid · Shift fine · type a value" % (
                  ("[%s]  " % self.buffer) if self.buffer else "", d, self.join.title(),
                  "new curve" if self.keep else "changes the original")
        context.area.header_text_set(txt)
        context.area.tag_redraw()

    def _finish(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self._h, 'WINDOW')
        context.area.header_text_set(None)
        context.area.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        if t == 'MOUSEMOVE' and not self.buffer:
            dx = event.mouse_region_x - self.start_x
            d = dx * self.upp * (0.1 if event.shift else 1.0)
            if event.ctrl:
                step = getattr(getattr(context.scene, "nkdraw", None), "grid_step", 0.1) or 0.1
                d = round(d / step) * step
            self.distance = d
            self._update(context)
            return {'RUNNING_MODAL'}
        if v != 'PRESS':
            return {'RUNNING_MODAL'}
        if t == 'J':
            self.join = JOINS[(JOINS.index(self.join) + 1) % len(JOINS)]
            self._update(context)
            return {'RUNNING_MODAL'}
        if t == 'K':
            self.keep = not self.keep
            self._update(context)
            return {'RUNNING_MODAL'}
        if event.unicode and event.unicode in "0123456789.,-" and not event.ctrl:
            self.buffer += "." if event.unicode == "," else event.unicode
            self._typed(context)
            return {'RUNNING_MODAL'}
        if t == 'BACK_SPACE':
            self.buffer = self.buffer[:-1]
            self._typed(context)
            return {'RUNNING_MODAL'}
        if t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'}:
            self._finish(context)
            return self.execute(context)
        if t in {'ESC', 'RIGHTMOUSE'}:
            self._finish(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _typed(self, context):
        try:
            us = context.scene.unit_settings
            val = bpy.utils.units.to_value(us.system, 'LENGTH', self.buffer) / (us.scale_length or 1.0)
        except (ValueError, TypeError):
            try:
                val = float(self.buffer)
            except ValueError:
                val = None
        if val is not None:
            self.distance = val
        self._update(context)

    def execute(self, context):
        objs = getattr(self, "objs", None) or self._objs(context)
        made = []
        for o in objs:
            loops = [lp for lp in contour_object(o, self.distance, self.join) if lp]
            if not loops:
                continue
            if self.keep:
                cu = o.data.copy()
                cu.name = o.name + " Contour"
                for anim in (cu.animation_data,):
                    if anim is not None:
                        cu.animation_data_clear()
                write_curve(cu, loops)
                ob = bpy.data.objects.new(o.name + " Contour", cu)
                ob.matrix_world = o.matrix_world.copy()
                for c in o.users_collection:
                    c.objects.link(ob)
                try:
                    from . import surface
                    if surface.get_mod(o) is not None:
                        surface.wrap(ob, context.scene.nkpf_surface)
                except Exception:  # noqa: BLE001
                    pass
                made.append(ob)
            else:
                sh = getattr(o, "nkpf_shape", None)
                if sh is not None and sh.live:
                    sh.live = False                   # a primitive becomes curves when its outline changes
                write_curve(o.data, loops)
                made.append(o)
        if not made:
            self.report({'WARNING'}, "Nothing left at this distance")
            return {'CANCELLED'}
        for x in context.selected_objects:
            x.select_set(False)
        for ob in made:
            ob.select_set(True)
        context.view_layer.objects.active = made[-1]
        return {'FINISHED'}


classes = (NKDRAW_OT_contour,)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
