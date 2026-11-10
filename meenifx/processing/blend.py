"""Layer blend modes: one implementation for the preview, the export and thumbnails.

Colour maths happens on float32 BGR arrays in the 0..255 domain and the result is
composited with the layer's own alpha, so a rotated clip, text or a PNG character
blends by its real coverage instead of painting its bounding box. The mode list and
labels live here so the clip properties dropdown, the model validator and the
compositor can never disagree about which modes exist.
"""

from __future__ import annotations

import numpy as np

BLEND_MODES = ("normal", "add", "screen", "multiply", "overlay", "soft_light", "hard_light",
               "difference", "darken", "lighten", "color_dodge", "color_burn")

BLEND_LABELS = (
    ("normal", "Normal"),
    ("add", "Add"),
    ("screen", "Screen"),
    ("multiply", "Multiply"),
    ("overlay", "Overlay"),
    ("soft_light", "Soft Light"),
    ("hard_light", "Hard Light"),
    ("difference", "Difference"),
    ("darken", "Darken"),
    ("lighten", "Lighten"),
    ("color_dodge", "Color Dodge"),
    ("color_burn", "Color Burn"),
)

BLEND_LABEL_BY_KEY = dict(BLEND_LABELS)


def normalise_mode(mode) -> str:
    """The stored key for `mode`, falling back to "normal" for anything unknown."""
    key = str(mode or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in BLEND_MODES else "normal"


def mode_label(mode) -> str:
    return BLEND_LABEL_BY_KEY.get(normalise_mode(mode), "Normal")


def blend_rgb(base, top, mode="normal"):
    """Blend float32 BGR arrays in the 0..255 domain (no clipping; callers clamp).

    Each mode follows the W3C compositing formulas, with the channel value scaled to
    0..1 where the formula is expressed that way. `base` is the backdrop (what is
    already on the canvas), `top` is the source layer's colour.
    """
    key = normalise_mode(mode)
    if key == "normal":
        return top
    base = base.astype(np.float32, copy=False)
    top = top.astype(np.float32, copy=False)
    if key == "add":
        return base + top
    b = np.clip(base, 0.0, 255.0)
    t = np.clip(top, 0.0, 255.0)
    if key == "screen":
        return 255.0 - (255.0 - b) * (255.0 - t) / 255.0
    if key == "multiply":
        return b * t / 255.0
    if key == "overlay":
        return np.where(b <= 127.5, 2.0 * b * t / 255.0,
                        255.0 - 2.0 * (255.0 - b) * (255.0 - t) / 255.0)
    if key == "hard_light":
        return np.where(t <= 127.5, 2.0 * b * t / 255.0,
                        255.0 - 2.0 * (255.0 - b) * (255.0 - t) / 255.0)
    if key == "soft_light":
        return ((1.0 - 2.0 * t / 255.0) * b * b / 255.0) + 2.0 * t / 255.0 * b
    if key == "difference":
        return np.abs(b - t)
    if key == "darken":
        return np.minimum(b, t)
    if key == "lighten":
        return np.maximum(b, t)
    if key == "color_dodge":
        return np.where(t >= 255.0, 255.0, np.minimum(255.0, b * 255.0 / np.maximum(1e-3, 255.0 - t)))
    if key == "color_burn":
        return np.where(t <= 0.0, 0.0,
                        255.0 - np.minimum(255.0, (255.0 - b) * 255.0 / np.maximum(1e-3, t)))
    return top


def composite(canvas, layer, opacity=1.0, mode="normal"):
    """Alpha-composite a BGRA `layer` onto a float32 BGR `canvas` in place.

    The blended colour is mixed in by the layer's alpha times `opacity`, which is what
    keeps a translucent or anti-aliased edge identical to a plain "normal" composite.
    """
    if layer.shape[:2] != canvas.shape[:2]:
        raise ValueError("blend.composite needs a layer the size of the canvas")
    alpha = layer[:, :, 3]
    if opacity <= 0.0:
        return canvas
    # A title's pixels live in a band of the frame; compositing only that band keeps a
    # 60-character line cheap without knowing anything about what drew it.
    rows = np.flatnonzero(alpha.any(axis=1))
    if rows.size == 0:
        return canvas
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    cols = np.flatnonzero(alpha[y0:y1].any(axis=0))
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    key = normalise_mode(mode)
    # A full-frame opaque layer is the common base of every composite: copying it is one
    # pass instead of three float buffers.
    if (opacity >= 1.0 and key == "normal" and rows.size == alpha.shape[0]
            and cols.size == alpha.shape[1] and int(alpha.min()) == 255):
        np.copyto(canvas, layer[:, :, :3])
        return canvas
    sub_alpha = alpha[y0:y1, x0:x1].astype(np.float32) / 255.0
    sub_alpha *= float(opacity)
    rgb = layer[y0:y1, x0:x1, :3].astype(np.float32)
    a = sub_alpha[:, :, None]
    view = canvas[y0:y1, x0:x1]
    if key == "normal":
        view *= 1.0 - a
        view += rgb * a
        return canvas
    blended = np.clip(blend_rgb(view, rgb, key), 0.0, 255.0)
    view *= 1.0 - a
    view += blended * a
    return canvas


def composite_rgba(canvas, layer, opacity=1.0, mode="normal"):
    """Alpha-composite a layer onto a float32 BGRA canvas whose alpha is 0..1.

    The normal compositor above returns only BGR, which is right for the main timeline
    because the project background is opaque. A transparent pre-composition needs the
    fourth channel to survive, so this path accumulates source-over alpha and keeps the
    colour straight (not premultiplied). The W3C blend term is used only where the
    backdrop actually has coverage, so a blend cannot paint a dark edge into empty alpha.
    """
    if layer.shape[:2] != canvas.shape[:2]:
        raise ValueError("blend.composite_rgba needs a layer the size of the canvas")
    if canvas.ndim != 3 or canvas.shape[2] != 4:
        raise ValueError("blend.composite_rgba needs a BGRA canvas")
    if opacity <= 0.0:
        return canvas
    src_rgb = layer[:, :, :3].astype(np.float32)
    src_a = layer[:, :, 3].astype(np.float32)
    if layer.dtype == np.uint8:
        src_a /= 255.0
    src_a *= float(opacity)
    if not np.any(src_a):
        return canvas
    dst_rgb = canvas[:, :, :3]
    dst_a = canvas[:, :, 3]
    out_a = src_a + dst_a * (1.0 - src_a)
    if normalise_mode(mode) == "normal":
        source_colour = src_rgb
    else:
        blended = np.clip(blend_rgb(dst_rgb, src_rgb, mode), 0.0, 255.0)
        source_colour = (src_rgb * (1.0 - dst_a)[:, :, None]
                         + blended * dst_a[:, :, None])
    colour = (source_colour * src_a[:, :, None]
              + dst_rgb * (dst_a * (1.0 - src_a))[:, :, None])
    colour /= np.maximum(out_a, 1e-6)[:, :, None]
    canvas[:, :, :3] = np.where(out_a[:, :, None] > 1e-6, colour, 0.0)
    canvas[:, :, 3] = out_a
    return canvas


def mix_layers(original, result, amount):
    """Linear blend of two BGRA layers (the effect "Mix" control).

    Both colour and alpha are mixed, so an effect that adds transparency (a glow's
    halo) fades out with the same control as one that only changes colour.
    """
    amount = max(0.0, min(1.0, float(amount)))
    if amount >= 0.999:
        return result
    if amount <= 0.001:
        return original
    out = original.astype(np.float32) * (1.0 - amount) + result.astype(np.float32) * amount
    return np.clip(out, 0.0, 255.0).astype(np.uint8)
