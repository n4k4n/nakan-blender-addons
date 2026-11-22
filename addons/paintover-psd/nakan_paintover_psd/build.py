# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns render images, Cryptomatte masks and shadows into the layered PSD."""

import colorsys
import fnmatch

import numpy as np

from . import cryptomatte, psd

GOLDEN = 0.618033988749895


def to_u8(x):
    return (np.clip(x, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def linear_to_srgb(x):
    x = np.clip(x, 0.0, None)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def mask_colors(n):
    cols = []
    for i in range(n):
        h = (0.07 + i * GOLDEN) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.95)
        cols.append((int(r * 255), int(g * 255), int(b * 255)))
    return cols


def shadow_alpha(ratio):
    """Light multiplier (1 = fully lit, 0 = full shadow) -> opacity of a black
    layer that darkens the image the same way (in display space)."""
    lit = linear_to_srgb(np.clip(ratio, 0.0, 1.0))
    return to_u8(1.0 - lit)


def luminance(rgb):
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def shadow_ratio(lit_with, lit_without):
    a, b = luminance(lit_with), luminance(lit_without)
    ratio = np.ones_like(a)
    ok = b > 1e-4
    ratio[ok] = a[ok] / b[ok]
    return np.clip(ratio, 0.0, 1.0)


def _skip(name, patterns):
    low = name.lower()
    for p in patterns:
        if fnmatch.fnmatch(low, p) or (not any(c in p for c in "*?[") and p in low):
            return True
    return False


def parse_patterns(text):
    return [p.strip().lower() for p in text.replace(";", ",").split(",") if p.strip()]


MATERIAL_RENAMES = {"default_surface": "No Material"}
MATERIAL_SKIP = ("default_volume", "default_light", "default_background", "default_empty",
                 "default_hair", "default_pointcloud", "default_grease_pencil")


def collect_masks(exr, kinds, skip_patterns, progress=None):
    """[(group title, [(name, coverage)])] for the Cryptomatte layers whose type
    ('Object', 'Material', 'Asset') is in kinds."""
    groups = []
    layers = cryptomatte.find_layers(exr)
    for li, (layer, ids) in enumerate(layers):
        kind = next((k for k in ("Object", "Material", "Asset") if layer.endswith("Crypto" + k)), None)
        if kinds is not None and kind not in kinds:
            continue
        if kind == "Material":
            ids = {MATERIAL_RENAMES.get(n, n): v for n, v in ids.items() if n not in MATERIAL_SKIP}
        ids = {n: v for n, v in ids.items() if not _skip(n, skip_patterns)}
        sub = None
        if progress:
            sub = lambda f, li=li: progress((li + f) / max(1, len(layers)))
        masks = cryptomatte.decode_masks(exr, layer, ids, sub)
        if masks:
            title = (kind + " Masks") if kind else layer
            if len(layers) > 3:  # several view layers: keep them apart
                title = "%s (%s)" % (title, layer.rsplit(".", 1)[0])
            groups.append((title, sorted(masks.items(), key=lambda kv: kv[0].lower())))
    return groups


def write_psd(path, width, height, render_rgba=None, extra_images=(), shadows=None,
              shadows_visible=True, mask_groups=(), masks_visible=False, colored=True):
    """
    render_rgba   (H, W, 4) uint8, bottom layer "Render"
    extra_images  [(name, rgba uint8, visible)] above the render (bottom to top)
    shadows       (H, W) uint8 opacity of a black "Shadows" layer
    mask_groups   [(title, [(name, float coverage)])], one PSD group each
    """
    doc = psd.PSD(width, height)
    n_masks = sum(len(m) for _t, m in mask_groups)
    colors = iter(mask_colors(n_masks))
    for title, masks in mask_groups:
        g = doc.add_group(title, visible=masks_visible, open=False)
        for name, cov in masks:
            col = next(colors) if colored else (255, 255, 255)
            g.add_fill(name, col, to_u8(cov))
    if shadows is not None:
        black = np.zeros((height, width, 4), np.uint8)
        black[..., 3] = shadows
        doc.add_image("Shadows", black, visible=shadows_visible, compress="rle")
    for name, rgba, vis in reversed(list(extra_images)):
        doc.add_image(name, rgba, visible=vis)
    if render_rgba is not None:
        doc.add_image("Render", render_rgba)

    # flat preview: render over white, plus the shadows if they are shown
    comp = np.full((height, width, 3), 255.0, np.float32)
    if render_rgba is not None:
        a = render_rgba[..., 3:4].astype(np.float32) / 255
        comp = comp * (1 - a) + render_rgba[..., :3].astype(np.float32) * a
    if shadows is not None and shadows_visible:
        comp *= 1 - shadows[..., None].astype(np.float32) / 255
    doc.save(path, comp.astype(np.uint8))
    return n_masks
