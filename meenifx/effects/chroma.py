"""Chroma Key: a soft YCbCr matte with spill suppression and edge shaping.

The key is a colour *distance* in chroma space, not a hard green-pixel test. Tolerance
sets the core band, Similarity widens the transition, and Edge Softness, Cleanup and
Choke work on the matte afterward. That keeps hair, motion blur and compression noise
as fractional alpha instead of turning them into a jagged binary cutout.

The effect works on BGRA layers, so transparent source areas stay transparent. Matte
views are diagnostic renders; Composite and Spill Preview preserve the source alpha.
"""

from __future__ import annotations

import hashlib

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec

MATTE_MODES = ("composite", "alpha", "black_white", "spill")
MATTE_LABELS = ("Composite", "Alpha matte", "Black / white matte", "Spill preview")
QUALITIES = ("draft", "balanced", "high")
QUALITY_LABELS = ("Draft", "Balanced", "High")

# Default is a saturated chroma green; the properties panel also offers Blue.
DEFAULT_KEY_RGB = (0, 177, 64)


def _num(value, frame=0):
    from ..model.keyframes import AnimatedValue, parameter_value_at
    if isinstance(value, AnimatedValue):
        return float(parameter_value_at(value, frame))
    if hasattr(value, "get"):
        return float(value.get())
    return float(value)


