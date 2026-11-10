"""The professional Glow family: Multi-Glow, Neon, Soft Bloom, Light Wrap and
Directional Glow.

Every variant thresholds a bright pass, blurs it in premultiplied float space and screens
its light back over the BGRA layer.  Alpha is screened rather than added, which keeps a
transparent title's halo soft instead of flattening it into a hard fringe.  Each variant
has a genuinely different light construction: multiple octaves, a sharp neon core, a wide
low-threshold haze, an inside-edge light wrap, or a directional streak.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec
from .creative_utils import (BLEND_LABELS, BLEND_MODES, blend, colour_bgr, directional,
                             gaussian, luma, num)

_COMMON = (
    ParamSpec("intensity", "Intensity", "float", 0.0, 400.0, 120.0, step=1.0, unit="%",
              help="How strong the light is"),
    ParamSpec("radius", "Radius", "float", 0.0, 300.0, 28.0, step=0.5, unit=" px", pixels=True,
              help="How far the halo spreads"),
    ParamSpec("threshold", "Threshold", "float", 0.0, 100.0, 58.0, step=1.0, unit="%",
              help="Brightness a pixel needs before it glows"),
    ParamSpec("falloff", "Falloff", "float", 0.0, 100.0, 55.0, step=1.0, unit="%",
              help="Softness of the threshold edge"),
    ParamSpec("saturation", "Saturation", "float", 0.0, 200.0, 110.0, step=1.0, unit="%",
              help="Colour strength in the halo"),
    ParamSpec("glow_r", "Red", "int", 0, 255, 255),
    ParamSpec("glow_g", "Green", "int", 0, 255, 255),
    ParamSpec("glow_b", "Blue", "int", 0, 255, 255),
    ParamSpec("blend", "Blend mode", "choice", choices=BLEND_MODES,
              choice_labels=BLEND_LABELS, default="screen",
              help="How the halo mixes with the picture"),
    ParamSpec("quality", "Quality", "int", 1, 4, 2,
              help="Blur passes; more is smoother on wide glows"),
)


class _GlowVariant(LayerEffect):
    category = "Glow & Light"
    PARAMS = _COMMON

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def _scales(self, frame):
        return ((1.0, 1.0),)

    def _directional(self, bright, mask, frame, sigma):
        return gaussian(bright, sigma, int(self.quality.get())), \
            gaussian(mask, sigma, int(self.quality.get()))

    def _light(self, rgb, alpha, mask, blurred, frame, scales):
        return blurred

    def _alpha_gain(self, mask, frame):
        return mask

    def apply_layer(self, layer):
        frame = self.local_frame
        intensity = num(self.intensity, frame) / 100.0
        radius = num(self.radius, frame)
        if intensity <= 0.001 or radius <= 0.3:
            return layer
        data = layer.astype(np.float32)
        alpha = data[:, :, 3] / 255.0
        alpha_full = alpha
        rgb = data[:, :, :3]
        level = luma(rgb) / 255.0
        threshold = float(np.clip(num(self.threshold, frame) / 100.0, 0.0, 1.0))
        softness = max(0.002, num(self.falloff, frame) / 100.0)
        mask = np.clip((level - threshold) / softness, 0.0, 1.0)
        mask *= alpha
        if not mask.any():
            return layer
        bright = rgb * mask[:, :, None]
        saturation = max(0.0, num(self.saturation, frame) / 100.0)
        # 100% keeps the source colour. Expanding the upper half makes the control
        # visibly push a low-chroma highlight instead of barely tinting it.
        chroma = max(0.0, saturation * 3.0 - 2.0)
        if abs(chroma - 1.0) > 1e-4:
            grey = (bright @ np.array([0.0722, 0.7152, 0.2126], np.float32))[:, :, None]
            bright = np.clip(grey + (bright - grey) * chroma, 0.0, 255.0)

        # Low-frequency light is computed on a bounded grid; all variants share this
        # stability rule so preview and export keep the same halo shape.
        factor = 1.0
        longest = max(layer.shape[:2])
        sigma = max(0.1, radius * max(0.02, float(self.param_scale)) / 2.5)
        if longest > 720:
            factor = 720.0 / float(longest)
            small = (max(1, int(round(layer.shape[1] * factor))),
                     max(1, int(round(layer.shape[0] * factor))))
            bright = cv2.resize(bright, small, interpolation=cv2.INTER_AREA)
            mask = cv2.resize(mask.astype(np.float32), small, interpolation=cv2.INTER_AREA)
            alpha = cv2.resize(alpha, small, interpolation=cv2.INTER_AREA)
            sigma *= factor

        scales = self._scales(frame)
        light = np.zeros_like(bright, dtype=np.float32)
        coverage = np.zeros_like(mask, dtype=np.float32)
        total = sum(weight for _step, weight in scales) or 1.0
        for step, weight in scales:
            pass_light, pass_coverage = self._directional(bright, mask, frame, sigma * step)
            light += pass_light * (weight / total)
            coverage += pass_coverage * (weight / total)
        light = self._light(rgb, alpha, mask, light, frame, scales)
        if factor < 1.0:
            size = (layer.shape[1], layer.shape[0])
            light = cv2.resize(light, size, interpolation=cv2.INTER_LINEAR)
            coverage = cv2.resize(coverage, size, interpolation=cv2.INTER_LINEAR)
        colour = colour_bgr("#%02X%02X%02X" % (
            int(self.glow_r.get()), int(self.glow_g.get()), int(self.glow_b.get())))
        light = light * (colour / 255.0)[None, None, :] * intensity
        mode = str(self.blend.get())
        out_rgb = np.clip(blend(rgb, light, mode), 0.0, 255.0)
        glow_alpha = np.clip(coverage * intensity, 0.0, 1.0)
        if getattr(self, "preserve_alpha", None) is not None and bool(self.preserve_alpha.get()):
            glow_alpha = np.zeros_like(glow_alpha)
        out_alpha = 1.0 - (1.0 - alpha_full) * (1.0 - glow_alpha)
        output = np.empty_like(data)
        output[:, :, :3] = out_rgb
        output[:, :, 3] = out_alpha * 255.0
        return output.astype(np.uint8)


class MultiGlowEffect(_GlowVariant):
    key = "multi_glow"
    name = "Multi-Glow"
    PARAMS = _COMMON + (
        ParamSpec("scales", "Light passes", "int", 2, 6, 4,
                  help="Number of independently weighted halo scales"),
        ParamSpec("spread", "Octave spread", "float", 120.0, 320.0, 190.0, step=5.0, unit="%",
                  help="How much wider each successive pass is"),
        ParamSpec("balance", "Balance", "float", 0.0, 100.0, 35.0, step=1.0, unit="%",
                  help="Favour the tight core (0) or the wide atmosphere (100)"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.scales = Value(4)
        self.spread = Value(190.0)
        self.balance = Value(35.0)

    def _scales(self, frame):
        count = max(2, int(num(self.scales, frame)))
        spread = max(1.05, num(self.spread, frame) / 100.0)
        balance = float(np.clip(num(self.balance, frame) / 100.0, 0.0, 1.0))
        rows = []
        for index in range(count):
            weight = (1.0 - balance) ** index
            rows.append((spread ** index, max(0.03, weight)))
        return tuple(rows)


class NeonGlowEffect(_GlowVariant):
    key = "neon_glow"
    name = "Neon Glow"
    PARAMS = _COMMON + (
        ParamSpec("core", "Neon core", "float", 0.0, 200.0, 85.0, step=1.0, unit="%",
                  help="Bright filament drawn inside the thresholded edge"),
        ParamSpec("core_width", "Core width", "float", 1.0, 20.0, 3.0, step=0.5, unit=" px",
                  pixels=True, help="Width of the bright filament"),
        ParamSpec("halo", "Halo spread", "float", 100.0, 500.0, 260.0, step=5.0, unit="%",
                  help="How far the coloured halo reaches beyond the core"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.core = Value(85.0)
        self.core_width = Value(3.0)
        self.halo = Value(260.0)

    def _scales(self, frame):
        return ((1.0, 1.0), (max(1.2, num(self.halo, frame) / 100.0), 0.55))

    def _light(self, rgb, alpha, mask, blurred, frame, scales):
        width = max(0.5, num(self.core_width, frame)) * 2.5
        core = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), max(0.1, width),
                                borderType=cv2.BORDER_CONSTANT)
        filament = np.clip(mask - core, 0.0, 1.0)
        light = blurred + filament[:, :, None] * 255.0 * max(0.0, num(self.core, frame) / 100.0)
        return light


class SoftBloomEffect(_GlowVariant):
    key = "soft_bloom"
    name = "Soft Bloom"
    PARAMS = _COMMON + (
        ParamSpec("haze", "Haze", "float", 0.0, 100.0, 45.0, step=1.0, unit="%",
                  help="A broad, low-contrast atmospheric lift"),
        ParamSpec("highlight_rolloff", "Highlight rolloff", "float", 0.0, 100.0, 65.0,
                  step=1.0, unit="%",
                  help="Compress bright cores so the bloom stays soft instead of clipping"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.haze = Value(45.0)
        self.highlight_rolloff = Value(65.0)
        self.threshold = Value(32.0)
        self.falloff = Value(90.0)

    def _scales(self, frame):
        return ((0.65, 0.55), (2.0, 0.30), (4.5, 0.15))

    def _light(self, rgb, alpha, mask, blurred, frame, scales):
        rolloff = np.clip(blurred / 255.0, 0.0, 1.0)
        amount = float(np.clip(num(self.highlight_rolloff, frame) / 100.0, 0.0, 1.0))
        softened = 255.0 * (rolloff / (1.0 + rolloff * amount))
        light = blurred * (1.0 - amount) + softened * amount
        haze = float(np.clip(num(self.haze, frame) / 100.0, 0.0, 1.0))
        if haze > 0.001:
            # `mask` has the same bounded working size as `light`; using the full-size
            # alpha here broke 1080p / reduced-grid previews with a broadcast error.
            light = light + 255.0 * haze * 0.12 * mask[:, :, None]
        return light


class LightWrapEffect(_GlowVariant):
    key = "light_wrap"
    name = "Light Wrap"
    PARAMS = _COMMON + (
        ParamSpec("wrap", "Wrap distance", "float", 1.0, 120.0, 22.0, step=1.0, unit=" px",
                  pixels=True, help="How far the surrounding light spills inside the subject"),
        ParamSpec("edge_bias", "Edge bias", "float", 0.0, 100.0, 55.0, step=1.0, unit="%",
                  help="Keep the wrap on the edge (0) or spread it inward (100)"),
        ParamSpec("preserve_alpha", "Preserve alpha", "bool", default=True,
                  help="Wrap colour without making a transparent layer more opaque"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.wrap = Value(22.0)
        self.edge_bias = Value(55.0)
        self.preserve_alpha = Value(True)
        self.threshold = Value(20.0)
        self.falloff = Value(100.0)

    def _scales(self, frame):
        return ((1.0, 1.0),)

    def _light(self, rgb, alpha, mask, blurred, frame, scales):
        # The blurred matte reaches inward from the edge. Subtracting the original alpha
        # leaves exactly the inside band where a light wrap belongs.
        wrap_distance = num(self.wrap, frame) * float(self.param_scale)
        radius = num(self.radius, frame) * float(self.param_scale)
        # Radius and Wrap both describe the spill reach: the first is the optical halo,
        # the second the distance the edge light is allowed to travel inward.
        spread = gaussian(alpha, max(1.0, (wrap_distance + radius * 0.5) / 1.2), 2)
        bias = float(np.clip(num(self.edge_bias, frame) / 100.0, 0.0, 1.0))
        band = np.clip(spread * 1.5 - alpha * 0.5, 0.0, 1.0)
        band *= alpha
        # Longer wraps visibly increase the spill; a light wrap should never look like a
        # barely perceptible edge tint just because the source is mostly opaque.
        wrap_gain = 0.65 + 0.75 * min(1.0, wrap_distance / 120.0)
        radius_gain = 0.35 + 1.4 * min(1.0, radius / 300.0)
        return (blurred * band[:, :, None] * 4.0 * wrap_gain * radius_gain
                + blurred * (1.0 - bias) * 0.65 * alpha[:, :, None])

    def _alpha_gain(self, mask, frame):
        return mask * (0.0 if bool(self.preserve_alpha.get()) else 1.0)


class DirectionalGlowEffect(_GlowVariant):
    key = "directional_glow"
    name = "Directional Glow"
    PARAMS = _COMMON + (
        ParamSpec("angle", "Angle", "float", -180.0, 180.0, 0.0, step=1.0, unit="\u00b0",
                  help="Direction of the light streak"),
        ParamSpec("length", "Streak length", "float", 0.0, 800.0, 180.0, step=5.0, unit=" px",
                  pixels=True, help="How far the highlight is smeared"),
        ParamSpec("streak_falloff", "Streak falloff", "float", 0.0, 100.0, 50.0, step=1.0,
                  unit="%", help="Fade the streak toward its ends"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.angle = Value(0.0)
        self.length = Value(180.0)
        self.streak_falloff = Value(50.0)

    def _scales(self, frame):
        return ((1.0, 1.0),)

    def _directional(self, bright, mask, frame, sigma):
        length = num(self.length, frame) * float(self.param_scale)
        angle = num(self.angle, frame)
        samples = max(3, int(self.quality.get()) * 4 + 1)
        light = directional(bright, length, angle, samples)
        coverage = directional(mask.astype(np.float32), length, angle, samples)
        falloff = float(np.clip(num(self.streak_falloff, frame) / 100.0, 0.0, 1.0))
        if falloff > 0.001:
            # A small cross-blur softens the hard ends of the averaging line.
            light = gaussian(light, max(0.5, sigma * 0.4), 1) * (1.0 - falloff) + light * falloff
            coverage = gaussian(coverage, max(0.5, sigma * 0.4), 1) * (1.0 - falloff) + coverage * falloff
        # The ends of a directional light are dimmer than its centre. Keep that taper
        # connected to the visible control even when the cross-blur is subtle.
        taper = 1.0 - 0.45 * falloff
        light *= taper
        coverage *= taper
        return light, coverage


GLOW_EXTRA_EFFECTS = (MultiGlowEffect, NeonGlowEffect, SoftBloomEffect,
                      LightWrapEffect, DirectionalGlowEffect)
