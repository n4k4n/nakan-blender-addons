# SPDX-License-Identifier: GPL-3.0-or-later
"""Small layered PSD writer (8-bit RGB) with groups, blend modes and hidden layers.

Usage:
    doc = PSD(width, height)
    doc.add_image("Render", rgba)                         # (H, W, 4) uint8
    g = doc.add_group("Objects")
    g.add_fill("Box", color=(255, 0, 0), alpha=mask_u8)   # (H, W) uint8
    doc.save(path, composite=rgb)                         # (H, W, 3) uint8
Layers are added top to bottom, like the Layers panel.
"""

import struct

import numpy as np

BLEND = {"normal": b"norm", "multiply": b"mul ", "screen": b"scrn", "overlay": b"over",
         "soft_light": b"sLit", "pass": b"pass"}


# ------------------------------------------------------------------ PackBits

def _packbits_row(row):
    n = len(row)
    if n == 0:
        return b""
    cut = np.flatnonzero(row[1:] != row[:-1]) + 1
    starts = np.concatenate(([0], cut))
    ends = np.concatenate((cut, [n]))
    out = bytearray()
    lit_start = None

    def flush(a, b):
        while a < b:
            k = min(128, b - a)
            out.append(k - 1)
            out.extend(row[a:a + k].tobytes())
            a += k

    for s, e in zip(starts.tolist(), ends.tolist()):
        length = e - s
        if length >= 3:
            if lit_start is not None:
                flush(lit_start, s)
                lit_start = None
            v = int(row[s])
            while length > 0:
                k = min(128, length)
                if k < 2:  # a single leftover byte goes as a literal
                    out.append(0)
                    out.append(v)
                else:
                    out.append((1 - k) & 0xFF)
                    out.append(v)
                length -= k
        elif lit_start is None:
            lit_start = s
    if lit_start is not None:
        flush(lit_start, n)
    return bytes(out)


def _rle_channel(plane):
    rows = [_packbits_row(plane[y]) for y in range(plane.shape[0])]
    counts = struct.pack(">%dH" % len(rows), *[len(r) for r in rows])
    return b"\x00\x01" + counts + b"".join(rows)


def _raw_channel(plane):
    return b"\x00\x00" + np.ascontiguousarray(plane, np.uint8).tobytes()


def _const_channel(value, h, w):
    """RLE channel filled with one value (cheap, no per-pixel work)."""
    if h == 0 or w == 0:
        return b"\x00\x00"
    row = np.full(w, value, np.uint8)
    packed = _packbits_row(row)
    counts = struct.pack(">H", len(packed)) * h
    return b"\x00\x01" + counts + packed * h


# ------------------------------------------------------------------ layers

