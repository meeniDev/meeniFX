"""Glitch and distortion effects: RGB split, glitch, waves, bulge, twirl, kaleidoscope.

All of them run on the BGRA layer and only resample or shift colour, so the alpha
channel keeps the picture's real coverage. Every pixel radius/length is a logical
project pixel multiplied by ``param_scale``, and every random choice is drawn from a
generator seeded with the effect's seed *and the frame number*, so the preview and the
export render the same pattern.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number


def _seed(*parts):
    """A numpy generator seeded from a seed, the frame and any extra integers."""
    seed = 0
    for part in parts:
        seed = (seed * 1000003 + int(part)) % (2 ** 32)
    return np.random.default_rng(seed)


def _remap(layer, map_x, map_y):
    return cv2.remap(layer, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def _shift(channel, dx, dy):
    """Shift one 2-D channel by (dx, dy) pixels, replicating the border."""
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return channel
    matrix = np.array([[1.0, 0.0, float(dx)], [0.0, 1.0, float(dy)]], dtype=np.float32)
    return cv2.warpAffine(channel, matrix, (channel.shape[1], channel.shape[0]),
                          flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


class ChromaticAberrationEffect(LayerEffect):
    """Split the red and blue channels apart, linearly or radially from a centre."""

    key = "chromatic_aberration"
    name = "RGB Split"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 60.0, 5.0, step=0.5, unit=" px", pixels=True,
                  help="How far the colour channels separate"),
        ParamSpec("falloff", "Edge falloff", "float", 0.0, 100.0, 40.0, step=1.0, unit="%",
                  help="Increase the split toward the frame edges"),
        ParamSpec("mode", "Direction", "choice", choices=("linear", "radial"), default="linear",
                  choice_labels=("Linear", "Radial"),
                  help="Linear separates along the angle, radial scales from the centre"),
        ParamSpec("angle", "Angle", "float", -90.0, 90.0, 0.0, step=1.0, unit="\u00b0",
                  advanced=True, help="Direction of a linear split"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(5.0)
        self.falloff = Value(40.0)
        self.mode = Value("linear")
        self.angle = Value(0.0)
        self.center_x = Value(0.5)
        self.center_y = Value(0.5)

    def apply_layer(self, layer):
        amount = param_number(self.amount, self.local_frame) * max(0.02, float(self.param_scale))
        if amount <= 0.2:
            return layer
        height, width = layer.shape[:2]
        cx = param_number(self.center_x, self.local_frame) * (width - 1)
        cy = param_number(self.center_y, self.local_frame) * (height - 1)
        falloff = max(0.0, param_number(self.falloff, self.local_frame)) / 100.0

        if str(self.mode.get()) == "radial":
            scale = max(0.001, amount / max(1.0, float(max(width, height))))
            red_matrix = np.array([[1.0 + scale, 0.0, cx * -scale],
                                   [0.0, 1.0 + scale, cy * -scale]], dtype=np.float32)
            blue_matrix = np.array([[1.0 - scale, 0.0, cx * scale],
                                    [0.0, 1.0 - scale, cy * scale]], dtype=np.float32)
            red = cv2.warpAffine(layer[:, :, 2], red_matrix, (width, height),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            blue = cv2.warpAffine(layer[:, :, 0], blue_matrix, (width, height),
                                  flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        else:
            angle = math.radians(param_number(self.angle, self.local_frame))
            dx, dy = math.cos(angle) * amount, math.sin(angle) * amount
            if falloff > 0.0:
                # A wider split at the edges, with the centre as its own anchor.
                yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
                distance = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
                weight = 1.0 + falloff * (distance / max(1.0, math.hypot(width, height) * 0.5))
                red = _remap(layer[:, :, 2], xx - dx * weight, yy - dy * weight)
                blue = _remap(layer[:, :, 0], xx + dx * weight, yy + dy * weight)
            else:
                red = _shift(layer[:, :, 2], -dx, -dy)
                blue = _shift(layer[:, :, 0], dx, dy)

        out = layer.copy()
        out[:, :, 2] = red
        out[:, :, 0] = blue
        return out


class GlitchEffect(LayerEffect):
    """Datamosh-style bands, displaced blocks and channel tears, seeded per frame."""

    key = "glitch"
    name = "Glitch"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("slices", "Slice shift", "float", 0.0, 100.0, 35.0, step=1.0, unit="%",
                  help="Horizontal bands torn sideways"),
        ParamSpec("block", "Block noise", "float", 0.0, 100.0, 25.0, step=1.0, unit="%",
                  help="Blocky corruption strength"),
        ParamSpec("color", "Colour shift", "float", 0.0, 100.0, 20.0, step=1.0, unit="%",
                  help="Channel tears inside the bands"),
        ParamSpec("seed", "Seed", "int", 0, 9999, 7, advanced=True,
                  help="Change it for a different pattern"),
        ParamSpec("size", "Block size", "int", 8, 160, 40, unit=" px", pixels=True, advanced=True),
        ParamSpec("blocks", "Blocks", "int", 1, 40, 8, advanced=True),
        ParamSpec("speed", "Hold", "int", 1, 30, 2, unit=" f", advanced=True,
                  help="Frames between pattern changes: a slower, chunkier glitch"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.slices = Value(35.0)
        self.block = Value(25.0)
        self.color = Value(20.0)
        self.seed = Value(7)
        self.size = Value(40)
        self.blocks = Value(8)
        self.speed = Value(2)

    def apply_layer(self, layer):
        height, width = layer.shape[:2]
        frame = int(self.local_frame)
        speed = max(1, int(self.speed.get()))
        rng = _seed(self.seed.get() * 7919, speed * 104729, frame // speed)
        output = layer
        slices = param_number(self.slices, frame) / 100.0
        blocks = param_number(self.block, frame) / 100.0
        tears = param_number(self.color, frame) / 100.0

        if slices > 0.001:
            output = output.copy()
            count = 6
            for _ in range(count):
                band_h = int(rng.integers(max(2, height // 18), max(3, height // 5)))
                y0 = int(rng.integers(0, max(1, height - band_h)))
                shift = int(rng.integers(-1, 2) * slices * width * 0.12)
                if shift:
                    band = output[y0:y0 + band_h].copy()
                    output[y0:y0 + band_h] = np.roll(band, shift, axis=1)
            if tears > 0.001:
                output = output.copy()
                for _ in range(4):
                    y0 = int(rng.integers(0, max(1, height - 8)))
                    band_h = int(rng.integers(4, max(5, height // 8)))
                    offset = int(tears * 30 * rng.integers(-1, 2))
                    if offset:
                        output[y0:y0 + band_h, :, 2] = np.roll(
                            output[y0:y0 + band_h, :, 2], offset, axis=1)
                        output[y0:y0 + band_h, :, 0] = np.roll(
                            output[y0:y0 + band_h, :, 0], -offset, axis=1)

        if blocks > 0.001:
            output = output.copy()
            size = max(4, self.px(self.size.get()))
            count = max(1, int(self.blocks.get()))
            for _ in range(count):
                block_w = int(rng.integers(size // 2, size + 1))
                block_h = int(rng.integers(max(4, size // 4), max(5, size // 2)))
                x0 = int(rng.integers(0, max(1, width - block_w)))
                y0 = int(rng.integers(0, max(1, height - block_h)))
                source_x = int(np.clip(x0 + rng.integers(-size, size + 1), 0, max(0, width - block_w)))
                patch = layer[y0:y0 + block_h, source_x:source_x + block_w].copy()
                if blocks < 0.999 and patch.size:
                    original = output[y0:y0 + block_h, x0:x0 + block_w]
                    output[y0:y0 + block_h, x0:x0 + block_w] = (
                        original.astype(np.float32) * (1.0 - blocks)
                        + patch[:, :, :4].astype(np.float32) * blocks).astype(np.uint8)
                else:
                    output[y0:y0 + block_h, x0:x0 + block_w] = patch
        return output


class WaveWarpEffect(LayerEffect):
    """Sine ripple across or down the picture."""

    key = "wave_warp"
    name = "Wave Warp"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("amplitude", "Amplitude", "float", 0.0, 120.0, 12.0, step=0.5, unit=" px",
                  pixels=True, help="How far the picture ripples"),
        ParamSpec("wavelength", "Wavelength", "float", 8.0, 600.0, 80.0, step=1.0, unit=" px",
                  pixels=True, help="Distance between crests"),
        ParamSpec("direction", "Direction", "choice", choices=("horizontal", "vertical", "both"),
                  default="horizontal", choice_labels=("Horizontal", "Vertical", "Both")),
        ParamSpec("speed", "Speed", "float", 0.0, 20.0, 2.0, step=0.1, unit="\u00b0/f",
                  advanced=True, help="How fast the wave travels"),
        ParamSpec("phase", "Phase", "float", 0.0, 180.0, 0.0, step=1.0, unit="\u00b0",
                  advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amplitude = Value(12.0)
        self.wavelength = Value(80.0)
        self.direction = Value("horizontal")
        self.speed = Value(2.0)
        self.phase = Value(0.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        amplitude = param_number(self.amplitude, frame) * max(0.02, float(self.param_scale))
        wavelength = max(2.0, param_number(self.wavelength, frame) * max(0.02, float(self.param_scale)))
        if amplitude <= 0.2:
            return layer
        height, width = layer.shape[:2]
        phase = math.radians(param_number(self.phase, frame)) + \
            math.radians(param_number(self.speed, frame)) * frame
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        direction = str(self.direction.get())
        column = 2.0 * math.pi * xx / wavelength + phase
        row = 2.0 * math.pi * yy / wavelength + phase
        offset_x = np.zeros_like(xx)
        offset_y = np.zeros_like(yy)
        if direction in ("horizontal", "both"):
            offset_y += amplitude * np.sin(column)
        if direction in ("vertical", "both"):
            offset_x += amplitude * np.sin(row)
        return _remap(layer, xx + offset_x, yy + offset_y)


class BulgePinchEffect(LayerEffect):
    """Bulge toward the viewer or pinch away, with a radius in project pixels."""

    key = "bulge_pinch"
    name = "Bulge / Pinch"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", -100.0, 100.0, 55.0, step=1.0, unit="%",
                  help="Positive bulges out, negative pinches in"),
        ParamSpec("radius", "Radius", "float", 10.0, 900.0, 220.0, step=1.0, unit=" px",
                  pixels=True, help="Size of the lens"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
        ParamSpec("smooth", "Edge softness", "float", 0.0, 100.0, 60.0, step=1.0, unit="%",
                  advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(55.0)
        self.radius = Value(220.0)
        self.center_x = Value(0.5)
        self.center_y = Value(0.5)
        self.smooth = Value(60.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        strength = param_number(self.amount, frame) / 100.0
        radius = max(2.0, param_number(self.radius, frame) * max(0.02, float(self.param_scale)))
        if abs(strength) < 1e-4:
            return layer
        height, width = layer.shape[:2]
        cx = param_number(self.center_x, frame) * (width - 1)
        cy = param_number(self.center_y, frame) * (height - 1)
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        dx, dy = xx - cx, yy - cy
        distance = np.sqrt(dx * dx + dy * dy)
        inside = np.clip(1.0 - distance / radius, 0.0, 1.0)
        softness = max(0.05, param_number(self.smooth, frame) / 100.0)
        falloff = np.power(inside, 1.0 / softness)
        # A bulge samples closer to the centre; a pinch samples further out.
        factor = 1.0 - strength * falloff * (1.0 - inside)
        return _remap(layer, cx + dx * factor, cy + dy * factor)


class TwirlEffect(LayerEffect):
    """Rotate pixels around a centre with a smooth falloff."""

    key = "twirl"
    name = "Twirl"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("angle", "Angle", "float", -720.0, 720.0, 140.0, step=1.0, unit="\u00b0",
                  help="Total rotation at the centre, unwinding to zero at the radius"),
        ParamSpec("radius", "Radius", "float", 10.0, 900.0, 260.0, step=1.0, unit=" px",
                  pixels=True),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.angle = Value(140.0)
        self.radius = Value(260.0)
        self.center_x = Value(0.5)
        self.center_y = Value(0.5)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        angle = math.radians(param_number(self.angle, frame))
        if abs(angle) < 1e-4:
            return layer
        radius = max(2.0, param_number(self.radius, frame) * max(0.02, float(self.param_scale)))
        height, width = layer.shape[:2]
        cx = param_number(self.center_x, frame) * (width - 1)
        cy = param_number(self.center_y, frame) * (height - 1)
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        dx, dy = xx - cx, yy - cy
        distance = np.sqrt(dx * dx + dy * dy)
        weight = np.clip(1.0 - distance / radius, 0.0, 1.0)
        rotate = angle * weight * weight
        cos_t, sin_t = np.cos(rotate), np.sin(rotate)
        return _remap(layer, cx + dx * cos_t - dy * sin_t, cy + dx * sin_t + dy * cos_t)


class KaleidoscopeEffect(LayerEffect):
    """Fold the picture into mirrored wedges (or a plain kaleidoscope)."""

    key = "kaleidoscope"
    name = "Kaleidoscope"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("segments", "Segments", "int", 2, 16, 6, help="Number of wedges"),
        ParamSpec("mode", "Mode", "choice", choices=("kaleidoscope", "mirror"), default="mirror",
                  choice_labels=("Kaleidoscope", "Mirror"),
                  help="Kaleidoscope repeats one wedge; mirror folds the picture in half"),
        ParamSpec("rotation", "Rotation", "float", 0.0, 30.0, 0.0, step=0.5, unit="\u00b0"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
        ParamSpec("zoom", "Zoom", "float", 50.0, 200.0, 100.0, step=0.5, unit="%",
                  advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.segments = Value(6)
        self.mode = Value("mirror")
        self.rotation = Value(0.0)
        self.center_x = Value(0.5)
        self.center_y = Value(0.5)
        self.zoom = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        segments = max(2, int(round(self.segments.get())))
        height, width = layer.shape[:2]
        cx = param_number(self.center_x, frame) * (width - 1)
        cy = param_number(self.center_y, frame) * (height - 1)
        zoom = max(0.05, param_number(self.zoom, frame) / 100.0)
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        dx, dy = xx - cx, yy - cy
        distance = np.maximum(1e-3, np.sqrt(dx * dx + dy * dy)) / zoom
        theta = np.arctan2(dy, dx) + math.radians(param_number(self.rotation, frame))
        wedge = 2.0 * math.pi / segments
        folded = np.mod(theta, wedge)
        if str(self.mode.get()) == "mirror" and segments % 2 == 0:
            folded = np.abs(folded - wedge / 2.0)
        return _remap(layer, cx + distance * np.cos(folded), cy + distance * np.sin(folded))


GLITCH_EFFECTS = (ChromaticAberrationEffect, GlitchEffect, WaveWarpEffect, BulgePinchEffect,
                  TwirlEffect, KaleidoscopeEffect)
