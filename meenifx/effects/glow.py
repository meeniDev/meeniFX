"""Glow: a thresholded, blurred bright pass added back under a blend mode.

It works on the BGRA layer, so text, PNG characters, images and video all glow, and
the halo raises the layer's own alpha instead of only painting colour inside an
existing shape - that is what makes a glow visible over the tracks below it. The
colour maths happens in premultiplied float space, and preview and export both call
this one function.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec

BLEND_MODES = ("screen", "add", "soft_light", "overlay")
_LUMA = np.array([0.0722, 0.7152, 0.2126], dtype=np.float32)   # BGR order


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


class GlowEffect(LayerEffect):
    key = "glow"
    name = "Glow"
    category = "Glow & Light"

    PARAMS = (
        ParamSpec("intensity", "Intensity", "float", 0.0, 400.0, 100.0, step=1.0, unit="%",
                  help="How strong the halo is; above 100% it blooms harder"),
        ParamSpec("radius", "Radius", "float", 0.0, 300.0, 24.0, step=0.5, unit=" px", pixels=True,
                  help="How far the light spreads"),
        ParamSpec("threshold", "Threshold", "float", 0.0, 100.0, 62.0, step=1.0, unit="%",
                  help="Brightness a pixel needs before it glows"),
        ParamSpec("softness", "Falloff", "float", 0.0, 100.0, 50.0, step=1.0, unit="%",
                  help="Soft edge of the threshold: low is a hard cut, high is a gentle bloom"),
        ParamSpec("glow_r", "Glow red", "int", 0, 255, 255, help="Colour of the glow"),
        ParamSpec("glow_g", "Glow green", "int", 0, 255, 255, help="Colour of the glow"),
        ParamSpec("glow_b", "Glow blue", "int", 0, 255, 255, help="Colour of the glow"),
        ParamSpec("opacity", "Opacity", "float", 0.0, 100.0, 100.0, step=1.0, unit="%",
                  help="Overall opacity of the halo layer"),
        ParamSpec("blend", "Blend mode", "choice", default="screen", choices=BLEND_MODES,
                  help="How the halo is mixed with the picture"),
        ParamSpec("quality", "Quality", "int", 1, 4, 2,
                  help="Blur passes: more is smoother on wide glows and slower"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.intensity = Value(100.0)
        self.radius = Value(24.0)
        self.threshold = Value(62.0)
        self.softness = Value(50.0)
        self.glow_r = Value(255)
        self.glow_g = Value(255)
        self.glow_b = Value(255)
        self.opacity = Value(100.0)
        self.blend = Value("screen")
        self.quality = Value(2)

    def apply_layer(self, layer):
        opacity = _num(self.opacity, self.local_frame) / 100.0
        intensity = _num(self.intensity, self.local_frame) / 100.0
        radius = _num(self.radius, self.local_frame)
        if opacity <= 0.0 or intensity <= 0.0 or radius <= 0.3:
            return layer

        frame = self.local_frame
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
        sigma = max(0.1, (radius * max(0.02, float(self.param_scale))) / 2.5)
        passes = max(1, int(self.quality.get()))
        # Splitting the blur over N passes at sigma/sqrt(N) adds up to the same spread,
        # but is smoother (and cheaper) than one very wide kernel.
        step = sigma / math.sqrt(passes)
        blurred = bright
        for _pass in range(passes):
            blurred = cv2.GaussianBlur(blurred, (0, 0), sigmaX=step, sigmaY=step,
                                       borderType=cv2.BORDER_REPLICATE)
        glow_mask = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), sigmaX=sigma, sigmaY=sigma,
                                     borderType=cv2.BORDER_REPLICATE)

        # Bloom compensation: a wide blur spreads the same light thin, so a big radius
        # would otherwise nearly vanish. Normalising by the halo's own peak (bounded)
        # keeps the spread visible without letting one highlight blow the frame out.
        peak = float(glow_mask.max())
        gain = min(4.0, 1.0 / max(0.18, peak)) if peak > 0.0 else 1.0

        colour = np.array([float(self.glow_b.get()), float(self.glow_g.get()),
                           float(self.glow_r.get())], dtype=np.float32) / 255.0
        light = blurred * colour * intensity * gain
        weight = opacity
        mode = str(self.blend.get())
        if mode == "add":
            out_rgb = rgb + light * weight
        elif mode == "screen":
            glow = np.clip(light * weight, 0.0, 255.0)
            out_rgb = 255.0 - (255.0 - rgb) * (255.0 - glow) / 255.0
        else:
            out_rgb = _blend(rgb, np.clip(light * weight, 0.0, 255.0), mode)
        out_alpha = np.clip(alpha + glow_mask * opacity, 0.0, 1.0) * 255.0
        out = np.empty_like(data)
        out[:, :, :3] = np.clip(out_rgb, 0.0, 255.0)
        out[:, :, 3] = out_alpha
        return out.astype(np.uint8)


GLOW_EFFECTS = (GlowEffect,)
