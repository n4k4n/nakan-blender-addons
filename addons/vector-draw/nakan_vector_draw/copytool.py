"""Copy / Move / Rotate (AutoCAD): the selected objects are copied from a base point to as many destination
points as wanted (all snappable), until Esc; or moved; or rotated about a pivot picked on screen. A full-size
ghost of the selection hangs on the cursor; R (in Copy / Move) turns it about a pivot point you pick, then by
the angle (15 degree steps, Shift free, snap to aim, or typed).

A curve with a Vector Draw solid is copied together with its solid (the copy's solid follows the copied curve),
and a solid picked alone brings its curve along."""

import math

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import geom, snaps

STEP = 15.0
MAX_EDGES = 120000
GHOST = (0.1, 0.75, 1.0, 0.9)
ORIG = (1.0, 0.62, 0.15, 0.55)


# ----------------------------------------------------------------------------------- what is copied
def copy_set(objs):
    """the objects to copy: the selection, plus the solids of selected curves and the curves of selected
    solids, plus the children of all of them (so a hierarchy goes along)"""
    out = []
    seen = set()

    def add(o):
        if o is None or o.name in seen:
            return
        seen.add(o.name)
        out.append(o)
        sp = getattr(o, "penforge_solid", None)
        if sp is not None and sp.is_solid and sp.source is not None:
            add(sp.source)
        for ch in o.children:
            add(ch)
    for o in objs:
        add(o)
    return out


def roots(objs):
    names = {o.name for o in objs}
    return [o for o in objs if o.parent is None or o.parent.name not in names]


def make_copies(objs, M):
    """copies of objs (each with its own data), the roots moved by the world matrix M -> [new objects]"""
    new = {}
    for o in objs:
        c = o.copy()
        if o.data is not None:
            c.data = o.data.copy()
        if o.animation_data is not None and c.animation_data is not None and o.animation_data.action:
            c.animation_data.action = o.animation_data.action     # shared animation, like Shift+D
        new[o.name] = c
        for coll in o.users_collection:
            coll.objects.link(c)
    for o in objs:
        c = new[o.name]
        if o.parent is not None and o.parent.name in new:
            c.parent = new[o.parent.name]
        sp = getattr(c, "penforge_solid", None)
        if sp is not None and sp.is_solid and sp.source is not None and sp.source.name in new:
            sp.source = new[sp.source.name]
    for o in roots(objs):
        new[o.name].matrix_world = M @ o.matrix_world
    return [new[o.name] for o in objs]


def ghost_edges(context, objs):
    """world-space edge list (2N x 3 float32) of the evaluated objects"""
    dg = context.evaluated_depsgraph_get()
    parts = []
    total = 0
    for o in objs:
        if o.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            continue
        ev = o.evaluated_get(dg)
        try:
            me = ev.to_mesh()
        except RuntimeError:
            continue
        if me is None:
            continue
        try:
            nv, ne = len(me.vertices), len(me.edges)
            if not ne:
                continue
            co = np.empty(nv * 3, dtype=np.float32)
            me.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)
            ed = np.empty(ne * 2, dtype=np.int32)
            me.edges.foreach_get("vertices", ed)
            mw = np.array(o.matrix_world, dtype=np.float32)
            w = co @ mw[:3, :3].T + mw[:3, 3]
            parts.append(w[ed])
            total += ne
        finally:
            ev.to_mesh_clear()
    if not parts:
        return np.zeros((0, 3), dtype=np.float32)
    E = np.concatenate(parts)
    if total > MAX_EDGES:                       # a light preview for very dense meshes
        k = int(math.ceil(total / MAX_EDGES))
        E = E.reshape(-1, 2, 3)[::k].reshape(-1, 3)
    return E


# ----------------------------------------------------------------------------------- drawing
def _line3d(pts, col, width=1.6):
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    vp = gpu.state.viewport_get()
    sh.uniform_float("viewportSize", (vp[2], vp[3]))
    sh.uniform_float("lineWidth", width)
    sh.uniform_float("color", col)
    batch_for_shader(sh, 'LINES', {"pos": [tuple(p) for p in pts]}).draw(sh)


