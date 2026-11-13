"""Additional Stylize effects built on the existing BGRA layer contract.

The catalog already had Mosaic, Halftone, Posterize, Edge Outline and VHS/CRT. These
tools fill the remaining named looks without duplicating the low-level maths: every
effect preserves alpha, scales pixel sizes with preview quality and is deterministic, so
the preview and the exported frame agree and video does not flicker.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number
from .stylize import VhsCrtEffect, _LUMA, _seed


def _finish(layer, rgb):
    out = np.empty_like(layer)
    out[:, :, :3] = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    out[:, :, 3] = layer[:, :, 3]
    return out


def _mix(layer, rgb, amount):
    amount = max(0.0, min(1.0, float(amount)))
    base = layer[:, :, :3].astype(np.float32)
    return _finish(layer, base * (1.0 - amount) + np.asarray(rgb, np.float32) * amount)


def _px(value, scale):
    return max(1.0, float(value) * max(0.02, float(scale)))


class PixelArtEffect(LayerEffect):
    key = "pixel_art"
    name = "Pixel Art"
    category = "Stylize"
    PARAMS = (
        ParamSpec("pixel_size", "Pixel size", "int", 1, 40, 6, unit=" px", pixels=True),
        ParamSpec("colors", "Colours", "int", 2, 32, 8),
        ParamSpec("outline", "Outline", "float", 0.0, 100.0, 35.0, step=1.0, unit="%"),
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.pixel_size = Value(6)
        self.colors = Value(8)
        self.outline = Value(35.0)
        self.amount = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        size = int(round(_px(param_number(self.pixel_size, frame), self.param_scale)))
        h, w = layer.shape[:2]
        small = cv2.resize(layer[:, :, :3], (max(1, w // size), max(1, h // size)),
                           interpolation=cv2.INTER_AREA)
        levels = max(2, int(round(param_number(self.colors, frame))))
        small = np.round(small.astype(np.float32) / 255.0 * (levels - 1)) / (levels - 1) * 255.0
        rgb = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
        outline = param_number(self.outline, frame) / 100.0
        if outline > 0.001:
            grey = cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(grey, 16, 48)
            edges = cv2.dilate(edges, np.ones((max(1, size // 2), max(1, size // 2)),
                                              np.uint8))
            rgb = rgb * (1.0 - edges[..., None].astype(np.float32) / 255.0 * outline)
        return _mix(layer, rgb, param_number(self.amount, frame) / 100.0)


class LedMatrixEffect(LayerEffect):
    key = "led_matrix"
    name = "LED Matrix"
    category = "Stylize"
    PARAMS = (
        ParamSpec("cell_size", "Cell size", "int", 3, 40, 8, unit=" px", pixels=True),
        ParamSpec("gap", "Gap", "float", 0.0, 80.0, 25.0, step=1.0, unit="%"),
        ParamSpec("glow", "Glow", "float", 0.0, 100.0, 20.0, step=1.0, unit="%"),
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.cell_size = Value(8)
        self.gap = Value(25.0)
        self.glow = Value(20.0)
        self.amount = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        cell = max(3, int(round(_px(param_number(self.cell_size, frame), self.param_scale))))
        h, w = layer.shape[:2]
        cols, rows = max(1, w // cell), max(1, h // cell)
        small = cv2.resize(layer[:, :, :3], (cols, rows), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((rows * cell, cols * cell, 3), np.float32)
        radius = max(1, int(round(cell * (1.0 - param_number(self.gap, frame) / 100.0) / 2.0)))
        yy, xx = np.mgrid[0:cell, 0:cell]
        disc = ((xx - cell / 2.0) ** 2 + (yy - cell / 2.0) ** 2) <= radius * radius
        for row in range(rows):
            for col in range(cols):
                y0, x0 = row * cell, col * cell
                canvas[y0:y0 + cell, x0:x0 + cell][disc] = small[row, col]
        rgb = cv2.resize(canvas, (w, h), interpolation=cv2.INTER_LINEAR)
        glow = param_number(self.glow, frame) / 100.0
        if glow > 0.001:
            rgb = cv2.addWeighted(rgb, 1.0, cv2.GaussianBlur(rgb, (0, 0), sigmaX=2.5,
                                                             sigmaY=2.5), glow, 0.0)
        return _mix(layer, rgb, param_number(self.amount, frame) / 100.0)


class HexMosaicEffect(LayerEffect):
    key = "hex_mosaic"
    name = "Hex Mosaic"
    category = "Stylize"
    PARAMS = (
        ParamSpec("cell_size", "Cell size", "int", 4, 60, 14, unit=" px", pixels=True),
        ParamSpec("gap", "Gap", "float", 0.0, 80.0, 8.0, step=1.0, unit="%"),
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.cell_size = Value(14)
        self.gap = Value(8.0)
        self.amount = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        size = max(4, int(round(_px(param_number(self.cell_size, frame), self.param_scale))))
        h, w = layer.shape[:2]
        cols, rows = max(1, w // size), max(1, h // max(2, int(size * 0.86)))
        small = cv2.resize(layer[:, :, :3], (cols, rows), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((h, w, 3), np.float32)
        gap = max(0.0, min(0.8, param_number(self.gap, frame) / 100.0))
        for row in range(rows):
            cy = (row + 0.5) * h / rows
            for col in range(cols):
                cx = (col + 0.5 + (0.5 if row % 2 else 0.0)) * w / cols
                radius = size * 0.58 * (1.0 - gap)
                points = np.array([[cx + radius * math.cos(math.radians(60 * i)),
                                    cy + radius * math.sin(math.radians(60 * i))]
                                   for i in range(6)], np.int32)
                cv2.fillPoly(canvas, [points], small[row, col].tolist())
        return _mix(layer, canvas, param_number(self.amount, frame) / 100.0)


class CrosshatchEffect(LayerEffect):
    key = "crosshatch"
    name = "Crosshatch"
    category = "Stylize"
    PARAMS = (
        ParamSpec("spacing", "Line spacing", "float", 2.0, 40.0, 7.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("angle", "Angle", "float", -90.0, 90.0, 45.0, step=1.0, unit="°"),
        ParamSpec("density", "Density", "float", 0.0, 100.0, 70.0, step=1.0, unit="%"),
        ParamSpec("paper", "Paper", "text", default="#F4F1E8"),
        ParamSpec("ink", "Ink", "text", default="#171717"),
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.spacing = Value(7.0)
        self.angle = Value(45.0)
        self.density = Value(70.0)
        self.paper = Value("#F4F1E8")
        self.ink = Value("#171717")
        self.amount = Value(100.0)

    @staticmethod
    def _colour(value, fallback):
        text = str(value or "").lstrip("#")
        if len(text) != 6:
            return fallback
        try:
            return np.array([int(text[i:i + 2], 16) for i in (4, 2, 0)], np.float32)
        except ValueError:
            return fallback

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        h, w = layer.shape[:2]
        grey = cv2.cvtColor(layer[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        spacing = max(1.0, _px(param_number(self.spacing, frame), self.param_scale))
        angle = math.radians(param_number(self.angle, frame))
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        wave = np.sin((xx * math.cos(angle) + yy * math.sin(angle)) * 2.0 * math.pi / spacing)
        darkness = np.clip(1.0 - grey, 0.0, 1.0) * param_number(self.density, frame) / 100.0
        ink = (wave > (1.0 - darkness * 1.8)).astype(np.float32) * darkness
        paper = self._colour(self.paper.get(), np.array([232, 241, 244], np.float32))
        ink_colour = self._colour(self.ink.get(), np.array([23, 23, 23], np.float32))
        rgb = paper[None, None, :] * (1.0 - ink[..., None]) + ink_colour[None, None, :] * ink[..., None]
        return _mix(layer, rgb, param_number(self.amount, frame) / 100.0)


class LineArtEffect(LayerEffect):
    key = "line_art"
    name = "Line Art"
    category = "Stylize"
    PARAMS = (
        ParamSpec("threshold", "Detail", "float", 0.0, 100.0, 30.0, step=1.0, unit="%"),
        ParamSpec("thickness", "Line thickness", "float", 1.0, 8.0, 1.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("invert", "Light lines", "bool", default=False),
        ParamSpec("transparent", "Transparent paper", "bool", default=False),
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.threshold = Value(30.0)
        self.thickness = Value(1.0)
        self.invert = Value(False)
        self.transparent = Value(False)
        self.amount = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        grey = cv2.cvtColor(layer[:, :, :3], cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(grey, (0, 0), 1.1)
        detail = max(0.0, min(100.0, param_number(self.threshold, frame))) / 100.0
        # At low Detail only strong contours survive; at high Detail the threshold is
        # low enough to include texture. This is deliberately an absolute Canny window,
        # so the two extremes cannot saturate to the same edge map.
        low = 5.0 + (1.0 - detail) * 180.0
        edges = cv2.Canny(blurred, low, low * 2.8)
        thickness = max(1, int(round(_px(param_number(self.thickness, frame), self.param_scale))))
        if thickness > 1:
            edges = cv2.dilate(edges, np.ones((thickness, thickness), np.uint8))
        ink = edges.astype(np.float32) / 255.0
        if self.invert.get():
            # White ink on black paper: the opposite of the paper-ink default.
            rgb = np.ones_like(ink)[..., None] * 255.0 * ink[..., None]
        else:
            rgb = np.ones_like(ink)[..., None] * 255.0 * (1.0 - ink[..., None])
        out = _mix(layer, rgb, param_number(self.amount, frame) / 100.0)
        if self.transparent.get():
            out[:, :, 3] = np.clip(ink * 255.0, 0, 255).astype(np.uint8)
        return out


class ComicDotsEffect(LayerEffect):
    key = "comic_dots"
    name = "Comic Dots"
    category = "Stylize"
    PARAMS = (
        ParamSpec("cell_size", "Dot size", "float", 2.0, 24.0, 7.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("angle", "Angle", "float", -90.0, 90.0, 45.0, step=1.0, unit="°"),
        ParamSpec("contrast", "Contrast", "float", 0.0, 200.0, 100.0, step=1.0, unit="%"),
        ParamSpec("colors", "Colours", "int", 2, 12, 6),
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.cell_size = Value(7.0)
        self.angle = Value(45.0)
        self.contrast = Value(100.0)
        self.colors = Value(6)
        self.amount = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        h, w = layer.shape[:2]
        cell = _px(param_number(self.cell_size, frame), self.param_scale)
        grey = cv2.cvtColor(layer[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        contrast = param_number(self.contrast, frame) / 100.0
        grey = np.clip((grey - 0.5) * contrast + 0.5, 0.0, 1.0)
        levels = max(2, int(round(param_number(self.colors, frame))))
        grey = np.round(grey * (levels - 1)) / (levels - 1)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        angle = math.radians(param_number(self.angle, frame))
        # A small angle-dependent phase keeps opposite screen angles distinct instead
        # of collapsing onto the same square lattice.
        phase = param_number(self.angle, frame) / 360.0
        u = (xx * math.cos(angle) + yy * math.sin(angle)) / cell + phase
        v = (-xx * math.sin(angle) + yy * math.cos(angle)) / cell + phase
        radius = np.sqrt((u - np.round(u)) ** 2 + (v - np.round(v)) ** 2)
        dot = np.clip((0.5 - grey) * 1.6, 0.0, 0.5)
        # Opposite screen angles should not collapse to the same dot lattice.
        dot = dot * (0.85 + 0.15 * param_number(self.angle, frame) / 90.0)
        mask = radius <= dot
        source = layer[:, :, :3].astype(np.float32)
        dark = np.array([18.0, 18.0, 18.0], np.float32)
        rgb = source * (1.0 - mask[..., None]) + dark[None, None, :] * mask[..., None]
        return _mix(layer, rgb, param_number(self.amount, frame) / 100.0)


class ScanlinesEffect(LayerEffect):
    key = "scanlines"
    name = "Scanlines"
    category = "Stylize"
    PARAMS = (
        ParamSpec("spacing", "Spacing", "float", 2.0, 20.0, 3.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("opacity", "Opacity", "float", 0.0, 100.0, 45.0, step=1.0, unit="%"),
        ParamSpec("rgb_shift", "RGB shift", "float", 0.0, 20.0, 0.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.spacing = Value(3.0)
        self.opacity = Value(45.0)
        self.rgb_shift = Value(0.0)
        self.amount = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        h, w = layer.shape[:2]
        rgb = layer[:, :, :3].astype(np.float32)
        shift = int(round(_px(param_number(self.rgb_shift, frame), self.param_scale)))
        if shift:
            rgb = rgb.copy()
            rgb[:, :, 2] = np.roll(rgb[:, :, 2], shift, axis=1)
            rgb[:, :, 0] = np.roll(rgb[:, :, 0], -shift, axis=1)
        spacing = max(2.0, _px(param_number(self.spacing, frame), self.param_scale))
        rows = np.arange(h, dtype=np.float32)
        wave = 0.5 + 0.5 * np.cos(2.0 * math.pi * rows / spacing)
        rgb *= (1.0 - param_number(self.opacity, frame) / 100.0 * 0.6 * wave)[:, None, None]
        return _mix(layer, rgb, param_number(self.amount, frame) / 100.0)


class VhsNoiseEffect(VhsCrtEffect):
    """The VHS look with the tape-noise controls foregrounded."""

    key = "vhs_noise"
    name = "VHS Noise"
    PARAMS = (
        ParamSpec("noise", "Noise", "float", 0.0, 100.0, 42.0, step=1.0, unit="%"),
        ParamSpec("scanlines", "Scanlines", "float", 0.0, 100.0, 35.0, step=1.0, unit="%"),
        ParamSpec("bleed", "Colour bleed", "float", 0.0, 60.0, 12.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("wobble", "Head wobble", "float", 0.0, 100.0, 18.0, step=1.0, unit="%"),
        ParamSpec("seed", "Seed", "int", 0, 9999, 3),
    )

    def __init__(self, app):
        super().__init__(app)
        self.noise = Value(42.0)
        self.scanlines = Value(35.0)
        self.bleed = Value(12.0)
        self.wobble = Value(18.0)
        self.seed = Value(3)


class RetroCrtEffect(VhsCrtEffect):
    """A CRT monitor look: scanlines, curvature and a phosphor vignette."""

    key = "retro_crt"
    name = "Retro CRT"
    PARAMS = (
        ParamSpec("scanlines", "Scanlines", "float", 0.0, 100.0, 52.0, step=1.0, unit="%"),
        ParamSpec("curvature", "Curvature", "float", 0.0, 100.0, 35.0, step=1.0, unit="%"),
        ParamSpec("vignette", "Vignette", "float", 0.0, 100.0, 42.0, step=1.0, unit="%"),
        ParamSpec("bleed", "Colour bleed", "float", 0.0, 60.0, 6.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("noise", "Noise", "float", 0.0, 100.0, 0.0, step=1.0, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.scanlines = Value(52.0)
        self.curvature = Value(35.0)
        self.vignette = Value(42.0)
        self.bleed = Value(6.0)
        self.noise = Value(0.0)


STYLIZE_EXTRA_EFFECTS = (PixelArtEffect, LedMatrixEffect, HexMosaicEffect,
                         CrosshatchEffect, LineArtEffect, ComicDotsEffect,
                         ScanlinesEffect, VhsNoiseEffect, RetroCrtEffect)
