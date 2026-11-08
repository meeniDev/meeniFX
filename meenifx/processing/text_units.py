"""Per-unit animated text: each letter, word or line posed on its own.

The static renderer in :mod:`meenifx.processing.text` draws a whole line in one Pillow
call, which is what makes its kerning exact. A preset animates units separately, so this
module lays the same text out once, caches a bitmap per glyph (stroke and shadow
included) and warps those bitmaps around their own unit centre.

Only frames where a preset is running take this path. At rest the static renderer runs
untouched, which is why a clip without an active preset renders byte-identical pixels to
what it did before presets existed.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .text import _colour, _line_width, _wrap, font_for

# A glyph bitmap depends on the font, the colours and the stroke/shadow, never on the
# frame, so a 60-character title costs 60 bitmaps once and then 60 warps per frame.
_STAMP_CACHE = {}
_STAMP_LIMIT = 4096


class GlyphPose:
    """Line ∘ word ∘ letter pose for one glyph: offsets add, scales and opacity multiply."""

    __slots__ = ("dx", "dy", "sx", "sy", "rotation", "opacity", "blur", "hue", "light", "text")

    def __init__(self):
        self.dx = self.dy = self.rotation = self.blur = self.hue = self.light = 0.0
        self.sx = self.sy = self.opacity = 1.0
        self.text = None

    def add(self, pose):
        if pose is None:
            return self
        self.dx += float(getattr(pose, "dx", 0.0))
        self.dy += float(getattr(pose, "dy", 0.0))
        self.rotation += float(getattr(pose, "rotation", 0.0))
        self.blur += float(getattr(pose, "blur", 0.0))
        self.hue += float(getattr(pose, "hue", 0.0))
        self.light += float(getattr(pose, "light", 0.0))
        self.sx *= float(getattr(pose, "scale_x", 1.0))
        self.sy *= float(getattr(pose, "scale_y", 1.0))
        self.opacity *= float(getattr(pose, "opacity", 1.0))
        override = getattr(pose, "text", None)
        if override is not None:
            self.text = str(override)
        return self

    def is_identity(self):
        return (abs(self.dx) < 1e-6 and abs(self.dy) < 1e-6
                and abs(self.sx - 1.0) < 1e-6 and abs(self.sy - 1.0) < 1e-6
                and abs(self.rotation) < 1e-6 and abs(self.opacity - 1.0) < 1e-6
                and abs(self.blur) < 1e-6 and abs(self.hue) < 1e-6
                and abs(self.light) < 1e-6 and self.text is None)


def layout(spec, width, height, scale=1.0):
    """Glyph boxes, unit boxes and block placement, in device pixels.

    Returns None when there is nothing to draw. The maths mirrors
    :func:`meenifx.processing.text.render` exactly (same wrap, tracking, alignment and
    offsets), so a unit's centre is where the static renderer puts those glyphs.
    """
    width, height = max(1, int(width)), max(1, int(height))
    scale = max(0.05, float(scale))
    probe = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    scratch = ImageDraw.Draw(probe)
    words_text = str(spec.get("text", ""))
    reveal = int(spec.get("reveal", -1))
    if reveal >= 0:
        words_text = words_text[:reveal]
    if not words_text.strip():
        return None
    size = max(2.0, float(spec.get("size", 96.0)) * scale)
    font = font_for(spec.get("font"), spec.get("bold"), spec.get("italic"), size)
    tracking = float(spec.get("letter_spacing", 0.0)) * scale
    stroke = max(0.0, float(spec.get("stroke_width", 0.0)) * scale)
    lines = _wrap(scratch, words_text, font, tracking, stroke,
                  float(spec.get("wrap", 0.0)) * scale)
    if not lines:
        return None
    ascent, descent = font.getmetrics()
    line_height = float(ascent + descent)
    gap = line_height * (max(0.5, float(spec.get("line_spacing", 1.15))) - 1.0)
    widths = [_line_width(scratch, line, font, tracking, stroke) for line in lines]
    block_w = max(widths) if widths else 0.0
    block_h = line_height * len(lines) + gap * (len(lines) - 1)
    align = spec.get("align", "center")
    offset_x = float(spec.get("offset_x", 0.0)) * scale
    offset_y = float(spec.get("offset_y", 0.0)) * scale
    left = (width - block_w) / 2.0 + offset_x
    if align == "left":
        left = offset_x
    elif align == "right":
        left = width - block_w + offset_x
    top = (height - block_h) / 2.0 + offset_y

    glyphs, letters, line_units = [], [], []
    for index, line in enumerate(lines):
        y = top + index * (line_height + gap)
        if align == "center":
            x = left + (block_w - widths[index]) / 2.0
        elif align == "right":
            x = left + (block_w - widths[index])
        else:
            x = left
        cursor = x
        word_index = -1
        inside_word = False
        for char in line:
            advance = scratch.textlength(char, font=font)
            if char.strip():
                if not inside_word:
                    word_index += 1
                    inside_word = True
                glyphs.append({"line": index, "word": word_index, "char": len(letters),
                               "x": cursor, "y": y, "w": advance, "text": char})
                letters.append((cursor + advance / 2.0, y + line_height / 2.0, char))
            else:
                inside_word = False
            cursor += advance + tracking
        line_units.append((x + widths[index] / 2.0, y + line_height / 2.0, line))

    word_boxes = {}
    for glyph in glyphs:
        key = (glyph["line"], glyph["word"])
        box = word_boxes.get(key)
        if box is None:
            word_boxes[key] = [glyph["x"], glyph["y"], glyph["x"] + glyph["w"], glyph["text"]]
        else:
            box[0] = min(box[0], glyph["x"])
            box[2] = max(box[2], glyph["x"] + glyph["w"])
            box[3] = box[3] + glyph["text"]
    words = []
    for key in sorted(word_boxes):
        x0, y, x1, text_value = word_boxes[key]
        words.append(((x0 + x1) / 2.0, y + line_height / 2.0, text_value))

    return {"font": font, "lines": lines, "widths": widths, "glyphs": glyphs,
            "letters": letters, "words": words, "lines_units": line_units,
            "line_height": line_height, "gap": gap, "left": left, "top": top,
            "align": align, "tracking": tracking, "stroke": stroke,
            "ascent": ascent, "descent": descent, "block_w": block_w, "size": size}


def layout_units(spec, width, height, scale=1.0):
    """{"letter": [...], "word": [...], "line": [(cx, cy, text)]} for the preset model."""
    built = layout(spec, width, height, scale)
    if built is None:
        return {"letter": [], "word": [], "line": []}
    return {"letter": built["letters"], "word": built["words"], "line": built["lines_units"]}


def _stamp_key(spec, glyph_text, scale):
    return (str(spec.get("font")), round(float(spec.get("size", 96.0)) * scale, 2),
            bool(spec.get("bold")), bool(spec.get("italic")), glyph_text,
            str(spec.get("color")), str(spec.get("stroke_color")),
            round(float(spec.get("stroke_width", 0.0)) * scale, 2),
            str(spec.get("shadow_color")), round(float(spec.get("shadow_opacity", 0.0)), 3),
            round(float(spec.get("shadow_offset_x", 0.0)) * scale, 2),
            round(float(spec.get("shadow_offset_y", 0.0)) * scale, 2),
            round(float(spec.get("shadow_blur", 0.0)) * scale, 2),
            round(float(spec.get("fill_opacity", 1.0)), 3),
            bool(spec.get("underline")))


def stamp(spec, glyph_text, scale):
    """(BGRA bitmap, ox, oy) of one glyph with its stroke and shadow, cached.

    The caller composites into a BGRA layer, so the RGBA bitmap Pillow produces is
    converted once here. Returning it as RGBA made every animated glyph swap red and
    blue (and made the hue rotation read the wrong channels).

    ox/oy is where the bitmap's top-left corner sits when the glyph is drawn at (0, 0)
    with Pillow's "la" anchor - the same origin the static renderer uses.
    """
    key = _stamp_key(spec, glyph_text, scale)
    cached = _STAMP_CACHE.get(key)
    if cached is not None:
        return cached
    size = max(2.0, float(spec.get("size", 96.0)) * scale)
    font = font_for(spec.get("font"), spec.get("bold"), spec.get("italic"), size)
    stroke = max(0.0, float(spec.get("stroke_width", 0.0)) * scale)
    stroke_px = int(round(stroke))
    fill_alpha = int(round(255 * max(0.0, min(1.0, float(spec.get("fill_opacity", 1.0))))))
    fill = _colour(spec.get("color"), fill_alpha)
    stroke_fill = _colour(spec.get("stroke_color"))
    x0, y0, x1, y1 = font.getbbox(glyph_text, anchor="la", stroke_width=stroke_px)
    shadow_alpha = max(0.0, min(1.0, float(spec.get("shadow_opacity", 0.0))))
    shadow_dx = float(spec.get("shadow_offset_x", 0.0)) * scale
    shadow_dy = float(spec.get("shadow_offset_y", 0.0)) * scale
    shadow_blur = max(0.0, float(spec.get("shadow_blur", 0.0)) * scale / 2.0)
    has_shadow = shadow_alpha > 0.001 and (abs(shadow_dx) > 0.05 or abs(shadow_dy) > 0.05
                                           or shadow_blur > 0.05)
    pad = shadow_blur * 2.0 + 2.0 if has_shadow else 0.0
    left = min(x0, x0 + shadow_dx - pad) if has_shadow else x0
    top = min(y0, y0 + shadow_dy - pad) if has_shadow else y0
    right = max(x1, x1 + shadow_dx + pad) if has_shadow else x1
    bottom = max(y1, y1 + shadow_dy + pad) if has_shadow else y1
    bitmap_w = max(1, int(math.ceil(right - left)) + 1)
    bitmap_h = max(1, int(math.ceil(bottom - top)) + 1)
    image = Image.new("RGBA", (bitmap_w, bitmap_h), (0, 0, 0, 0))
    if has_shadow:
        shadow = Image.new("RGBA", (bitmap_w, bitmap_h), (0, 0, 0, 0))
        shadow_colour = _colour(spec.get("shadow_color"), int(255 * shadow_alpha))
        ImageDraw.Draw(shadow).text((-left + shadow_dx, -top + shadow_dy), glyph_text,
                                    font=font, fill=shadow_colour, anchor="la",
                                    stroke_width=stroke_px, stroke_fill=shadow_colour)
        if shadow_blur > 0.05:
            shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
        image = Image.alpha_composite(image, shadow)
    painter = ImageDraw.Draw(image)
    painter.text((-left, -top), glyph_text, font=font, fill=fill, anchor="la",
                 stroke_width=stroke_px, stroke_fill=stroke_fill if stroke_px > 0 else None)
    if spec.get("underline"):
        ascent, descent = font.getmetrics()
        thickness = max(1.0, (ascent + descent) * 0.06)
        underline_y = -top + ascent + descent * 0.35
        painter.rectangle([-left, underline_y, -left + max(1.0, font.getlength(glyph_text)),
                           underline_y + thickness], fill=fill)
    rgba = np.asarray(image, dtype=np.uint8)
    result = (rgba[:, :, [2, 1, 0, 3]].copy(), left, top)
    if len(_STAMP_CACHE) > _STAMP_LIMIT:
        _STAMP_CACHE.clear()
    _STAMP_CACHE[key] = result
    return result


def _tint(bitmap, hue, light):
    """Hue rotation (degrees) and brightness lift on a glyph bitmap; alpha kept."""
    if abs(hue) < 0.05 and abs(light) < 0.005:
        return bitmap
    import cv2
    rgb = bitmap[:, :, :3]
    if abs(hue) >= 0.05:
        hsv = cv2.cvtColor(rgb, cv2.COLOR_BGR2HSV)
        shift = int(round(hue / 2.0)) % 180
        hsv[:, :, 0] = (hsv[:, :, 0].astype(np.int32) + shift) % 180
        rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    if abs(light) >= 0.005:
        rgb = np.clip(rgb.astype(np.float32) + light * 255.0, 0, 255).astype(np.uint8)
    return np.dstack([rgb, bitmap[:, :, 3]])


def _warp(bitmap, origin, centre, pose, scale):
    """Rotate/scale a glyph bitmap around its unit centre; (bitmap, x, y)."""
    import cv2
    height, width = bitmap.shape[:2]
    theta = math.radians(pose.rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    a, b = cos_t * pose.sx, -sin_t * pose.sy
    c, d = sin_t * pose.sx, cos_t * pose.sy
    cx, cy = centre
    ox, oy = origin
    # The stamp's pixel (u, v) stands for the layer point (ox + u, oy + v), so the
    # translation has to carry that origin through the rotation/scale as well.
    tx = cx + (a * (ox - cx) + b * (oy - cy)) + pose.dx
    ty = cy + (c * (ox - cx) + d * (oy - cy)) + pose.dy
    # Source pixels, not layer coordinates: the translation above already carries the
    # stamp's layer origin through the rotation.
    corners = np.array([[0.0, 0.0], [width, 0.0], [0.0, height],
                        [width, height]], dtype=np.float64)
    moved = np.stack([a * corners[:, 0] + b * corners[:, 1] + tx,
                      c * corners[:, 0] + d * corners[:, 1] + ty], axis=1)
    x0, y0 = int(math.floor(moved[:, 0].min())), int(math.floor(moved[:, 1].min()))
    x1, y1 = int(math.ceil(moved[:, 0].max())), int(math.ceil(moved[:, 1].max()))
    out_w, out_h = max(1, x1 - x0), max(1, y1 - y0)
    matrix = np.array([[a, b, tx - x0], [c, d, ty - y0]], dtype=np.float64)
    warped = cv2.warpAffine(bitmap, matrix, (out_w, out_h), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    if pose.blur > 0.05:
        warped = cv2.GaussianBlur(warped, (0, 0), max(0.1, pose.blur * scale / 2.0))
    return warped, x0, y0


def _blend(layer, bitmap, x, y, opacity):
    """Composite a straight-alpha BGRA bitmap into a uint8 BGRA layer at (x, y)."""
    if opacity <= 0.002:
        return
    height, width = bitmap.shape[:2]
    left, top = int(x), int(y)
    src_x0, src_y0 = max(0, -left), max(0, -top)
    dst_x0, dst_y0 = max(0, left), max(0, top)
    span_w = min(width - src_x0, layer.shape[1] - dst_x0)
    span_h = min(height - src_y0, layer.shape[0] - dst_y0)
    if span_w <= 0 or span_h <= 0:
        return
    source = bitmap[src_y0:src_y0 + span_h, src_x0:src_x0 + span_w].astype(np.float32)
    alpha = source[:, :, 3:4] / 255.0 * float(opacity)
    region = layer[dst_y0:dst_y0 + span_h, dst_x0:dst_x0 + span_w].astype(np.float32)
    region_alpha = region[:, :, 3:4] / 255.0
    out_alpha = alpha + region_alpha * (1.0 - alpha)
    weight = np.maximum(out_alpha, 1e-6)
    rgb = (source[:, :, :3] * alpha + region[:, :, :3] * region_alpha * (1.0 - alpha)) / weight
    layer[dst_y0:dst_y0 + span_h, dst_x0:dst_x0 + span_w] = np.concatenate(
        [rgb, out_alpha * 255.0], axis=2).astype(np.uint8)


def render(spec, width, height, scale=1.0, poses=None, crop=None):
    """A BGRA text layer with the per-unit animation applied; None when there is no text.

    `crop` limits the allocation to an (x, y, w, h) window of the full canvas, exactly
    like :func:`meenifx.processing.text.render`, so transformed text can be rasterised
    at its destination density without allocating a frame at that density.
    """
    width, height = max(1, int(width)), max(1, int(height))
    scale = max(0.05, float(scale))
    from .text import _view
    view_x, view_y, canvas_w, canvas_h = _view(width, height, crop)
    built = layout(spec, width, height, scale)
    if built is None:
        return None
    layer = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)
    # The background plate holds still while the glyphs animate.
    plate_alpha = float(spec.get("bg_opacity", 0.0))
    if plate_alpha > 0.001:
        pad = max(0.0, float(spec.get("bg_padding", 0.0)) * scale)
        block_h = built["line_height"] * len(built["lines"]) + built["gap"] * (len(built["lines"]) - 1)
        rect = [built["left"] - pad - view_x, built["top"] - pad - view_y,
                built["left"] + built["block_w"] + pad - view_x,
                built["top"] + block_h + pad - view_y]
        plate = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rectangle(rect, fill=_colour(spec.get("bg_color"),
                                                           int(255 * plate_alpha)))
        layer = np.asarray(plate, dtype=np.uint8).copy()[:, :, [2, 1, 0, 3]]
    line_poses = (poses or {}).get("line") or []
    word_poses = (poses or {}).get("word") or []
    letter_poses = (poses or {}).get("letter") or []
    fill_opacity = max(0.0, min(1.0, float(spec.get("fill_opacity", 1.0))))
    for glyph in built["glyphs"]:
        pose = GlyphPose()
        if glyph["line"] < len(line_poses):
            pose.add(line_poses[glyph["line"]])
        if glyph["word"] < len(word_poses):
            pose.add(word_poses[glyph["word"]])
        if glyph["char"] < len(letter_poses):
            pose.add(letter_poses[glyph["char"]])
        glyph_text = pose.text if pose.text is not None else glyph["text"]
        if not glyph_text:
            continue
        bitmap, ox, oy = stamp(spec, glyph_text, scale)
        if pose.is_identity():
            _blend(layer, bitmap, glyph["x"] + ox - view_x,
                   glyph["y"] + oy - view_y, fill_opacity)
            continue
        bitmap = _tint(bitmap, pose.hue, pose.light)
        unit_x = glyph["x"] + glyph["w"] / 2.0
        unit_y = glyph["y"] + built["line_height"] / 2.0
        warped, wx, wy = _warp(bitmap, (glyph["x"] + ox, glyph["y"] + oy),
                               (unit_x, unit_y), pose, scale)
        _blend(layer, warped, wx - view_x, wy - view_y, pose.opacity)
    return np.ascontiguousarray(layer)
