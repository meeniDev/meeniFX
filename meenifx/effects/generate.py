"""Procedural generators: Gradient Ramp, Fractal Noise, Checkerboard and Grid.

They replace the layer's picture and return a full BGRA frame, so they work on clips,
shapes, text, adjustment layers and nested compositions.  Transparent mode leaves the
untouched areas genuinely transparent instead of painting a black background.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec
from .creative_utils import colour_bgr, fractal_field, num

GENERATOR_NOTE = ("This effect generates its own picture; the source image underneath is "
                  "only visible where the generated result is transparent.")


class GradientRampEffect(LayerEffect):
    key = "gradient_ramp"
    name = "Gradient Ramp"
    category = "Generate"
    NOTE = GENERATOR_NOTE

    PARAMS = (
        ParamSpec("start_color", "Start colour", "text", default="#FF6A3D"),
        ParamSpec("end_color", "End colour", "text", default="#2B5DFF"),
        ParamSpec("ramp_type", "Type", "choice",
                  choices=("linear", "radial", "angle", "diamond"), default="linear",
                  choice_labels=("Linear", "Radial", "Angle", "Diamond")),
        ParamSpec("angle", "Angle", "float", -180.0, 180.0, 0.0, step=1.0, unit="\u00b0"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("scale", "Scale", "float", 5.0, 300.0, 100.0, step=1.0, unit="%"),
        ParamSpec("offset", "Offset", "float", -100.0, 100.0, 0.0, step=1.0, unit="%"),
        ParamSpec("opacity", "Opacity", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
        ParamSpec("transparent", "Transparent start", "bool", default=False,
                  help="Fade the start of the ramp to transparent"),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_layer(self, layer):
        frame = self.local_frame
        height, width = layer.shape[:2]
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        nx = (xx / max(1.0, width - 1.0)) - num(self.center_x, frame)
        ny = (yy / max(1.0, height - 1.0)) - num(self.center_y, frame)
        angle = math.radians(num(self.angle, frame))
        ramp_type = str(self.ramp_type.get())
        if ramp_type == "radial":
            value = np.sqrt(nx * nx + ny * ny)
        elif ramp_type == "angle":
            value = (np.arctan2(ny, nx) / (2.0 * math.pi)) + 0.5
        elif ramp_type == "diamond":
            value = np.abs(nx) + np.abs(ny)
        else:
            value = nx * math.cos(angle) + ny * math.sin(angle)
        value = value * max(0.01, num(self.scale, frame) / 100.0) + num(self.offset, frame) / 100.0 + 0.5
        value = np.clip(value, 0.0, 1.0)
        low = colour_bgr(self.start_color.get())
        high = colour_bgr(self.end_color.get())
        ramp = low[None, None, :] + (high - low)[None, None, :] * value[:, :, None]
        alpha = np.full((height, width, 1), 255.0 * num(self.opacity, frame) / 100.0, np.float32)
        if bool(self.transparent.get()):
            alpha *= value[:, :, None]
        out = np.empty((height, width, 4), np.uint8)
        out[:, :, :3] = np.clip(ramp, 0, 255).astype(np.uint8)
        out[:, :, 3] = np.clip(alpha[:, :, 0], 0, 255).astype(np.uint8)
        return out


class FractalNoiseEffect(LayerEffect):
    key = "fractal_noise"
    name = "Fractal Noise"
    category = "Generate"
    NOTE = GENERATOR_NOTE

    PARAMS = (
        ParamSpec("scale", "Scale", "float", 4.0, 600.0, 96.0, step=1.0, unit=" px",
                  pixels=True, help="Size of the largest noise features"),
        ParamSpec("complexity", "Complexity", "float", 1.0, 10.0, 5.0, step=1.0,
                  help="Number of noise octaves"),
        ParamSpec("contrast", "Contrast", "float", 0.0, 400.0, 140.0, step=1.0, unit="%"),
        ParamSpec("brightness", "Brightness", "float", -100.0, 100.0, 0.0, step=1.0, unit="%"),
        ParamSpec("evolution", "Evolution", "float", 0.0, 2000.0, 0.0, step=1.0, unit="\u00b0",
                  help="Animate this to make the noise flow"),
        ParamSpec("speed", "Speed", "float", -20.0, 20.0, 0.0, step=0.1, unit="\u00b0/f"),
        ParamSpec("seed", "Seed", "int", 0, 99999, 12),
        ParamSpec("color_a", "Low colour", "text", default="#070A12"),
        ParamSpec("color_b", "High colour", "text", default="#E9F2FF"),
        ParamSpec("transparent", "Transparent noise", "bool", default=False,
                  help="Use the noise field as alpha instead of an opaque background"),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_layer(self, layer):
        frame = self.local_frame
        height, width = layer.shape[:2]
        factor = min(1.0, 720.0 / max(1.0, max(width, height)))
        work = (max(2, int(round(width * factor))), max(2, int(round(height * factor))))
        phase = (num(self.evolution, frame) + num(self.speed, frame) * frame) / 4.0
        field = fractal_field(work[0], work[1], max(2.0, num(self.scale, frame) * factor),
                              int(round(num(self.complexity, frame))), int(self.seed.get()),
                              phase)
        if factor < 1.0:
            field = cv2.resize(field, (width, height), interpolation=cv2.INTER_LINEAR)
        contrast = num(self.contrast, frame) / 100.0
        field = np.clip((field - 0.5) * contrast + 0.5 + num(self.brightness, frame) / 100.0,
                        0.0, 1.0)
        low = colour_bgr(self.color_a.get())
        high = colour_bgr(self.color_b.get())
        colour = low[None, None, :] + (high - low)[None, None, :] * field[:, :, None]
        out = np.empty((height, width, 4), np.uint8)
        out[:, :, :3] = np.clip(colour, 0, 255).astype(np.uint8)
        out[:, :, 3] = (field * 255.0 if bool(self.transparent.get())
                        else np.full((height, width), 255.0)).astype(np.uint8)
        return out


class CheckerboardEffect(LayerEffect):
    key = "checkerboard"
    name = "Checkerboard"
    category = "Generate"
    NOTE = GENERATOR_NOTE

    PARAMS = (
        ParamSpec("size", "Square size", "float", 4.0, 500.0, 48.0, step=1.0, unit=" px",
                  pixels=True),
        ParamSpec("color_a", "Colour A", "text", default="#F4F1E8"),
        ParamSpec("color_b", "Colour B", "text", default="#171A21"),
        ParamSpec("angle", "Angle", "float", -180.0, 180.0, 0.0, step=1.0, unit="\u00b0"),
        ParamSpec("offset_x", "Offset X", "float", -500.0, 500.0, 0.0, step=1.0, unit=" px",
                  pixels=True),
        ParamSpec("offset_y", "Offset Y", "float", -500.0, 500.0, 0.0, step=1.0, unit=" px",
                  pixels=True),
        ParamSpec("transparent", "Transparent squares", "bool", default=False,
                  help="Make the darker squares transparent"),
        ParamSpec("opacity", "Opacity", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_layer(self, layer):
        frame = self.local_frame
        height, width = layer.shape[:2]
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        angle = math.radians(num(self.angle, frame))
        px = xx * math.cos(angle) + yy * math.sin(angle) + num(self.offset_x, frame)
        py = -xx * math.sin(angle) + yy * math.cos(angle) + num(self.offset_y, frame)
        size = max(1.0, num(self.size, frame) * max(0.02, float(self.param_scale)))
        checker = (np.floor(px / size).astype(np.int32) + np.floor(py / size).astype(np.int32)) & 1
        low, high = colour_bgr(self.color_a.get()), colour_bgr(self.color_b.get())
        colour = np.where(checker[:, :, None].astype(bool), high[None, None, :], low[None, None, :])
        alpha = np.full((height, width), 255.0 * num(self.opacity, frame) / 100.0, np.float32)
        if bool(self.transparent.get()):
            alpha *= checker.astype(np.float32)
        out = np.empty((height, width, 4), np.uint8)
        out[:, :, :3] = np.clip(colour, 0, 255).astype(np.uint8)
        out[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
        return out


class GridEffect(LayerEffect):
    key = "grid"
    name = "Grid"
    category = "Generate"
    NOTE = GENERATOR_NOTE

    PARAMS = (
        ParamSpec("size", "Cell size", "float", 4.0, 500.0, 64.0, step=1.0, unit=" px",
                  pixels=True),
        ParamSpec("thickness", "Line thickness", "float", 0.5, 30.0, 2.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("subdivisions", "Subdivisions", "int", 1, 8, 2,
                  help="Extra lines inside every cell"),
        ParamSpec("color", "Line colour", "text", default="#F5F2E9"),
        ParamSpec("background", "Background colour", "text", default="#151A24"),
        ParamSpec("angle", "Angle", "float", -180.0, 180.0, 0.0, step=1.0, unit="\u00b0"),
        ParamSpec("offset_x", "Offset X", "float", -500.0, 500.0, 0.0, step=1.0, unit=" px",
                  pixels=True),
        ParamSpec("offset_y", "Offset Y", "float", -500.0, 500.0, 0.0, step=1.0, unit=" px",
                  pixels=True),
        ParamSpec("transparent", "Transparent background", "bool", default=False),
        ParamSpec("opacity", "Opacity", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_layer(self, layer):
        frame = self.local_frame
        height, width = layer.shape[:2]
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        angle = math.radians(num(self.angle, frame))
        px = xx * math.cos(angle) + yy * math.sin(angle) + num(self.offset_x, frame)
        py = -xx * math.sin(angle) + yy * math.cos(angle) + num(self.offset_y, frame)
        size = max(2.0, num(self.size, frame) * max(0.02, float(self.param_scale)))
        thickness = max(0.5, num(self.thickness, frame) * max(0.02, float(self.param_scale)))
        subdivisions = max(1, int(num(self.subdivisions, frame)))
        step = size / subdivisions
        distance_x = np.abs((px % step) - step * 0.5)
        distance_y = np.abs((py % step) - step * 0.5)
        line = ((distance_x < thickness * 0.5) | (distance_y < thickness * 0.5)).astype(np.float32)
        colour = colour_bgr(self.color.get())
        background = colour_bgr(self.background.get())
        mixed = background[None, None, :] * (1.0 - line[:, :, None]) + colour[None, None, :] * line[:, :, None]
        alpha = np.full((height, width), 255.0 * num(self.opacity, frame) / 100.0, np.float32)
        if bool(self.transparent.get()):
            alpha *= line
        out = np.empty((height, width, 4), np.uint8)
        out[:, :, :3] = np.clip(mixed, 0, 255).astype(np.uint8)
        out[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
        return out


GENERATE_EFFECTS = (GradientRampEffect, FractalNoiseEffect, CheckerboardEffect, GridEffect)
