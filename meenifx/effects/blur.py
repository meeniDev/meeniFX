"""Blur family: gaussian, box, directional, radial, zoom and motion blur.

All of them run on the BGRA layer and blur premultiplied colour, which is what keeps
soft edges from turning dark (a naive blur mixes black into transparent pixels). The
alpha channel is blurred with the same kernel, so the picture keeps a soft,
correctly-feathered edge instead of a hard cut-out around a blurred picture.

Radii and lengths are logical-resolution pixels and are scaled by the preview quality,
so a 35% preview draws the same blur as the export, only at a smaller size.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec


def _num(value, frame=0):
    from ..model.keyframes import AnimatedValue, parameter_value_at
    if isinstance(value, AnimatedValue):
        return float(parameter_value_at(value, frame))
    if hasattr(value, "get"):
        return float(value.get())
    return float(value)


def premultiply(layer):
    """BGRA -> float32 premultiplied BGRA (colour multiplied by alpha)."""
    data = layer.astype(np.float32)
    alpha = data[:, :, 3:4] / 255.0
    data[:, :, :3] *= alpha
    return data


def fully_opaque(layer):
    """True when every pixel is opaque, so premultiplication changes nothing.

    A picture that fills its layer exactly (the ordinary full-frame clip) has no
    transparency at all. Round-tripping it through a float32 premultiplied buffer is then
    mathematically an identity, but it cost three full-frame float passes: at 1080p that
    was ~90 % of a Gaussian Blur (43 ms premultiply + 59 ms unpremultiply against 10 ms
    of actual blur). Blurring the 8-bit layer directly is the same picture within one
    8-bit step, and it is what makes a blur usable during playback.
    """
    if layer.ndim != 3 or layer.shape[2] != 4 or layer.size == 0:
        return False
    return int(layer[:, :, 3].min()) == 255


def unpremultiply(data, like):
    """Premultiplied float32 BGRA -> uint8 BGRA with the original alpha restored."""
    alpha = np.clip(data[:, :, 3:4], 0.0, 255.0)
    safe = np.where(alpha > 1e-3, alpha / 255.0, 1.0)
    out = np.empty_like(data)
    out[:, :, :3] = np.clip(data[:, :, :3] / safe, 0.0, 255.0)
    out[:, :, 3] = alpha[:, :, 0]
    return out.astype(np.uint8)


def _average(layer, samples):
    """Average several equally weighted BGRA uint8 images."""
    total = samples[0].astype(np.float32)
    for extra in samples[1:]:
        total += extra
    total /= float(len(samples))
    return total.astype(np.uint8)


def _warp(layer, matrix, size):
    return cv2.warpAffine(layer, matrix, size, flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


class GaussianBlurEffect(LayerEffect):
    key = "gaussian_blur"
    name = "Gaussian Blur"
    category = "Blur & Sharpen"

    PARAMS = (
        ParamSpec("radius", "Radius", "float", 0.0, 200.0, 8.0, step=0.5, unit=" px", pixels=True,
                  help="Softness of the blur; 0 leaves the picture untouched"),
        ParamSpec("keep_edges", "Keep edges sharp", "bool", default=False,
                  help="Blur the colour but keep the original alpha (a soft inside, crisp outline)"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.radius = Value(8.0)
        self.keep_edges = Value(False)

    def apply_layer(self, layer):
        radius = _num(self.radius, self.local_frame) * max(0.02, float(self.param_scale))
        if radius <= 0.05:
            return layer
        sigma = max(0.1, radius / 2.5)
        if not self.keep_edges.get() and fully_opaque(layer):
            # No transparency to protect: blur the 8-bit layer in one pass.
            return cv2.GaussianBlur(layer, (0, 0), sigmaX=sigma, sigmaY=sigma,
                                    borderType=cv2.BORDER_REPLICATE)
        data = premultiply(layer) if not self.keep_edges.get() else layer.astype(np.float32)
        blurred = cv2.GaussianBlur(data, (0, 0), sigmaX=sigma, sigmaY=sigma,
                                   borderType=cv2.BORDER_REPLICATE)
        if self.keep_edges.get():
            blurred[:, :, 3] = layer[:, :, 3]
            return np.clip(blurred, 0.0, 255.0).astype(np.uint8)
        return unpremultiply(blurred, layer)


class BoxBlurEffect(LayerEffect):
    key = "box_blur"
    name = "Box Blur"
    category = "Blur & Sharpen"

    PARAMS = (
        ParamSpec("radius", "Radius", "float", 0.0, 120.0, 6.0, step=0.5, unit=" px", pixels=True,
                  help="Size of the square average; two passes look almost gaussian"),
        ParamSpec("passes", "Passes", "int", 1, 4, 1,
                  help="Repeat the average: more passes is smoother and still fast"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.radius = Value(6.0)
        self.passes = Value(1)

    def apply_layer(self, layer):
        radius = int(round(_num(self.radius, self.local_frame) * max(0.02, float(self.param_scale))))
        if radius < 1:
            return layer
        k = radius * 2 + 1
        if fully_opaque(layer):
            # Same identity as the gaussian: no alpha to protect, no float passes.
            data = layer
            for _ in range(max(1, int(self.passes.get()))):
                data = cv2.blur(data, (k, k), borderType=cv2.BORDER_REPLICATE)
            return data
        data = premultiply(layer)
        for _ in range(max(1, int(self.passes.get()))):
            data = cv2.blur(data, (k, k), borderType=cv2.BORDER_REPLICATE)
        return unpremultiply(data, layer)


class DirectionalBlurEffect(LayerEffect):
    key = "directional_blur"
    name = "Directional Blur"
    category = "Blur & Sharpen"

    PARAMS = (
        ParamSpec("length", "Length", "float", 0.0, 200.0, 18.0, step=0.5, unit=" px", pixels=True,
                  help="How far the picture is smeared, in project pixels"),
        ParamSpec("angle", "Angle", "float", -180.0, 180.0, 0.0, step=1.0, unit="\u00b0",
                  help="Direction of the smear: 0 is left to right"),
        ParamSpec("samples", "Samples", "int", 3, 48, 12,
                  help="Copies averaged; more samples is smoother and slower"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.length = Value(18.0)
        self.angle = Value(0.0)
        self.samples = Value(12)

    def apply_layer(self, layer):
        length = _num(self.length, self.local_frame) * max(0.02, float(self.param_scale))
        if length <= 0.25:
            return layer
        count = max(2, int(self.samples.get()))
        angle = math.radians(_num(self.angle, self.local_frame))
        dx, dy = math.cos(angle), math.sin(angle)
        height, width = layer.shape[:2]
        samples = []
        for index in range(count):
            offset = (index / float(count - 1) - 0.5) * length
            matrix = np.array([[1.0, 0.0, dx * offset], [0.0, 1.0, dy * offset]], dtype=np.float32)
            samples.append(_warp(layer, matrix, (width, height)))
        return _average(layer, samples)


class RadialBlurEffect(LayerEffect):
    """Rotational streak around a centre."""

    key = "radial_blur"
    name = "Radial Blur"
    category = "Blur & Sharpen"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 90.0, 8.0, step=0.5, unit="\u00b0",
                  help="Arc of rotation smeared into each pixel"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("samples", "Samples", "int", 3, 48, 12),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(8.0)
        self.center_x = Value(0.5)
        self.center_y = Value(0.5)
        self.samples = Value(12)

    def apply_layer(self, layer):
        amount = _num(self.amount, self.local_frame)
        if abs(amount) <= 0.05:
            return layer
        height, width = layer.shape[:2]
        cx = _num(self.center_x, self.local_frame) * (width - 1)
        cy = _num(self.center_y, self.local_frame) * (height - 1)
        count = max(2, int(self.samples.get()))
        samples = []
        for index in range(count):
            angle = math.radians(amount) * (index / float(count - 1) - 0.5)
            matrix = cv2.getRotationMatrix2D((cx, cy), math.degrees(angle), 1.0)
            samples.append(_warp(layer, matrix, (width, height)))
        return _average(layer, samples)


class ZoomBlurEffect(LayerEffect):
    key = "zoom_blur"
    name = "Zoom Blur"
    category = "Blur & Sharpen"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 10.0, step=0.5, unit="%",
                  help="How much the picture is scaled into each pixel"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("samples", "Samples", "int", 3, 48, 12),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(10.0)
        self.center_x = Value(0.5)
        self.center_y = Value(0.5)
        self.samples = Value(12)

    def apply_layer(self, layer):
        amount = _num(self.amount, self.local_frame) / 100.0
        if abs(amount) <= 0.0005:
            return layer
        height, width = layer.shape[:2]
        cx = _num(self.center_x, self.local_frame) * (width - 1)
        cy = _num(self.center_y, self.local_frame) * (height - 1)
        count = max(2, int(self.samples.get()))
        samples = []
        for index in range(count):
            # Split the zoom around 1.0 so the streak is centred on the real picture.
            scale = 1.0 + amount * (index / float(count - 1) - 0.5)
            scale = max(0.05, scale)
            matrix = np.array([[scale, 0.0, cx * (1.0 - scale)],
                               [0.0, scale, cy * (1.0 - scale)]], dtype=np.float32)
            samples.append(_warp(layer, matrix, (width, height)))
        return _average(layer, samples)


class MotionBlurEffect(LayerEffect):
    """Shutter-style motion blur, optionally driven by the clip's own animation."""

    key = "motion_blur"
    name = "Motion Blur"
    category = "Blur & Sharpen"

    PARAMS = (
        ParamSpec("shutter", "Shutter angle", "float", 0.0, 360.0, 180.0, step=1.0, unit="\u00b0",
                  help="180\u00b0 is a film camera; a longer angle smears further"),
        ParamSpec("samples", "Samples", "int", 2, 48, 10),
        ParamSpec("follow_motion", "Follow the animation", "bool", default=True,
                  help="Use the clip's animated position to find the direction and length"),
        ParamSpec("angle", "Manual angle", "float", -180.0, 180.0, 0.0, step=1.0, unit="\u00b0",
                  help="Used when the clip is not animated"),
        ParamSpec("length", "Manual length", "float", 0.0, 200.0, 0.0, step=0.5, unit=" px",
                  pixels=True, help="Extra smear added to the measured motion"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.shutter = Value(180.0)
        self.samples = Value(10)
        self.follow_motion = Value(True)
        self.angle = Value(0.0)
        self.length = Value(0.0)

    # ---------------------------------------------------------------- motion
    def _motion(self):
        """(dx, dy) in project pixels for one frame of the clip's own movement."""
        if not self.follow_motion.get():
            return 0.0, 0.0
        clip = getattr(self, "clip", None)
        if clip is None:
            return 0.0, 0.0
        try:
            from ..model import transform as T
            frame = int(self.local_frame)
            here = T.evaluate(clip, frame)
            before = T.evaluate(clip, max(0, frame - 1))
        except Exception:
            return 0.0, 0.0
        return float(here.position_x - before.position_x), float(here.position_y - before.position_y)

    def apply_layer(self, layer):
        shutter = max(0.0, min(360.0, _num(self.shutter, self.local_frame))) / 360.0
        dx, dy = self._motion()
        manual = _num(self.length, self.local_frame)
        if manual > 0.05:
            angle = math.radians(_num(self.angle, self.local_frame))
            dx += math.cos(angle) * manual
            dy += math.sin(angle) * manual
        dx *= shutter
        dy *= shutter
        length = math.hypot(dx, dy) * max(0.02, float(self.param_scale))
        if length <= 0.3:
            return layer
        count = max(2, int(self.samples.get()))
        height, width = layer.shape[:2]
        samples = []
        for index in range(count):
            # The streak covers the movement that happened during the shutter window.
            offset = (index / float(count - 1) - 0.5)
            matrix = np.array([[1.0, 0.0, dx * offset * max(0.02, float(self.param_scale))],
                               [0.0, 1.0, dy * offset * max(0.02, float(self.param_scale))]],
                              dtype=np.float32)
            samples.append(_warp(layer, matrix, (width, height)))
        return _average(layer, samples)


BLUR_EFFECTS = (GaussianBlurEffect, BoxBlurEffect, DirectionalBlurEffect, RadialBlurEffect,
                ZoomBlurEffect, MotionBlurEffect)
