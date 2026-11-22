# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal OpenEXR reader (pure Python + numpy).

Reads scanline images, single-part or multi-part (Blender 5 writes one part per
pass), with NONE, RLE, ZIPS or ZIP compression and HALF / FLOAT / UINT channels.
That covers what Blender writes with the ZIP codec, which is what this add-on
asks for. Tiled images and the PIZ / PXR24 / B44 / DWA codecs are not supported.
"""

import struct
import zlib

import numpy as np

COMPRESSION_NAMES = {0: "None", 1: "RLE", 2: "ZIPS", 3: "ZIP", 4: "PIZ", 5: "PXR24",
                     6: "B44", 7: "B44A", 8: "DWAA", 9: "DWAB", 10: "HTJ2K"}
LINES_PER_BLOCK = {0: 1, 1: 1, 2: 1, 3: 16}
PIXEL_DTYPES = {0: np.dtype("<u4"), 1: np.dtype("<f2"), 2: np.dtype("<f4")}


class EXRError(Exception):
    pass


def _cstr(d, p):
    e = d.index(b"\0", p)
    return d[p:e].decode("utf-8", "replace"), e + 1


def _read_header(d, p):
    attrs = {}
    while d[p] != 0:
        name, p = _cstr(d, p)
        typ, p = _cstr(d, p)
        size = struct.unpack_from("<i", d, p)[0]
        p += 4
        attrs[name] = (typ, d[p:p + size])
        p += size
    return attrs, p + 1


def _parse_channels(v):
    chans, q = [], 0
    while v[q] != 0:
        name, q = _cstr(v, q)
        ptype, _plin, xs, ys = struct.unpack_from("<iB3xii", v, q)
        q += 16
        if xs != 1 or ys != 1:
            raise EXRError("Subsampled channels are not supported")
        chans.append((name, ptype))
    return chans


def _undo_predictor_and_interleave(raw):
    a = np.frombuffer(raw, dtype=np.uint8)
    # predictor: t[0] = a[0], t[i] = t[i-1] + a[i] - 128  (mod 256)
    t = ((np.cumsum(a, dtype=np.int64) - 128 * np.arange(len(a))) & 0xFF).astype(np.uint8)
    # the two halves hold the even and the odd bytes
    half = (len(t) + 1) // 2
    out = np.empty_like(t)
    out[0::2] = t[:half]
    out[1::2] = t[half:]
    return out.tobytes()


def _rle_decode(raw, expected):
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c > 127:
            c -= 256
        i += 1
        if c < 0:
            out += raw[i:i - c]
            i += -c
        else:
            out += bytes([raw[i]]) * (c + 1)
            i += 1
    return bytes(out[:expected])


class Part:
    def __init__(self, attrs):
        self.attrs = attrs
        self.channels = _parse_channels(attrs["channels"][1])
        x0, y0, x1, y1 = struct.unpack("<iiii", attrs["dataWindow"][1])
        self.x0, self.y0 = x0, y0
        self.width, self.height = x1 - x0 + 1, y1 - y0 + 1
        dx0, dy0, dx1, dy1 = struct.unpack("<iiii", attrs["displayWindow"][1])
        self.display = (dx0, dy0, dx1 - dx0 + 1, dy1 - dy0 + 1)
        self.compression = attrs["compression"][1][0]
        typ = attrs.get("type", ("", b""))[1].rstrip(b"\0").decode()
        if typ and typ != "scanlineimage" or "tiles" in attrs:
            raise EXRError("Tiled EXR files are not supported")
        if self.compression not in LINES_PER_BLOCK:
            raise EXRError(
                "EXR compression %s is not supported. Save the EXR with the ZIP codec."
                % COMPRESSION_NAMES.get(self.compression, self.compression))
        self.lines = LINES_PER_BLOCK[self.compression]
        self.nchunks = (self.height + self.lines - 1) // self.lines
        self.name = attrs.get("name", ("", b""))[1].rstrip(b"\0").decode("utf-8", "replace")
        self.offsets = []


class EXRFile:
    """Lazy reader: headers are parsed up front, pixels on request."""

    def __init__(self, path):
        with open(path, "rb") as f:
            self.data = d = f.read()
        if d[:4] != b"\x76\x2f\x31\x01":
            raise EXRError("Not an OpenEXR file")
        flags = struct.unpack_from("<I", d, 4)[0]
        if flags & 0x200:
            raise EXRError("Tiled EXR files are not supported")
        if flags & 0x800:
            raise EXRError("Deep EXR files are not supported")
        self.multipart = bool(flags & 0x1000)
        p = 8
        self.parts = []
        while True:
            attrs, p = _read_header(d, p)
            self.parts.append(Part(attrs))
            if not self.multipart or d[p] == 0:
                break
        if self.multipart:
            p += 1  # empty header that ends the list
        for part in self.parts:
            part.offsets = list(struct.unpack_from("<%dQ" % part.nchunks, d, p))
            p += 8 * part.nchunks
        # channel name -> (part index, channel index)
        self.index = {}
        for pi, part in enumerate(self.parts):
            for ci, (name, _t) in enumerate(part.channels):
                self.index[name] = (pi, ci)
        self._cache = {}

    # ---------------------------------------------------------------- metadata
    def strings(self):
        """All string attributes from every header (Blender stores the
        Cryptomatte manifests here)."""
        out = {}
        for part in self.parts:
            for k, (t, v) in part.attrs.items():
                if t == "string":
                    out[k] = v.decode("utf-8", "replace")
        return out

    @property
    def size(self):
        _x, _y, w, h = self.parts[0].display
        return w, h

    def channel_names(self):
        return list(self.index)

    # ---------------------------------------------------------------- pixels
    def _decode_part(self, pi):
        if pi in self._cache:
            return self._cache[pi]
        part, d = self.parts[pi], self.data
        widths = [PIXEL_DTYPES[t].itemsize * part.width for _n, t in part.channels]
        line_bytes = sum(widths)
        planes = [np.empty((part.height, part.width), PIXEL_DTYPES[t]) for _n, t in part.channels]
        for off in part.offsets:
            q = off
            if self.multipart:
                q += 4
            y, size = struct.unpack_from("<ii", d, q)
            raw = d[q + 8:q + 8 + size]
            row0 = y - part.y0
            nlines = min(part.lines, part.height - row0)
            expected = line_bytes * nlines
            if size < expected:
                if part.compression in (2, 3):
                    raw = _undo_predictor_and_interleave(zlib.decompress(raw))
                elif part.compression == 1:
                    raw = _undo_predictor_and_interleave(_rle_decode(raw, expected))
            p = 0
            for li in range(nlines):
                for ci, (_n, t) in enumerate(part.channels):
                    w = widths[ci]
                    planes[ci][row0 + li] = np.frombuffer(raw, PIXEL_DTYPES[t], part.width, p)
                    p += w
        self._cache[pi] = planes
        return planes

    def channel(self, name, as_float=True):
        """Full-frame channel (display window size), top row first."""
        pi, ci = self.index[name]
        part = self.parts[pi]
        plane = self._decode_part(pi)[ci]
        dx, dy, w, h = part.display
        if (part.x0, part.y0, part.width, part.height) == (dx, dy, w, h):
            out = plane
        else:
            out = np.zeros((h, w), plane.dtype)
            x0, y0 = part.x0 - dx, part.y0 - dy
            xs0, ys0 = max(0, -x0), max(0, -y0)
            xs1, ys1 = min(part.width, w - x0), min(part.height, h - y0)
            if xs1 > xs0 and ys1 > ys0:
                out[y0 + ys0:y0 + ys1, x0 + xs0:x0 + xs1] = plane[ys0:ys1, xs0:xs1]
        if as_float and out.dtype != np.float32:
            out = out.astype(np.float32)
        return out

    def raw_channel(self, name):
        """Channel as float32 bits untouched (for Cryptomatte IDs)."""
        c = self.channel(name, as_float=False)
        if c.dtype != np.float32:
            raise EXRError("Cryptomatte channel %s is not 32-bit float" % name)
        return c

    def free(self):
        self._cache.clear()
        self.data = b""
