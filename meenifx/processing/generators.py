"""Deterministic full-frame renderers for generated source clips.

The preview and exporter both call :func:`render_crop` through the same generated-layer
path used by text and shapes.  Every generator is a pure function of its settings: no
random state is stored in a clip, and the same seed/evolution always produces the same
pixels.  Pattern math is done in project coordinates, so a transformed or cropped render
keeps the generator's scale instead of stretching a cached image.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import ImageColor

from ..model import generators as MODEL

_CACHE = {}
CACHE_LIMIT = 48


def _rgba(colour):
    """(r, g, b, alpha 0..1) from #RGB, #RRGGBB, #AARRGGBB or a colour name."""
    text = str(colour or "#000000").strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        alpha = 1.0
        if len(digits) == 8:
            try:
                alpha = int(digits[0:2], 16) / 255.0
            except ValueError:
                alpha = 1.0
            digits = digits[2:]
        if len(digits) == 6:
            try:
                return (int(digits[0:2], 16), int(digits[2:4], 16),
                        int(digits[4:6], 16), alpha)
            except ValueError:
                pass
    try:
        red, green, blue = ImageColor.getrgb(text)[:3]
        return int(red), int(green), int(blue), 1.0
    except Exception:
        return 0, 0, 0, 1.0


def _stops(spec):
    raw = []
    for part in str(spec.get("gen_stops") or "").split(";"):
        position, _sep, colour = part.partition(":")
        if colour.strip():
            try:
                at = max(0.0, min(100.0, float(position))) / 100.0
            except ValueError:
                continue
            raw.append((at, _rgba(colour.strip())))
    if len(raw) < 2:
        raw = [(0.0, _rgba(spec.get("gen_color_a"))),
               (1.0, _rgba(spec.get("gen_color_b")))]
    raw.sort(key=lambda item: item[0])
    if raw[0][0] > 0.0:
        raw.insert(0, (0.0, raw[0][1]))
    if raw[-1][0] < 1.0:
        raw.append((1.0, raw[-1][1]))
    return raw


def _gradient_t(kind, angle, xx, yy, width, height):
    nx = (xx + 0.5) / max(1.0, width) - 0.5
    ny = (yy + 0.5) / max(1.0, height) - 0.5
    angle = math.radians(float(angle or 0.0))
    if kind == "radial":
        radius = math.hypot(nx, ny) / 0.70710678
        return np.clip(radius, 0.0, 1.0)
    if kind == "angle":
        return (np.arctan2(ny, nx) / (2.0 * math.pi) + 0.5) % 1.0
    if kind == "diamond":
        return np.clip((np.abs(nx) + np.abs(ny)) / 0.70710678, 0.0, 1.0)
    return np.clip(nx * math.cos(angle) + ny * math.sin(angle) + 0.5, 0.0, 1.0)


def _gradient(stops, t):
    """BGRA float planes for a stop list evaluated at ``t`` (0..1)."""
    planes = []
    for channel in range(4):
        values = np.empty_like(t, dtype=np.float32)
        values[:] = stops[-1][1][channel]
        for index in range(len(stops) - 1, 0, -1):
            p0, c0 = stops[index - 1]
            p1, c1 = stops[index]
            if p1 <= p0:
                mask = t >= p1
                values[mask] = c1[channel]
                continue
            local = np.clip((t - p0) / (p1 - p0), 0.0, 1.0)
            values = np.where(t < p1, c0[channel] + (c1[channel] - c0[channel]) * local,
                              values)
        planes.append(values)
    return planes


def _hash2(ix, iy, seed):
    """A stable integer hash -> float in [0, 1) for each lattice point."""
    value = (ix.astype(np.uint64) * np.uint64(0x9E3779B1)
             ^ iy.astype(np.uint64) * np.uint64(0x85EBCA77)
             ^ np.uint64(int(seed) & 0xFFFFFFFF) * np.uint64(0xC2B2AE3D))
    value ^= value >> np.uint64(15)
    value *= np.uint64(0x2545F491)
    value ^= value >> np.uint64(13)
    return (value & np.uint64(0xFFFFFF)).astype(np.float32) / float(0x1000000)


def _value_noise(x, y, seed):
    ix = np.floor(x).astype(np.int64)
    iy = np.floor(y).astype(np.int64)
    fx = x - ix
    fy = y - iy
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    a = _hash2(ix, iy, seed)
    b = _hash2(ix + 1, iy, seed)
    c = _hash2(ix, iy + 1, seed)
    d = _hash2(ix + 1, iy + 1, seed)
    top = a + (b - a) * fx
    bottom = c + (d - c) * fx
    return top + (bottom - top) * fy


