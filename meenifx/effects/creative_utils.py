"""Shared math for the Creative Tools effects.

Effects in this package work on straight (non-premultiplied) BGRA layers.  Keeping the
small colour, blend and noise helpers here means the New Glow and colour families agree
on one definition of Rec.709 luma, one blend implementation and one procedural-noise
field instead of quietly drifting apart.
"""

from __future__ import annotations

import math
import re

import cv2
import numpy as np

# BGR order: OpenCV arrays are BGR throughout the renderer.
LUMA_BGR = np.array([0.0722, 0.7152, 0.2126], dtype=np.float32)

BLEND_MODES = ("screen", "add", "overlay", "soft_light", "multiply", "normal")
BLEND_LABELS = ("Screen", "Add", "Overlay", "Soft light", "Multiply", "Normal")

_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def num(value, frame=0):
    """A numeric parameter at a frame: Value, AnimatedValue or plain number."""
    from ..model.keyframes import AnimatedValue, parameter_value_at
    if isinstance(value, AnimatedValue):
        return float(parameter_value_at(value, frame))
    if hasattr(value, "get"):
        return float(value.get())
    return float(value)


def has_value(value):
    return hasattr(value, "get")


def colour_bgr(text, default=(255, 255, 255)):
    """#RRGGBB / #AARRGGBB -> float BGR, with a safe fallback for partial edits."""
    match = _HEX.match(str(text or "").strip())
    if match is None:
        return np.array(default, dtype=np.float32)
    digits = match.group(1)
    if len(digits) == 8:                 # Qt stores #AARRGGBB
        digits = digits[2:]
    return np.array([int(digits[4:6], 16), int(digits[2:4], 16),
                     int(digits[0:2], 16)], dtype=np.float32)


def colour_rgb(text, default=(255, 255, 255)):
    bgr = colour_bgr(text, (default[2], default[1], default[0]))
    return bgr[::-1]


def luma(rgb_bgr):
    return np.clip(rgb_bgr @ LUMA_BGR, 0.0, 255.0)


def blend(base, top, mode="screen"):
    """Blend float32 BGR arrays in the 0..255 domain."""
    b = np.clip(base, 0.0, 255.0)
    t = np.clip(top, 0.0, 255.0)
    if mode == "add":
        return b + t
    if mode == "normal":
        return t
    if mode == "multiply":
        return b * t / 255.0
    if mode == "screen":
        return 255.0 - (255.0 - b) * (255.0 - t) / 255.0
    bn, tn = b / 255.0, t / 255.0
    if mode == "overlay":
        out = np.where(bn <= 0.5, 2.0 * bn * tn, 1.0 - 2.0 * (1.0 - bn) * (1.0 - tn))
    else:                                # W3C soft light
        out = (1.0 - 2.0 * tn) * bn * bn + 2.0 * tn * bn
    return out * 255.0


def gaussian(field, sigma, passes=1):
    """Gaussian blur with a transparent zero border."""
    result = field
    step = max(0.1, float(sigma)) / math.sqrt(max(1, int(passes)))
    for _ in range(max(1, int(passes))):
        result = cv2.GaussianBlur(result, (0, 0), sigmaX=step, sigmaY=step,
                                  borderType=cv2.BORDER_CONSTANT)
    return result


def directional(field, length, angle, samples):
    """Average `field` along a line; a cheap, deterministic directional blur."""
    length = max(0.0, float(length))
    if length < 0.05:
        return field
    count = max(3, int(samples) | 1)
    angle = math.radians(float(angle))
    dx, dy = math.cos(angle), -math.sin(angle)
    result = np.zeros_like(field, dtype=np.float32)
    offsets = np.linspace(-0.5, 0.5, count, dtype=np.float32)
    for offset in offsets:
        matrix = np.array([[1.0, 0.0, float(dx * length * offset)],
                           [0.0, 1.0, float(dy * length * offset)]], dtype=np.float32)
        result += cv2.warpAffine(field, matrix, (field.shape[1], field.shape[0]),
                                 flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return result / float(count)


def remap(layer, map_x, map_y, border=cv2.BORDER_CONSTANT):
    return cv2.remap(layer, map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=border,
                     borderValue=(0, 0, 0, 0))


def smoothstep(edge0, edge1, value):
    width = max(1e-6, float(edge1) - float(edge0))
    x = np.clip((value - float(edge0)) / width, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def value_noise(width, height, scale, seed, phase=0.0):
    """One band-limited value-noise field in 0..1, computed on a small grid."""
    scale = max(2.0, float(scale))
    cols = max(2, int(math.ceil(width / scale)) + 2)
    rows = max(2, int(math.ceil(height / scale)) + 2)
    rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
    lattice = rng.random((rows, cols)).astype(np.float32)
    field = cv2.resize(lattice, (max(2, int(width)), max(2, int(height))),
                       interpolation=cv2.INTER_CUBIC)
    field = field[:int(height), :int(width)]
    shift = int(round(float(phase))) % max(1, field.shape[1])
    if shift:
        field = np.roll(field, shift, axis=1)
    return field


def fractal_field(width, height, scale, octaves, seed, phase=0.0):
    """Fractal value noise normalised to 0..1; deterministic for a seed/frame."""
    total = np.zeros((int(height), int(width)), np.float32)
    weight = 0.0
    amplitude = 1.0
    for octave in range(max(1, int(octaves))):
        total += value_noise(width, height, max(2.0, scale / (2 ** octave)),
                             int(seed) + octave * 7919, phase + octave * 13.0) * amplitude
        weight += amplitude
        amplitude *= 0.5
    total /= max(1e-6, weight)
    low, high = float(total.min()), float(total.max())
    return (total - low) / max(1e-6, high - low)


def radial_coordinates(width, height, center_x=0.5, center_y=0.5, aspect=True):
    yy, xx = np.mgrid[0:int(height), 0:int(width)].astype(np.float32)
    cx, cy = float(center_x) * width, float(center_y) * height
    dx = (xx - cx) / max(1.0, width * 0.5)
    dy = (yy - cy) / max(1.0, height * 0.5 if aspect else width * 0.5)
    return dx, dy
