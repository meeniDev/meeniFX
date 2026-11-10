"""Professional colour tools: Curves, Hue/Saturation, Tint, Tritone, Levels and
Color Wheels.

All effects keep the layer's alpha untouched and use LUTs for tone operations, so a
full-frame grade is a small number of 256-entry gathers rather than a per-pixel Python
loop.  Curves are evaluated with monotone cubic interpolation: control points never make
the curve fold back on itself, and every channel is editable independently in the graph
editor.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec
from .creative_utils import LUMA_BGR, colour_bgr, luma, num

CURVE_CHANNELS = ("master", "red", "green", "blue")
CURVE_LABELS = ("Master", "Red", "Green", "Blue")
IDENTITY_CURVE = "0,0;255,255"


def parse_curve(text):
    """[(x, y)] from ``x,y; x,y`` with safe handling of half-typed text."""
    points = []
    for part in str(text or "").replace("\n", ";").split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        values = chunk.replace(" ", ",").split(",")
        if len(values) < 2:
            continue
        try:
            x = max(0.0, min(255.0, float(values[0])))
            y = max(0.0, min(255.0, float(values[1])))
        except ValueError:
            continue
        points.append((x, y))
    points.sort()
    merged = []
    for point in points:
        if merged and abs(merged[-1][0] - point[0]) < 1e-6:
            merged[-1] = point
        else:
            merged.append(point)
    if not merged:
        return [(0.0, 0.0), (255.0, 255.0)]
    if merged[0][0] > 0.0:
        merged.insert(0, (0.0, merged[0][1]))
    if merged[-1][0] < 255.0:
        merged.append((255.0, merged[-1][1]))
    return merged


def curve_text(points):
    return ";".join(f"{max(0, min(255, int(round(x))))},{max(0, min(255, int(round(y))))}"
                    for x, y in sorted(points))


def curve_lut(points_or_text):
    """A 256-entry uint8 lookup table for a monotone cubic through the points."""
    points = parse_curve(points_or_text) if isinstance(points_or_text, str) else list(points_or_text)
    xs = np.array([point[0] for point in points], np.float64)
    ys = np.array([point[1] for point in points], np.float64)
    if len(xs) == 2:
        values = np.interp(np.arange(256), xs, ys)
        return np.clip(np.rint(values), 0, 255).astype(np.uint8)
    h = np.diff(xs)
    delta = np.diff(ys) / np.maximum(1e-6, h)
    slopes = np.zeros_like(ys)
    slopes[0], slopes[-1] = delta[0], delta[-1]
    for index in range(1, len(xs) - 1):
        if delta[index - 1] * delta[index] <= 0:
            slopes[index] = 0.0
        else:
            w1 = 2.0 * h[index] + h[index - 1]
            w2 = h[index] + 2.0 * h[index - 1]
            slopes[index] = (w1 + w2) / (w1 / delta[index - 1] + w2 / delta[index])
    x = np.arange(256, dtype=np.float64)
    index = np.clip(np.searchsorted(xs, x) - 1, 0, len(xs) - 2)
    span = np.maximum(1e-6, xs[index + 1] - xs[index])
    t = np.clip((x - xs[index]) / span, 0.0, 1.0)
    t2, t3 = t * t, t * t * t
    values = ((2 * t3 - 3 * t2 + 1) * ys[index]
              + (t3 - 2 * t2 + t) * span * slopes[index]
              + (-2 * t3 + 3 * t2) * ys[index + 1]
              + (t3 - t2) * span * slopes[index + 1])
    return np.clip(np.rint(values), 0, 255).astype(np.uint8)


def _apply_lut(rgb, lut):
    index = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    return lut[index].astype(np.float32)


class CurvesEffect(LayerEffect):
    key = "curves"
    name = "Curves"
    category = "Color Correction"

    PARAMS = (
        ParamSpec("strength", "Strength", "float", 0.0, 100.0, 100.0, step=1.0, unit="%",
                  help="Mix between the original picture and the curved result; keyframeable"),
        ParamSpec("curve_master", "Master curve", "text", default=IDENTITY_CURVE,
                  advanced=True, help="Tone curve applied to all three channels"),
        ParamSpec("curve_red", "Red curve", "text", default=IDENTITY_CURVE,
                  advanced=True, help="Per-channel tone curve for red"),
        ParamSpec("curve_green", "Green curve", "text", default=IDENTITY_CURVE,
                  advanced=True, help="Per-channel tone curve for green"),
        ParamSpec("curve_blue", "Blue curve", "text", default=IDENTITY_CURVE,
                  advanced=True, help="Per-channel tone curve for blue"),
        ParamSpec("preserve_luma", "Preserve luminance", "bool", default=False,
                  help="Keep the original brightness when individual channels change hue"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.strength = Value(100.0)
        self.curve_master = Value(IDENTITY_CURVE)
        self.curve_red = Value(IDENTITY_CURVE)
        self.curve_green = Value(IDENTITY_CURVE)
        self.curve_blue = Value(IDENTITY_CURVE)
        self.preserve_luma = Value(False)

    def luts(self):
        return tuple(curve_lut(getattr(self, f"curve_{channel}").get())
                     for channel in CURVE_CHANNELS)

    def apply_layer(self, layer):
        luts = self.luts()
        if all(np.array_equal(lut, np.arange(256, dtype=np.uint8)) for lut in luts):
            return layer
        rgb = layer[:, :, :3].astype(np.float32)
        original = rgb
        graded = _apply_lut(rgb, luts[0])
        channels = [graded[:, :, 2], graded[:, :, 1], graded[:, :, 0]]
        for index, lut in enumerate(luts[1:]):
            if not np.array_equal(lut, np.arange(256, dtype=np.uint8)):
                channels[index] = _apply_lut(channels[index], lut)
        graded = np.dstack([channels[2], channels[1], channels[0]])
        if bool(self.preserve_luma.get()):
            old = luma(original)[:, :, None]
            new = luma(graded)[:, :, None]
            graded = graded + (old - new)
        amount = max(0.0, min(1.0, num(self.strength, self.local_frame) / 100.0))
        if amount < 0.999:
            graded = original * (1.0 - amount) + graded * amount
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(graded, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class HueSaturationEffect(LayerEffect):
    key = "hue_saturation"
    name = "Hue / Saturation"
    category = "Color Correction"

    PARAMS = (
        ParamSpec("hue", "Hue", "float", -180.0, 180.0, 0.0, step=0.5, unit="\u00b0",
                  help="Rotate every hue around the colour wheel"),
        ParamSpec("saturation", "Saturation", "float", -100.0, 200.0, 0.0, step=1.0, unit="%",
                  help="-100 removes colour; 100 doubles it"),
        ParamSpec("lightness", "Lightness", "float", -100.0, 100.0, 0.0, step=1.0, unit="%",
                  help="Lift or lower the value channel without changing hue"),
        ParamSpec("colorize", "Colorize", "float", 0.0, 100.0, 0.0, step=1.0, unit="%",
                  help="Replace the source hue with the colour below"),
        ParamSpec("colorize_hue", "Colorize hue", "float", 0.0, 360.0, 32.0, step=1.0,
                  unit="\u00b0", advanced=True,
                  help="Hue used when Colorize is above zero"),
        ParamSpec("colorize_saturation", "Colorize saturation", "float", 0.0, 100.0, 45.0,
                  step=1.0, unit="%", advanced=True),
        ParamSpec("preserve_luma", "Preserve luminance", "bool", default=False,
                  help="Keep the original brightness after changing hue or saturation"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.hue = Value(0.0)
        self.saturation = Value(0.0)
        self.lightness = Value(0.0)
        self.colorize = Value(0.0)
        self.colorize_hue = Value(32.0)
        self.colorize_saturation = Value(45.0)
        self.preserve_luma = Value(False)

    def apply_layer(self, layer):
        frame = self.local_frame
        hue = num(self.hue, frame)
        saturation = num(self.saturation, frame)
        lightness = num(self.lightness, frame)
        colorize = float(np.clip(num(self.colorize, frame) / 100.0, 0.0, 1.0))
        if (abs(hue) < 1e-4 and abs(saturation) < 1e-4 and abs(lightness) < 1e-4
                and colorize <= 1e-4):
            return layer
        rgb = layer[:, :, :3]
        original = rgb.astype(np.float32)
        hsv = cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_BGR2HSV)
        hsv[:, :, 0] = (hsv[:, :, 0].astype(np.int16) + int(round(hue / 2.0))) % 180
        sat_factor = 1.0 + saturation / 100.0
        hsv[:, :, 1] = np.clip(hsv[:, :, 1].astype(np.float32) * sat_factor, 0, 255).astype(np.uint8)
        if abs(lightness) > 1e-4:
            value = hsv[:, :, 2].astype(np.float32) / 255.0
            if lightness >= 0:
                value = value + (1.0 - value) * (lightness / 100.0) * 0.65
            else:
                value = value * (1.0 + lightness / 100.0)
            hsv[:, :, 2] = np.clip(value * 255.0, 0, 255).astype(np.uint8)
        graded = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).astype(np.float32)
        if colorize > 0.001:
            target = np.zeros_like(hsv)
            target[:, :, 0] = int(round(num(self.colorize_hue, frame) / 2.0)) % 180
            target[:, :, 1] = np.clip(num(self.colorize_saturation, frame) * 2.55, 0, 255)
            target[:, :, 2] = cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_BGR2GRAY)
            tinted = cv2.cvtColor(target, cv2.COLOR_HSV2BGR).astype(np.float32)
            graded = graded * (1.0 - colorize) + tinted * colorize
        if bool(self.preserve_luma.get()):
            old = luma(original)[:, :, None]
            new = luma(graded)[:, :, None]
            graded = graded + (old - new)
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(graded, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class TintEffect(LayerEffect):
    key = "tint"
    name = "Tint"
    category = "Color Correction"

    PARAMS = (
        ParamSpec("shadow_color", "Shadow colour", "text", default="#101820",
                  help="Colour mapped to black"),
        ParamSpec("highlight_color", "Highlight colour", "text", default="#FFF1D6",
                  help="Colour mapped to white"),
        ParamSpec("strength", "Strength", "float", 0.0, 100.0, 65.0, step=1.0, unit="%"),
        ParamSpec("preserve_luma", "Preserve luminance", "bool", default=False,
                  help="Use the tint's hue but keep the picture's original brightness"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.shadow_color = Value("#101820")
        self.highlight_color = Value("#FFF1D6")
        self.strength = Value(65.0)
        self.preserve_luma = Value(False)

    def apply_layer(self, layer):
        frame = self.local_frame
        amount = float(np.clip(num(self.strength, frame) / 100.0, 0.0, 1.0))
        if amount <= 1e-4:
            return layer
        low = colour_bgr(self.shadow_color.get())
        high = colour_bgr(self.highlight_color.get())
        steps = np.linspace(0.0, 1.0, 256, dtype=np.float32)[:, None]
        ramp = low[None, :] * (1.0 - steps) + high[None, :] * steps
        source = layer[:, :, :3].astype(np.float32)
        index = np.clip(luma(source), 0, 255).astype(np.uint8)
        tinted = ramp[index]
        if bool(self.preserve_luma.get()):
            old = luma(source)[:, :, None]
            tinted = tinted + (old - luma(tinted)[:, :, None])
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(source * (1.0 - amount) + tinted * amount, 0, 255).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class TritoneEffect(LayerEffect):
    key = "tritone"
    name = "Tritone"
    category = "Color Correction"

    PARAMS = (
        ParamSpec("shadow_color", "Shadow colour", "text", default="#131A2A"),
        ParamSpec("mid_color", "Midtone colour", "text", default="#B06A4A"),
        ParamSpec("highlight_color", "Highlight colour", "text", default="#FFF0C9"),
        ParamSpec("balance", "Midpoint", "float", 10.0, 90.0, 50.0, step=1.0, unit="%",
                  help="Where the midtone colour sits on the tone ramp"),
        ParamSpec("strength", "Strength", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
        ParamSpec("preserve_luma", "Preserve luminance", "bool", default=False),
    )

    def __init__(self, app):
        super().__init__(app)
        self.shadow_color = Value("#131A2A")
        self.mid_color = Value("#B06A4A")
        self.highlight_color = Value("#FFF0C9")
        self.balance = Value(50.0)
        self.strength = Value(100.0)
        self.preserve_luma = Value(False)

    def apply_layer(self, layer):
        frame = self.local_frame
        amount = float(np.clip(num(self.strength, frame) / 100.0, 0.0, 1.0))
        if amount <= 1e-4:
            return layer
        low = colour_bgr(self.shadow_color.get())
        middle = colour_bgr(self.mid_color.get())
        high = colour_bgr(self.highlight_color.get())
        point = float(np.clip(num(self.balance, frame) / 100.0, 0.01, 0.99))
        steps = np.linspace(0.0, 1.0, 256, dtype=np.float32)[:, None]
        ramp = np.where(steps < point,
                        low[None, :] + (middle - low)[None, :] * (steps / point),
                        middle[None, :] + (high - middle)[None, :] * ((steps - point) / (1.0 - point)))
        source = layer[:, :, :3].astype(np.float32)
        index = np.clip(luma(source), 0, 255).astype(np.uint8)
        tinted = ramp[index]
        if bool(self.preserve_luma.get()):
            tinted = tinted + (luma(source)[:, :, None] - luma(tinted)[:, :, None])
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(source * (1.0 - amount) + tinted * amount, 0, 255).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class LevelsEffect(LayerEffect):
    key = "levels"
    name = "Levels"
    category = "Color Correction"

    PARAMS = (
        ParamSpec("input_black", "Input black", "float", 0.0, 254.0, 0.0, step=1.0),
        ParamSpec("input_white", "Input white", "float", 1.0, 255.0, 255.0, step=1.0),
        ParamSpec("gamma", "Gamma", "float", 0.1, 4.0, 1.0, step=0.01,
                  help="Midtone curve between the input black and white points"),
        ParamSpec("output_black", "Output black", "float", 0.0, 255.0, 0.0, step=1.0),
        ParamSpec("output_white", "Output white", "float", 0.0, 255.0, 255.0, step=1.0),
        ParamSpec("red_gamma", "Red gamma", "float", 0.1, 4.0, 1.0, step=0.01, advanced=True),
        ParamSpec("green_gamma", "Green gamma", "float", 0.1, 4.0, 1.0, step=0.01, advanced=True),
        ParamSpec("blue_gamma", "Blue gamma", "float", 0.1, 4.0, 1.0, step=0.01, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def luts(self):
        frame = self.local_frame
        black = min(254.0, max(0.0, num(self.input_black, frame)))
        white = max(black + 1.0, min(255.0, num(self.input_white, frame)))
        out_black = float(np.clip(num(self.output_black, frame), 0, 255))
        out_white = float(np.clip(num(self.output_white, frame), 0, 255))
        x = np.arange(256, dtype=np.float32)
        base = np.clip((x - black) / (white - black), 0.0, 1.0)
        tables = []
        for name in ("gamma", "red_gamma", "green_gamma", "blue_gamma"):
            gamma = max(0.01, num(getattr(self, name), frame))
            shaped = np.power(base, 1.0 / gamma)
            tables.append(np.clip(np.rint(out_black + shaped * (out_white - out_black)),
                                  0, 255).astype(np.uint8))
        return (tables[0], tables[3], tables[2], tables[1])   # master, R, G, B

    def apply_layer(self, layer):
        luts = self.luts()
        identity = np.arange(256, dtype=np.uint8)
        if all(np.array_equal(table, identity) for table in luts):
            return layer
        rgb = layer[:, :, :3].astype(np.float32)
        graded = _apply_lut(rgb, luts[0])
        for index, table in enumerate(luts[1:]):
            if not np.array_equal(table, identity):
                channel = 2 - index                    # R, G, B in a BGR array
                graded[:, :, channel] = _apply_lut(graded[:, :, channel], table)
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(graded, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class ColorWheelsEffect(LayerEffect):
    key = "color_wheels"
    name = "Color Wheels"
    category = "Color Correction"
    WHEELS = ("lift", "gamma", "gain")

    PARAMS = (
        ParamSpec("master_lift", "Master lift", "float", -100.0, 100.0, 0.0, step=1.0,
                  advanced=True),
        ParamSpec("master_gamma", "Master gamma", "float", 0.25, 4.0, 1.0, step=0.01,
                  advanced=True),
        ParamSpec("master_gain", "Master gain", "float", 0.0, 200.0, 100.0, step=1.0,
                  advanced=True),
        ParamSpec("lift_x", "Lift X", "float", -100.0, 100.0, 0.0, step=1.0, advanced=True),
        ParamSpec("lift_y", "Lift Y", "float", -100.0, 100.0, 0.0, step=1.0, advanced=True),
        ParamSpec("gamma_x", "Gamma X", "float", -100.0, 100.0, 0.0, step=1.0, advanced=True),
        ParamSpec("gamma_y", "Gamma Y", "float", -100.0, 100.0, 0.0, step=1.0, advanced=True),
        ParamSpec("gain_x", "Gain X", "float", -100.0, 100.0, 0.0, step=1.0, advanced=True),
        ParamSpec("gain_y", "Gain Y", "float", -100.0, 100.0, 0.0, step=1.0, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def wheel_values(self, frame=None):
        frame = self.local_frame if frame is None else frame
        return {name: (num(getattr(self, f"{name}_x"), frame),
                       num(getattr(self, f"{name}_y"), frame))
                for name in self.WHEELS}

    @staticmethod
    def _offset(x, y):
        # A trackball's horizontal axis balances blue/yellow; vertical balances
        # green/magenta. The third channel is inferred so the vector sums to zero.
        return np.array([-x * 0.006, y * 0.006, (x - y) * 0.003], dtype=np.float32)

    def apply_layer(self, layer):
        frame = self.local_frame
        wheels = self.wheel_values(frame)
        if all(abs(x) < 1e-4 and abs(y) < 1e-4 for x, y in wheels.values()) \
                and abs(num(self.master_lift, frame)) < 1e-4 \
                and abs(num(self.master_gamma, frame) - 1.0) < 1e-4 \
                and abs(num(self.master_gain, frame) - 100.0) < 1e-4:
            return layer
        rgb = layer[:, :, :3].astype(np.float32)
        level = luma(rgb)[:, :, None] / 255.0
        shadows, highlights = (1.0 - level) ** 2, level ** 2
        lift = self._offset(*wheels["lift"]) * 255.0 * shadows
        lift += num(self.master_lift, frame) * 0.9 * shadows
        graded = rgb + lift
        gamma_rgb = self._offset(*wheels["gamma"]) * 2.0
        gamma = np.clip(1.0 + gamma_rgb[None, None, :] * shadows, 0.2, 3.0)
        gamma *= max(0.1, num(self.master_gamma, frame))
        graded = 255.0 * np.power(np.clip(graded, 0.0, 255.0) / 255.0, 1.0 / gamma)
        gain = 1.0 + self._offset(*wheels["gain"]) * highlights
        gain *= max(0.0, num(self.master_gain, frame)) / 100.0
        graded = graded * np.clip(gain, 0.0, 3.0)
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(graded, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


COLOR_EXTRA_EFFECTS = (CurvesEffect, HueSaturationEffect, TintEffect, TritoneEffect,
                       LevelsEffect, ColorWheelsEffect)