def _draw3d(op, context):
    if context.region != op.region:
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    vp = gpu.state.viewport_get()
    sh.uniform_float("viewportSize", (vp[2], vp[3]))
    if op.batch is None and len(op.edges):
        op.batch = batch_for_shader(sh, 'LINES', {"pos": op.edges})
    if op.batch is not None:
        sh.uniform_float("lineWidth", 1.0)
        sh.uniform_float("color", ORIG)
        op.batch.draw(sh)                                   # what is being copied
        M = op.matrix()
        if M is not None:
            gpu.matrix.push()
            gpu.matrix.multiply_matrix(M)
            sh.uniform_float("lineWidth", 1.4)
            sh.uniform_float("color", GHOST)
            op.batch.draw(sh)                               # the copy on the cursor
            gpu.matrix.pop()
    if op.base is not None and op.target is not None:
        _line3d([op.base, op.target], (1.0, 1.0, 1.0, 0.6), 1.2)
    pv = op.pivot_world()
    if op.state == 'RANGLE' and pv is not None and op.aim is not None:
        _line3d([pv, op.aim], (1.0, 0.8, 0.1, 0.8), 1.2)
    gpu.state.blend_set('NONE')


def _draw2d(op, context):
    if context.region != op.region:
        return
    from . import ui as U
    gpu.state.blend_set('ALPHA')
    sn = op.snap
    if sn is not None:
        U._marker(sn.kind, sn.screen, 9)
        U._text(sn.label, sn.screen.x + 14, sn.screen.y + 11, U.SNAPC, U.SNAP_TEXT)
    pv = op.pivot_world() if op.state == 'RANGLE' else None
    if pv is not None:
        V = geom.View.from_context(context.region, context.region_data)
        sp = V.project(pv)
        if sp is not None:
            r = 7
            U._lines2d([(sp.x - r, sp.y), (sp.x + r, sp.y), (sp.x, sp.y - r), (sp.x, sp.y + r)], (1.0, 0.8, 0.1, 1.0), 2)
            U._lines2d(U._circle(sp, r + 3), (1.0, 0.8, 0.1, 1.0), 1.5, True)
    x, y = op.mouse
    txt = op.readout()
    if txt:
        U._text(txt, x + 18, y - 26, U.WHITE, U.SNAP_TEXT)
    gpu.state.blend_set('NONE')


