"""Glow: a thresholded, blurred bright pass added back under a blend mode.

It works on the BGRA layer, so text, PNG characters, images and video all glow, and
the halo raises the layer's own alpha instead of only painting colour inside an
existing shape - that is what makes a glow visible over the tracks below it. The
colour maths happens in premultiplied float space, and preview and export both call
this one function.

The bloom control blends a single Gaussian halo with two wider scales. That keeps a
large cinematic bloom visible without making every edge look like one hard box: the
wide passes carry soft light, while the base pass keeps the highlight's shape. All
passes use a constant zero border, so transparent clips cannot pick up a bright frame
edge from the blur.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec

BLEND_MODES = ("screen", "add", "soft_light", "overlay")
_LUMA = np.array([0.0722, 0.7152, 0.2126], dtype=np.float32)   # BGR order

# Starting points, not hidden render-time overrides. The properties panel writes every
# value into the clip's own parameters, so the sliders remain the source of truth and
# the preset can be refined or partly undone like any other edit.
PRESETS = {
    "soft_glow": ("Soft Glow", {
        "intensity": 75.0, "radius": 34.0, "threshold": 45.0, "softness": 85.0,
        "saturation": 90.0, "bloom": 0.0, "glow_r": 255, "glow_g": 255,
        "glow_b": 255, "opacity": 100.0, "blend": "screen", "quality": 2,
    }),
    "neon_glow": ("Neon Glow", {
        "intensity": 220.0, "radius": 14.0, "threshold": 65.0, "softness": 12.0,
        "saturation": 200.0, "bloom": 0.0, "glow_r": 0, "glow_g": 229,
        "glow_b": 255, "opacity": 100.0, "blend": "screen", "quality": 2,
    }),
    "dreamy_bloom": ("Dreamy Bloom", {
        "intensity": 95.0, "radius": 58.0, "threshold": 28.0, "softness": 92.0,
        "saturation": 75.0, "bloom": 80.0, "glow_r": 255, "glow_g": 255,
        "glow_b": 255, "opacity": 100.0, "blend": "screen", "quality": 3,
    }),
    "cinematic_bloom": ("Cinematic Bloom", {
        "intensity": 150.0, "radius": 42.0, "threshold": 58.0, "softness": 45.0,
        "saturation": 118.0, "bloom": 65.0, "glow_r": 255, "glow_g": 214,
        "glow_b": 165, "opacity": 100.0, "blend": "screen", "quality": 3,
    }),
    "outer_glow": ("Outer Glow", {
        "intensity": 160.0, "radius": 18.0, "threshold": 0.0, "softness": 100.0,
        "saturation": 100.0, "bloom": 0.0, "glow_r": 255, "glow_g": 255,
        "glow_b": 255, "opacity": 100.0, "blend": "screen", "quality": 2,
        "placement": "outside",
    }),
}


def _num(value, frame=0):
    from ..model.keyframes import AnimatedValue, parameter_value_at
    if isinstance(value, AnimatedValue):
        return float(parameter_value_at(value, frame))
    if hasattr(value, "get"):
        return float(value.get())
    return float(value)


def _blend(base, glow, mode):
    """Blend two float32 (H, W, 3) colour arrays in the 0..255 domain."""
    if mode == "add":
        return base + glow
    if mode == "screen":
        return 255.0 - (255.0 - np.clip(base, 0.0, 255.0)) * (255.0 - np.clip(glow, 0.0, 255.0)) / 255.0
    b = np.clip(base, 0.0, 255.0) / 255.0
    g = np.clip(glow, 0.0, 255.0) / 255.0
    if mode == "overlay":
        out = np.where(b <= 0.5, 2.0 * b * g, 1.0 - 2.0 * (1.0 - b) * (1.0 - g))
    else:   # soft light (W3C)
        out = (1.0 - 2.0 * g) * b * b + 2.0 * g * b
    return out * 255.0


def _blur(field, sigma, passes):
    """A Gaussian pass with a transparent border.

    ``BORDER_CONSTANT`` is deliberate: reproducing the edge pixel would turn the
    outside of a transparent title into a bright rectangle. The work buffer is
    already premultiplied, so blurring zeroes outside the layer is correct.
    """
    step = max(0.1, float(sigma)) / math.sqrt(max(1, int(passes)))
    result = field
    for _pass in range(max(1, int(passes))):
        result = cv2.GaussianBlur(result, (0, 0), sigmaX=step, sigmaY=step,
                                  borderType=cv2.BORDER_CONSTANT)
    return result


class GlowEffect(LayerEffect):
    key = "glow"
    name = "Glow"
    category = "Glow & Light"

    PRESETS = PRESETS

    PARAMS = (
        ParamSpec("intensity", "Intensity", "float", 0.0, 400.0, 100.0, step=1.0, unit="%",
                  help="How strong the halo is; above 100% it blooms harder"),
        ParamSpec("radius", "Radius", "float", 0.0, 300.0, 24.0, step=0.5, unit=" px", pixels=True,
                  help="How far the light spreads"),
        ParamSpec("threshold", "Threshold", "float", 0.0, 100.0, 62.0, step=1.0, unit="%",
                  help="Brightness a pixel needs before it glows"),
        ParamSpec("softness", "Falloff", "float", 0.0, 100.0, 50.0, step=1.0, unit="%",
                  help="Soft edge of the threshold: low is a hard cut, high is a gentle bloom"),
        ParamSpec("saturation", "Saturation", "float", 0.0, 200.0, 100.0, step=1.0, unit="%",
                  help="Colour in the halo: 0 is neutral, 100 keeps the source colour, "
                       "200 pushes it further"),
        ParamSpec("bloom", "Multi-scale bloom", "float", 0.0, 100.0, 0.0, step=1.0, unit="%",
                  help="Mix in wider blur scales for a soft cinematic bloom; 0 keeps a "
                       "single halo"),
        ParamSpec("glow_r", "Glow red", "int", 0, 255, 255, help="Colour of the glow"),
        ParamSpec("glow_g", "Glow green", "int", 0, 255, 255, help="Colour of the glow"),
        ParamSpec("glow_b", "Glow blue", "int", 0, 255, 255, help="Colour of the glow"),
        ParamSpec("opacity", "Opacity", "float", 0.0, 100.0, 100.0, step=1.0, unit="%",
                  help="Overall opacity of the halo layer"),
        ParamSpec("blend", "Blend mode", "choice", default="screen", choices=BLEND_MODES,
                  help="How the halo is mixed with the picture"),
        ParamSpec("quality", "Quality", "int", 1, 4, 2,
                  help="Blur passes: more is smoother on wide glows and slower"),
        ParamSpec("placement", "Glow placement", "choice", default="both",
                  choices=("both", "inside", "outside"),
                  choice_labels=("Inside and outside", "Inside", "Outside"),
                  help="Choose whether the halo stays inside the text or image edge, extends "
                       "outward, or does both"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.intensity = Value(100.0)
        self.radius = Value(24.0)
        self.threshold = Value(62.0)
        self.softness = Value(50.0)
        self.saturation = Value(100.0)
        self.bloom = Value(0.0)
        self.glow_r = Value(255)
        self.glow_g = Value(255)
        self.glow_b = Value(255)
        self.opacity = Value(100.0)
        self.blend = Value("screen")
        self.quality = Value(2)
        self.placement = Value("both")

    def apply_layer(self, layer):
        frame = self.local_frame
        opacity = _num(self.opacity, frame) / 100.0
        intensity = _num(self.intensity, frame) / 100.0
        radius = _num(self.radius, frame)
        bloom = max(0.0, min(1.0, _num(self.bloom, frame) / 100.0))
        if opacity <= 0.0 or intensity <= 0.0 or radius <= 0.3:
            return layer

        data = layer.astype(np.float32)
        alpha = data[:, :, 3] / 255.0
        rgb = data[:, :, :3]
        luma = np.clip((rgb @ _LUMA) / 255.0, 0.0, 1.0)
        threshold = max(0.0, min(1.0, _num(self.threshold, frame) / 100.0))
        softness = max(0.001, _num(self.softness, frame) / 100.0)
        # Smooth threshold: a hard cut makes glow flicker when brightness hovers.
        mask = np.clip((luma - threshold) / softness, 0.0, 1.0)
        mask *= alpha
        if not mask.any():
            return layer

        # Bright pass in premultiplied space, so transparency never blooms as black.
        bright = rgb * mask[:, :, None]

        # Saturation belongs to the light, not the source: a coloured highlight can
        # produce a white halo, and a grey one can be pushed toward its own hue.
        saturation = max(0.0, _num(self.saturation, frame) / 100.0)
        # 100% is the source colour, 0% removes all halo colour, and 200% is a
        # deliberately strong neon push. A simple 1:1 multiplier is almost invisible
        # on a low-chroma frame, so the upper half of the control is expanded.
        chroma = max(0.0, saturation * 3.0 - 2.0)
        if abs(chroma - 1.0) > 1e-4:
            bright_luma = (bright @ _LUMA)[:, :, None]
            bright = np.clip(bright_luma + (bright - bright_luma) * chroma,
                             0.0, 255.0)

        sigma = max(0.1, (radius * max(0.02, float(self.param_scale))) / 2.5)
        passes = max(1, int(self.quality.get()))
        # A wide halo is low frequency: blur it on a smaller grid and scale it back.
        # The chosen size depends only on the render size, so a 50% preview keeps the
        # same shape as the export instead of switching algorithms at a threshold.
        factor = 1.0
        longest = max(layer.shape[:2])
        if sigma > 4.0 and longest > 480:
            factor = min(0.5, 960.0 / float(longest))
            small_w = max(1, int(round(layer.shape[1] * factor)))
            small_h = max(1, int(round(layer.shape[0] * factor)))
            bright = cv2.resize(bright, (small_w, small_h), interpolation=cv2.INTER_AREA)
            mask = cv2.resize(mask.astype(np.float32), (small_w, small_h),
                              interpolation=cv2.INTER_AREA)

        base_light = _blur(bright, sigma * factor, passes)
        base_mask = _blur(mask.astype(np.float32), sigma * factor, passes)
        if bloom > 0.001:
            # Three low-frequency scales: the base keeps the highlight described, the
            # middle and wide passes carry the atmospheric spill. Weights sum to one,
            # so Bloom never changes the overall exposure by itself.
            middle_light = _blur(bright, sigma * 2.6 * factor, passes)
            wide_light = _blur(bright, sigma * 5.2 * factor, passes)
            middle_mask = _blur(mask.astype(np.float32), sigma * 2.6 * factor, passes)
            wide_mask = _blur(mask.astype(np.float32), sigma * 5.2 * factor, passes)
            bloom_light = base_light * 0.55 + middle_light * 0.30 + wide_light * 0.15
            bloom_mask = base_mask * 0.55 + middle_mask * 0.30 + wide_mask * 0.15
            blurred = base_light * (1.0 - bloom) + bloom_light * bloom
            glow_mask = base_mask * (1.0 - bloom) + bloom_mask * bloom
        else:
            blurred = base_light
            glow_mask = base_mask

        if factor < 1.0:
            blurred = cv2.resize(blurred, (layer.shape[1], layer.shape[0]),
                                 interpolation=cv2.INTER_LINEAR)
            glow_mask = cv2.resize(glow_mask, (layer.shape[1], layer.shape[0]),
                                   interpolation=cv2.INTER_LINEAR)

        # Bloom compensation: a wide blur spreads the same light thin. Normalising by
        # the halo's own peak (bounded) keeps the spread visible without letting one
        # highlight blow the frame out.
        peak = float(glow_mask.max())
        gain = min(4.0, 1.0 / max(0.18, peak)) if peak > 0.0 else 1.0

        colour = np.array([float(self.glow_b.get()), float(self.glow_g.get()),
                           float(self.glow_r.get())], dtype=np.float32) / 255.0
        light = blurred * colour * intensity * gain
        placement = str(self.placement.get())
        if placement == "outside":
            light *= (1.0 - alpha)[:, :, None]
        elif placement == "inside":
            light *= alpha[:, :, None]
        weight = opacity
        mode = str(self.blend.get())
        if mode == "add":
            out_rgb = rgb + light * weight
        elif mode == "screen":
            glow = np.clip(light * weight, 0.0, 255.0)
            out_rgb = 255.0 - (255.0 - rgb) * (255.0 - glow) / 255.0
        else:
            out_rgb = _blend(rgb, np.clip(light * weight, 0.0, 255.0), mode)

        # Screen the alpha too. Adding it would make overlapping halo and source
        # coverage exceed 1 and flatten the soft edge into a hard white fringe.
        glow_alpha = np.clip(glow_mask * opacity, 0.0, 1.0)
        if placement == "outside":
            glow_alpha *= 1.0 - alpha
        elif placement == "inside":
            glow_alpha *= alpha
        out_alpha = 1.0 - (1.0 - alpha) * (1.0 - glow_alpha)
        out = np.empty_like(data)
        out[:, :, :3] = np.clip(out_rgb, 0.0, 255.0)
        out[:, :, 3] = out_alpha * 255.0
        return out.astype(np.uint8)


GLOW_EFFECTS = (GlowEffect,)
