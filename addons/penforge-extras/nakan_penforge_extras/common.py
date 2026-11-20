"""Shared helpers: the CPlane of the pen (geom.scene_plane)."""
from mathutils import Matrix, Vector


def _penforge_geom():
    from . import geom
    return geom


def penforge_loaded():
    return _penforge_geom() is not None


def cplane_frame(scene):
    """(origin, 3x3 rotation whose columns are the CPlane X, Y, normal).
    Uses the add-on's own CPlane (live turn included); falls back to the 3D cursor."""
    g = _penforge_geom()
    if g is not None and hasattr(scene, "penforge"):
        try:
            pl = g.scene_plane(scene)
            R = Matrix((pl.x, pl.y, pl.n)).transposed()
            return Vector(pl.origin), R
        except Exception:  # noqa: BLE001
            pass
    cur = scene.cursor
    return cur.location.copy(), cur.rotation_euler.to_matrix()