class _Layer:
    def __init__(self, name, kind="pixel", blend="normal", opacity=255, visible=True):
        self.name = name
        self.kind = kind                # pixel | folder | divider
        self.blend = blend
        self.opacity = opacity
        self.visible = visible
        self.rect = (0, 0, 0, 0)        # top, left, bottom, right
        self.channels = []              # [(id, bytes)]
        self.open = True

    def record(self):
        t, l, b, r = self.rect
        chans = self.channels or [(cid, b"\x00\x00") for cid in (-1, 0, 1, 2)]
        out = bytearray(struct.pack(">iiiiH", t, l, b, r, len(chans)))
        for cid, data in chans:
            out += struct.pack(">hI", cid, len(data))
        flags = 0x08 | (0x02 if not self.visible else 0)
        if self.kind != "pixel":
            flags |= 0x10
        blend = b"pass" if self.kind == "folder" and self.blend == "normal" else BLEND[self.blend]
        out += b"8BIM" + blend + struct.pack(">BBBB", self.opacity, 0, flags, 0)

        extra = bytearray(struct.pack(">II", 0, 0))      # no layer mask, no blending ranges
        nm = self.name.encode("latin-1", "replace")[:255]
        pas = bytes([len(nm)]) + nm
        pas += b"\x00" * ((4 - len(pas) % 4) % 4)
        extra += pas
        uni = self.name.encode("utf-16-be")
        luni = struct.pack(">I", len(uni) // 2) + uni
        luni += b"\x00" * ((4 - len(luni) % 4) % 4)
        extra += b"8BIMluni" + struct.pack(">I", len(luni)) + luni
        if self.kind == "folder":
            lsct = struct.pack(">I", 1 if self.open else 2) + b"8BIM" + BLEND["pass"]
            extra += b"8BIMlsct" + struct.pack(">I", len(lsct)) + lsct
        elif self.kind == "divider":
            lsct = struct.pack(">I", 3)
            extra += b"8BIMlsct" + struct.pack(">I", len(lsct)) + lsct
        out += struct.pack(">I", len(extra)) + extra
        return bytes(out), b"".join(d for _c, d in chans)


def _bbox(alpha):
    ys = np.flatnonzero(alpha.any(axis=1))
    if len(ys) == 0:
        return None
    xs = np.flatnonzero(alpha.any(axis=0))
    return int(ys[0]), int(xs[0]), int(ys[-1]) + 1, int(xs[-1]) + 1


class _Container:
    def __init__(self, doc):
        self.doc = doc
        self.items = []

    def add_image(self, name, rgba, blend="normal", opacity=255, visible=True, compress="raw"):
        """rgba: (H, W, 4) or (H, W, 3) uint8, full canvas size."""
        lay = _Layer(name, blend=blend, opacity=opacity, visible=visible)
        h, w = rgba.shape[:2]
        alpha = rgba[..., 3] if rgba.shape[2] == 4 else np.full((h, w), 255, np.uint8)
        box = _bbox(alpha) if rgba.shape[2] == 4 else (0, 0, h, w)
        if box is None:
            box = (0, 0, 0, 0)
        t, l, b, r = box
        enc = _raw_channel if compress == "raw" else _rle_channel
        lay.rect = box
        lay.channels = [(-1, enc(alpha[t:b, l:r]))] + [
            (c, enc(rgba[t:b, l:r, c])) for c in range(3)]
        self.items.append(lay)
        return lay

    def add_fill(self, name, color, alpha, blend="normal", opacity=255, visible=True):
        """A flat-color layer whose transparency is `alpha` ((H, W) uint8)."""
        lay = _Layer(name, blend=blend, opacity=opacity, visible=visible)
        box = _bbox(alpha) or (0, 0, 0, 0)
        t, l, b, r = box
        lay.rect = box
        h, w = b - t, r - l
        lay.channels = [(-1, _rle_channel(alpha[t:b, l:r]) if h and w else b"\x00\x00")] + [
            (c, _const_channel(color[c], h, w)) for c in range(3)]
        self.items.append(lay)
        return lay

    def add_group(self, name, visible=True, open=True):
        g = _Group(self.doc, name, visible, open)
        self.items.append(g)
        return g

    def _flatten(self):
        out = []
        for it in self.items:
            if isinstance(it, _Group):
                out += it._flat()
            else:
                out.append(it)
        return out


class _Group(_Container):
    def __init__(self, doc, name, visible, open):
        super().__init__(doc)
        self.folder = _Layer(name, kind="folder", visible=visible)
        self.folder.open = open

    def _flat(self):
        return [self.folder] + self._flatten() + [_Layer("</Layer group>", kind="divider")]


class PSD(_Container):
    def __init__(self, width, height):
        super().__init__(self)
        self.width, self.height = width, height

    def save(self, path, composite):
        """composite: (H, W, 3) uint8 shown by apps that only read the flat image."""
        layers = list(reversed(self._flatten()))          # PSD stores bottom first
        records, data = bytearray(), bytearray()
        for lay in layers:
            rec, d = lay.record()
            records += rec
            data += d
        info = struct.pack(">h", len(layers)) + records + data
        if len(info) % 2:
            info += b"\x00"
        layer_info = struct.pack(">I", len(info)) + info
        lmi = layer_info + struct.pack(">I", 0)           # no global layer mask

        h, w = self.height, self.width
        with open(path, "wb") as f:
            f.write(b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, h, w, 8, 3))
            f.write(struct.pack(">I", 0))                 # color mode data
            f.write(struct.pack(">I", 0))                 # image resources
            f.write(struct.pack(">I", len(lmi)) + lmi)
            f.write(b"\x00\x00")                          # raw composite
            comp = np.ascontiguousarray(composite[..., :3], np.uint8)
            for c in range(3):
                f.write(comp[..., c].tobytes())