def _fractal(spec, xx, yy, width, height):
    seed = int(round(float(spec.get("gen_seed", 7.0))))
    scale = max(0.05, float(spec.get("gen_scale", 100.0)) / 100.0)
    feature = max(2.0, min(width, height) * scale / 3.0)
    detail = max(1, min(8, int(round(float(spec.get("gen_detail", 5.0))))))
    evolution = float(spec.get("gen_evolution", 0.0))
    total = np.zeros_like(xx, dtype=np.float32)
    weight_sum = 0.0
    amplitude = 1.0
    frequency = 1.0 / feature
    for octave in range(detail):
        # The evolution offset moves the field without a per-frame RNG, so scrubbing or
        # exporting the same frame always yields exactly the same noise.
        offset = evolution * 0.37 * (octave + 1)
        layer = _value_noise(xx * frequency + offset * frequency,
                             yy * frequency - offset * frequency,
                             seed + octave * 1013)
        total += layer * amplitude
        weight_sum += amplitude
        amplitude *= 0.52
        frequency *= 2.0
    noise = total / max(1e-6, weight_sum)
    contrast = max(0.0, float(spec.get("gen_contrast", 100.0)) / 100.0)
    return np.clip((noise - 0.5) * contrast + 0.5, 0.0, 1.0)


def _view(crop, width, height):
    if not crop:
        return 0, 0, int(width), int(height)
    try:
        x, y, w, h = (int(round(float(value))) for value in crop)
    except (TypeError, ValueError):
        return 0, 0, int(width), int(height)
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    return x, y, max(1, min(width - x, w)), max(1, min(height - y, h))


def render_crop(spec, width, height, crop=None):
    """A BGRA crop of a full-frame generated picture."""
    width, height = max(1, int(width)), max(1, int(height))
    if not isinstance(spec, dict):
        spec = {}
    view_x, view_y, w, h = _view(crop, width, height)
    key = (MODEL.normalise(spec.get("kind")),
           tuple(sorted((str(name), _clean(value)) for name, value in spec.items())),
           width, height, view_x, view_y, w, h)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached.copy()

    xx, yy = np.meshgrid(np.arange(view_x, view_x + w, dtype=np.float32),
                         np.arange(view_y, view_y + h, dtype=np.float32))
    kind = MODEL.normalise(spec.get("kind"))
    colour_a = np.array(_rgba(spec.get("gen_color_a", "#000000")), dtype=np.float32)
    colour_b = np.array(_rgba(spec.get("gen_color_b", "#FFFFFF")), dtype=np.float32)
    image = np.zeros((h, w, 4), dtype=np.float32)

    if kind == "solid":
        colour = np.array(_rgba(spec.get("gen_color", spec.get("gen_color_a"))),
                          dtype=np.float32)
        image[:, :, :3] = colour[:3]
        image[:, :, 3] = colour[3]
    elif kind == "gradient":
        t = _gradient_t(str(spec.get("gen_type") or "linear"),
                        spec.get("gen_angle", 0.0), xx, yy, width, height)
        planes = _gradient(_stops(spec), t)
        for channel in range(4):
            image[:, :, channel] = planes[channel]
    elif kind == "checkerboard":
        size = max(2.0, float(spec.get("gen_size", 48.0)))
        ox, oy = float(spec.get("gen_offset_x", 0.0)), float(spec.get("gen_offset_y", 0.0))
        bx = np.floor((xx + ox) / size).astype(np.int64)
        by = np.floor((yy + oy) / size).astype(np.int64)
        second = ((bx + by) & 1).astype(bool)
        image[:, :, :3] = np.where(second[:, :, None], colour_b[:3], colour_a[:3])
        image[:, :, 3] = np.where(second, colour_b[3], colour_a[3])
    elif kind == "grid":
        spacing = max(2.0, float(spec.get("gen_spacing", 64.0)))
        thickness = max(1.0, float(spec.get("gen_thickness", 1.0)))
        ox, oy = float(spec.get("gen_offset_x", 0.0)), float(spec.get("gen_offset_y", 0.0))
        line_x = np.mod(xx + ox, spacing) < thickness
        line_y = np.mod(yy + oy, spacing) < thickness
        line = line_x | line_y
        image[:, :, :3] = np.where(line[:, :, None], colour_a[:3], colour_b[:3])
        image[:, :, 3] = np.where(line, colour_a[3], colour_b[3])
    elif kind == "fractal_noise":
        noise = _fractal(spec, xx, yy, width, height)
        image[:, :, :3] = (colour_a[:3] + (colour_b[:3] - colour_a[:3])
                           * noise[:, :, None])
        image[:, :, 3] = colour_a[3] + (colour_b[3] - colour_a[3]) * noise

    opacity = max(0.0, min(1.0, float(spec.get("gen_opacity", 100.0)) / 100.0))
    image[:, :, 3] *= opacity
    canvas = np.empty((h, w, 4), dtype=np.uint8)
    canvas[:, :, :3] = np.clip(image[:, :, 2::-1], 0, 255).astype(np.uint8)
    canvas[:, :, 3] = np.clip(image[:, :, 3] * 255.0, 0, 255).astype(np.uint8)
    if len(_CACHE) >= CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = canvas.copy()
    return canvas


def _clean(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return tuple(sorted((str(key), _clean(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_clean(item) for item in value)
    return value


def clear_cache():
    _CACHE.clear()
