"""The pen: a Rhino/Illustrator style drawing session on a Bezier curve object.

PenSession holds all the logic and talks to Blender only to read/write the curve, so it can be
driven by the modal operator or by simulated events in tests.

  DRAW   a path is being drawn: click = corner point, click-drag = smooth point
         (Alt while dragging = break the tangent), click the first point = close,
         click the last point = drop its outgoing handle, C = close, Enter = finish the path
  IDLE   no path in progress: click an open end = continue it, click a segment = add a point,
         drag a point / handle = move it (with snaps), Alt+click point = corner <-> smooth,
         Alt+click segment = straight <-> curved, X / Del on a point = delete it,
         click empty space = new path, Enter / right click / Esc = leave the pen
  Typing  3.5 Enter = length lock, <45 Enter = angle lock, 3.5<45 Enter = point now,
         x,y = absolute on the CPlane, @x,y = relative to the last point (units allowed: 35cm, 2m)
  Keys   Shift = 45 degree steps, O = Ortho 90, D = Osnap on/off, S = Osnap menu,
         Tab = next CPlane, Backspace = undo last step
  Illustrator modifiers (also while drawing):
         Ctrl  = direct selection: drag anchors / handles / segments, Ctrl+click anchor = cusp <-> tangent
         Alt   = convert: drag anchor = new smooth handles, click anchor = corner,
                 drag handle = break it, drag segment = bend it live, click segment = straight <-> curved
         Space = while dragging a new anchor, reposition it
  While dragging (mouse still down), toggles like Blender (press again to go back to drawing):
         Rectangle / Circle / Polygon: G = move the shape, R = rotate it, S = scale it
         (X / Y lock G to a CPlane axis, Shift = 15 degree steps on R, Esc / right click = undo the move,
         release the mouse = place the shape as it is)
         Pen: G = move the new anchor with its handles
"""

import copy
import math

import bpy
from mathutils import Matrix, Vector

from . import geom, snaps

CORNER, SMOOTH, CUSP = "CORNER", "SMOOTH", "CUSP"
PICK_PX = 9.0
DRAG_PX = 4.0
PLANES = ['TOP', 'FRONT', 'RIGHT', 'CURSOR', 'VIEW']
MODKEYS = {'LEFT_SHIFT', 'RIGHT_SHIFT', 'LEFT_CTRL', 'RIGHT_CTRL', 'LEFT_ALT', 'RIGHT_ALT'}
TOOL_NAMES = {'PEN': "Pen", 'EDIT': "Direct Selection", 'RECT': "Rectangle", 'CIRCLE': "Circle", 'POLY': "Polygon"}
TOOL_KEYS = {'P': 'PEN', 'A': 'EDIT', 'M': 'RECT', 'L': 'CIRCLE', 'Y': 'POLY'}     # Illustrator letters
PATH_TOOLS = ('PEN', 'EDIT')      # tools that work on the paths (the others draw shapes)
# Osnap pop-up menu (S while the command runs): (setting, label, key)
MENU_ITEMS = [("osnap", "Snap (all)", 'D'), None,
              ("snap_end", "End", 'E'), ("snap_vert", "Vertex", 'V'), ("snap_mid", "Mid", 'M'),
              ("snap_cen", "Cen", 'C'), ("snap_int", "Int", 'I'), ("snap_perp", "Perp", 'P'),
              ("snap_near", "Near", 'N'), ("snap_grid", "Grid", 'G'), ("snap_project", "Project", 'J'),
              ("snap_track", "Track", 'K'), ("snap_axis", "Axis", 'A'), None,
              ("ortho", "Ortho", 'O'), ("planar", "Planar", 'L'), ("snap_hidden", "Snap hidden", 'H'),
              ("show_grid", "Show grid", 'W')]
MENU_W, MENU_ROW, MENU_SEP, MENU_HEAD = 330, 36, 12, 62
MENU_BOX = 46          # width of the tick-box column (click there = always on/off)
ONCE_PROPS = {"snap_end", "snap_vert", "snap_mid", "snap_cen", "snap_int", "snap_perp", "snap_near", "snap_grid"}


def _fcurves(anim_data):
    """F-curves of an animation (legacy actions and Blender 4.4+ layered actions)."""
    act = anim_data.action
    if act is None:
        return []
    fcs = list(getattr(act, "fcurves", []) or [])
    if fcs:
        return fcs
    out = []
    try:
        slot = anim_data.action_slot
        for layer in act.layers:
            for strip in layer.strips:
                cb = strip.channelbag(slot)
                if cb is not None:
                    out += list(cb.fcurves)
    except (AttributeError, RuntimeError, TypeError):
        pass
    return out


class _OnceSettings:
    """The settings as the snap engine sees them during a one-shot Osnap (Rhino style):
    only that snap is on, for the next pick; everything else (radius, Project, grid step) as usual."""

    def __init__(self, st, prop):
        self._st, self._prop = st, prop

    def __getattr__(self, name):
        if name == "osnap":
            return True
        if name in ONCE_PROPS:
            return name == self._prop
        return getattr(self._st, name)
SHAPE_HINT = {'RECT': "Opposite corner (Shift square, Alt from centre) or type w,h",
              'CIRCLE': "Opposite corner (Alt from centre) or type the radius",
              'POLY': "Opposite corner (Alt from centre, Shift flat \u00b7 Up/Down sides) or type the radius"}
SHAPE_DRAG_HINT = " \u00b7 while dragging: G move, R rotate, S scale"
XF_NAMES = {'G': "Move", 'R': "Rotate", 'S': "Scale"}


class _MoveEv:
    """a mouse move at the current position (to refresh a sub-tool when it starts)"""

    def __init__(self, mx, my, ev):
        self.type, self.value = 'MOUSEMOVE', 'NOTHING'
        self.mouse_region_x, self.mouse_region_y = mx, my
        self.shift, self.ctrl, self.alt = ev.shift, ev.ctrl, ev.alt
        self.unicode = ""


class Pt:
    __slots__ = ("co", "hl", "hr", "kind", "tilt", "radius")

    def __init__(self, co, kind=CORNER, hl=None, hr=None, tilt=0.0, radius=1.0):
        self.co = Vector(co)
        self.hl = Vector(hl) if hl is not None else Vector(co)
        self.hr = Vector(hr) if hr is not None else Vector(co)
        self.kind = kind
        self.tilt, self.radius = tilt, radius

    def move(self, d):
        self.co += d
        self.hl += d
        self.hr += d


class Path:
    def __init__(self, pts=None, cyclic=False, ob=None):
        self.pts = pts or []
        self.cyclic = cyclic
        self.attrs = {}
        self.ob = ob                      # the curve object this path belongs to

    def nseg(self):
        n = len(self.pts)
        return n if (self.cyclic and n > 2) else max(0, n - 1)

    def handles(self, i):
        """Effective (left, right) handles: corners point a third of the way to their neighbours."""
        p = self.pts[i]
        if p.kind != CORNER:
            return p.hl, p.hr
        n = len(self.pts)
        prv = self.pts[i - 1].co if (i > 0 or self.cyclic) and n > 1 else None
        nxt = self.pts[(i + 1) % n].co if (i < n - 1 or self.cyclic) and n > 1 else None
        hl = p.co + (prv - p.co) / 3.0 if prv is not None else (p.co - (nxt - p.co) / 3.0 if nxt is not None else p.co)
        hr = p.co + (nxt - p.co) / 3.0 if nxt is not None else (p.co - (prv - p.co) / 3.0 if prv is not None else p.co)
        return hl, hr

    def seg(self, i):
        """Control points of segment i (from point i to i+1)."""
        n = len(self.pts)
        a, b = self.pts[i], self.pts[(i + 1) % n]
        return a.co, self.handles(i)[1], self.handles((i + 1) % n)[0], b.co

    def reverse(self):
        self.pts.reverse()
        for p in self.pts:
            p.hl, p.hr = p.hr, p.hl


class _EditEvent:
    """An event seen as if Ctrl were held (Direct Selection), unless Alt converts points."""

    def __init__(self, ev):
        self._ev = ev
        self.ctrl = not ev.alt

    def __getattr__(self, name):
        return getattr(self._ev, name)