def _smoothstep(edge0, edge1, value):
    width = max(1e-5, float(edge1) - float(edge0))
    t = np.clip((value - float(edge0)) / width, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


class ChromaKeyEffect(LayerEffect):
    key = "chroma_key"
    name = "Chroma Key"
    category = "Keying & Masking"
    KEY_COLOR_NAMES = ("key_r", "key_g", "key_b")

    PARAMS = (
        ParamSpec("tolerance", "Tolerance", "float", 0.0, 100.0, 32.0, step=1.0, unit="%",
                  help="How much colour variation around the key is treated as background"),
        ParamSpec("similarity", "Similarity", "float", 0.0, 100.0, 25.0, step=1.0, unit="%",
                  help="Width of the soft transition between background and foreground"),
        ParamSpec("edge_softness", "Edge softness", "float", 0.0, 20.0, 1.5, step=0.1,
                  unit=" px", pixels=True, help="Soften the finished matte edge"),
        ParamSpec("spill", "Spill suppression", "float", 0.0, 100.0, 40.0, step=1.0,
                  unit="%", help="Remove the key colour reflected onto foreground edges"),
        ParamSpec("cleanup", "Matte cleanup", "float", 0.0, 100.0, 12.0, step=1.0,
                  unit="%", help="Remove speckles and close small holes in the matte"),
        ParamSpec("choke", "Erode / expand", "float", -30.0, 30.0, 0.0, step=0.5,
                  unit=" px", pixels=True,
                  help="Positive pulls the matte inward; negative grows it outward"),
        ParamSpec("transparency", "Transparency", "float", 0.0, 100.0, 100.0, step=1.0,
                  unit="%", help="How much of the keyed background is removed"),
        ParamSpec("matte_view", "Matte view", "choice", default="composite",
                  choices=MATTE_MODES, choice_labels=MATTE_LABELS,
                  help="Inspect the key or the spill result instead of the composite"),
        ParamSpec("quality", "Quality", "choice", default="balanced",
                  choices=QUALITIES, choice_labels=QUALITY_LABELS,
                  help="Draft solves at half resolution; High adds a final cleanup pass"),
        ParamSpec("key_r", "Key red", "int", 0, 255, DEFAULT_KEY_RGB[0], advanced=True,
                  help="Red channel of the screen colour"),
        ParamSpec("key_g", "Key green", "int", 0, 255, DEFAULT_KEY_RGB[1], advanced=True,
                  help="Green channel of the screen colour"),
        ParamSpec("key_b", "Key blue", "int", 0, 255, DEFAULT_KEY_RGB[2], advanced=True,
                  help="Blue channel of the screen colour"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.tolerance = Value(32.0)
        self.similarity = Value(25.0)
        self.edge_softness = Value(1.5)
        self.spill = Value(40.0)
        self.cleanup = Value(12.0)
        self.choke = Value(0.0)
        self.transparency = Value(100.0)
        self.matte_view = Value("composite")
        self.quality = Value("balanced")
        self.key_r = Value(DEFAULT_KEY_RGB[0])
        self.key_g = Value(DEFAULT_KEY_RGB[1])
        self.key_b = Value(DEFAULT_KEY_RGB[2])
        self._cache_key = None
        self._cache_output = None

    def _key_rgb(self, frame):
        return np.array([_num(self.key_r, frame), _num(self.key_g, frame),
                         _num(self.key_b, frame)], dtype=np.float32)

    def _matte(self, layer, frame, working_scale):
        rgb = layer[:, :, :3]
        height, width = rgb.shape[:2]
        factor = 1.0
        if str(self.quality.get()) == "draft":
            factor = 0.5
            rgb = cv2.resize(rgb, (max(1, int(round(width * factor))),
                                   max(1, int(round(height * factor)))),
                             interpolation=cv2.INTER_AREA)

        ycc = cv2.cvtColor(rgb, cv2.COLOR_BGR2YCrCb).astype(np.float32)
        key_rgb = self._key_rgb(frame)
        key_bgr = key_rgb[::-1].reshape(1, 1, 3).astype(np.uint8)
        key_ycc = cv2.cvtColor(key_bgr, cv2.COLOR_BGR2YCrCb)[0, 0].astype(np.float32)
        chroma_dist = np.sqrt((ycc[:, :, 2] - key_ycc[2]) ** 2 +
                              (ycc[:, :, 1] - key_ycc[1]) ** 2) / 128.0
        luma_dist = np.abs(ycc[:, :, 0] - key_ycc[0]) / 255.0
        key_chroma = np.sqrt(key_ycc[1] ** 2 + key_ycc[2] ** 2)
        if key_chroma < 4.0:
            # A grey key has no stable chroma direction; fall back to RGB distance.
            rgb_dist = np.linalg.norm(rgb.astype(np.float32) - key_rgb, axis=2) / 441.0
            distance = np.maximum(chroma_dist, rgb_dist)
        else:
            # Chroma is the main key. A little luma keeps deep screen shadows from
            # surviving as a dark fringe without making a lit green object safe.
            distance = chroma_dist + luma_dist * 0.18

        tolerance = max(0.0, min(1.0, _num(self.tolerance, frame) / 100.0))
        similarity = max(0.0, min(1.0, _num(self.similarity, frame) / 100.0))
        inner = tolerance * 0.58
        transition = max(0.001, similarity * 0.9)
        matte = _smoothstep(inner, inner + transition, distance)
        matte = (np.clip(matte, 0.0, 1.0) * 255.0).astype(np.uint8)

        cleanup = max(0.0, min(100.0, _num(self.cleanup, frame)))
        if cleanup > 0.01:
            size = max(1, int(round(cleanup / 100.0 * 6.0)))
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                               (size * 2 + 1, size * 2 + 1))
            matte = cv2.medianBlur(matte, size * 2 + 1)
            matte = cv2.morphologyEx(matte, cv2.MORPH_CLOSE, kernel)
            matte = cv2.morphologyEx(matte, cv2.MORPH_OPEN, kernel)
        if str(self.quality.get()) == "high":
            matte = cv2.medianBlur(matte, 3)

        choke = _num(self.choke, frame) * working_scale * factor
        amount = int(round(abs(choke)))
        if amount > 0:
            size = min(61, amount * 2 + 1)
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
            if choke > 0:
                matte = cv2.erode(matte, kernel)
            else:
                matte = cv2.dilate(matte, kernel)

        edge = max(0.0, _num(self.edge_softness, frame) * working_scale * factor)
        if edge > 0.01:
            matte = cv2.GaussianBlur(matte, (0, 0), sigmaX=edge / 3.0, sigmaY=edge / 3.0,
                                      borderType=cv2.BORDER_REPLICATE)

        if factor < 1.0:
            matte = cv2.resize(matte, (width, height), interpolation=cv2.INTER_LINEAR)
        return matte.astype(np.float32) / 255.0

    @staticmethod
    def _suppress_spill(rgb, key_rgb, amount):
        strength = max(0.0, min(1.0, float(amount) / 100.0))
        key_vector = key_rgb - float(key_rgb.mean())
        norm = float(np.linalg.norm(key_vector))
        if strength <= 0.001 or norm <= 1e-3:
            return rgb
        direction = key_vector / norm
        values = rgb.astype(np.float32)
        centred = values - values.mean(axis=2, keepdims=True)
        projection = np.einsum("hwc,c->hw", centred, direction, optimize=True)
        excess = np.maximum(projection, 0.0) * strength
        return np.clip(values - excess[:, :, None] * direction, 0.0, 255.0)

    def apply_layer(self, layer):
        frame = self.local_frame
        key_rgb = self._key_rgb(frame)
        values = (
            round(_num(self.tolerance, frame), 3), round(_num(self.similarity, frame), 3),
            round(_num(self.edge_softness, frame), 3), round(_num(self.spill, frame), 3),
            round(_num(self.cleanup, frame), 3), round(_num(self.choke, frame), 3),
            round(_num(self.transparency, frame), 3), str(self.matte_view.get()),
            str(self.quality.get()), tuple(int(round(v)) for v in key_rgb),
            round(float(self.param_scale), 4), layer.shape,
        )
        digest = hashlib.blake2b(layer.tobytes(), digest_size=12).digest()
        cache_key = (values, digest)
        if cache_key == self._cache_key and self._cache_output is not None:
            return self._cache_output

        alpha = layer[:, :, 3].astype(np.float32) / 255.0
        matte = self._matte(layer, frame, float(self.param_scale))
        view = str(self.matte_view.get())
        if view == "spill":
            rgb = self._suppress_spill(layer[:, :, :3], key_rgb, _num(self.spill, frame))
            out_alpha = alpha
        else:
            transparency = max(0.0, min(1.0, _num(self.transparency, frame) / 100.0))
            out_alpha = alpha * (1.0 - transparency * (1.0 - matte))
            rgb = self._suppress_spill(layer[:, :, :3], key_rgb, _num(self.spill, frame)) \
                if _num(self.spill, frame) > 0.0 else layer[:, :, :3]
            if view == "alpha":
                rgb = np.repeat((matte * 255.0).astype(np.uint8)[:, :, None], 3, axis=2)
                out_alpha = np.ones_like(alpha)
            elif view == "black_white":
                binary = np.where(matte >= 0.5, 255, 0).astype(np.uint8)
                rgb = np.repeat(binary[:, :, None], 3, axis=2)
                out_alpha = np.ones_like(alpha)
            else:
                rgb = np.clip(rgb, 0.0, 255.0).astype(np.uint8)

        out = np.empty_like(layer)
        out[:, :, :3] = rgb
        out[:, :, 3] = np.clip(out_alpha * 255.0, 0.0, 255.0).astype(np.uint8)
        self._cache_key = cache_key
        self._cache_output = out
        return out


CHROMA_EFFECTS = (ChromaKeyEffect,)
