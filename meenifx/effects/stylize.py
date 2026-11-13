"""Stylize effects: VHS/CRT, grain, halftone, posterize, edge outline, duotone, invert, vignette.

Each effect works on the BGRA layer, keeps the alpha channel and scales its pixel
parameters with the preview quality. Random patterns come from a generator seeded with
the effect's seed *and the frame*, so a preview is the picture the export will write.

Every low-frequency field (scanline vignette, grain weighting, the vignette mask) is
built on a bounded working grid and scaled back: at 1080p that turns hundreds of
milliseconds into a few, without changing the look, and it stays resolution-independent
because the grid depends only on the render size.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number

_LUMA = np.array([0.0722, 0.7152, 0.2126], dtype=np.float32)      # BGR order


def _seed(*parts):
    seed = 0
    for part in parts:
        seed = (seed * 1000003 + int(part)) % (2 ** 32)
    return np.random.default_rng(seed)


def _luma(rgb):
    return np.clip(rgb.astype(np.float32) @ _LUMA / 255.0, 0.0, 1.0)


def _smoothstep(edge0, edge1, value):
    span = max(1e-6, float(edge1) - float(edge0))
    x = np.clip((value - edge0) / span, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


class VhsCrtEffect(LayerEffect):
    """Tape wobble, chroma bleed, scanlines, noise, barrel curvature and a vignette."""

    key = "vhs_crt"
    name = "VHS / CRT"
    category = "Stylize"

    PARAMS = (
        ParamSpec("scanlines", "Scanlines", "float", 0.0, 100.0, 35.0, step=1.0, unit="%",
                  help="Dark horizontal lines, like a CRT's raster"),
        ParamSpec("noise", "Noise", "float", 0.0, 100.0, 18.0, step=1.0, unit="%",
                  help="Tape grain"),
        ParamSpec("bleed", "Colour bleed", "float", 0.0, 60.0, 8.0, step=0.5, unit=" px",
                  pixels=True, help="Smear the chroma channels sideways"),
        ParamSpec("curvature", "Curvature", "float", 0.0, 100.0, 0.0, step=1.0, unit="%",
                  advanced=True, help="Barrel-distort the picture like a tube"),
        ParamSpec("vignette", "Vignette", "float", 0.0, 100.0, 25.0, step=1.0, unit="%",
                  advanced=True),
        ParamSpec("seed", "Seed", "int", 0, 9999, 3, advanced=True),
        ParamSpec("wobble", "Head wobble", "float", 0.0, 100.0, 0.0, step=1.0, unit="%",
                  advanced=True, help="Shift each line a little, like a worn tape"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.scanlines = Value(35.0)
        self.noise = Value(18.0)
        self.bleed = Value(8.0)
        self.curvature = Value(0.0)
        self.vignette = Value(25.0)
        self.seed = Value(3)
        self.wobble = Value(0.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        height, width = layer.shape[:2]
        data = layer.astype(np.float32)
        bleed = param_number(self.bleed, frame) * max(0.02, float(self.param_scale))
        if bleed >= 0.5:
            sigma = max(0.3, bleed / 2.0)
            data[:, :, 2] = cv2.GaussianBlur(data[:, :, 2], (0, 0), sigmaX=sigma, sigmaY=0.6)
            data[:, :, 0] = cv2.GaussianBlur(data[:, :, 0], (0, 0), sigmaX=sigma, sigmaY=0.6)

        wobble = param_number(self.wobble, frame) / 100.0
        if wobble > 0.001:
            rng = _seed(self.seed.get(), frame, 17)
            shifts = np.round(rng.normal(0.0, 1.0, size=height) * wobble * 3.0
                              * max(0.02, self.param_scale)).astype(np.int32)
            if shifts.any():
                columns = (np.arange(width)[None, :] - shifts[:, None]) % width
                data = np.take_along_axis(data, columns[:, :, None], axis=1)

        scan = param_number(self.scanlines, frame) / 100.0
        if scan > 0.001:
            rows = np.arange(height, dtype=np.float32)
            period = max(2.0, 3.0 * max(0.02, self.param_scale))
            wave = 0.5 + 0.5 * np.cos(2.0 * math.pi * rows / period)
            data *= (1.0 - scan * 0.55 * wave)[:, None, None]

        noise = param_number(self.noise, frame) / 100.0
        if noise > 0.001:
            rng = _seed(self.seed.get(), frame, 23)
            low_w, low_h, _factor = self.work_size(width, height, 480)
            grain = rng.normal(0.0, 1.0, size=(low_h, low_w)).astype(np.float32)
            if (low_w, low_h) != (width, height):
                grain = cv2.resize(grain, (width, height), interpolation=cv2.INTER_LINEAR)
            data[:, :, :3] += (grain * (noise * 60.0))[:, :, None]

        darken = param_number(self.vignette, frame) / 100.0
        if darken > 0.001:
            low_w, low_h, _factor = self.work_size(width, height, 480)
            yy, xx = np.mgrid[0:low_h, 0:low_w].astype(np.float32)
            nx = (xx - low_w / 2.0) / max(1.0, low_w / 2.0)
            ny = (yy - low_h / 2.0) / max(1.0, low_h / 2.0)
            radius = np.sqrt(nx * nx + ny * ny) / math.sqrt(2.0)
            mask = 1.0 - darken * 0.7 * _smoothstep(0.45, 1.0, radius)
            data[:, :, :3] *= self.resize_to(mask, width, height)[:, :, None]

        out = np.clip(data, 0.0, 255.0).astype(np.uint8)
        bend = param_number(self.curvature, frame) / 100.0
        if bend > 0.001:
            out = _curve(out, bend)
        return out


def _curve(layer, amount):
    """Barrel distortion: sample the picture through a curved coordinate grid."""
    height, width = layer.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    nx = (xx - width / 2.0) / max(1.0, width / 2.0)
    ny = (yy - height / 2.0) / max(1.0, height / 2.0)
    radius = nx * nx + ny * ny
    scale = 1.0 + amount * 0.35 * radius
    map_x = nx * scale * (width / 2.0) + width / 2.0
    map_y = ny * scale * (height / 2.0) + height / 2.0
    return cv2.remap(layer, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


class FilmGrainEffect(LayerEffect):
    """Photographic grain, optionally monochrome and weighted toward the shadows."""

    key = "film_grain"
    name = "Film Grain"
    category = "Stylize"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 40.0, step=1.0, unit="%"),
        ParamSpec("size", "Grain size", "int", 1, 10, 2, unit=" px", pixels=True,
                  help="Bigger grain is a faster film stock"),
        ParamSpec("seed", "Seed", "int", 0, 9999, 11),
        ParamSpec("monochrome", "Monochrome", "bool", default=True, advanced=True,
                  help="Off gives colour speckle instead of silver grain"),
        ParamSpec("shadows", "Shadow weight", "float", 0.0, 100.0, 55.0, step=1.0, unit="%",
                  advanced=True, help="Put more grain in the dark areas"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(40.0)
        self.size = Value(2)
        self.seed = Value(11)
        self.monochrome = Value(True)
        self.shadows = Value(55.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        amount = param_number(self.amount, frame) / 100.0
        if amount <= 0.001:
            return layer
        height, width = layer.shape[:2]
        size = max(1, self.px(self.size.get()))
        rng = _seed(self.seed.get(), frame, 31)
        low_w, low_h = max(1, width // size), max(1, height // size)
        channels = 1 if self.monochrome.get() else 3
        grain = rng.normal(0.0, 1.0, size=(low_h, low_w, channels)).astype(np.float32)
        if (low_w, low_h) != (width, height):
            grain = cv2.resize(grain, (width, height), interpolation=cv2.INTER_LINEAR)
            if grain.ndim == 2:
                # OpenCV drops a single-channel axis on resize; keep the last axis so the
                # grain broadcasts across BGR without changing the picture.
                grain = grain[:, :, None]
        shadows = param_number(self.shadows, frame) / 100.0
        weight = None
        if shadows > 0.001:
            # The weight is smooth, so a small luma grid is enough and costs almost nothing.
            small = cv2.resize(layer[:, :, :3], (max(1, width // 4), max(1, height // 4)),
                               interpolation=cv2.INTER_AREA)
            weight = 1.0 - shadows * _luma(small)[:, :, None]
            weight = self.resize_to(weight, width, height)
        out = layer.astype(np.float32)
        if weight is None:
            out[:, :, :3] += grain * (amount * 70.0)
        else:
            out[:, :, :3] += grain * (amount * 70.0) * weight
        return np.clip(out, 0.0, 255.0).astype(np.uint8)


class HalftoneEffect(LayerEffect):
    """Print-style dot or line screens with anti-aliased edges."""

    key = "halftone"
    name = "Halftone"
    category = "Stylize"

    PARAMS = (
        ParamSpec("dot", "Dot size", "float", 2.0, 40.0, 9.0, step=0.5, unit=" px",
                  pixels=True, help="Spacing of the screen"),
        ParamSpec("angle", "Screen angle", "float", 0.0, 45.0, 45.0, step=1.0, unit="\u00b0"),
        ParamSpec("mode", "Pattern", "choice", choices=("dots", "lines"), default="dots",
                  choice_labels=("Dots", "Lines")),
        ParamSpec("contrast", "Contrast", "float", 0.0, 100.0, 30.0, step=1.0, unit="%",
                  advanced=True),
        ParamSpec("size_mix", "Dot size mix", "float", 0.0, 100.0, 100.0, step=1.0, unit="%",
                  advanced=True, help="How much brightness controls the dot radius"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.dot = Value(9.0)
        self.angle = Value(45.0)
        self.mode = Value("dots")
        self.contrast = Value(30.0)
        self.size_mix = Value(100.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        spacing = max(2.0, param_number(self.dot, frame) * max(0.02, float(self.param_scale)))
        height, width = layer.shape[:2]
        angle = math.radians(param_number(self.angle, frame))
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        # Separable screen: cos(a*x + b*y) from a pair of 1-D cos/sin ramps, so the
        # 1080p pass never evaluates a trig function per pixel.
        column_u = 2.0 * math.pi / spacing * cos_a * np.arange(width, dtype=np.float32)
        row_u = 2.0 * math.pi / spacing * sin_a * np.arange(height, dtype=np.float32)
        column_v = -2.0 * math.pi / spacing * sin_a * np.arange(width, dtype=np.float32)
        row_v = 2.0 * math.pi / spacing * cos_a * np.arange(height, dtype=np.float32)

        def screen(columns, rows):
            return (np.cos(columns)[None, :] * np.cos(rows)[:, None]
                    - np.sin(columns)[None, :] * np.sin(rows)[:, None])
        pattern = screen(column_u, row_u)
        if str(self.mode.get()) == "dots":
            pattern = pattern * screen(column_v, row_v)
        pattern = 0.5 + 0.5 * pattern

        luma = _luma(layer[:, :, :3])
        contrast = param_number(self.contrast, frame) / 100.0
        if contrast > 0.001:
            luma = np.clip((luma - 0.5) * (1.0 + contrast * 1.5) + 0.5, 0.0, 1.0)
        mix = param_number(self.size_mix, frame) / 100.0
        ink = _smoothstep(0.5, 0.58, luma - pattern * (1.0 - 0.55 * mix))
        colour = layer[:, :, :3].astype(np.float32)
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(235.0 * (1.0 - ink[:, :, None]) + colour * ink[:, :, None],
                                0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class PosterizeEffect(LayerEffect):
    """Quantise the picture to a fixed number of steps."""

    key = "posterize"
    name = "Posterize"
    category = "Stylize"

    PARAMS = (
        ParamSpec("levels", "Levels", "int", 2, 32, 6, help="Number of brightness steps"),
        ParamSpec("mode", "Channels", "choice", choices=("rgb", "luma"), default="rgb",
                  choice_labels=("Per channel", "Luminance")),
        ParamSpec("smooth", "Smooth steps", "bool", default=False, advanced=True,
                  help="Soften each step edge instead of a hard cut"),
        ParamSpec("gamma", "Gamma", "float", 0.2, 3.0, 1.0, step=0.05, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.levels = Value(6)
        self.mode = Value("rgb")
        self.smooth = Value(False)
        self.gamma = Value(1.0)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        levels = max(2, int(self.levels.get()))
        gamma = max(0.05, param_number(self.gamma, frame))
        data = layer[:, :, :3].astype(np.float32) / 255.0
        if abs(gamma - 1.0) > 1e-3:
            data = np.power(np.clip(data, 0.0, 1.0), gamma)
        if str(self.mode.get()) == "luma":
            luma = np.clip(data @ _LUMA, 0.0, 1.0)
            stepped = np.round(luma * (levels - 1)) / (levels - 1)
            ratio = np.where(luma > 1e-3, stepped / np.maximum(luma, 1e-3), 1.0)
            data = np.clip(data * ratio[:, :, None], 0.0, 1.0)
        else:
            scaled = data * (levels - 1)
            if self.smooth.get():
                data = (np.floor(scaled) + _smoothstep(0.35, 0.65, scaled - np.floor(scaled))) / (levels - 1)
            else:
                data = np.round(scaled) / (levels - 1)
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(data * 255.0, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class EdgeOutlineEffect(LayerEffect):
    """Cartoon outlines: dark edges over the picture, or a flat colour illustration."""

    key = "edge_outline"
    name = "Edge Outline"
    category = "Stylize"

    PARAMS = (
        ParamSpec("strength", "Strength", "float", 0.0, 100.0, 70.0, step=1.0, unit="%"),
        ParamSpec("thickness", "Thickness", "float", 1.0, 12.0, 2.0, step=0.5, unit=" px",
                  pixels=True),
        ParamSpec("mode", "Look", "choice", choices=("edges", "cartoon"), default="cartoon",
                  choice_labels=("Edges only", "Cartoon")),
        ParamSpec("threshold", "Detail", "float", 0.0, 100.0, 30.0, step=1.0, unit="%",
                  advanced=True, help="Lower catches finer edges"),
        ParamSpec("invert", "Light edges", "bool", default=False, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.strength = Value(70.0)
        self.thickness = Value(2.0)
        self.mode = Value("cartoon")
        self.threshold = Value(30.0)
        self.invert = Value(False)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        strength = param_number(self.strength, frame) / 100.0
        if strength <= 0.001:
            return layer
        grey = cv2.cvtColor(layer[:, :, :3], cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(grey, (0, 0), sigmaX=1.2, sigmaY=1.2)
        # 16-bit gradients and a saturating scale keep this on 8-bit paths (fast), with
        # the same threshold shape a float Sobel gives.
        gx = cv2.Sobel(blurred, cv2.CV_16S, 1, 0, ksize=3)
        gy = cv2.Sobel(blurred, cv2.CV_16S, 0, 1, ksize=3)
        magnitude = cv2.addWeighted(cv2.convertScaleAbs(gx), 0.5,
                                    cv2.convertScaleAbs(gy), 0.5, 0)
        threshold = 8.0 + (100.0 - param_number(self.threshold, frame)) * 1.6
        edges = cv2.convertScaleAbs(magnitude, alpha=2.0, beta=-threshold)
        if self.invert.get():
            edges = cv2.bitwise_not(edges)
        thickness = max(1, int(round(param_number(self.thickness, frame) * max(0.02, self.param_scale))))
        if thickness > 1:
            edges = cv2.dilate(edges, np.ones((thickness, thickness), np.uint8))
        base = layer[:, :, :3].astype(np.float32)
        if str(self.mode.get()) == "cartoon":
            quant = np.round(base / 48.0) * 48.0
            base = base * 0.35 + quant * 0.65
        ink = 0.0 if not self.invert.get() else 255.0
        weight = (edges.astype(np.float32) / 255.0 * strength)[:, :, None]
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(base * (1.0 - weight) + ink * weight, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class DuotoneEffect(LayerEffect):
    """Map the picture's luma between two tinted colours."""

    key = "duotone"
    name = "Duotone"
    category = "Stylize"

    PARAMS = (
        ParamSpec("shadow_hue", "Shadow tint", "float", -180.0, 180.0, -120.0, step=1.0,
                  unit="\u00b0", help="Hue that dark areas take"),
        ParamSpec("highlight_hue", "Highlight tint", "float", -180.0, 180.0, 30.0, step=1.0,
                  unit="\u00b0", help="Hue that bright areas take"),
        ParamSpec("intensity", "Intensity", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
        ParamSpec("balance", "Balance", "float", 0.0, 100.0, 50.0, step=1.0, unit="%",
                  advanced=True),
        ParamSpec("detail", "Keep detail", "float", 0.0, 100.0, 45.0, step=1.0, unit="%",
                  advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.shadow_hue = Value(-120.0)
        self.highlight_hue = Value(30.0)
        self.intensity = Value(100.0)
        self.balance = Value(50.0)
        self.detail = Value(45.0)

    @staticmethod
    def _tint(hue):
        hsv = np.uint8([[[int(round((float(hue) % 360.0) / 2.0)) % 180, 255, 255]]])
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0].astype(np.float32)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        intensity = param_number(self.intensity, frame) / 100.0
        if intensity <= 0.001:
            return layer
        luma = _luma(layer[:, :, :3])
        balance = max(0.02, param_number(self.balance, frame) / 50.0)
        detail = param_number(self.detail, frame) / 100.0
        # Build the 256-step gradient once and gather it: the per-pixel work is one
        # lookup instead of a power curve and three full-frame temporaries.
        steps = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        shaped = np.power(steps, balance)
        shadow = self._tint(param_number(self.shadow_hue, frame)) * 0.55
        highlight = self._tint(param_number(self.highlight_hue, frame))
        ramp = shadow[None, :] * (1.0 - shaped[:, None]) + highlight[None, :] * shaped[:, None]
        if detail > 0.001:
            ramp = ramp * (0.4 + 0.6 * shaped[:, None]) + \
                (steps[:, None] * 255.0 - 128.0) * detail * 0.35
        index = np.clip(luma * 255.0, 0.0, 255.0).astype(np.uint8)
        mapped = np.clip(ramp[index], 0.0, 255.0).astype(np.uint8)
        blended = cv2.addWeighted(np.ascontiguousarray(layer[:, :, :3]), 1.0 - intensity,
                                  mapped, intensity, 0.0)
        out = np.empty_like(layer)
        out[:, :, :3] = blended
        out[:, :, 3] = layer[:, :, 3]
        return out


class InvertEffect(LayerEffect):
    """Negative, in full colour or on luminance only."""

    key = "invert"
    name = "Invert"
    category = "Stylize"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
        ParamSpec("mode", "Mode", "choice", choices=("rgb", "luma"), default="rgb",
                  choice_labels=("Full colour", "Luminance only")),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(100.0)
        self.mode = Value("rgb")

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        amount = param_number(self.amount, frame) / 100.0
        if amount <= 0.001:
            return layer
        data = layer[:, :, :3].astype(np.float32)
        if str(self.mode.get()) == "luma":
            luma = _luma(data)[:, :, None] * 255.0
            inverted = data + (255.0 - 2.0 * luma)
        else:
            inverted = 255.0 - data
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(data * (1.0 - amount) + inverted * amount, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class VignetteEffect(LayerEffect):
    """Darken the corners with a controllable shape."""

    key = "vignette"
    name = "Vignette"
    category = "Stylize"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 55.0, step=1.0, unit="%"),
        ParamSpec("radius", "Radius", "float", 0.0, 100.0, 55.0, step=1.0, unit="%",
                  help="Where the darkening starts"),
        ParamSpec("softness", "Softness", "float", 0.0, 100.0, 55.0, step=1.0, unit="%"),
        ParamSpec("roundness", "Roundness", "float", 0.0, 100.0, 100.0, step=1.0, unit="%",
                  advanced=True, help="0 follows the frame's rectangle, 100 is a circle"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(55.0)
        self.radius = Value(55.0)
        self.softness = Value(55.0)
        self.roundness = Value(100.0)
        self.center_x = Value(0.5)
        self.center_y = Value(0.5)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        amount = param_number(self.amount, frame) / 100.0
        if amount <= 0.001:
            return layer
        height, width = layer.shape[:2]
        low_w, low_h, _factor = self.work_size(width, height, 480)
        yy, xx = np.mgrid[0:low_h, 0:low_w].astype(np.float32)
        cx = param_number(self.center_x, frame) * low_w
        cy = param_number(self.center_y, frame) * low_h
        nx = np.abs(xx - cx) / max(1.0, low_w / 2.0)
        ny = np.abs(yy - cy) / max(1.0, low_h / 2.0)
        roundness = param_number(self.roundness, frame) / 100.0
        distance = (1.0 - roundness) * np.maximum(nx, ny) + roundness * np.sqrt(nx * nx + ny * ny)
        radius = max(0.0, param_number(self.radius, frame) / 100.0)
        softness = max(0.02, 1.0 - (param_number(self.softness, frame) / 100.0) * 0.95)
        mask = _smoothstep(radius, max(radius + 0.02, radius * softness + 0.35), distance)
        mask = self.resize_to(mask, width, height)
        gain = np.clip((1.0 - amount * mask) * 255.0, 0.0, 255.0).astype(np.uint8)
        gain = cv2.merge([gain, gain, gain])
        darkened = cv2.multiply(np.ascontiguousarray(layer[:, :, :3]), gain,
                                scale=1.0 / 255.0, dtype=cv2.CV_8U)
        out = np.empty_like(layer)
        out[:, :, :3] = darkened
        out[:, :, 3] = layer[:, :, 3]
        return out


class DitherEffect(LayerEffect):
    """Deterministic ordered and error-diffusion palette dithering."""

    key = "dither"
    name = "Dither"
    category = "Stylize"
    PARAMS = (
        ParamSpec("algorithm", "Algorithm", "choice", default="floyd_steinberg",
                  choices=("floyd_steinberg", "atkinson", "bayer2", "bayer4", "bayer8",
                           "jarvis_judice_ninke", "stucki", "sierra", "burkes"),
                  choice_labels=("Floyd–Steinberg", "Atkinson", "Bayer 2×2", "Bayer 4×4",
                                 "Bayer 8×8", "Jarvis–Judice–Ninke", "Stucki", "Sierra",
                                 "Burkes")),
        ParamSpec("pixel_size", "Pixel size", "int", 1, 32, 1),
        ParamSpec("palette", "Palette", "choice", default="bw",
                  choices=("bw", "grayscale", "retro", "game_boy", "rgb", "custom")),
        ParamSpec("color_count", "Colour count", "int", 2, 16, 2),
        ParamSpec("palette_text", "Custom palette", "text",
                  default="#101010,#2B2B3A,#F5F5F5",
                  help="Comma-separated colours used by the Custom palette"),
        ParamSpec("custom_dark", "Legacy custom dark", "text", default="#101010",
                  advanced=True),
        ParamSpec("custom_light", "Legacy custom light", "text", default="#F5F5F5",
                  advanced=True),
        ParamSpec("contrast", "Contrast", "float", 0, 200, 100, step=1, unit="%"),
        ParamSpec("brightness", "Brightness", "float", -100, 100, 0, step=1),
        ParamSpec("gamma", "Gamma", "float", 0.1, 4, 1, step=0.05),
        ParamSpec("strength", "Strength", "float", 0, 100, 100, step=1, unit="%"),
        ParamSpec("pattern_scale", "Pattern scale", "float", 0.25, 8, 1, step=0.25),
        ParamSpec("pattern_offset", "Pattern offset", "int", 0, 63, 0),
        ParamSpec("invert", "Invert", "bool", default=False),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    @staticmethod
    def _palette_lut(palette):
        """5-bit RGB nearest-colour table for the error-diffusion inner loop.

        Per-pixel calls to argmin over the palette dominated runtime (seconds per
        640x360 frame). A tiny quantised cube keeps the same nearest-colour decision at
        a fraction of the cost; error diffusion remains explicitly sequential.
        """
        axis = np.arange(32, dtype=np.float32) * 8.0 + 4.0
        grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
        distances = ((grid[:, None, :] - np.asarray(palette, np.float32)[None, :, :]) ** 2).sum(axis=2)
        return distances.argmin(axis=1).astype(np.uint8).reshape(32, 32, 32)

    def _custom_palette(self):
        text = str(self.palette_text.get() or "")
        colours = []
        for token in text.split(","):
            token = token.strip()
            if not token:
                continue
            colour = self._hex(token, None)
            if colour is not None:
                colours.append(colour)
        if len(colours) < 2:
            return None
        return np.stack(colours[:16])

    @staticmethod
    def _hex(value, fallback):
        try:
            raw = value.get() if hasattr(value, "get") else value
            text = str(raw).lstrip("#")
            if len(text) == 6:
                return np.array([int(text[i:i+2], 16) for i in (0, 2, 4)], np.float32)
        except (ValueError, TypeError, AttributeError):
            pass
        return None if fallback is None else np.array(fallback, np.float32)

    def apply_layer(self, layer):
        if layer is None or layer.size == 0:
            return layer
        frame = int(self.local_frame)
        strength = param_number(self.strength, frame) / 100.0
        if strength <= 0:
            return layer
        src = layer[:, :, :3].astype(np.float32)
        alpha = layer[:, :, 3].copy()
        pixel = max(1, int(round(param_number(self.pixel_size, frame) *
                                 max(.02, float(self.param_scale)))))
        if pixel > 1:
            small = cv2.resize(src, (max(1, src.shape[1] // pixel),
                                     max(1, src.shape[0] // pixel)),
                               interpolation=cv2.INTER_AREA)
        else:
            small = src.copy()
        levels = max(2, int(round(param_number(self.color_count, frame))))
        palette_name = str(self.palette.get())
        if palette_name == "bw":
            # The two-colour default stays pure black/white; raising Colour count turns
            # the same control into a true grayscale ramp instead of leaving it dead.
            palette = (np.array([[0,0,0], [255,255,255]], np.float32) if levels <= 2
                       else np.linspace(0, 255, levels, dtype=np.float32)[:, None].repeat(3, axis=1))
        elif palette_name == "game_boy":
            palette = np.array([[15,56,15], [48,98,48], [139,172,15], [155,188,15]], np.float32)
        elif palette_name == "retro":
            palette = np.array([[0,0,0], [36,24,111], [140,42,18], [250,210,90],
                                [245,235,210]], np.float32)
        elif palette_name == "rgb":
            palette = np.array([[0,0,0], [0,0,255], [0,255,0], [0,255,255],
                                [255,0,0], [255,0,255], [255,255,0], [255,255,255]], np.float32)
        elif palette_name == "custom":
            # The editable swatch strip is the authoritative custom palette. Older
            # projects only stored the two legacy colours, so fall back to those when
            # the new field is absent or has no valid entries.
            palette = self._custom_palette()
            if palette is None:
                palette = np.stack([self._hex(self.custom_dark, [16]*3),
                                    self._hex(self.custom_light, [245]*3)])
        else:
            palette = np.linspace(0, 255, max(3, levels), dtype=np.float32)[:, None].repeat(3, axis=1)
        contrast = param_number(self.contrast, frame) / 100.0
        brightness = param_number(self.brightness, frame) * 2.55
        gamma = max(.1, param_number(self.gamma, frame))
        work = np.clip((small - 127.5) * contrast + 127.5 + brightness, 0, 255) / 255.0
        work = np.power(work, 1.0 / gamma) * 255.0
        if bool(self.invert.get()):
            work = 255.0 - work
        algorithm = str(self.algorithm.get())
        scale = max(.25, param_number(self.pattern_scale, frame))
        offset = max(0, int(self.pattern_offset.get()))
        if algorithm.startswith("bayer"):
            size = int(algorithm[-1])
            matrix = np.array([[0, 2], [3, 1]], np.float32)
            while matrix.shape[0] < size:
                matrix = np.block([[4*matrix, 4*matrix+2], [4*matrix+3, 4*matrix+1]])
            yy, xx = np.indices(work.shape[:2])
            threshold = ((matrix[(yy + offset) % size,
                                 (xx + offset) % size] + .5) /
                         (size * size) - .5)[:, :, None] * (255.0 / max(1, len(palette)-1)) * scale
            work += threshold
            out_small = palette[np.abs(work[:, :, None, :] - palette[None, None, :, :]).sum(axis=3).argmin(axis=2)]
        else:
            out_small = np.empty_like(work)
            kernels = {
                "floyd_steinberg": ([(1,0,7),( -1,1,3),(0,1,5),(1,1,1)],16),
                "atkinson": ([(1,0,1),(2,0,1),(-1,1,1),(0,1,1),(1,1,1),(0,2,1)],8),
                "jarvis_judice_ninke": ([(1,0,7),(2,0,5),(-2,1,3),(-1,1,5),(0,1,7),(1,1,5),(2,1,3),(-2,2,1),(-1,2,3),(0,2,5),(1,2,3),(2,2,1)],48),
                "stucki": ([(1,0,8),(2,0,4),(-2,1,2),(-1,1,4),(0,1,8),(1,1,4),(2,1,2),(-2,2,1),(-1,2,2),(0,2,4),(1,2,2),(2,2,1)],42),
                "sierra": ([(1,0,5),(2,0,3),(-2,1,2),(-1,1,4),(0,1,5),(1,1,4),(2,1,2),(-1,2,2),(0,2,3),(1,2,2)],32),
                "burkes": ([(1,0,8),(2,0,4),(-2,1,2),(-1,1,4),(0,1,8),(1,1,4),(2,1,2)],32),
            }
            kernel, divisor = kernels.get(algorithm, kernels["floyd_steinberg"])
            if abs(scale - 1.0) > 1e-4 or offset:
                # Pattern Scale/Offset stay meaningful for error diffusion too: a small
                # ordered modulation breaks flat areas into the requested texture rather
                # than being silently ignored outside the Bayer branch.
                yy, xx = np.indices(work.shape[:2])
                matrix = np.array([[0, 2], [3, 1]], np.float32)
                ordered = ((matrix[(yy + offset) % 2, (xx + offset) % 2] + .5) / 4.0 - .5)
                work += (ordered * (scale - 1.0) * 22.0 + offset * 0.35)[:, :, None]
            h, w = work.shape[:2]
            original_small = (w, h)
            # Diffusion is sequential. Cap its working grid so a full-resolution dither
            # cannot block the UI for seconds; the quantised result is scaled back with
            # nearest-neighbour blocks, so pixel size and palette are preserved.
            if max(w, h) > 256:
                factor = 256.0 / max(w, h)
                work = cv2.resize(work, (max(1, int(round(w * factor))),
                                         max(1, int(round(h * factor)))),
                                  interpolation=cv2.INTER_AREA)
                h, w = work.shape[:2]
            lut = self._palette_lut(palette)
            out_small = np.empty_like(work)
            for y in range(h):
                for x in range(w):
                    old = work[y, x]
                    b, g, r = (min(255, max(0, int(value))) >> 3 for value in old)
                    new = palette[lut[b, g, r]]
                    out_small[y, x] = new
                    error = (old - new) * strength
                    for dx, dy, weight in kernel:
                        xx, yy = x + dx, y + dy
                        if 0 <= xx < w and 0 <= yy < h:
                            work[yy, xx] += error * (weight / divisor)
            if (w, h) != original_small:
                out_small = cv2.resize(out_small, original_small, interpolation=cv2.INTER_NEAREST)
        if pixel > 1:
            quantized = cv2.resize(out_small, (src.shape[1], src.shape[0]), interpolation=cv2.INTER_NEAREST)
        else:
            quantized = out_small
        output = np.empty_like(layer)
        output[:, :, :3] = np.clip(src * (1-strength) + quantized * strength, 0, 255).astype(np.uint8)
        output[:, :, 3] = alpha
        return output


STYLIZE_EFFECTS = (VhsCrtEffect, FilmGrainEffect, HalftoneEffect, DitherEffect, PosterizeEffect,
                   EdgeOutlineEffect, DuotoneEffect, InvertEffect, VignetteEffect)