class PenSession:
    def __init__(self, context, settings, target=None, tool='PEN'):
        self.ctx = context
        self.st = settings
        self.target = target              # last curve object drawn or edited (selected at the end)
        self.created = []                 # objects made by this session
        self.objects = []                 # every curve object the pen can edit
        self.paths = []
        self.active = None                # index of the path being drawn
        self.state = "IDLE"
        self.tool = tool                  # PEN / RECT / CIRCLE / POLY
        self.anchor = None                # first point of a shape being defined
        self.shape_drag = False
        self.shape_ang = 0.0              # turn of the shape being drawn around the CPlane normal (R while dragging)
        self.shape_xf = None              # G / R / S running on the shape being dragged
        self.shape_moved = False          # the shape was moved / turned / scaled: the release places it
        self.warp = None                  # (x, y) region pixels: the operator puts the mouse there
        self.history = []
        self.buffer = ""
        self.len_lock = None
        self.ang_lock = None
        self.cursor = None                # 3D point under the mouse (after snaps/ortho/locks)
        self.mods = (False, False)        # (shift, alt) of the last event, for shape previews
        self.snap = None
        self.face_pick = False            # F: next click on a face sets the CPlane
        self.pick_pts = []                # F + Ctrl+click: CPlane by 2 / 3 points
        self.last_round = None            # last corner rounding (to type an exact radius after it)
        self.hover_widget = None          # (path, index) of the corner widget under the mouse
        self.menu = None                  # Osnap pop-up: {"x", "y", "hot"} in region pixels
        self.once = None                  # one-shot Osnap from the pop-up (prop name), for the next pick
        self.ui = 1.0                     # UI scale, set by the operator
        self.hover = None                 # ('pt', path, i) | ('seg', path, i, t) | ('hl'/'hr', path, i)
        self.drag = None
        self.sel = set()                  # selected anchors {(path, index)} (Ctrl+drag a box)
        self.sel_handle = None            # Direct Selection: selected handle (path, index, 'hl' / 'hr')
        self.xform = None                 # Direct Selection: G / R / S running on the selection
        self.box = None                   # marquee being dragged: (start, end) in region pixels
        self.message = ""
        self.done = False
        self.cache = snaps.SnapCache()
        self.view = None
        self.track_pts = []               # points acquired by hovering a snap (tracking, AutoCAD style)
        self.track_draw = []              # (from acquired point, to cursor) guides shown
        for ob in context.visible_objects:
            if ob.type == 'CURVE' and any(sp.type == 'BEZIER' for sp in ob.data.splines) \
                    and sum(len(sp.bezier_points) for sp in ob.data.splines) < 5000:
                sh = getattr(ob, "nkpf_shape", None)
                if sh is not None and sh.live:
                    continue          # Nakan Vector Draw: a primitive keeps its points until Convert to Curves
                self._load(ob)
        self.cache.gather(context, exclude=set(self.objects))
        self._refresh_dynamic()
        act = getattr(context, "active_object", None)
        if self.target is None and act is not None and act in self.objects:
            self.target = act                 # the selected curve: its corners show their dots at once
        self.focus = None                     # last path under the mouse (its corner dots stay visible)

    # ------------------------------------------------------------ data in/out
    def _load(self, ob):
        mw = ob.matrix_world
        self.objects.append(ob)
        for s in ob.data.splines:
            if s.type != 'BEZIER':
                continue
            pts = []
            for bp in s.bezier_points:
                lt, rt = bp.handle_left_type, bp.handle_right_type
                if lt == 'VECTOR' and rt == 'VECTOR':
                    kind = CORNER
                elif lt in {'ALIGNED', 'AUTO'} and rt in {'ALIGNED', 'AUTO'}:
                    kind = SMOOTH
                else:
                    kind = CUSP
                pts.append(Pt(mw @ bp.co, kind, mw @ bp.handle_left, mw @ bp.handle_right, bp.tilt, bp.radius))
            P = Path(pts, s.use_cyclic_u, ob)
            P.attrs = {"material_index": s.material_index, "resolution_u": s.resolution_u, "use_smooth": s.use_smooth}
            self.paths.append(P)

    def _new_object(self, at, name="Vector Curve", ang=0.0):
        """A new curve object with its origin at `at` (the first point / the shape centre)."""
        cu = bpy.data.curves.new(name, 'CURVE')
        cu.dimensions = '3D'
        cu.resolution_u = 24
        ob = bpy.data.objects.new(name, cu)
        # set the whole world matrix, not just .location: Blender recomputes matrix_world only on
        # the next redraw, and the points are written through it right away
        ob.matrix_world = self._object_frame(at, ang)
        coll = self.ctx.collection or self.ctx.scene.collection
        coll.objects.link(ob)
        self.objects.append(ob)
        self.created.append(ob)
        return ob

    def _object_frame(self, at, ang=0.0):
        """Origin at `at`, local axes = CPlane axes (X, Y, normal), so the object's Local
        orientation (G Z Z, N panel, Extrude) follows the plane it was drawn on. `ang` turns the
        axes around the normal (a shape rotated while it was drawn keeps straight local axes)."""
        m = Matrix.Translation(Vector(at))
        if self.st.cplane in {'TOP', 'VIEW'} and not any(abs(a) > 1e-9 for a in self.st.cplane_offset) \
                and abs(ang) < 1e-12:
            return m
        try:
            pl = self.cplane()
            x, y = self._turned_axes(pl, ang)
            z = x.cross(y).normalized()
            y = z.cross(x).normalized()
        except Exception:      # noqa: BLE001
            return m
        r = Matrix((x, y, z)).transposed().to_4x4()
        r.translation = Vector(at)
        return r

    def write(self, ob=None):
        """Rewrite the bezier splines of `ob` (or of every object the pen manages)."""
        for o in ([ob] if ob is not None else list(self.objects)):
            try:
                self._write_one(o)
            except ReferenceError:
                pass

    def _write_one(self, ob):
        cu = ob.data
        inv = ob.matrix_world.inverted_safe()
        for s in list(cu.splines):
            if s.type == 'BEZIER':
                cu.splines.remove(s)
        for P in self.paths:
            if P.ob is not ob or not P.pts:
                continue
            s = cu.splines.new('BEZIER')
            n = len(P.pts)
            if n > 1:
                s.bezier_points.add(n - 1)
            for i, (bp, p) in enumerate(zip(s.bezier_points, P.pts)):
                hl, hr = P.handles(i)
                bp.handle_left_type = bp.handle_right_type = 'FREE'
                bp.co = inv @ p.co
                bp.handle_left = inv @ hl
                bp.handle_right = inv @ hr
                bp.tilt, bp.radius = p.tilt, p.radius
            s.use_cyclic_u = P.cyclic and n > 2
            for k, v in P.attrs.items():
                try:
                    setattr(s, k, v)
                except (AttributeError, TypeError):
                    pass
            for bp, p in zip(s.bezier_points, P.pts):
                if p.kind == CORNER:
                    bp.handle_left_type = bp.handle_right_type = 'VECTOR'
                elif p.kind == SMOOTH:
                    bp.handle_left_type = bp.handle_right_type = 'ALIGNED'
        cu.update_tag()
        ob.update_tag()

    def _push(self):
        snap_ = [(P.ob, copy.deepcopy(P.pts), P.cyclic, dict(P.attrs)) for P in self.paths]
        self.history.append((snap_, self.active, self.state))
        if len(self.history) > 200:
            self.history.pop(0)

    def undo(self):
        if not self.history:
            self.message = "Nothing to undo"
            return
        data, self.active, state = self.history.pop()
        self.sel = set()
        self.paths = []
        for ob, pts, cyc, attrs in data:
            P = Path(pts, cyc, ob)
            P.attrs = attrs
            self.paths.append(P)
        self.state = "DRAW" if (self.active is not None and self.active < len(self.paths)) else "IDLE"
        if self.state == "IDLE":
            self.active = None
        self.len_lock = self.ang_lock = None
        self.write()
        self._refresh_dynamic()

    def _refresh_dynamic(self, skip=None):
        """Snap geometry of the curve being edited; skip = (path, point) being moved."""
        pts, kinds, segs = [], [], []
        if skip is None:
            skip = set()
        elif isinstance(skip, tuple):
            skip = {skip}
        for pi, P in enumerate(self.paths):
            n = len(P.pts)
            for i, p in enumerate(P.pts):
                if (pi, i) in skip:
                    continue
                pts.append(tuple(p.co))
                kinds.append(snaps.KIND_ID["END"])
            for i in range(P.nseg()):
                j = (i + 1) % n
                if (pi, i) in skip or (pi, j) in skip:
                    continue
                sm = geom.bez_samples(*P.seg(i), 12)
                pts.append(tuple(sm[6]))
                kinds.append(snaps.KIND_ID["MID"])
                segs += [(tuple(a), tuple(b)) for a, b in zip(sm, sm[1:])]
        self.cache.set_dynamic(pts, kinds, segs)

    # ------------------------------------------------------------ planes & points
    def cplane(self):
        sc = self.ctx.scene
        cur = sc.cursor
        vm = self.view.vm if self.view is not None else None
        return geom.scene_plane(sc, vm)

    def last_point(self):
        if self.tool not in PATH_TOOLS:
            return Pt(self.anchor) if self.anchor is not None else None
        if self.state in ("DRAW", "DRAG_NEW") and self.active is not None:
            P = self.paths[self.active]
            if P.pts:
                return P.pts[-1]
        return None

    def work_plane(self, through=None):
        """CPlane moved to the elevation of `through` (used for handles and Planar mode)."""
        pl = self.cplane()
        if through is None:
            return pl
        h = (Vector(through) - pl.origin).dot(pl.n)
        return geom.Plane(pl.origin + pl.n * h, pl.x, pl.y, pl.name)

    def _visible(self, p):
        """False if the point is hidden behind a surface, seen from the viewer."""
        if getattr(self.st, "snap_hidden", False) or self.view is None:
            return True
        s = self.view.project(p)
        if s is None:
            return False
        ro, rd = self.view.ray(s.x, s.y)
        dist = (Vector(p) - ro).dot(rd)
        try:
            dg = self.ctx.evaluated_depsgraph_get()
            hit, loc, _n, _i, _o, _m = self.ctx.scene.ray_cast(dg, ro, rd, distance=max(dist, 0.0) + 1e-3)
        except (RuntimeError, TypeError, ValueError):
            return True
        if not hit:
            return True
        tol = self.view.pixel_size(p) * 3.0 + 1e-5 * max(dist, 1.0)
        return (Vector(loc) - ro).dot(rd) >= dist - tol

    def _angle_step(self, ev):
        """Constraint step in degrees for the direction from the last point (None = free).
        Shift = 45 degree steps (Illustrator), O = ortho 90 (Rhino)."""
        if ev.shift:
            return float(getattr(self.st, "shift_angle", 45.0))
        if self.st.ortho:
            return 90.0
        return None

    def _ortho_on(self, ev):
        return self._angle_step(ev) is not None

    @staticmethod
    def _constrain(origin, p, plane, step):
        d = p - origin
        u, v = d.dot(plane.x), d.dot(plane.y)
        r = math.hypot(u, v)
        if r < 1e-12:
            return p
        a = math.radians(step) * round(math.atan2(v, u) / math.radians(step))
        # keep the length along the constrained direction (projection), like Illustrator
        L = u * math.cos(a) + v * math.sin(a)
        return origin + (plane.x * math.cos(a) + plane.y * math.sin(a)) * L + plane.n * d.dot(plane.n) * 0.0

    def compute_cursor(self, mx, my, ev, skip_snap=False):
        last = self.last_point()
        # free points lie on the CPlane (Rhino); Planar keeps them at the last point's elevation
        planar = getattr(self.st, "planar", False) and self.tool == 'PEN'
        plane = self.work_plane(last.co if (last and planar) else None)
        snap = None
        if not skip_snap and self.snap_st.osnap:
            snap = self.cache.find(self.view, mx, my, self.snap_st, last=last.co if last else None, plane=plane,
                                   visible=self._visible)
        ro, rd = self.view.ray(mx, my)
        point_snap = snap is not None and snap.kind not in ("GRID",)
        if point_snap:
            p = snap.point.copy()
        else:
            p = snap.point.copy() if snap is not None else plane.hit(ro, rd)
            step = self._angle_step(ev) if (last is not None and self.tool == 'PEN') else None
            if step and self.ang_lock is None:
                p = self._constrain(last.co, p, plane, step)
                if not planar:
                    p = plane.project(p)          # Shift/Ortho from a point off the CPlane: stay on the CPlane
        if (not point_snap and not skip_snap and self.snap_st.osnap and self.len_lock is None
                and self.ang_lock is None and not self.once and getattr(self.st, "snap_axis", True)):
            ax = self._axis_snap(mx, my, p, last.co if last is not None else None,
                                 bool(last is not None and self.tool == 'PEN' and self._angle_step(ev)))
            if ax is not None:
                p, snap = ax
                point_snap = True
        if last is not None:
            if self.ang_lock is not None:
                a = math.radians(self.ang_lock)
                dvec = plane.x * math.cos(a) + plane.y * math.sin(a)
                L = (p - last.co).dot(dvec)
                if self.len_lock is not None:
                    L = self.len_lock
                p = last.co + dvec * max(L, 0.0)
            elif self.len_lock is not None:
                d = p - last.co
                if d.length < 1e-12:
                    d = plane.x.copy()
                p = last.co + d.normalized() * self.len_lock
        self.snap = snap if point_snap or (snap is not None and snap.kind == "GRID") else None
        if self.snap is not None and (self.len_lock is not None or self.ang_lock is not None):
            self.snap = None
        self.track_draw = []
        if point_snap and snap.kind in ("END", "VERT", "MID", "CEN", "INT", "PERP") \
                and getattr(self.st, "snap_track", True):
            self._acquire(snap.point, last.co if last is not None else None)
        elif not skip_snap and self.snap_st.osnap and getattr(self.st, "snap_track", True) \
                and self.len_lock is None and self.track_pts:
            constrained = last is not None and (self.ang_lock is not None or
                                                 (self.tool == 'PEN' and self._angle_step(ev)))
            t = self._track(mx, my, p, plane, last.co if (last is not None and constrained) else None)
            if t is not None:
                p, label = t
                s_ = self.view.project(p)
                self.snap = snaps.Snap("TRACK", p, s_ if s_ is not None else Vector((mx, my)))
                self.snap.owner = label
        self.cursor = p
        return p

    def _axis_snap(self, mx, my, p, last, constrained):
        """The cursor caught on an axis -> (point, Snap) or None. Mirror on: its axis only; mirror off: the
        CPlane X and Y axes through its origin (for the Mirror modifier and the like), their crossing too.
        Free cursor: the foot on the axis; Shift / Ortho from the last point: where that direction meets it."""
        mp = self.mirror_plane()
        if mp is not None:
            axes = [(mp[0], mp[1], "Mirror axis")]
        else:
            pl = self.cplane()
            o = Vector(pl.origin)
            axes = [(o, Vector(pl.y).normalized(), "Axis X"), (o, Vector(pl.x).normalized(), "Axis Y")]
        m2 = Vector((mx, my))
        r = float(self.st.snap_radius)
        hits = []
        for o, m, label in axes:
            if constrained and last is not None:
                v = p - Vector(last)
                den = v.dot(m)
                if abs(den) < 1e-12:
                    continue
                q = Vector(last) + v * ((o - Vector(last)).dot(m) / den)
            else:
                q = p - m * (p - o).dot(m)
            s_ = self.view.project(q)
            if s_ is None:
                continue
            dd = (s_ - m2).length
            if dd <= r:
                hits.append((dd, q, s_, label))
        if not hits:
            return None
        if len(hits) == 2 and not constrained:
            o = axes[0][0]                                   # near both: their crossing, the CPlane origin
            s_ = self.view.project(o)
            if s_ is not None and (s_ - m2).length <= r * 1.5:
                return o.copy(), snaps.Snap("AXIS", o, s_, "Origin")
        dd, q, s_, label = min(hits, key=lambda h: h[0])
        return q, snaps.Snap("AXIS", q, s_, label)

    def _acquire(self, q, last):
        q = Vector(q)
        if last is not None and (q - Vector(last)).length < 1e-9:
            return
        self.track_pts = [t for t in self.track_pts if (t - q).length > 1e-9]
        self.track_pts.insert(0, q)
        del self.track_pts[3:]

    def _track(self, mx, my, p, plane, origin):
        """cursor lined up with an acquired point along the CPlane X / Y: -> (point, label) or None.
        origin: the last point when the direction from it is locked (Shift / Ortho / typed angle):
        then the cursor goes where that direction meets the alignment."""
        r = float(self.st.snap_radius)
        m = Vector((mx, my))
        lines = []
        for a in self.track_pts:
            a = plane.project(a)
            for d in (plane.x, plane.y):
                lines.append((a, d.normalized()))
        if not lines:
            return None

        def meet(a1, d1, a2, d2):
            # lines in the plane: a1 + d1 s = a2 + d2 t
            u1, v1 = d1.dot(plane.x), d1.dot(plane.y)
            u2, v2 = d2.dot(plane.x), d2.dot(plane.y)
            den = u1 * v2 - v1 * u2
            if abs(den) < 1e-9:
                return None
            w = a2 - a1
            wu, wv = w.dot(plane.x), w.dot(plane.y)
            s_ = (wu * v2 - wv * u2) / den
            return a1 + d1 * s_
        best = None
        if origin is not None:
            o = plane.project(origin)
            dc = p - o
            if dc.length < 1e-12:
                return None
            dc.normalize()
            for a, d in lines:
                x = meet(o, dc, a, d)
                if x is None or (x - o).dot(dc) <= 1e-9:
                    continue
                sx = self.view.project(x)
                if sx is None:
                    continue
                dist = (sx - m).length
                if dist <= 2.0 * r and (best is None or dist < best[0]):
                    best = (dist, x, a)
            if best is None:
                return None
            self.track_draw = [(best[2], best[1]), (o, best[1])]
            return best[1], "Track"
        cands = []
        for a, d in lines:
            x = a + d * (p - a).dot(d)
            sx = self.view.project(x)
            if sx is None:
                continue
            dist = (sx - m).length
            if dist <= r:
                cands.append((dist, a, d, x))
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                _d1, a1, d1, _x1 = cands[i]
                _d2, a2, d2, _x2 = cands[j]
                y = meet(a1, d1, a2, d2)
                if y is None:
                    continue
                sy = self.view.project(y)
                if sy is not None and (sy - m).length <= r:
                    self.track_draw = [(a1, y), (a2, y)]
                    return y, "Track Int"
        _d, a, d, x = cands[0]
        self.track_draw = [(a, x)]
        return x, "Track"

    # ------------------------------------------------------------ hover
    def pick(self, mx, my, full=None):
        """('pt', path, i) | ('hl'/'hr', path, i) | ('seg', path, i, t) | None.
        Handles and segments are pickable when idle, or while drawing with Ctrl/Alt held."""
        m = Vector((mx, my))
        if full is None:
            full = self.state == "IDLE"
        best = None
        for pi, P in enumerate(self.paths):
            for i, p in enumerate(P.pts):
                s = self.view.project(p.co)
                if s is not None and (s - m).length <= PICK_PX:
                    d = (s - m).length
                    if best is None or d < best[0]:
                        best = (d, ("pt", pi, i))
        if best is not None:
            return best[1]
        if not full:
            return None
        for pi, P in enumerate(self.paths):
            if self._handles_visible(pi):
                for i, p in enumerate(P.pts):
                    if p.kind == CORNER:
                        continue
                    for side, h in (("hl", p.hl), ("hr", p.hr)):
                        if (h - p.co).length < 1e-9:
                            continue
                        s = self.view.project(h)
                        if s is not None and (s - m).length <= PICK_PX:
                            return (side, pi, i)
        for pi, P in enumerate(self.paths):
            for i in range(P.nseg()):
                t, d = geom.bez_closest(*P.seg(i), self.view, mx, my)
                if t is not None and d <= PICK_PX * 0.8:
                    return ("seg", pi, i, t)
        return None

    def _handles_visible(self, pi):
        """Handles are shown (and grabbable) on the path being drawn, the last edited one and the
        one under the mouse, like Illustrator shows them on the selected path."""
        P = self.paths[pi]
        if pi == self.active:
            return True
        if self.target is not None and P.ob is self.target:
            return True
        hv = self.hover
        return hv is not None and hv[1] == pi

    # ------------------------------------------------------------ edits
    def _add_point(self, p):
        if self.active is None:
            ob = self._new_object(p)
            self.paths.append(Path([], False, ob))
            self.active = len(self.paths) - 1
        self.target = self.paths[self.active].ob
        self.paths[self.active].pts.append(Pt(p, CORNER))
        self.len_lock = self.ang_lock = None

    def _make_smooth(self, P, i):
        p = P.pts[i]
        n = len(P.pts)
        prv = P.pts[i - 1].co if (i > 0 or P.cyclic) and n > 1 else None
        nxt = P.pts[(i + 1) % n].co if (i < n - 1 or P.cyclic) and n > 1 else None
        if prv is not None and nxt is not None:
            tan = nxt - prv
        elif nxt is not None:
            tan = nxt - p.co
        elif prv is not None:
            tan = p.co - prv
        else:
            return False
        if tan.length < 1e-12:
            return False
        tan.normalize()
        ll = (p.co - prv).length / 3.0 if prv is not None else (nxt - p.co).length / 3.0
        lr = (nxt - p.co).length / 3.0 if nxt is not None else ll
        p.hl, p.hr, p.kind = p.co - tan * ll, p.co + tan * lr, SMOOTH
        return True

    def toggle_point(self, pi, i):
        P = self.paths[pi]
        p = P.pts[i]
        self._push()
        if p.kind == CORNER:
            self._make_smooth(P, i)
            self.message = "Point made smooth"
        else:
            p.kind = CORNER
            self.message = "Point made corner"
        self.write()
        self._refresh_dynamic()

    def toggle_segment(self, pi, i):
        P = self.paths[pi]
        n = len(P.pts)
        j = (i + 1) % n
        a, b = P.pts[i], P.pts[j]
        p0, p1, p2, p3 = P.seg(i)
        chord = p3 - p0
        L2 = max(chord.length_squared, 1e-18)
        straight = (p1 - p0).cross(chord).length_squared < 1e-10 * L2 * L2 and \
            (p2 - p0).cross(chord).length_squared < 1e-10 * L2 * L2
        self._push()
        if straight:
            for k in (i, j):
                if P.pts[k].kind != SMOOTH:
                    self._make_smooth(P, k)
            self.message = "Segment curved"
        else:
            for q, k in ((a, i), (b, j)):
                q.hl, q.hr = (Vector(h) for h in P.handles(k))
            a.hr = a.co + chord / 3.0
            b.hl = b.co - chord / 3.0
            if a.kind == SMOOTH:
                a.kind = CUSP
            if b.kind == SMOOTH:
                b.kind = CUSP
            self.message = "Segment straightened"
        self.write()
        self._refresh_dynamic()

    def insert_point(self, pi, i, t):
        P = self.paths[pi]
        n = len(P.pts)
        self._push()
        p0, p1, p2, p3 = P.seg(i)
        a, b = P.pts[i], P.pts[(i + 1) % n]
        chord = p3 - p0
        straight = a.kind == CORNER and b.kind == CORNER
        nl, np_, nr, a_r, b_l = geom.bez_split(p0, p1, p2, p3, t)
        if straight:
            new = Pt(np_, CORNER)
        else:
            for q, (hl, hr) in ((a, P.handles(i)), (b, P.handles((i + 1) % n))):
                q.hl, q.hr = Vector(hl), Vector(hr)
                if q.kind == CORNER:
                    q.kind = CUSP
            a.hr, b.hl = a_r, b_l
            new = Pt(np_, SMOOTH, nl, nr)
        P.pts.insert(i + 1, new)
        if self.active == pi and self.state == "DRAW":
            pass
        self.message = "Point added"
        self.write()
        self._refresh_dynamic()
        return i + 1

    def delete_point(self, pi, i):
        P = self.paths[pi]
        self._push()
        del P.pts[i]
        if len(P.pts) < 3:
            P.cyclic = False
        if not P.pts:
            del self.paths[pi]
            if self.active is not None and self.active > pi:
                self.active -= 1
        self.message = "Point deleted"
        self.write()
        self._refresh_dynamic()

    def continue_from(self, pi, i):
        P = self.paths[pi]
        if P.cyclic or i not in (0, len(P.pts) - 1):
            return False
        self._push()
        if i == 0 and len(P.pts) > 1:
            P.reverse()
        self.active = pi
        self.state = "DRAW"
        self.message = "Continuing the curve"
        return True

    # ------------------------------------------------------------ mirror (symmetric drawing)
    def mirror_plane(self):
        """(origin, normal of the mirror, axis direction) or None. The axis lies on the CPlane through its
        origin: 'Y' = along the CPlane Y axis (left <-> right), 'X' = along the X axis (front <-> back)."""
        ax = getattr(self.st, "mirror", 'OFF')
        if ax == 'OFF':
            return None
        pl = self.cplane()
        m = Vector(pl.x if ax == 'Y' else pl.y).normalized()
        d = Vector(pl.y if ax == 'Y' else pl.x).normalized()
        return Vector(pl.origin), m, d

    @staticmethod
    def _reflect(p, mp):
        o, m, _d = mp
        p = Vector(p)
        return p - m * (2.0 * (p - o).dot(m))

    def mirror_preview(self):
        """Mirrored copy of the curve being drawn (polylines) and the mirror axis (2 points), world space."""
        mp = self.mirror_plane()
        if mp is None or self.tool not in PATH_TOOLS:
            return [], []
        o, m, d = mp
        strips = []
        if self.active is not None and self.active < len(self.paths):
            P = self.paths[self.active]
            for i in range(P.nseg()):
                strips.append([self._reflect(q, mp) for q in geom.bez_samples(*P.seg(i), 16)])
        pv = self.preview()
        if len(pv) > 1:
            strips.append([self._reflect(q, mp) for q in pv])
        L = (self.view.pixel_size(o) * 2000.0) if self.view is not None else 100.0
        return strips, [o - d * L, o + d * L]

    def _apply_mirror(self, pi):
        """Mirror the finished path. An end on the axis joins the two halves into one curve (both ends
        on the axis = a closed symmetric shape); otherwise a mirrored copy is added to the same object."""
        mp = self.mirror_plane()
        P = self.paths[pi]
        o, m, _d = mp
        R = lambda p: self._reflect(p, mp)                        # noqa: E731
        ext = max((q.co - P.pts[0].co).length for q in P.pts) or 1.0
        tol = max(1e-6, ext * 1e-4)
        if self.view is not None:
            tol = max(tol, self.view.pixel_size(P.pts[-1].co) * 3.0)

        def on(q):
            return abs((q.co - o).dot(m)) <= tol

        def mpt(q):
            return Pt(R(q.co), q.kind, R(q.hr), R(q.hl), q.tilt, q.radius)

        if P.cyclic or not (on(P.pts[0]) or on(P.pts[-1])):
            Q = Path([mpt(q) for q in reversed(P.pts)], P.cyclic, P.ob)
            Q.attrs = dict(P.attrs)
            self.paths.append(Q)
            return "Mirrored copy added"
        if on(P.pts[0]) and not on(P.pts[-1]):
            P.reverse()
        both = on(P.pts[0]) and len(P.pts) > 2

        def to_axis(q):
            q.move(-m * (q.co - o).dot(m))

        def join(q, h_attr, other_attr):
            """q sits on the axis: its free side becomes the mirror of the kept handle."""
            h = getattr(q, h_attr)
            setattr(q, other_attr, R(h))
            v = h - q.co
            if q.kind == CORNER or v.length < 1e-9:
                q.kind = CORNER
                q.hl, q.hr = q.co.copy(), q.co.copy()
            else:
                q.kind = SMOOTH if abs(v.normalized().dot(m)) > 0.999 else CUSP

        last, first = P.pts[-1], P.pts[0]
        to_axis(last)
        if both:
            to_axis(first)
        mids = [mpt(q) for q in reversed(P.pts[1:-1] if both else P.pts[:-1])]
        join(last, "hl", "hr")
        if both:
            join(first, "hr", "hl")
        P.pts = P.pts + mids
        if both:
            P.cyclic = True
            return "Mirrored: closed symmetric shape"
        return "Mirrored: the curve continues on the other side"

    def close_active(self):
        if self.active is None:
            return
        P = self.paths[self.active]
        if len(P.pts) < 3:
            self.message = "Need 3 points to close"
            return
        self._push()
        P.cyclic = True
        self.finish_path()
        self.message = "Curve closed"

    def finish_path(self):
        if self.active is not None and self.active < len(self.paths):
            P = self.paths[self.active]
            if len(P.pts) < 2:
                del self.paths[self.active]
            elif self.mirror_plane() is not None and self.tool == 'PEN':
                self._push()
                self.message = self._apply_mirror(self.active)
        self.active = None
        self.state = "IDLE"
        self.len_lock = self.ang_lock = None
        self.buffer = ""
        self.write()
        self._refresh_dynamic()

    # ------------------------------------------------------------ typed input
    def _to_bu(self, text):
        sc = self.ctx.scene
        us = sc.unit_settings
        text = text.strip().replace(",", ".") if text.count(",") == 0 else text.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            v = bpy.utils.units.to_value(us.system, 'LENGTH', text)
            return v / (us.scale_length or 1.0)
        except (ValueError, RuntimeError):
            return None

    def apply_buffer(self):
        txt = self.buffer.strip()
        self.buffer = ""
        if not txt:
            return False
        if self.last_round is not None and self.state == "IDLE" and "," not in txt and "<" not in txt:
            r = self._to_bu(txt)
            if r is not None:
                d = self.last_round
                self._push()
                r = self._round_corners(d, max(r, 0.0))
                self.message = "Corner radius %.4g" % r
                self._refresh_dynamic()
                return True
        last = self.last_point()
        plane = self.work_plane(last.co if last else None)
        rel = txt[:1] in ("@", "r", "R")
        if rel:
            txt = txt[1:].strip()
        if self.tool not in PATH_TOOLS and self.anchor is not None:
            return self.apply_shape_buffer(txt)
        if "," in txt and "<" not in txt:
            parts = [self._to_bu(t) for t in txt.split(",")]
            if any(v is None for v in parts) or len(parts) not in (2, 3):
                self.message = "Can't read '%s'" % txt
                return False
            u, v = parts[0], parts[1]
            w = parts[2] if len(parts) == 3 else 0.0
            if rel:
                base = last.co if last else plane.origin
                su, sv = self._mouse_signs(base, plane)       # AutoCAD: + = towards the mouse
                p = base + plane.x * (u * su) + plane.y * (v * sv) + plane.n * w
            else:
                p = self.cplane().from_local(u, v, w)
            self._commit_point(p)
            return True
        L = A = None
        if "<" in txt:
            a, b = txt.split("<", 1)
            if a.strip():
                L = self._to_bu(a)
            try:
                A = float(b)
            except ValueError:
                A = None
            if A is None or (a.strip() and L is None):
                self.message = "Can't read '%s'" % txt
                return False
        else:
            L = self._to_bu(txt)
            if L is None:
                self.message = "Can't read '%s'" % txt
                return False
        if last is None:
            self.message = "Pick the first point, then type a length"
            return False
        if L is not None and A is not None:
            a = math.radians(A)
            self._commit_point(last.co + (plane.x * math.cos(a) + plane.y * math.sin(a)) * L)
            return True
        if L is not None:
            self.len_lock = L
            self.message = "Length locked: pick the direction"
        if A is not None:
            self.ang_lock = A
            self.message = "Angle locked: pick the length"
        return True

    def _mouse_signs(self, base, plane):
        """+1/-1 per CPlane axis: which side of `base` the mouse is on (AutoCAD style)."""
        c = self.cursor
        if c is None or base is None:
            return 1.0, 1.0
        d = Vector(c) - Vector(base)
        tol = 1e-9
        ax, ay = self._turned_axes(plane, self.shape_ang if self.anchor is not None else 0.0)
        du, dv = d.dot(ax), d.dot(ay)
        return (-1.0 if du < -tol else 1.0), (-1.0 if dv < -tol else 1.0)

    def _commit_point(self, p):
        self.once = None
        self._push()
        if self.state == "IDLE":
            self.state = "DRAW"
            self.active = None
        self._add_point(p)
        self.write()
        self._refresh_dynamic()

    # ------------------------------------------------------------ events
    # ------------------------------------------------------------ osnap pop-up
    @property
    def snap_st(self):
        return _OnceSettings(self.st, self.once) if self.once else self.st

    def once_label(self):
        for it in MENU_ITEMS:
            if it is not None and it[0] == self.once:
                return it[1]
        return ""

    def _set_once(self, prop):
        self.once = None if self.once == prop else prop
        self.menu = None
        self.message = ("Once: %s (next point only)" % self.once_label()) if self.once else "One-shot Osnap off"

    def _clear_once(self):
        if self.once:
            self.once = None

    def menu_rows(self):
        """[(item or None, x0, y0, x1, y1)] in region pixels, top to bottom."""
        m = self.menu
        u = self.ui
        x0, x1 = m["x"], m["x"] + MENU_W * u
        y = m["y"] - MENU_HEAD * u
        out = []
        for it in MENU_ITEMS:
            h = (MENU_SEP if it is None else MENU_ROW) * u
            out.append((it, x0, y - h, x1, y))
            y -= h
        return out

    def menu_rect(self):
        rows = self.menu_rows()
        return self.menu["x"], rows[-1][2], self.menu["x"] + MENU_W * self.ui, self.menu["y"]

    def open_menu(self, mx, my):
        u = self.ui
        h = (MENU_HEAD + sum(MENU_SEP if it is None else MENU_ROW for it in MENU_ITEMS)) * u
        x = mx + 24 * u
        y = my + h / 2
        if self.view is not None:
            x = min(x, self.view.w - MENU_W * u - 6 * u)
            if x < mx and mx - 24 * u - MENU_W * u > 0:
                x = mx - 24 * u - MENU_W * u
            y = max(min(y, self.view.h - 6 * u), h + 6 * u)
        self.menu = {"x": x, "y": y, "hot": None}
        self.message = "Snap: click the name / letter = once (next point) \u00b7 click the box / Shift = always on/off \u00b7 S / Esc close"

    def _menu_toggle(self, prop):
        setattr(self.st, prop, not getattr(self.st, prop))

    def _menu_event(self, ev, mx, my):
        t, v = ev.type, ev.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.menu["hot"] = None
            self.menu["part"] = None
            for it, x0, y0, x1, y1 in self.menu_rows():
                if it is not None and x0 <= mx <= x1 and y0 <= my <= y1:
                    self.menu["hot"] = it[0]
                    self.menu["part"] = "box" if mx <= x0 + MENU_BOX * self.ui else "name"
            return 'RUNNING'
        if t == 'LEFTMOUSE':
            if v != 'PRESS':
                return 'RUNNING'
            x0, y0, x1, y1 = self.menu_rect()
            if not (x0 <= mx <= x1 and y0 <= my <= y1):
                self.menu = None          # click outside: close, nothing is drawn
                self.message = ""
                return 'RUNNING'
            for it, a0, b0, a1, b1 in self.menu_rows():
                if it is not None and a0 <= mx <= a1 and b0 <= my <= b1:
                    on_box = mx <= a0 + MENU_BOX * self.ui
                    if it[0] in ONCE_PROPS and not ev.shift and not on_box:
                        self._set_once(it[0])
                        self.swallow_release = True     # this click only picks the snap
                    else:
                        self._menu_toggle(it[0])
                    break
            return 'RUNNING'
        if v != 'PRESS':
            return 'RUNNING'
        if t in {'S', 'ESC', 'RET', 'NUMPAD_ENTER', 'RIGHTMOUSE', 'SPACE'}:
            self.menu = None
            self.message = ""
            return 'RUNNING'
        for it in MENU_ITEMS:
            if it is not None and t == it[2]:
                if it[0] in ONCE_PROPS and not ev.shift:
                    self._set_once(it[0])
                else:
                    self._menu_toggle(it[0])
                return 'RUNNING'
        if t == 'A':
            for it in MENU_ITEMS[2:11]:
                setattr(self.st, it[0], True)
        if t == 'X':
            for it in MENU_ITEMS[2:11]:
                setattr(self.st, it[0], False)
        return 'RUNNING'

    def _face_event(self, ev, mx, my):
        from . import cplane
        t, v = ev.type, ev.value
        pts = self.pick_pts
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.compute_cursor(mx, my, ev, skip_snap=True)
            hit = None if ev.alt else cplane.pick(self.ctx, self.view, self.cache, mx, my, self._visible)
            self.snap = None
            if hit is not None:
                s_ = self.view.project(hit[1])
                if s_ is not None:
                    self.snap = snaps.Snap("NEAR" if hit[0] == "edge" else "VERT", hit[1], s_)
            if pts:
                self.cursor = cplane.point_under_mouse(self.ctx, self.view, self.cache, mx, my, self._visible)
            return 'RUNNING'
        if t == 'LEFTMOUSE' and v == 'PRESS':
            self.swallow_release = True
            if pts or ev.ctrl:
                # Rhino "CPlane 3 points": origin, X direction, (optional) a point on the plane
                p = cplane.point_under_mouse(self.ctx, self.view, self.cache, mx, my, self._visible)
                pts.append(p)
                vm = self.view.vm if self.view is not None else None
                if len(pts) == 1:
                    self.message = "CPlane: origin picked \u00b7 click the X direction"
                elif len(pts) == 2:
                    if not cplane.frame_from_points(self.ctx, pts[0], pts[1], None, vm):
                        pts.pop()
                        self.message = "Same point twice: click the X direction"
                        return 'RUNNING'
                    self.message = ("CPlane: X set \u00b7 click a 3rd point to tilt the plane through it, "
                                    "or Enter / right click to keep it")
                else:
                    cplane.frame_from_points(self.ctx, pts[0], pts[1], pts[2], vm)
                    self._end_pick("CPlane through 3 points")
                return 'RUNNING'
            msg = cplane.apply_pick(self.ctx, self.view, self.cache, mx, my, hinge=ev.shift, force_face=ev.alt,
                                    visible=self._visible)
            if msg is None:
                self.message = "Nothing there: click a point, an edge or a face (Esc to cancel)"
                return 'RUNNING'
            self._end_pick(msg)
            return 'RUNNING'
        if v == 'PRESS' and t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and len(pts) >= 2:
            self._end_pick("CPlane by 2 points")
            return 'RUNNING'
        if v == 'PRESS' and t in {'ESC', 'RIGHTMOUSE', 'F'}:
            self._end_pick("CPlane by 2 points" if len(pts) >= 2 else "CPlane unchanged")
        return 'RUNNING'

    def stand_on_last_segment(self):
        """Shift+F while drawing: the CPlane stands up on the last segment (origin = last point, X along the
        segment, turned by the CPlane angle around it), so the curve goes on in the new plane."""
        from . import cplane
        P = self.paths[self.active]
        a, b = P.pts[-2].co, P.pts[-1].co
        if (b - a).length < 1e-9:
            self.message = "Last segment too short"
            return
        vm = self.view.vm if self.view is not None else None
        cplane.align_axis(self.ctx, b, b - a, vm, axis='X')
        cplane.rotate(self.ctx, b, b - a, self.st.cplane_angle, vm)
        cplane.move_origin(self.ctx, b)
        self.message = ("CPlane turned %.4g\u00b0 on the last segment \u00b7 keep drawing (Shift+F again: turn further)"
                        % math.degrees(self.st.cplane_angle))

    def _end_pick(self, msg):
        self.face_pick = False
        self.pick_pts = []
        self.snap = None
        self.message = msg

    def rotate_cplane(self, which, sign=1.0):
        from . import cplane
        ang = float(getattr(self.st, "cplane_angle", math.radians(90))) * sign
        cplane.rotate_local(self.ctx, which, ang, self.view.vm if self.view is not None else None)
        off = self.st.cplane_offset
        self.message = ("CPlane turn  X %.4g\u00b0  Y %.4g\u00b0  Z %.4g\u00b0  (live \u00b7 sidebar Apply makes it stay, "
                        "Reset clears it)" % tuple(math.degrees(a) for a in off))

    def set_tool(self, tool):
        if self.state == "DRAW":
            self.finish_path()
        self.tool = tool
        self.anchor = None
        self.shape_drag = False
        self.buffer = ""
        self.len_lock = self.ang_lock = None
        self.hover = None
        self.message = TOOL_NAMES[tool]

    def handle(self, ev, view):
        """ev: object with type, value, mouse_region_x/y, shift, ctrl, alt, unicode.
        Returns 'RUNNING' / 'FINISHED' / 'PASS'."""
        if self.once and self.menu is None and not self.face_pick and ev.value == 'PRESS' \
                and ev.type == 'ESC' and not self.buffer:
            self.once = None
            self.message = "One-shot Osnap cancelled"
            self.view = view
            self.compute_cursor(ev.mouse_region_x, ev.mouse_region_y, ev)
            return 'RUNNING'
        if getattr(self, "swallow_release", False) and ev.type == 'LEFTMOUSE' and ev.value == 'RELEASE':
            self.swallow_release = False
            return 'RUNNING'
        if ev.type == 'LEFTMOUSE' and ev.value == 'PRESS':
            self.swallow_release = False
        picking = self.once and self.menu is None and not self.face_pick and ev.type == 'LEFTMOUSE' \
            and ev.value == 'RELEASE'
        r = self._handle(ev, view)
        if picking:
            self.once = None             # the one-shot Osnap is used up by this pick
        return r

    def _handle(self, ev, view):
        self.view = view
        mx, my = ev.mouse_region_x, ev.mouse_region_y
        t, v = ev.type, ev.value
        if self.shape_xf is None:          # G / R / S keep the shape's Shift / Alt (Shift = steps on R)
            self.mods = (ev.shift, ev.alt)
        shape = self.tool not in PATH_TOOLS
        if self.menu is not None:
            r = self._menu_event(ev, mx, my)
            if self.menu is None:
                self.compute_cursor(mx, my, ev) if shape else self._on_move(mx, my, ev)
            return r
        if self.face_pick:
            return self._face_event(ev, mx, my)
        if self.xform is not None:
            return self._xform_event(ev, mx, my)
        if self.shape_xf is not None:
            return self._shape_xf_event(ev, mx, my)
        if shape and t in {'G', 'R', 'S'} and v == 'PRESS' and not self.buffer and not ev.ctrl \
                and self.anchor is not None and self.shape_drag is not False and self._shape_xf_start(t, mx, my):
            return 'RUNNING'
        if t == 'G' and v == 'PRESS' and not self.buffer and self.drag is not None \
                and self.drag.get("kind") == "new":
            # G while dragging a new anchor: move it with its handles (press G again to go on shaping it)
            dd = self.drag
            on = not dd.get("g_move", False)
            dd["g_move"] = dd["space"] = on
            p0 = self.paths[dd["path"]].pts[dd["i"]].co
            dd["space_from"] = self._plane_hit(mx, my, p0)[0] if on else None
            dd["moved"] = True
            self.message = ("Move the anchor \u00b7 G again = back to its handles" if on else
                            "Drag the handles \u00b7 G = move the anchor")
            return 'RUNNING'
        if self.tool == 'EDIT' and t in {'G', 'R', 'S'} and v == 'PRESS' and not self.buffer \
                and self.drag is None and not ev.ctrl and not ev.alt and self._xform_start(t, mx, my):
            return 'RUNNING'
        if t == 'S' and v == 'PRESS' and not self.buffer and self.drag is None:
            self.open_menu(mx, my)
            return 'RUNNING'
        if t == 'F' and v == 'PRESS' and ev.shift and not self.buffer and self.drag is None \
                and self.state == "DRAW" and self.active is not None and len(self.paths[self.active].pts) >= 2:
            self.stand_on_last_segment()
            self._on_move(mx, my, ev)
            return 'RUNNING'
        if t == 'F' and v == 'PRESS' and not self.buffer and self.drag is None:
            self.face_pick = True
            self.pick_pts = []
            self.message = ("CPlane: point = origin \u00b7 edge = origin + axis along it \u00b7 Shift+edge = turn around it"
                            " \u00b7 face/Alt = lie on it \u00b7 Ctrl+click = by 2-3 points \u00b7 Esc" +
                            (" \u00b7 the curve goes on after the pick" if self.state == "DRAW" else ""))
            return 'RUNNING'
        if t == 'SPACE' and self.drag is not None:
            # Illustrator: hold Space while dragging a new anchor to reposition it
            if self.drag.get("kind") == "new":
                dd = self.drag
                dd["space"] = (v == 'PRESS') or dd.get("g_move", False)
                p0 = self.paths[dd["path"]].pts[dd["i"]].co
                dd["space_from"] = self._plane_hit(mx, my, p0)[0] if v == 'PRESS' else None
                dd["moved"] = True
            return 'RUNNING'

        rounding = self.drag is not None and self.drag.get("kind") == "round"
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if shape and not rounding:
                self.compute_cursor(mx, my, ev)
                self.hover_widget = self._widget_at(mx, my) if self.anchor is None else None
            else:
                self._on_move(mx, my, ev)
            return 'RUNNING'
        if t == 'LEFTMOUSE':
            if shape and rounding:
                if v == 'RELEASE':
                    self._on_release(mx, my, ev)
            elif shape and v == 'PRESS' and self.anchor is None and not ev.alt and self._widget_at(mx, my):
                self.last_round = None
                self._start_round(self._widget_at(mx, my), Vector((mx, my)))
            elif shape:
                self._shape_mouse(mx, my, ev, v)
            elif v == 'PRESS':
                self._on_press(mx, my, ev)
            elif v == 'RELEASE':
                self._on_release(mx, my, ev)
            return 'RUNNING'
        if v != 'PRESS':
            return 'RUNNING' if t in MODKEYS else 'PASS'
        if t in MODKEYS:
            if shape:
                self.compute_cursor(mx, my, ev)
            else:
                self._on_move(mx, my, ev)
            return 'RUNNING'

        if (t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and not self.buffer) or t == 'RIGHTMOUSE':
            if shape and self.anchor is not None:
                self.anchor = None
                self.message = "Shape cancelled"
                return 'RUNNING'
            if self.state in ("DRAW",):
                self.finish_path()
                if self.mirror_plane() is None:
                    self.message = "Path finished (Enter again to leave the pen)"
                return 'RUNNING'
            return 'FINISHED'
        if t in {'RET', 'NUMPAD_ENTER'}:
            self.apply_buffer()
            self.compute_cursor(mx, my, ev) if shape else self._on_move(mx, my, ev)
            return 'RUNNING'
        if t == 'ESC' and self.sel and not self.buffer and self.state == "IDLE":
            self.sel = set()
            self.message = "Selection cleared"
            return 'RUNNING'
        if t == 'ESC':
            if self.buffer or self.len_lock is not None or self.ang_lock is not None:
                self.buffer = ""
                self.len_lock = self.ang_lock = None
                return 'RUNNING'
            if shape and self.anchor is not None:
                self.anchor = None
                return 'RUNNING'
            if self.state == "DRAW":
                self.finish_path()
                return 'RUNNING'
            return 'FINISHED'
        if t == 'BACK_SPACE' or (t == 'Z' and ev.ctrl and not ev.alt):
            if self.buffer:
                self.buffer = self.buffer[:-1]
            elif shape and self.anchor is not None:
                self.anchor = None
            else:
                self.undo()
            self.compute_cursor(mx, my, ev) if shape else self._on_move(mx, my, ev)
            return 'RUNNING'
        if self.tool == 'POLY' and t in {'UP_ARROW', 'DOWN_ARROW', 'PAGE_UP', 'PAGE_DOWN'} and not self.buffer:
            self.st.poly_sides = max(3, min(64, self.st.poly_sides + (1 if t in {'UP_ARROW', 'PAGE_UP'} else -1)))
            self.message = "%d sides" % self.st.poly_sides
            return 'RUNNING'

        ch = getattr(ev, "unicode", "") or ""
        if ch and ch.isprintable() and (self.buffer or ch in "0123456789.-<@+" or
                                        (ch in "rR" and self.tool != 'EDIT')):
            self.buffer += ch
            return 'RUNNING'
        if t == 'D' and not self.drag:
            self.st.osnap = not self.st.osnap
            self.message = "Snap %s" % ("on" if self.st.osnap else "off")
            self.compute_cursor(mx, my, ev)
            return 'RUNNING'
        if t == 'M' and ev.shift and not self.drag:
            order = ['OFF', 'Y', 'X']
            cur = getattr(self.st, "mirror", 'OFF')
            self.st.mirror = order[(order.index(cur) + 1) % 3 if cur in order else 0]
            self.message = {"OFF": "Mirror off", "Y": "Mirror on: left \u2194 right (axis = CPlane Y through its origin)",
                            "X": "Mirror on: front \u2194 back (axis = CPlane X through its origin)"}[self.st.mirror]
            return 'RUNNING'
        if t in TOOL_KEYS and not self.drag:
            self.set_tool(TOOL_KEYS[t])
            return 'RUNNING'
        if t == 'C' and self.state == "DRAW":
            self.close_active()
            return 'RUNNING'
        if t == 'O':
            self.st.ortho = not self.st.ortho
            self.message = "Ortho %s" % ("on" if self.st.ortho else "off")
            self._on_move(mx, my, ev)
            return 'RUNNING'
        if t == 'Q' and not self.drag:
            # Q / Shift+Q / Alt+Q: turn the CPlane around its X / Y / Z axis (Ctrl: the other way)
            which = 'Y' if ev.shift else ('Z' if ev.alt else 'X')
            self.rotate_cplane(which, -1.0 if ev.ctrl else 1.0)
            self.compute_cursor(mx, my, ev)
            return 'RUNNING'
        if t == 'TAB':
            planes = PLANES + (['OBJECT'] if getattr(self.st, "plane_object", None) is not None else [])
            i = planes.index(self.st.cplane) if self.st.cplane in planes else 0
            self.st.cplane = planes[(i + 1) % len(planes)]
            self.message = "CPlane: %s" % self.st.cplane.title()
            self.compute_cursor(mx, my, ev)
            return 'RUNNING'
        if t == 'K' and not self.drag and not shape:
            self.insert_keys()
            return 'RUNNING'
        if t in {'LEFT_ARROW', 'RIGHT_ARROW', 'UP_ARROW', 'DOWN_ARROW'} and not self.drag and not self.buffer:
            self.step_frame(t)
            return 'RUNNING'
        if t in {'X', 'DEL'} and self.state == "IDLE" and not shape and self.sel:
            self._push()
            for pi, i in sorted(self.sel, reverse=True):
                P = self.paths[pi]
                if i < len(P.pts):
                    del P.pts[i]
            self.paths = [P for P in self.paths if len(P.pts) >= 2 or not P.pts]
            for P in self.paths:
                if len(P.pts) < 3:
                    P.cyclic = False
            self.message = "%d point(s) deleted" % len(self.sel)
            self.sel = set()
            self.write()
            self._refresh_dynamic()
            return 'RUNNING'
        if t in {'X', 'DEL'} and self.state == "IDLE" and not shape:
            h = self.pick(mx, my)
            if h and h[0] == "pt":
                self.delete_point(h[1], h[2])
                self._on_move(mx, my, ev)
            return 'RUNNING'
        return 'PASS'

    # ------------------------------------------------------------ shapes
    def _shape_mouse(self, mx, my, ev, value):
        m = Vector((mx, my))
        if value == 'PRESS':
            p = self.compute_cursor(mx, my, ev)
            if self.anchor is None:
                self.anchor = p.copy()
                self.shape_drag = m
                self.shape_ang = 0.0
                self.shape_moved = False
                self.message = SHAPE_HINT[self.tool] + SHAPE_DRAG_HINT
            else:
                self.commit_shape(p, ev.shift, ev.alt)
        elif value == 'RELEASE' and self.anchor is not None and self.shape_drag is not False:
            start = self.shape_drag
            self.shape_drag = False
            if (m - start).length >= DRAG_PX * 2 or self.shape_moved:
                self.commit_shape(self.compute_cursor(mx, my, ev), ev.shift, ev.alt)

    def shape_geometry(self, a, b, shift, alt):
        """(points, centre, info) of the shape from the first point a to the cursor b."""
        plane = self.cplane()
        a, b = plane.project(a), plane.project(b)     # shapes always lie on the CPlane
        px, py = self._turned_axes(plane, self.shape_ang)
        d = b - a
        u, v = d.dot(px), d.dot(py)
        us = self.ctx.scene.unit_settings

        def fmt(x):
            try:
                return bpy.utils.units.to_string(us.system, 'LENGTH', abs(x) * (us.scale_length or 1.0), precision=3)
            except Exception:      # noqa: BLE001
                return "%.3f" % abs(x)

        if self.tool == 'RECT':
            if shift:
                sz = max(abs(u), abs(v))
                u, v = math.copysign(sz, u or 1.0), math.copysign(sz, v or 1.0)
            if alt:
                c = a
                corners = [(-u, -v), (u, -v), (u, v), (-u, v)]
                w, h = 2 * abs(u), 2 * abs(v)
            else:
                c = a + px * (u / 2) + py * (v / 2)
                corners = [(-u / 2, -v / 2), (u / 2, -v / 2), (u / 2, v / 2), (-u / 2, v / 2)]
                w, h = abs(u), abs(v)
            pts = [Pt(c + px * x + py * y, CORNER) for x, y in corners]
            if u * v < 0:
                pts.reverse()
            return pts, c, "%s \u00d7 %s" % (fmt(w), fmt(h))
        if self.tool == 'CIRCLE':
            if alt:                                   # Alt: from the centre, the cursor on the circle
                c = a
                r = math.hypot(u, v)
                e1 = (px * u + py * v)
                e1 = e1.normalized() if e1.length > 1e-12 else px.copy()
            else:                                     # inscribed in the square from corner a to the cursor
                sz = max(abs(u), abs(v))
                c = a + px * math.copysign(sz / 2, u or 1.0) + py * math.copysign(sz / 2, v or 1.0)
                r = sz / 2
                e1 = px.copy()
            e2 = plane.n.cross(e1)
            k = 0.5522847498 * r
            pts = []
            for ang in range(4):
                ca, sa = math.cos(ang * math.pi / 2), math.sin(ang * math.pi / 2)
                dirv = e1 * ca + e2 * sa
                tan = e2 * ca - e1 * sa
                co = c + dirv * r
                pts.append(Pt(co, SMOOTH, co - tan * k, co + tan * k))
            return pts, c, "R %s   \u00d8 %s" % (fmt(r), fmt(2 * r))
        # POLY
        n = max(3, int(self.st.poly_sides))
        if alt:                                       # Alt: from the centre, a corner on the cursor
            c = a
            r = math.hypot(u, v)
            a0 = math.atan2(v, u)
            if shift:
                a0 = -math.pi / 2 + math.pi / n       # flat bottom edge, like a drawn polygon
        else:                                         # inscribed in the circle inscribed in the square a-cursor
            sz = max(abs(u), abs(v))
            c = a + px * math.copysign(sz / 2, u or 1.0) + py * math.copysign(sz / 2, v or 1.0)
            r = sz / 2
            a0 = -math.pi / 2 + math.pi / n           # flat bottom edge
        pts = [Pt(c + px * (r * math.cos(a0 + 2 * math.pi * k / n)) +
                  py * (r * math.sin(a0 + 2 * math.pi * k / n)), CORNER) for k in range(n)]
        side = 2 * r * math.sin(math.pi / n)
        return pts, c, "%d sides   R %s   side %s" % (n, fmt(r), fmt(side))

    def shape_preview(self):
        if self.tool in PATH_TOOLS or self.anchor is None or self.cursor is None:
            return [], ""
        pts, _c, info = self.shape_geometry(self.anchor, self.cursor, *self.mods)
        P = Path(pts, True)
        out = []
        for i in range(P.nseg()):
            out += geom.bez_samples(*P.seg(i), 16)
        return out, info

    def commit_shape(self, b, shift=False, alt=False):
        pts, c, info = self.shape_geometry(self.anchor, b, shift, alt)
        if (pts[0].co - pts[2].co).length < 1e-9:
            self.message = "Too small"
            return False
        self._push()
        ob = self._new_object(c, TOOL_NAMES[self.tool], self.shape_ang)
        P = Path(pts, True, ob)
        self.paths.append(P)
        self.target = ob
        self.write(ob)
        self.anchor = None
        self.shape_drag = False
        self.shape_xf = None
        self.shape_moved = False
        self._refresh_dynamic()
        self.message = "%s %s" % (TOOL_NAMES[self.tool], info)
        return True

    def apply_shape_buffer(self, txt):
        plane = self.work_plane(self.anchor)
        shift, alt = self.mods
        ax, ay = self._turned_axes(plane, self.shape_ang)
        if self.tool == 'RECT':
            parts = [self._to_bu(t) for t in txt.replace(";", ",").split(",")]
            if any(x is None for x in parts) or len(parts) not in (1, 2):
                self.message = "Type width,height (or one value for a square)"
                return False
            su, sv = self._mouse_signs(self.anchor, plane)    # + = towards the mouse
            w = parts[0] * su
            h = (parts[1] if len(parts) == 2 else parts[0]) * sv
            if alt:
                b = self.anchor + ax * (w / 2) + ay * (h / 2)
            else:
                b = self.anchor + ax * w + ay * h
            return self.commit_shape(b, False, alt)
        r = self._to_bu(txt)
        if r is None:
            self.message = "Type the radius"
            return False
        if alt:                                       # Alt: the anchor is the centre
            d = (self.cursor - self.anchor) if self.cursor is not None else plane.x
            d = d - plane.n * d.dot(plane.n)
            d = d.normalized() if d.length > 1e-12 else ax.copy()
            return self.commit_shape(self.anchor + d * r, shift, True)
        su, sv = self._mouse_signs(self.anchor, plane)  # the anchor is a corner: the square goes towards the mouse
        return self.commit_shape(self.anchor + ax * (2 * r * su) + ay * (2 * r * sv), shift, False)

    # ------------------------------------------------------------ shapes: G / R / S while dragging
    @staticmethod
    def _turned_axes(plane, ang):
        """The CPlane X / Y axes turned by `ang` around its normal (normal = x cross y kept)."""
        x, y = Vector(plane.x).normalized(), Vector(plane.y).normalized()
        if abs(ang) < 1e-12:
            return x, y
        c, s_ = math.cos(ang), math.sin(ang)
        return x * c + y * s_, y * c - x * s_

    def _shape_xf_start(self, key, mx, my):
        """Blender style G / R / S on the shape being dragged, from where it is now."""
        if self.anchor is None or self.cursor is None:
            return False
        plane = self.cplane()
        a, b = plane.project(self.anchor), plane.project(self.cursor)
        shift, alt = self.mods
        _pts, c, _info = self.shape_geometry(a, b, shift, alt)
        m0 = self._plane_hit(mx, my, c)[0]
        if m0 is None:
            return False
        self.snap = None
        self.track_draw = []
        self.shape_xf = {"mode": key, "a": a.copy(), "b": b.copy(), "c": Vector(c), "ang": self.shape_ang,
                         "m0": m0, "last": m0 - c, "turn": 0.0, "axis": None,
                         "orig": (a.copy(), b.copy(), self.shape_ang)
                         if self.shape_xf is None else self.shape_xf["orig"]}
        self.shape_moved = True
        self._shape_xf_message()
        return True

    def _shape_xf_message(self):
        X = self.shape_xf
        extra = {'G': " \u00b7 X / Y lock an axis", 'R': " \u00b7 Shift 15\u00b0", 'S': ""}[X["mode"]]
        lock = (" (%s)" % X["axis"]) if X["axis"] else ""
        self.message = "%s shape%s \u00b7 %s again = draw on \u00b7 release = place%s \u00b7 Esc undo" % (
            XF_NAMES[X["mode"]], lock, X["mode"], extra)

    def _shape_xf_apply(self, mx, my, ev):
        X = self.shape_xf
        plane = self.cplane()
        c, a0, b0 = X["c"], X["a"], X["b"]
        T = self._plane_hit(mx, my, c)[0]
        if T is None:
            return
        n = Vector(plane.n).normalized()
        mode = X["mode"]
        if mode == 'G':
            d = T - X["m0"]
            d -= n * d.dot(n)
            if X["axis"] is not None:
                ax = Vector(plane.x if X["axis"] == 'X' else plane.y).normalized()
                d = ax * d.dot(ax)
            self.anchor, self.cursor = a0 + d, b0 + d
            self.shape_ang = X["ang"]
            return
        v1 = T - c
        v1 -= n * v1.dot(n)
        if mode == 'R':
            v0 = X["last"]
            if v1.length > 1e-12 and v0.length > 1e-12:
                X["turn"] += math.atan2(n.dot(v0.cross(v1)), v0.dot(v1))   # summed: turns past 180 degrees
                X["last"] = v1
            ang = X["turn"]
            if ev.shift:
                step = math.radians(15.0)
                ang = round(ang / step) * step
            R = Matrix.Rotation(ang, 3, n)
            self.anchor, self.cursor = c + R @ (a0 - c), c + R @ (b0 - c)
            self.shape_ang = X["ang"] + ang
            return
        r0 = X["m0"] - c
        r0 -= n * r0.dot(n)
        f = v1.length / r0.length if r0.length > 1e-12 else 1.0
        self.anchor, self.cursor = c + (a0 - c) * f, c + (b0 - c) * f
        self.shape_ang = X["ang"]

    def _shape_xf_end(self):
        """Back to drawing: the mouse jumps to the corner being dragged so the shape does not."""
        self.shape_xf = None
        if self.cursor is not None and self.view is not None:
            s_ = self.view.project(self.cursor)
            if s_ is not None:
                self.warp = (int(round(s_.x)), int(round(s_.y)))
        self.message = SHAPE_HINT[self.tool] + SHAPE_DRAG_HINT

    def _shape_xf_event(self, ev, mx, my):
        X = self.shape_xf
        t, v = ev.type, ev.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'} or t in MODKEYS:
            self._shape_xf_apply(mx, my, ev)
            return 'RUNNING'
        if t == 'LEFTMOUSE' and v == 'RELEASE':
            shift, alt = self.mods
            self.commit_shape(self.cursor, shift, alt)
            self.compute_cursor(mx, my, ev)
            return 'RUNNING'
        if v != 'PRESS':
            return 'RUNNING'
        if t in {'ESC', 'RIGHTMOUSE'}:
            a, b, ang = X["orig"]
            self.anchor, self.cursor, self.shape_ang = a, b, ang
            self._shape_xf_end()
            self.message = "Move cancelled \u00b7 " + self.message
            return 'RUNNING'
        if t in {'G', 'R', 'S'}:
            if t == X["mode"]:
                self._shape_xf_end()
            else:                                     # switch G <-> R <-> S from where the shape is now
                orig = X["orig"]
                self.shape_xf = None
                if self._shape_xf_start(t, mx, my):
                    self.shape_xf["orig"] = orig
            return 'RUNNING'
        if t in {'X', 'Y'} and X["mode"] == 'G':
            X["axis"] = None if X["axis"] == t else t
            self._shape_xf_apply(mx, my, ev)
            self._shape_xf_message()
            return 'RUNNING'
        return 'RUNNING'

    # ------------------------------------------------------------ Direct Selection: G / R / S
    def _xform_targets(self):
        """(kind, items, pivot): 'handle' with [(path, i, side)] around its anchor, or 'points' with the anchors."""
        if self.sel_handle is not None:
            pi, i, side = self.sel_handle
            if pi < len(self.paths) and i < len(self.paths[pi].pts):
                p = self.paths[pi].pts[i]
                if p.kind != CORNER:
                    return "handle", [self.sel_handle], p.co.copy()
            self.sel_handle = None
        keys = [k for k in self.sel if k[0] < len(self.paths) and k[1] < len(self.paths[k[0]].pts)]
        if not keys and self.hover is not None and self.hover[0] == "pt":
            keys = [(self.hover[1], self.hover[2])]
        if not keys:
            return None
        piv = Vector()
        for pi, i in keys:
            piv += self.paths[pi].pts[i].co
        return "points", keys, piv / len(keys)

    def _xform_start(self, key, mx, my):
        tg = self._xform_targets()
        if tg is None:
            self.message = "Select points or a handle first (click, Shift+click or Ctrl+drag a box)"
            return False
        kind, items, piv = tg
        mode = {'G': 'MOVE', 'R': 'ROTATE', 'S': 'SCALE'}[key]
        self._push()
        self._refresh_dynamic(skip={(it[0], it[1]) for it in items})   # don't snap to what is moving
        snap_ = {}
        for it in items:
            pi, i = it[0], it[1]
            p = self.paths[pi].pts[i]
            snap_[(pi, i)] = (p.co.copy(), p.hl.copy(), p.hr.copy(), p.kind)
        start, _plane = self._plane_hit(mx, my, piv)
        if kind == "handle":
            pi, i, side = items[0]
            ref = getattr(self.paths[pi].pts[i], side).copy()
        else:
            ref = self.paths[items[0][0]].pts[items[0][1]].co.copy() if len(items) == 1 else piv.copy()
        self.xform = {"mode": mode, "kind": kind, "items": items, "pivot": piv, "snap": snap_,
                      "start": start, "ref": ref, "typed": ""}
        self._xform_apply(mx, my, None)
        return True

    def _xform_restore(self):
        X = self.xform
        for (pi, i), (co, hl, hr, k) in X["snap"].items():
            p = self.paths[pi].pts[i]
            p.co, p.hl, p.hr, p.kind = co.copy(), hl.copy(), hr.copy(), k

    def _xform_apply(self, mx, my, ev, typed=None):
        X = self.xform
        self._xform_restore()
        piv, mode = X["pivot"], X["mode"]
        hit, plane = self._plane_hit(mx, my, piv)
        n = Vector(plane.n).normalized() if hasattr(plane, "n") else Vector(plane.x).cross(Vector(plane.y)).normalized()
        if mode == 'MOVE':
            if typed is not None:
                return
            target = self.compute_cursor(mx, my, ev) if ev is not None else hit
            if target is None or hit is None:
                return
            M = Matrix.Translation(Vector(target) - X["ref"])
            info = "Move %.4g" % (Vector(target) - X["ref"]).length
        else:
            a0 = None if X["start"] is None else X["start"] - piv
            a1 = None if hit is None else hit - piv
            if mode == 'ROTATE':
                if typed is not None:
                    ang = math.radians(typed)
                else:
                    if a0 is None or a1 is None or a0.length < 1e-9 or a1.length < 1e-9:
                        return
                    ang = math.atan2(n.dot(a0.cross(a1)), a0.dot(a1))
                    if ev is not None and ev.shift:
                        ang = round(ang / math.radians(15)) * math.radians(15)
                M = Matrix.Translation(piv) @ Matrix.Rotation(ang, 4, n) @ Matrix.Translation(-piv)
                info = "Rotate %.4g\u00b0" % math.degrees(ang)
            else:
                if typed is not None:
                    f = typed
                else:
                    if a0 is None or a1 is None or a0.length < 1e-9:
                        return
                    f = a1.length / a0.length
                    if ev is not None and ev.shift:
                        f = round(f * 10) / 10
                M = Matrix.Translation(piv) @ Matrix.Scale(f, 4) @ Matrix.Translation(-piv)
                info = "Scale %.4g" % f
        if X["kind"] == "handle":
            pi, i, side = X["items"][0]
            p = self.paths[pi].pts[i]
            setattr(p, side, M @ getattr(p, side))
            if p.kind == CORNER:
                p.kind = CUSP
            self._realign(p, side)                     # a smooth point keeps its other handle opposite
        else:
            for pi, i in X["items"]:
                p = self.paths[pi].pts[i]
                hl, hr = (Vector(h) for h in self.paths[pi].handles(i)) if p.kind == CORNER else (p.hl, p.hr)
                p.co, p.hl, p.hr = M @ p.co, M @ hl, M @ hr
        X["info"] = info
        self.message = info + ("  (typed: %s)" % X["typed"] if X["typed"] else "") + \
            " \u00b7 click/Enter confirm \u00b7 right click/Esc cancel" + \
            (" \u00b7 Shift 15\u00b0" if mode == 'ROTATE' else "") + (" \u00b7 type a value" if mode != 'MOVE' else "")
        for ob in {self.paths[it[0]].ob for it in X["items"]}:
            self.write(ob)

    def _xform_event(self, ev, mx, my):
        X = self.xform
        t, v = ev.type, ev.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'} or (t in MODKEYS):
            if not X["typed"]:
                self._xform_apply(mx, my, ev)
            return 'RUNNING'
        if v != 'PRESS':
            return 'RUNNING'
        if t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'}:
            if X["typed"] and t != 'LEFTMOUSE':
                try:
                    self._xform_apply(mx, my, ev, typed=float(X["typed"]))
                except ValueError:
                    pass
            self.xform = None
            self.message = X.get("info", "") + " \u2713"
            self._refresh_dynamic()
            return 'RUNNING'
        if t in {'RIGHTMOUSE', 'ESC'}:
            self._xform_restore()
            for ob in {self.paths[it[0]].ob for it in X["items"]}:
                self.write(ob)
            self.xform = None
            if self.history:
                self.history.pop()
            self.message = "Cancelled"
            return 'RUNNING'
        if t in {'G', 'R', 'S'} and not X["typed"]:
            self._xform_restore()
            self.xform = None
            if self.history:
                self.history.pop()
            self._xform_start(t, mx, my)
            return 'RUNNING'
        ch = getattr(ev, "unicode", "") or ""
        if t == 'BACK_SPACE':
            X["typed"] = X["typed"][:-1]
        elif ch and ch in "0123456789.-" and X["mode"] != 'MOVE':
            X["typed"] += ch
        else:
            return 'RUNNING'
        try:
            val = float(X["typed"]) if X["typed"] not in ("", "-", ".", "-.") else None
        except ValueError:
            val = None
        if val is None:
            self._xform_apply(mx, my, ev)
        else:
            self._xform_apply(mx, my, ev, typed=val)
        return 'RUNNING'

    def _plane_hit(self, mx, my, through):
        plane = self.work_plane(through)
        ro, rd = self.view.ray(mx, my)
        return plane.hit(ro, rd), plane

    def _realign(self, p, moved_side):
        """Keep a smooth point smooth: the other handle turns to stay opposite (its length kept)."""
        if p.kind != SMOOTH:
            return
        h = p.hl if moved_side == "hl" else p.hr
        dirv = p.co - h
        if dirv.length < 1e-12:
            return
        other = "hr" if moved_side == "hl" else "hl"
        ln = (getattr(p, other) - p.co).length
        if ln < 1e-12:
            ln = dirv.length
        setattr(p, other, p.co + dirv.normalized() * ln)

    def _on_move(self, mx, my, ev):
        d = self.drag
        if d is None:
            self.compute_cursor(mx, my, ev)
            self.hover = self.pick(mx, my, full=(self.state == "IDLE" or ev.ctrl or ev.alt))
            if self.hover is not None:
                self.focus = self.hover[1]
            self.hover_widget = self._widget_at(mx, my) if self.state == "IDLE" else None
            return
        m = Vector((mx, my))
        if not d["moved"] and (m - d["start"]).length >= DRAG_PX:
            d["moved"] = True
        if d["kind"] == "box":
            self.box = (d["start"], m)
            return
        if d["kind"] == "round":
            if d["moved"]:
                self._snap_round = bool(ev.ctrl)
                self._round_move(mx, my)
            return
        if not d["moved"]:
            return
        P = self.paths[d["path"]]
        kind = d["kind"]
        if kind != "move":
            self.snap = None

        if kind == "seg":
            # reshape a segment live (Illustrator: Alt/Ctrl drag on a segment)
            t = min(max(d["t"], 0.05), 0.95)
            T, pl = self._plane_hit(mx, my, d["b0"])
            dv = T - d["b0"]
            b1, b2 = 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t
            a, b = P.pts[d["i"]], P.pts[(d["i"] + 1) % len(P.pts)]
            chord = b.co - a.co
            perp = pl.n.cross(chord)
            if ev.shift and perp.length > 1e-12 and chord.length > 1e-12:
                # Shift: both handles perpendicular to the chord, equal length (Illustrator)
                perp.normalize()
                off = (T - a.co).dot(perp)
                hlen = off / (b1 + b2)
                a.hr = a.co + perp * hlen
                b.hl = b.co + perp * hlen
            else:
                k = 1.0 / (b1 * b1 + b2 * b2)
                a.hr = d["p1"] + dv * (b1 * k)
                b.hl = d["p2"] + dv * (b2 * k)
            self._realign(a, "hr")
            self._realign(b, "hl")
            self.target = P.ob
            self.write(P.ob)
            return

        p = P.pts[d["i"]]
        if kind == "new" and d.get("space"):
            # Space held while dragging: reposition the new anchor, handles follow
            T, _pl = self._plane_hit(mx, my, p.co)
            if d.get("space_from") is None:
                d["space_from"] = T
            p.move(T - d["space_from"])
            d["space_from"] = T
            self.write(P.ob)
            return
        if kind in ("new", "convert", "lasth"):
            h, plane = self._plane_hit(mx, my, p.co)
            if ev.shift:
                h = self._constrain(p.co, h, plane, float(getattr(self.st, "shift_angle", 45.0)))
            if d.get("closing"):
                # dragging on the first point while closing shapes it like continuing the path
                if ev.alt:
                    p.hl, p.kind = p.co * 2 - h, CUSP
                else:
                    p.hl, p.hr, p.kind = p.co * 2 - h, h, SMOOTH
            elif kind == "lasth":
                p.hr, p.kind = h, CUSP                 # new outgoing direction from the last anchor
            elif kind == "convert" or not ev.alt:
                p.hr, p.hl, p.kind = h, p.co * 2 - h, SMOOTH
            else:
                p.hr, p.kind = h, CUSP                 # Alt while dragging: break the tangent
        elif kind == "move":
            np_ = self.compute_cursor(mx, my, ev)
            if ev.shift and d.get("orig") is not None:
                # Shift: move only along the CPlane axes (perpendicular), from where the point started
                np_ = self._constrain(d["orig"], np_, self.cplane(), 90.0)
                self.cursor = np_
            delta = np_ - p.co
            group = d.get("group") or {(d["path"], d["i"])}
            objs = set()
            for gpi, gi in group:
                GP = self.paths[gpi]
                GP.pts[gi].move(delta)
                objs.add(GP.ob)
            for o in objs:
                if o is not P.ob:
                    self.write(o)
        elif kind in ("hl", "hr"):
            h, plane = self._plane_hit(mx, my, p.co)
            snap = None if not self.snap_st.osnap else self.cache.find(self.view, mx, my, self.snap_st, plane=plane,
                                                                   visible=self._visible)
            if snap is not None and snap.kind != "GRID":
                h = snap.point
            elif ev.shift:
                h = self._constrain(p.co, h, plane, float(getattr(self.st, "shift_angle", 45.0)))
            setattr(p, kind, h)
            if d.get("break") or ev.alt or p.kind == CUSP:
                p.kind = CUSP                          # Alt: handles move independently
            else:
                p.kind = SMOOTH
                self._realign(p, kind)
        self.target = P.ob
        self.write(P.ob)

    def _start_seg_drag(self, pi, i, t, m):
        P = self.paths[pi]
        n = len(P.pts)
        a, b = P.pts[i], P.pts[(i + 1) % n]
        p0, p1, p2, p3 = P.seg(i)
        for q, k in ((a, i), (b, (i + 1) % n)):
            if q.kind == CORNER:
                q.hl, q.hr = (Vector(h) for h in P.handles(k))
                q.kind = CUSP
        a.hr, b.hl = p1.copy(), p2.copy()
        self.drag = {"kind": "seg", "path": pi, "i": i, "t": t, "start": m, "moved": False,
                     "b0": geom.bez(p0, p1, p2, p3, t), "p1": p1.copy(), "p2": p2.copy()}

    def _on_press(self, mx, my, ev):
        if self.tool == 'EDIT':
            ev = _EditEvent(ev)                 # Direct Selection: always the pen's Ctrl behaviour
        m = Vector((mx, my))
        self.last_round = None
        if self.state == "IDLE" and not ev.alt:
            wk = self._widget_at(mx, my)
            if wk is not None:
                self._start_round(wk, m)       # Illustrator live corner: drag the dot inside the corner
                return
        full = self.state == "IDLE" or ev.ctrl or ev.alt
        h = self.pick(mx, my, full=full)

        # Ctrl = direct selection (temporary): move anchors, handles, segments;
        # Ctrl+click on an anchor = cusp <-> smooth tangent
        if ev.ctrl and h is None:
            # Ctrl+drag on empty space: selection box (Shift adds to the selection)
            self.drag = {"kind": "box", "start": m, "moved": False, "add": ev.shift}
            self.box = (m, m)
            return
        if ev.ctrl and h is not None:
            self._push()
            if h[0] == "pt":
                group = self._group_for((h[1], h[2]))
                self._refresh_dynamic(skip=group)
                self.drag = {"kind": "move", "path": h[1], "i": h[2], "start": m, "moved": False,
                             "click": h, "ctrl": True, "group": group, "toggle_sel": ev.shift,
                             "orig": self.paths[h[1]].pts[h[2]].co.copy()}
            elif h[0] in ("hl", "hr"):
                self.drag = {"kind": h[0], "path": h[1], "i": h[2], "start": m, "moved": False}
            else:
                self._start_seg_drag(h[1], h[2], h[3], m)
                self.drag["ctrl"] = True
            return
        # Alt = convert anchor point: drag an anchor = new smooth handles, click = corner;
        # drag a handle = break it; drag a segment = bend it, click = straight <-> curved
        if ev.alt and h is not None:
            self._push()
            if h[0] == "pt":
                self.drag = {"kind": "convert", "path": h[1], "i": h[2], "start": m, "moved": False, "click": h}
            elif h[0] in ("hl", "hr"):
                self.drag = {"kind": h[0], "path": h[1], "i": h[2], "start": m, "moved": False, "break": True}
            else:
                self._start_seg_drag(h[1], h[2], h[3], m)
                self.drag["alt"] = True
            return

        if self.state == "DRAW":
            P = self.paths[self.active]
            if h and h[0] == "pt" and h[1] == self.active:
                if h[2] == 0 and len(P.pts) >= 3:
                    self._push()
                    P.cyclic = True
                    self.drag = {"kind": "new", "path": self.active, "i": 0, "start": m, "moved": False,
                                 "closing": True}
                    return
                if h[2] == len(P.pts) - 1:
                    # click the last anchor = drop its outgoing handle; drag it = new outgoing handle
                    self._push()
                    self.drag = {"kind": "lasth", "path": self.active, "i": h[2], "start": m, "moved": False}
                    return
            p = self.compute_cursor(mx, my, ev)
            self._push()
            self._add_point(p)
            self.drag = {"kind": "new", "path": self.active, "i": len(P.pts) - 1, "start": m, "moved": False}
            self.write(P.ob)
            return
        # IDLE
        if h is not None:
            if h[0] in ("hl", "hr"):
                self._push()
                self.drag = {"kind": h[0], "path": h[1], "i": h[2], "start": m, "moved": False}
                return
            if h[0] == "pt":
                self._push()
                group = self._group_for((h[1], h[2]))
                self._refresh_dynamic(skip=group)
                self.drag = {"kind": "move", "path": h[1], "i": h[2], "start": m, "moved": False, "click": h,
                             "group": group, "orig": self.paths[h[1]].pts[h[2]].co.copy(), "new_from": ev.shift}
                return
            if h[0] == "seg":
                if ev.shift:
                    # Shift (Illustrator: no auto add/delete): a new path starting on that segment
                    self._start_new_at(geom.bez(*self.paths[h[1]].seg(h[2]), h[3]), m)
                    return
                self.insert_point(h[1], h[2], h[3])
                return
        p = self.compute_cursor(mx, my, ev)
        self._push()
        self.state = "DRAW"
        self.active = None
        self._add_point(p)
        self.drag = {"kind": "new", "path": self.active, "i": 0, "start": m, "moved": False}
        self.write(self.target)

    def _start_new_at(self, p, m):
        """a new path whose first point is exactly p (an anchor or a point of another curve)"""
        self._push()
        self.state = "DRAW"
        self.active = None
        self._add_point(Vector(p))
        self.drag = {"kind": "new", "path": self.active, "i": 0, "start": m, "moved": False}
        self.write(self.target)

    # ------------------------------------------------------------ live corners (Illustrator)
    def _corner_geom(self, P, i):
        """(corner, unit dir to prev, unit dir to next, interior angle, len prev, len next) or None."""
        n = len(P.pts)
        if n < 3 and not P.cyclic:
            return None
        if not P.cyclic and i in (0, n - 1):
            return None
        p = P.pts[i]
        c = p.co
        if p.kind == CORNER:
            pass
        elif p.kind == CUSP and (p.hl - c).length < 1e-9 and (p.hr - c).length < 1e-9:
            pass
        else:
            return None
        a, b = P.pts[i - 1].co, P.pts[(i + 1) % n].co
        u1, u2 = a - c, b - c
        if u1.length < 1e-9 or u2.length < 1e-9:
            return None
        l1, l2 = u1.length, u2.length
        u1, u2 = u1.normalized(), u2.normalized()
        th = u1.angle(u2)
        if th < math.radians(1.0) or th > math.radians(179.0):
            return None
        return c, u1, u2, th, l1, l2

    def _arc_corner(self, P, i):
        """A rounded corner made by Vector Draw (points i, i+1 joined by a circular arc between two straight
        edges): (virtual corner, unit dir to prev, unit dir to next, angle, len prev, len next, radius) or None."""
        from mathutils.geometry import intersect_line_line
        n = len(P.pts)
        if n < 3 or (not P.cyclic and (i < 1 or i + 2 > n - 1)):
            return None
        A, B = P.pts[i], P.pts[(i + 1) % n]
        if A.kind != CUSP or B.kind != CUSP:
            return None
        if (A.hl - A.co).length > 1e-7 or (B.hr - B.co).length > 1e-7:
            return None
        ha, hb = A.hr - A.co, B.hl - B.co
        if ha.length < 1e-9 or hb.length < 1e-9:
            return None
        prv, nxt = P.pts[i - 1].co, P.pts[(i + 2) % n].co
        d1 = A.co - prv
        d2 = nxt - B.co
        if d1.length < 1e-9 or d2.length < 1e-9:
            return None
        d1, d2 = d1.normalized(), d2.normalized()
        if ha.normalized().dot(d1) < 0.9999 or hb.normalized().dot(-d2) < 0.9999:
            return None
        res = intersect_line_line(A.co, A.co + d1, B.co, B.co - d2)
        if res is None:
            return None
        c = (res[0] + res[1]) / 2
        ta, tb = (A.co - c).length, (B.co - c).length
        if ta < 1e-9 or abs(ta - tb) > 1e-4 * max(ta, tb) + 1e-7:
            return None
        u1, u2 = -d1, d2
        th = u1.angle(u2)
        if th < math.radians(1.0) or th > math.radians(179.0):
            return None
        return c, u1, u2, th, (prv - c).length, (nxt - c).length, ta * math.tan(th / 2.0)

    def corner_widgets(self):
        """[((path, index, kind), widget world point)] — kind 'sharp' (a corner point) or 'arc' (a corner
        rounded before: points index, index+1). Shown on the curves in focus when no path/shape is in progress."""
        if self.state != "IDLE" or self.view is None or self.anchor is not None:
            return []
        show = set()
        if self.hover is not None:
            show.add(self.hover[1])
        if getattr(self, "focus", None) is not None:
            show.add(self.focus)
        try:
            sel_obs = {o for o in self.ctx.selected_objects}
        except (AttributeError, ReferenceError):
            sel_obs = set()
        show |= {pi for pi, P in enumerate(self.paths) if P.ob in sel_obs}
        if self.hover_widget is not None:
            show.add(self.hover_widget[0])
        show |= {pi for pi, P in enumerate(self.paths) if self.target is not None and P.ob is self.target}
        show |= {pi for pi, _i in self.sel}
        out = []
        for pi in show:
            if pi >= len(self.paths):
                continue
            P = self.paths[pi]
            px = 22.0 * self.ui
            for i in range(len(P.pts)):
                g = self._corner_geom(P, i)
                if g is not None:
                    c, u1, u2 = g[0], g[1], g[2]
                    bis = (u1 + u2).normalized()
                    out.append(((pi, i, "sharp"), c + bis * (px * self.view.pixel_size(c))))
                    continue
                a = self._arc_corner(P, i)
                if a is not None:
                    c, u1, u2 = a[0], a[1], a[2]
                    bis = (u1 + u2).normalized()
                    j = (i + 1) % len(P.pts)
                    mid = geom.bez(P.pts[i].co, P.pts[i].hr, P.pts[j].hl, P.pts[j].co, 0.5)
                    out.append(((pi, i, "arc"), mid + bis * (px * self.view.pixel_size(mid))))
        return out

    def _widget_at(self, mx, my):
        m = Vector((mx, my))
        best = None
        for key, w in self.corner_widgets():
            s_ = self.view.project(w)
            if s_ is not None and (s_ - m).length <= 7.0 * self.ui:
                d = (s_ - m).length
                if best is None or d < best[0]:
                    best = (d, key)
        return best[1] if best else None

    @staticmethod
    def _fillet(P, i, r):
        """Replace corner i of P by two points joined by a circular arc of radius r (Bezier)."""
        n = len(P.pts)
        c = P.pts[i].co.copy()
        a, b = P.pts[i - 1].co, P.pts[(i + 1) % n].co
        u1, u2 = (a - c).normalized(), (b - c).normalized()
        th = u1.angle(u2)
        t = r / math.tan(th / 2.0)
        p1, p2 = c + u1 * t, c + u2 * t
        phi = math.pi - th                                  # how much the direction turns
        k = 4.0 / 3.0 * math.tan(phi / 4.0) * r            # handle length of a circular arc
        A = Pt(p1, CUSP, p1.copy(), p1 + (c - p1).normalized() * k)
        B = Pt(p2, CUSP, p2 + (c - p2).normalized() * k, p2.copy())
        A.tilt = B.tilt = P.pts[i].tilt
        A.radius = B.radius = P.pts[i].radius
        P.pts[i:i + 1] = [A, B]

    def _round_corners(self, d, r):
        """Re-apply rounding with radius r to the corners of the drag (from the saved originals)."""
        for pi, orig in d["orig"].items():
            self.paths[pi].pts = copy.deepcopy(orig)
        r_used = r
        for pi, idxs in d["corners"].items():
            P = self.paths[pi]
            n = len(P.pts)
            # the largest radius every corner of the group can take
            for i in idxs:
                g = self._corner_geom(P, i)
                if g is None:
                    continue
                _c, _u1, _u2, th, l1, l2 = g
                f1 = 0.5 if ((i - 1) % n) in idxs else 1.0
                f2 = 0.5 if ((i + 1) % n) in idxs else 1.0
                r_used = min(r_used, min(l1 * f1, l2 * f2) * math.tan(th / 2.0))
        r_used = max(r_used, 0.0)
        if r_used > 1e-9:
            for pi, idxs in d["corners"].items():
                P = self.paths[pi]
                for i in sorted(idxs, reverse=True):
                    if self._corner_geom(P, i) is not None:
                        self._fillet(P, i, r_used)
        for pi in d["orig"]:
            self.write(self.paths[pi].ob)
        d["r"] = r_used
        return r_used

    def _start_round(self, key, m):
        """key = (path, index, 'sharp' | 'arc'). Selected corners (sharp or rounded) go together."""
        keys = {key}
        if len(self.sel) > 1:
            def picked(k):
                n = len(self.paths[k[0]].pts)
                return k[:2] in self.sel or (k[2] == "arc" and (k[0], (k[1] + 1) % n) in self.sel)
            if picked(key):
                keys |= {k for k, _w in self.corner_widgets() if picked(k)}
        main_r0 = 0.0
        if key[2] == "arc":
            a = self._arc_corner(self.paths[key[0]], key[1])
            main_r0 = a[6] if a is not None else 0.0
        self._push()
        base, corners, main_i = {}, {}, None
        by_path = {}
        for k in keys:
            by_path.setdefault(k[0], []).append(k)
        for pi, ks in by_path.items():
            P = self.paths[pi]
            pts = copy.deepcopy(P.pts)
            cur = {k: k[1] for k in ks}
            for k in sorted(ks, key=lambda k: -k[1]):        # highest index first
                if k[2] != "arc":
                    continue
                a = self._arc_corner(P, k[1])
                if a is None:
                    continue
                i = cur[k]
                c = a[0]
                q = Pt(c.copy(), CORNER, c.copy(), c.copy(), pts[i].tilt, pts[i].radius)
                if i + 1 < len(pts):
                    pts[i:i + 2] = [q]
                    for o in cur:
                        if o is not k and cur[o] > i:
                            cur[o] -= 1
                else:                                       # the pair wraps round the end of a closed path
                    pts[i:i + 1] = [q]
                    del pts[0]
                    for o in cur:
                        cur[o] -= 1
            base[pi] = pts
            corners[pi] = set(cur.values())
            if key in cur:
                main_i = cur[key]
        for pi, pts in base.items():
            self.paths[pi].pts = copy.deepcopy(pts)
        pi = key[0]
        g = self._corner_geom(self.paths[pi], main_i) if main_i is not None else None
        if g is None:
            self.undo_silent()
            return
        c, u1, u2, th, l1, l2 = g
        bis = (u1 + u2).normalized()
        d = {"kind": "round", "path": pi, "i": main_i, "start": m, "moved": False,
             "orig": base, "corners": corners, "c": c.copy(), "bis": bis, "th": th,
             "n": u1.cross(u2).normalized(), "r": main_r0, "r0": main_r0, "D0": None}
        self.drag = d
        if main_r0 > 0:
            self._round_corners(d, main_r0)                 # keep the rounding on screen until the mouse moves
            ro, rd = self.view.ray(m.x, m.y)
            pl = geom.Plane(c, bis, d["n"].cross(bis))
            h = pl.hit(ro, rd)
            d["D0"] = (h - c).dot(bis) if h is not None else None
        self.sel = set()

    def _round_move(self, mx, my):
        d = self.drag
        ro, rd = self.view.ray(mx, my)
        c, bis, nrm = d["c"], d["bis"], d["n"]
        pl = geom.Plane(c, bis, nrm.cross(bis)) if nrm.length > 1e-9 else self.cplane().through(c)
        h = pl.hit(ro, rd)
        if h is None:
            return
        D = (h - c).dot(bis)
        if d.get("D0") is not None:
            r = d["r0"] + (D - d["D0"]) * math.sin(d["th"] / 2.0)    # a rounded corner: change it from where it is
        else:
            r = max(D, 0.0) * math.sin(d["th"] / 2.0)                # the arc starts level with the mouse on both edges
        r = max(r, 0.0)
        step = getattr(self.st, "grid_step", 0.0)
        if self._snap_round and step > 0:
            r = round(r / step) * step
        r = self._round_corners(d, r)
        self.message = "Corner radius %.4g  (type a value + Enter after releasing for an exact one)" % r

    _snap_round = False

    # ------------------------------------------------------------ selection
    def _group_for(self, key):
        """Points moved together when `key` is dragged: the whole selection if key is in it."""
        if key in self.sel and len(self.sel) > 1:
            return set(self.sel)
        return {key}

    def _box_select(self, a, b, add=False):
        x0, x1 = sorted((a.x, b.x))
        y0, y1 = sorted((a.y, b.y))
        found = set()
        for pi, P in enumerate(self.paths):
            for i, p in enumerate(P.pts):
                s_ = self.view.project(p.co) if self.view is not None else None
                if s_ is not None and x0 <= s_.x <= x1 and y0 <= s_.y <= y1:
                    found.add((pi, i))
        self.sel = (self.sel | found) if add else found
        self.message = "%d point(s) selected  \u00b7  Ctrl+drag one of them to move all  \u00b7  K keyframe" % len(self.sel)

    # ------------------------------------------------------------ animation
    def _autokey(self):
        try:
            return bool(self.ctx.scene.tool_settings.use_keyframe_insert_auto)
        except AttributeError:
            return False

    def insert_keys(self, keys=None):
        """Keyframe position and handles of the given anchors (default: the selection, or every
        point of the curve last edited) at the current frame."""
        if keys is None:
            keys = set(self.sel)
            if not keys:
                ob = self.target
                keys = {(pi, i) for pi, P in enumerate(self.paths) if P.ob is ob for i in range(len(P.pts))}
        if not keys:
            self.message = "Nothing to keyframe: select points (Ctrl+drag a box) or draw a curve"
            return 0
        self.write()
        frame = self.ctx.scene.frame_current
        n = 0
        for pi, i in keys:
            P = self.paths[pi]
            ob = P.ob
            if ob is None:
                continue
            # index of the spline of this path inside its object (same order as _write_one)
            si = [Q for Q in self.paths if Q.ob is ob and Q.pts].index(P)
            try:
                bp = ob.data.splines[si].bezier_points[i]
            except (IndexError, ReferenceError):
                continue
            for prop in ("co", "handle_left", "handle_right"):
                bp.keyframe_insert(prop, frame=frame, group="Vector points")
            n += 1
        self.message = "Keyframe on %d point(s) at frame %d" % (n, frame)
        return n

    def reload(self):
        """Re-read every curve (after a frame change the animation has moved the points)."""
        sel, active, state = set(self.sel), self.active, self.state
        objs = [o for o in self.objects]
        self.paths = []
        self.objects = []
        for o in objs:
            try:
                self._load(o)
            except ReferenceError:
                pass
        self.sel = {k for k in sel if k[0] < len(self.paths) and k[1] < len(self.paths[k[0]].pts)}
        if active is not None and active >= len(self.paths):
            active, state = None, "IDLE"
        self.active, self.state = active, state
        self._refresh_dynamic()

    def toggle_tangent(self, pi, i):
        """Cusp <-> smooth tangent: align the two handles on their average direction (lengths kept)."""
        P = self.paths[pi]
        p = P.pts[i]
        if p.kind == CORNER:
            self._make_smooth(P, i)
            self.message = "Point made smooth"
        elif p.kind == CUSP:
            hl, hr = P.handles(i)
            a, b = p.co - hl, hr - p.co
            if a.length < 1e-12 and b.length < 1e-12:
                self._make_smooth(P, i)
            else:
                dirv = (a.normalized() if a.length > 1e-12 else Vector()) + \
                    (b.normalized() if b.length > 1e-12 else Vector())
                if dirv.length < 1e-9:
                    dirv = b if b.length > 1e-12 else a
                dirv.normalize()
                la = a.length if a.length > 1e-12 else b.length
                lb = b.length if b.length > 1e-12 else a.length
                p.hl, p.hr, p.kind = p.co - dirv * la, p.co + dirv * lb, SMOOTH
            self.message = "Cusp \u2192 smooth tangent"
        else:
            p.kind = CUSP
            self.message = "Smooth \u2192 cusp (handles independent)"
        self.write(P.ob)
        self._refresh_dynamic()

    def _on_release(self, mx, my, ev):
        d = self.drag
        self.drag = None
        if d is None:
            return
        kind, moved = d["kind"], d["moved"]
        if kind == "round":
            if not moved or (d.get("r", 0.0) <= 1e-9 and d.get("r0", 0.0) <= 0.0):
                self.undo_silent()
                self.message = "Drag the dot inside a corner to round it"
            else:
                self.last_round = d
                self.message = ("Corner radius %.4g \u00b7 type a radius + Enter to set it exactly" % d["r"])
            self._refresh_dynamic()
            return
        if kind == "box":
            self.box = None
            if not moved:
                if not d.get("add"):
                    self.sel = set()
                    self.message = "Selection cleared"
                return
            self._box_select(d["start"], Vector((mx, my)), d.get("add"))
            self.sel_handle = None
            return
        if moved and kind in ("move", "hl", "hr", "seg", "convert") and self._autokey():
            if kind == "move":
                group = d.get("group") or {(d["path"], d["i"])}
            elif kind == "seg":
                n = len(self.paths[d["path"]].pts)
                group = {(d["path"], d["i"]), (d["path"], (d["i"] + 1) % n)}
            else:
                group = {(d["path"], d["i"])}
            self.write()
            self.insert_keys(group)
        if d.get("closing") and not moved:
            self.finish_path()                        # click on the first point: close and finish
            self.message = "Curve closed"
        elif not moved:
            if kind == "move" and d.get("toggle_sel"):
                self.history.pop() if self.history else None
                key = (d["path"], d["i"])
                self.sel ^= {key}                     # Ctrl+Shift+click: add / remove one anchor
                self.message = "%d point(s) selected" % len(self.sel)
                return
            if kind == "move" and self.tool == 'EDIT':
                self.history.pop() if self.history else None
                _k, pi, i = d["click"]
                self.sel = {(pi, i)}                  # Direct Selection: a click selects the anchor
                self.sel_handle = None
                self.message = "1 point selected \u00b7 drag it, or Shift+click to add more"
                return
            if kind == "move" and d.get("ctrl"):
                _k, pi, i = d["click"]
                self.toggle_tangent(pi, i)
            elif kind == "move" and d.get("new_from"):
                _k, pi, i = d["click"]
                self.history.pop() if self.history else None
                self._start_new_at(self.paths[pi].pts[i].co.copy(), Vector((mx, my)))
                self.drag = None
                self.message = "New path from that point"
                self._refresh_dynamic()
                return
            elif kind == "move":
                _k, pi, i = d["click"]
                P = self.paths[pi]
                if not P.cyclic and i in (0, len(P.pts) - 1):
                    self.history.pop() if self.history else None
                    self.continue_from(pi, i)
                else:
                    self.history.pop() if self.history else None
                    self.delete_point(pi, i)          # Illustrator: pen click on an anchor deletes it
            elif kind == "convert":
                self.history.pop() if self.history else None
                _k, pi, i = d["click"]
                self.toggle_point(pi, i)
            elif kind == "seg":
                if d.get("alt"):
                    self.history.pop() if self.history else None
                    self.toggle_segment(d["path"], d["i"])
                else:
                    self.undo_silent()                # a click on a segment: back to the state of the press
            elif kind == "lasth":
                P = self.paths[d["path"]]
                p = P.pts[d["i"]]
                if p.kind != CORNER:
                    p.hr = p.co.copy()
                    p.kind = CUSP
                    self.message = "Outgoing handle removed"
                else:
                    self.history.pop() if self.history else None
            elif kind in ("hl", "hr"):
                self.history.pop() if self.history else None
                if self.tool == 'EDIT':
                    self.sel_handle = (d["path"], d["i"], kind)   # G / R / S now act on this handle only
                    self.sel = set()
                    self.message = "Handle selected \u00b7 G move \u00b7 R rotate \u00b7 S scale its length"
        elif d.get("closing"):
            self.finish_path()
            self.message = "Curve closed"
        self.write()
        self._refresh_dynamic()
        self._on_move(mx, my, ev)

    def step_frame(self, t):
        """Left/Right: one frame; Up/Down: next/previous keyframe of the curves (like Blender)."""
        sc = self.ctx.scene
        f = sc.frame_current
        if t in {'LEFT_ARROW', 'RIGHT_ARROW'}:
            f += 1 if t == 'RIGHT_ARROW' else -1
        else:
            keys = set()
            for o in self.objects:
                try:
                    ad = o.data.animation_data
                except ReferenceError:
                    continue
                if ad and ad.action:
                    for fc in _fcurves(ad):
                        keys |= {round(k.co.x) for k in fc.keyframe_points}
            later = sorted(k for k in keys if k > f)
            earlier = sorted(k for k in keys if k < f)
            if t == 'UP_ARROW' and later:
                f = later[0]
            elif t == 'DOWN_ARROW' and earlier:
                f = earlier[-1]
            else:
                self.message = "No more keyframes that way"
                return
        sc.frame_set(f)
        self.reload()
        self.message = "Frame %d" % f

    def undo_silent(self):
        """Restore the state saved when the press started (a click that turned out to do nothing)."""
        msg = self.message
        self.undo()
        self.message = msg

    # ------------------------------------------------------------ teardown
    def finish(self):
        if self.state == "DRAW":
            self.finish_path()
        self.anchor = None
        self.write()
        for ob in list(self.created):
            if not any(P.ob is ob and len(P.pts) >= 2 for P in self.paths):
                try:
                    if self.target is ob:
                        self.target = None
                    from . import curveutil as SL
                    SL.safe_remove(ob)
                except ReferenceError:
                    pass

    # ------------------------------------------------------------ display data
    def preview(self):
        """Rubber band from the last point to the cursor (world points)."""
        if self.tool not in PATH_TOOLS or self.active is None or self.menu is not None:
            return []
        last = self.last_point()
        if last is None or self.cursor is None or self.drag is not None:
            return []
        P = self.paths[self.active]
        hr = P.handles(len(P.pts) - 1)[1] if last.kind == CORNER else last.hr
        if last.kind == CORNER:
            hr = last.co
        return geom.bez_samples(last.co, hr, self.cursor, self.cursor, 24)

    def readout(self):
        last = self.last_point()
        if last is None or self.cursor is None:
            return ""
        plane = self.work_plane(last.co)
        d = self.cursor - last.co
        us = self.ctx.scene.unit_settings
        try:
            ls = bpy.utils.units.to_string(us.system, 'LENGTH', d.length * (us.scale_length or 1.0), precision=3)
        except Exception:      # noqa: BLE001
            ls = "%.3f" % d.length
        ang = round(geom.angle_deg(d, plane), 1) + 0.0
        dz = d.dot(plane.n)
        extra = ("   Δz %.3g" % dz) if abs(dz) > 1e-6 else ""
        return "%s  ∠ %.1f°%s" % (ls, ang, extra)
