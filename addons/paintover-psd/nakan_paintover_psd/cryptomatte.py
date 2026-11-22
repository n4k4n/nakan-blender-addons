# SPDX-License-Identifier: GPL-3.0-or-later
"""Decode Cryptomatte layers from an EXRFile into antialiased coverage masks."""

import json
import re

import numpy as np


def hash_to_id_bits(h):
    """Manifest hash (uint32) -> the bits stored in the float ID channel.
    Cryptomatte avoids denormals, inf and NaN by flipping bit 23 when the
    exponent would be all zeros or all ones."""
    exp = (h >> 23) & 0xFF
    if exp == 0 or exp == 255:
        h ^= 1 << 23
    return h


def find_layers(exr):
    """[(layer_name, {object_name: id_bits})] for every Cryptomatte in the file,
    e.g. ('ViewLayer.CryptoObject', {...})."""
    meta = exr.strings()
    keys = {}
    for k, v in meta.items():
        m = re.match(r"cryptomatte/([0-9a-fA-F]+)/(name|manifest)$", k)
        if m:
            keys.setdefault(m.group(1), {})[m.group(2)] = v
    out = []
    for _key, info in sorted(keys.items(), key=lambda kv: kv[1].get("name", "")):
        name = info.get("name")
        if not name:
            continue
        try:
            manifest = json.loads(info.get("manifest", "{}"))
        except ValueError:
            manifest = {}
        ids = {}
        for obj, hx in manifest.items():
            try:
                ids[obj] = hash_to_id_bits(int(hx, 16))
            except ValueError:
                pass
        out.append((name, ids))
    return out


def rank_channels(exr, layer):
    """[(id_channel, coverage_channel)] for every rank of a Cryptomatte layer."""
    names = set(exr.channel_names())
    pairs = []
    i = 0
    while True:
        base = "%s%02d" % (layer, i)
        found = False
        for lo, up in (("r", "g"), ("b", "a")):
            for case in (str.lower, str.upper):
                idc, cov = "%s.%s" % (base, case(lo)), "%s.%s" % (base, case(up))
                if idc in names and cov in names:
                    pairs.append((idc, cov))
                    found = True
                    break
        if not found:
            break
        i += 1
    return pairs


def decode_masks(exr, layer, ids, progress=None):
    """{name: float32 coverage (H, W)} for every entry of the manifest that
    shows up in the image. Names that cover no pixel are skipped."""
    pairs = rank_channels(exr, layer)
    if not pairs:
        return {}
    h_w = exr.size[1], exr.size[0]
    id_planes = [exr.raw_channel(i).view(np.uint32) for i, _c in pairs]
    cov_planes = [exr.channel(c) for _i, c in pairs]
    present = set()
    for p in id_planes:
        present.update(np.unique(p).tolist())
    masks = {}
    items = [(n, b) for n, b in ids.items() if b in present]
    for k, (name, bits) in enumerate(items):
        m = np.zeros(h_w, np.float32)
        for idp, cov in zip(id_planes, cov_planes):
            hit = idp == bits
            if hit.any():
                m += np.where(hit, cov, 0.0)
        np.clip(m, 0.0, 1.0, out=m)
        if m.max() > 1.0 / 512:
            masks[name] = m
        if progress:
            progress((k + 1) / max(1, len(items)))
    return masks