# ----------------------------------------------------------------------------------- the command
class _Transform:
    """Copy / Move / Rotate (AutoCAD): the shared modal. mode: 'COPY' (base point, then as many destinations
as wanted), 'MOVE' (base point, one destination), 'ROTATE' (pivot point, then the angle). In Copy and Move,
R turns the selection too: pick the pivot (any point, snaps work), then the angle"""
    bl_options = {'REGISTER', 'UNDO'}
    mode = 'COPY'

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and bool(context.selected_objects)

    def invoke(self, context, event):
        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                return {'CANCELLED'}
        self.objs = copy_set(list(context.selected_objects)) if self.mode == 'COPY' else \
            roots(copy_set(list(context.selected_objects)))
        if not self.objs:
            return {'CANCELLED'}
        self.region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
        if context.region is not None and context.region.type == 'WINDOW':
            self.region = context.region
        self.rv3d = context.area.spaces.active.region_3d
        self.st = context.scene.nkdraw
        self.cache = snaps.SnapCache()
        self.cache.gather(context)
        self.edges = ghost_edges(context, copy_set(self.objs))
        self.batch = None
        self.state = 'RPIVOT' if self.mode == 'ROTATE' else 'BASE'
        self.base = self.target = self.aim = None
        self.cursor = None
        self.angle = 0.0                  # degrees, around the CPlane normal
        self.pv_rel = None                # pivot, relative to the destination (Copy / Move)
        self.pivot = None                 # pivot in world space (Rotate)
        self.rot_live = None
        self.ref = 0.0                    # the angle the mouse direction starts from
        self.keep = False                 # Rotate: C = rotate a copy, the original stays
        self.snap = None
        self.buffer = ""
        self.made = 0
        self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
        self._h3 = bpy.types.SpaceView3D.draw_handler_add(_draw3d, (self, context), 'WINDOW', 'POST_VIEW')
        self._h2 = bpy.types.SpaceView3D.draw_handler_add(_draw2d, (self, context), 'WINDOW', 'POST_PIXEL')
        context.window.cursor_modal_set('CROSSHAIR')
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        self._status(context)
        return {'RUNNING_MODAL'}

    # ---- helpers
    def _view(self):
        return geom.View.from_context(self.region, self.rv3d)

    def _plane(self, view):
        return geom.scene_plane(bpy.context.scene, view.vm)

    def _units(self, text):
        us = bpy.context.scene.unit_settings
        try:
            return float(text.replace(",", "."))
        except ValueError:
            pass
        try:
            return bpy.utils.units.to_value(us.system, 'LENGTH', text) / (us.scale_length or 1.0)
        except (ValueError, RuntimeError):
            return None

    def _fmt(self, x):
        us = bpy.context.scene.unit_settings
        try:
            return bpy.utils.units.to_string(us.system, 'LENGTH', x * us.scale_length, precision=3)
        except (ValueError, RuntimeError):
            return "%.3g" % x

    def cur_angle(self):
        return self.rot_live if (self.state == 'RANGLE' and self.rot_live is not None) else self.angle

    def pivot_world(self):
        if self.mode == 'ROTATE':
            return self.pivot
        if self.pv_rel is None or self.target is None:
            return None
        return self.target + self.pv_rel

    def matrix(self):
        """world matrix from the originals to the ghost / the result (None while nothing is set)"""
        T = Matrix.Identity(4)
        if self.mode != 'ROTATE':
            if self.base is None or self.target is None:
                return None
            T = Matrix.Translation(self.target - self.base)
        a = self.cur_angle()
        pv = self.pivot_world()
        if pv is not None and abs(a) > 1e-9:
            pl = self._plane(self._view())
            R = Matrix.Rotation(math.radians(a), 4, Vector(pl.n))
            T = Matrix.Translation(pv) @ R @ Matrix.Translation(-pv) @ T
        elif self.mode == 'ROTATE' and pv is None:
            return None
        return T

    def readout(self):
        if self.state == 'BASE':
            return "Base point"
        if self.state == 'RPIVOT':
            return "Pivot point (rotation centre)"
        if self.state == 'RANGLE':
            return ((self.buffer + "  " if self.buffer else "") + "Angle %.1f°" % self.cur_angle() +
                    ("   (copy)" if self.keep else ""))
        if self.base is None or self.target is None:
            return ""
        d = (self.target - self.base).length
        s = (self.buffer + "  " if self.buffer else "") + "Distance " + self._fmt(d)
        if abs(self.angle) > 1e-6:
            s += "   ↻ %.1f°" % self.angle
        return s

    def _status(self, context):
        what = {'COPY': "Copy", 'MOVE': "Move", 'ROTATE': "Rotate"}[self.mode]
        if self.state == 'BASE':
            t = what + ": pick the base point (snaps work)  ·  D Osnap  ·  Esc / right click: cancel"
        elif self.state == 'PLACE':
            t = (what + ": click the destination" + (" (as many as you like)" if self.mode == 'COPY' else "") +
                 "  ·  type a distance or x,y + Enter  ·  R rotate (pick the pivot, then the angle)  ·  "
                 "Shift 45° / O ortho  ·  D Osnap  ·  Esc" +
                 (" / Enter: done" if self.mode == 'COPY' else ": cancel"))
        elif self.state == 'RPIVOT':
            t = (what + ": pick the pivot point, the rotation centre (snaps work)  ·  " +
                 ("C copy on/off  ·  Esc: cancel" if self.mode == 'ROTATE' else "Esc: back"))
        else:
            t = (what + ": the angle - 15° steps, Shift free, snap a point to aim  ·  type an angle + Enter  "
                 "·  " + ("C copy on/off  ·  click: rotate  ·  Esc: cancel" if self.mode == 'ROTATE'
                             else "click: place it turned  ·  Esc: back without turning"))
        context.workspace.status_text_set(t)
        context.area.header_text_set("%s: %d object(s)%s" % (
            what, len(roots(self.objs)), ("  ·  %d copies made" % self.made) if self.made else ""))

    def _end(self, context):
        for h in (getattr(self, "_h3", None), getattr(self, "_h2", None)):
            if h is not None:
                bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        self._h3 = self._h2 = None
        try:
            context.window.cursor_modal_restore()
            context.workspace.status_text_set(None)
            context.area.header_text_set(None)
        except (AttributeError, RuntimeError):
            pass
        if context.area:
            context.area.tag_redraw()

    def _point(self, view, mx, my, constrain_from=None, shift=False):
        """snapped point (3D) or the mouse on the CPlane; Ortho / Shift line the free point up with the base"""
        pl = self._plane(view)
        sn = None
        if self.st.osnap:
            sn = self.cache.find(view, mx, my, self.st, last=constrain_from, plane=pl)
        self.snap = sn
        if sn is not None and sn.kind != "GRID":
            return Vector(sn.point)
        ro, rd = view.ray(mx, my)
        p = Vector(sn.point) if sn is not None else pl.hit(ro, rd)
        if p is None:
            return None
        ortho = bool(getattr(self.st, "ortho", False))
        if constrain_from is not None and (ortho or shift):
            step = 90.0 if (ortho and not shift) else 45.0
            b = pl.project(constrain_from)
            v = pl.project(p) - b
            u, w = v.dot(pl.x), v.dot(pl.y)
            L = math.hypot(u, w)
            if L > 1e-12:
                a = math.radians(step) * round(math.atan2(w, u) / math.radians(step))
                p = constrain_from + (pl.x * math.cos(a) + pl.y * math.sin(a)) * L
        return p

    def _update(self, context, event):
        view = self._view()
        mx, my = self.mouse
        if self.state == 'BASE':
            self.target = None
            self.cursor = self._point(view, mx, my)
            return
        if self.state == 'PLACE':
            p = self._point(view, mx, my, self.base, event.shift)
            if p is not None and self.buffer:
                p = self._typed(view, p) or p
            self.target = p
            return
        if self.state == 'RPIVOT':
            self.cursor = self._point(view, mx, my)
            return
        # RANGLE: the mouse direction from the pivot
        p = self._point(view, mx, my)
        self.aim = p
        pl = self._plane(view)
        if self.buffer:
            try:
                self.rot_live = float(self.buffer.replace(",", ".").lstrip("<"))
                return
            except ValueError:
                pass
        pv = self.pivot_world()
        if p is None or pv is None:
            return
        v = pl.project(p) - pl.project(pv)
        u, w = v.dot(pl.x), v.dot(pl.y)
        if math.hypot(u, w) < 1e-9:
            return
        a = math.degrees(math.atan2(w, u))
        if not event.shift and (self.snap is None or self.snap.kind == "GRID"):
            a = STEP * round(a / STEP)
        self.rot_live = a

    def _typed(self, view, p):
        pl = self._plane(view)
        t = self.buffer.replace(";", ",")
        if "," in t and t.count(",") == 1 and not t.endswith(","):
            a, b = t.split(",")
            x, y = self._units(a), self._units(b)
            if x is None or y is None:
                return None
            return self.base + pl.x * x + pl.y * y
        d = self._units(t)
        if d is None:
            return None
        v = p - self.base
        if v.length < 1e-12:
            v = Vector(pl.x)
        return self.base + v.normalized() * d

    def _apply(self, context):
        """Copy: drop a copy (and go on); Move / Rotate: transform the selection -> True when finished"""
        M = self.matrix()
        if M is None:
            return False
        if self.mode == 'COPY' or (self.mode == 'ROTATE' and self.keep):
            make_copies(copy_set(self.objs), M)
            self.made += 1
            context.view_layer.update()
            self.cache.gather(context)                       # the new copies snap at once
            return self.mode != 'COPY'
        for o in self.objs:                                  # roots only: children follow
            o.matrix_world = M @ o.matrix_world
        return True

    def _finish(self, context):
        self._end(context)
        if self.mode == 'COPY':
            self.report({'INFO'}, "Copy: %d cop%s" % (self.made, "y" if self.made == 1 else "ies"))
        return {'FINISHED'}

    def _start_rotate(self, context, event):
        """R in Copy / Move: the destination stays, pick the pivot"""
        self.state = 'RPIVOT'
        self.buffer = ""
        self._update(context, event)
        self._status(context)

    # ---- modal
    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM',
                 'MOUSEROTATE', 'NDOF_MOTION'} or t.startswith('NDOF'):
            return {'PASS_THROUGH'}
        context.area.tag_redraw()
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
            self._update(context, event)
            return {'RUNNING_MODAL'}
        if t in {'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            self._update(context, event)
            return {'RUNNING_MODAL'}
        if v != 'PRESS':
            return {'RUNNING_MODAL'}
        rot_sub = self.mode != 'ROTATE' and self.state in {'RPIVOT', 'RANGLE'}
        if t in {'ESC', 'RIGHTMOUSE'} or (t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and not self.buffer
                                           and self.state in {'BASE', 'PLACE'}):
            if rot_sub:                                      # back to placing, without turning
                self.state, self.rot_live, self.buffer = 'PLACE', None, ""
                self._update(context, event)
                self._status(context)
                return {'RUNNING_MODAL'}
            if self.made:
                return self._finish(context)
            self._end(context)
            return {'CANCELLED'}
        if t == 'LEFTMOUSE' or (t in {'RET', 'NUMPAD_ENTER'} and self.buffer):
            self._update(context, event)
            if self.state == 'BASE':
                if self.cursor is not None:
                    self.base = self.cursor.copy()
                    self.state = 'PLACE'
                    self._update(context, event)
            elif self.state == 'PLACE':
                self.buffer = ""
                if self._apply(context):
                    return self._finish(context)
            elif self.state == 'RPIVOT':
                if self.cursor is not None:
                    if self.mode == 'ROTATE':
                        self.pivot = self.cursor.copy()
                    else:
                        self.pv_rel = self.cursor - self.target
                    self.state = 'RANGLE'
                    self.buffer = ""
                    self.rot_live = None
                    self._update(context, event)
            else:                                            # RANGLE
                if self.rot_live is not None:
                    self.angle = self.rot_live
                self.buffer = ""
                if self.mode == 'ROTATE':
                    self._apply(context)
                    return self._finish(context)
                if t == 'LEFTMOUSE' and self._apply(context):   # Copy: placed turned; Move: done
                    return self._finish(context)
                self.state, self.rot_live = 'PLACE', None
                self._update(context, event)
            self._status(context)
            return {'RUNNING_MODAL'}
        if t == 'R' and self.state == 'PLACE' and not self.buffer and self.target is not None:
            self._start_rotate(context, event)
            return {'RUNNING_MODAL'}
        if t == 'C' and self.mode == 'ROTATE' and not self.buffer:
            self.keep = not self.keep
            self._status(context)
            return {'RUNNING_MODAL'}
        if t == 'D' and not self.buffer:
            self.st.osnap = not self.st.osnap
            self._update(context, event)
            return {'RUNNING_MODAL'}
        if t == 'O' and not self.buffer:
            self.st.ortho = not self.st.ortho
            self._update(context, event)
            return {'RUNNING_MODAL'}
        if t == 'BACK_SPACE':
            self.buffer = self.buffer[:-1]
            self._update(context, event)
            return {'RUNNING_MODAL'}
        if self.state in {'PLACE', 'RANGLE'} and event.unicode and event.unicode in "0123456789.,-<;" + (
                "" if self.state == 'RANGLE' else "mk "):
            self.buffer += event.unicode
            self._update(context, event)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}


class NKDRAW_OT_copy(_Transform, bpy.types.Operator):
    """Copy (AutoCAD): pick a base point, then click destination points (snaps work) to drop copies of the
selection, as many as you like, until Esc. R turns the copies: pick the pivot, then the angle"""
    bl_idname = "nkdraw.copy"
    bl_label = "Copy"
    mode = 'COPY'


class NKDRAW_OT_move(_Transform, bpy.types.Operator):
    """Move (AutoCAD): pick a base point, then the destination (snaps work). R turns the selection on the
way: pick the pivot, then the angle"""
    bl_idname = "nkdraw.move"
    bl_label = "Move"
    mode = 'MOVE'


class NKDRAW_OT_rotate(_Transform, bpy.types.Operator):
    """Rotate (AutoCAD): pick the pivot point (snaps work), then the angle: 15 degree steps, Shift free, snap a
point to aim at it, or type the angle. C rotates a copy and leaves the original"""
    bl_idname = "nkdraw.rotate"
    bl_label = "Rotate"
    mode = 'ROTATE'


classes = (NKDRAW_OT_copy, NKDRAW_OT_move, NKDRAW_OT_rotate)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
