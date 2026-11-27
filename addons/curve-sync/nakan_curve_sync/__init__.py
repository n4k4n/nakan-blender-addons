"""Nakan Curve Sync: see animation curves where you sync timing.

- Graph Editor: the final (NLA-combined) value of each visible channel.
- Video Sequencer: channels marked in a scene, drawn over its scene strips.
"""

from . import core, marks, graph, vse

_modules = (marks, graph, vse)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
    core.clear_caches()
