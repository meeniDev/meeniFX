"""Draw a shape clip into a transparent BGRA layer with OpenCV.

A shape is described by numbers (kind, box, radius, sides...), never by a stored bitmap:
every frame re-draws it at the *render* resolution, so a 35 % preview and a 100 % export are
the same picture and nothing is ever upscaled. Drawing happens in a tile around the shape's
own bounds, at 2x supersampling for smooth edges, so a small shape on a 4K frame costs a
small fraction of drawing the whole frame.

The returned layer is full-frame and transparent, matching the text renderer: the
compositor's transform then moves/scales it like any other picture.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from PIL import ImageColor

_TILE_CACHE = {}
CACHE_LIMIT = 96
SUPERSAMPLE = 2.0


def _bgr(colour):
    """(b, g, r) from a "#rrggbb" string (names accepted too)."""
    text = str(colour or "#000000").strip()
    rgb = (0, 0, 0)
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 8:
            digits = digits[2:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        try:
            rgb = (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        except ValueError:
            rgb = (0, 0, 0)
    else:
        try:
            rgb = ImageColor.getrgb(text)[:3]
        except ValueError:
            rgb = (0, 0, 0)
    return float(rgb[2]), float(rgb[1]), float(rgb[0])


def _rounded_path(x, y, w, h, radius, segments=12):
    """Points around a rounded rectangle, clockwise from the top-left corner."""
    radius = max(0.0, min(float(radius), min(w, h) / 2.0))
    if radius <= 0.01:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    points = []
    corners = ((x + radius, y + radius, math.pi, 1.5 * math.pi),
               (x + w - radius, y + radius, 1.5 * math.pi, 2.0 * math.pi),
               (x + w - radius, y + h - radius, 0.0, 0.5 * math.pi),
               (x + radius, y + h - radius, 0.5 * math.pi, math.pi))
    for cx, cy, start, end in corners:
        for step in range(segments + 1):
            angle = start + (end - start) * step / segments
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _ellipse_path(x, y, w, h, steps=64):
    cx, cy = x + w / 2.0, y + h / 2.0
    return [(cx + w / 2.0 * math.cos(2 * math.pi * i / steps),
             cy + h / 2.0 * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def _polygon_path(kind, x, y, w, h, spec):
    cx, cy = x + w / 2.0, y + h / 2.0
    rx, ry = w / 2.0, h / 2.0
    if kind == "polygon":
        sides = max(3, int(round(spec.get("sides", 6))))
        return [(cx + rx * math.cos(-math.pi / 2 + 2 * math.pi * i / sides),
                 cy + ry * math.sin(-math.pi / 2 + 2 * math.pi * i / sides))
                for i in range(sides)]
    if kind == "star":
        points = max(3, int(round(spec.get("points", 5))))
        inner = max(0.05, min(0.95, float(spec.get("inner_ratio", 0.5))))
        path = []
        for index in range(points * 2):
            scale = 1.0 if index % 2 == 0 else inner
            angle = -math.pi / 2 + math.pi * index / points
            path.append((cx + rx * scale * math.cos(angle), cy + ry * scale * math.sin(angle)))
        return path
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _arrow_masks(mask, start, end, thickness, spec):
    length = max(4.0, thickness * 3.2)
    half = max(2.0, thickness * 1.35)
    for enabled, tip, direction in ((spec.get("arrow_end"), end, 1.0),
                                    (spec.get("arrow_start"), start, -1.0)):
        if not enabled:
            continue
        base_x = tip[0] - direction * length
        triangle = np.array([[tip[0], tip[1]],
                             [base_x, tip[1] - half],
                             [base_x, tip[1] + half]], dtype=np.int32)
        cv2.fillPoly(mask, [triangle], 255, lineType=cv2.LINE_AA)


def _fill_path(mask, path):
    cv2.fillPoly(mask, [np.round(np.array(path)).astype(np.int32)], 255,
                 lineType=cv2.LINE_AA)


def _stroke_path(mask, path, weight):
    if weight <= 0:
        return
    points = np.round(np.array(path)).astype(np.int32)
    cv2.polylines(mask, [points], True, 255, max(1, int(round(weight))),
                  lineType=cv2.LINE_AA)


def _ring(fill, width, align):
    """A stroke of `width` entirely inside or entirely outside the filled path.

    Clipping a centred stroke to the fill would leave only half its width, which is why
    "inside" and "outside" are built from the signed distance to the outline instead: the
    band runs 0..width inward (inside) or outward (outside), with a one-pixel soft edge
    that the supersampled downscale turns into a smooth outline.
    """
    inside = cv2.distanceTransform((fill > 127).astype(np.uint8), cv2.DIST_L2, 5)
    outside = cv2.distanceTransform((fill <= 127).astype(np.uint8), cv2.DIST_L2, 5)
    signed = inside.astype(np.float32) - outside.astype(np.float32)
    if align == "inside":
        coverage = (np.clip(signed + 0.5, 0, 1)
                    * np.clip(width - signed + 0.5, 0, 1))
    else:
        coverage = (np.clip(-signed + 0.5, 0, 1)
                    * np.clip(width + signed + 0.5, 0, 1))
    return np.clip(coverage * 255.0, 0, 255).astype(np.uint8)


def _tile(spec, project_w, project_h, scale):
    """(BGRA uint8 tile, (left, top)) for one shape at one render scale."""
    width = max(1.0, float(spec.get("width", 1.0)))
    height = max(1.0, float(spec.get("height", 1.0)))
    kind = str(spec.get("kind") or "rectangle")
    stroke = float(spec.get("stroke_width", 0.0)) if spec.get("stroke_enabled") else 0.0
    if kind == "line":
        stroke = max(0.5, stroke)         # a line is its stroke: it is never invisible
    margin = max(stroke, 1.0) * 2.0 + 6.0
    x = (project_w - width) / 2.0
    y = (project_h - height) / 2.0
    # The tile never leaves the frame: a shape can be wider than the project.
    left, top = max(0.0, x - margin), max(0.0, y - margin)
    right, bottom = min(project_w, x + width + margin), min(project_h, y + height + margin)
    if right - left < 1.0 or bottom - top < 1.0:
        return None, (0, 0)

    tile_w = max(1, int(round((right - left) * scale)))
    tile_h = max(1, int(round((bottom - top) * scale)))
    s = SUPERSAMPLE
    big_w, big_h = max(1, int(tile_w * s)), max(1, int(tile_h * s))
    # Project pixels -> supersampled tile pixels.
    px = big_w / max(1e-6, right - left)
    py = big_h / max(1e-6, bottom - top)
    lx, ly = (x - left) * px, (y - top) * py
    lw, lh = width * px, height * py
    thickness = stroke * (px + py) / 2.0

    fill = np.zeros((big_h, big_w), dtype=np.uint8)
    stroke_mask = np.zeros((big_h, big_w), dtype=np.uint8)
    align = str(spec.get("align") or "center")

    if kind == "line":
        cy = ly + lh / 2.0
        start_x, end_x = lx, lx + lw
        weight = max(1.0, thickness)
        if spec.get("cap") == "square":
            start_x -= weight / 2.0
            end_x += weight / 2.0
        start, end = (start_x, cy), (end_x, cy)
        cv2.line(stroke_mask, (int(round(start[0])), int(round(start[1]))),
                 (int(round(end[0])), int(round(end[1]))), 255, int(round(weight)),
                 lineType=cv2.LINE_AA)
        if spec.get("cap") == "round":
            for point in (start, end):
                cv2.circle(stroke_mask, (int(round(point[0])), int(round(point[1]))),
                           max(1, int(round(weight / 2.0))), 255, -1, lineType=cv2.LINE_AA)
        _arrow_masks(stroke_mask, start, end, weight, spec)
    else:
        if kind == "ellipse":
            path = _ellipse_path(lx, ly, lw, lh)
        elif kind == "rounded_rectangle":
            path = _rounded_path(lx, ly, lw, lh, float(spec.get("radius", 0.0)) * px)
        else:
            path = _polygon_path(kind, lx, ly, lw, lh, spec)
        _fill_path(fill, path)
        align = align or "center"
        if align in ("inside", "outside"):
            stroke_mask = _ring(fill, thickness, align)
        else:
            _stroke_path(stroke_mask, path, thickness)

    # Masks arrive in 0..255; compositing needs them in 0..1 or the source-over maths
    # multiplies by a negative (1 - 255) and the antialiased edge vanishes.
    fill_layer = (_resize(fill, tile_w, tile_h).astype(np.float32) / 255.0
                  * float(spec.get("fill_opacity", 1.0)))
    stroke_layer = (_resize(stroke_mask, tile_w, tile_h).astype(np.float32) / 255.0
                    * float(spec.get("stroke_opacity", 1.0)))
    # The stroke is painted *over* the fill (an inside stroke lies on top of opaque fill;
    # the other order multiplied it by 1 - fill and made it disappear). Source-over in
    # premultiplied space, then un-premultiplied for OpenCV's BGRA.
    alpha = stroke_layer + fill_layer * (1.0 - stroke_layer)
    b_f, g_f, r_f = _bgr(spec.get("fill_color"))
    b_s, g_s, r_s = _bgr(spec.get("stroke_color"))
    safe = np.maximum(alpha, 1e-6)
    tile = np.zeros((tile_h, tile_w, 4), dtype=np.uint8)
    for index, (fill_c, stroke_c) in enumerate(((b_f, b_s), (g_f, g_s), (r_f, r_s))):
        tile[:, :, index] = np.clip(
            (stroke_c * stroke_layer + fill_c * fill_layer * (1.0 - stroke_layer)) / safe,
            0, 255)
    tile[:, :, 3] = np.clip(alpha * 255.0, 0, 255)
    return tile, (int(round(left * scale)), int(round(top * scale)))


def _resize(mask, width, height):
    if mask.shape[1] == width and mask.shape[0] == height:
        return mask
    interpolation = cv2.INTER_AREA if mask.shape[1] >= width else cv2.INTER_LINEAR
    return cv2.resize(mask, (width, height), interpolation=interpolation)


def _cache_key(spec, project_w, project_h, scale):
    def clean(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 4)
        return value

    return (tuple(sorted((name, clean(value)) for name, value in spec.items())),
            round(project_w, 3), round(project_h, 3), round(scale, 4))


def clear_cache():
    _TILE_CACHE.clear()


def render(spec, width, height, scale=1.0):
    """A (height, width, 4) BGRA uint8 layer with the shape drawn on transparency."""
    width, height = max(1, int(width)), max(1, int(height))
    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    if spec is None:
        return canvas
    scale = max(0.02, min(1.0, float(scale)))
    project_w = width / scale
    project_h = height / scale
    key = _cache_key(spec, project_w, project_h, scale)
    cached = _TILE_CACHE.get(key)
    if cached is None:
        cached = _tile(spec, project_w, project_h, scale)
        if len(_TILE_CACHE) >= CACHE_LIMIT:
            # Bounded: an animated shape must not grow the cache without end.
            _TILE_CACHE.clear()
        _TILE_CACHE[key] = cached
    tile, (left, top) = cached
    if tile is None:
        return canvas
    tile_h, tile_w = tile.shape[:2]
    left = max(0, min(width - 1, int(left)))
    top = max(0, min(height - 1, int(top)))
    right = min(width, left + tile_w)
    bottom = min(height, top + tile_h)
    canvas[top:bottom, left:right] = tile[:bottom - top, :right - left]
    return canvas
