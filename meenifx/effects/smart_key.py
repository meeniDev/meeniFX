"""Smart Key: one-click background keying from the picture's own edge.

Smart Key is deliberately different from Chroma Key. Chroma Key asks for a screen colour;
Smart Key samples the border of the layer, ignores transparent pixels, and uses the median
colour as the screen. That makes it useful for footage where the exact green/blue value is
unknown or changes slightly across the shot, without adding a colour-picker step.

The matte itself uses the same soft YCbCr distance as Chroma Key, so hair, motion blur and
compression noise stay fractional instead of becoming a hard binary cutout. Auto sampling
can be switched off to fall back to the standard green key, which makes the control useful
for diagnosing a shot as well as for ordinary one-click keying.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..model.values import Value
from .base import ParamSpec
from .chroma import (ChromaKeyEffect, DEFAULT_KEY_RGB, MATTE_LABELS, MATTE_MODES,
                     QUALITIES, QUALITY_LABELS, _num)


class SmartKeyEffect(ChromaKeyEffect):
    """Key a screen whose colour is estimated from the layer's border."""

    key = "smart_key"
    name = "Smart Key"
    category = "Keying & Masking"
    NOTE = ("Samples the edge of the picture, so it works best on a fairly even screen. "
            "If the subject touches the edge, use Chroma Key or paint a Roto Brush mask.")

    PARAMS = (
        ParamSpec("tolerance", "Tolerance", "float", 0.0, 100.0, 30.0, step=1.0, unit="%",
                  help="How much colour variation around the sampled screen is background"),
        ParamSpec("similarity", "Softness", "float", 0.0, 100.0, 24.0, step=1.0, unit="%",
                  help="Width of the soft transition between background and foreground"),
        ParamSpec("edge_softness", "Edge softness", "float", 0.0, 20.0, 1.5, step=0.1,
                  unit=" px", pixels=True, help="Soften the finished matte edge"),
        ParamSpec("spill", "Spill suppression", "float", 0.0, 100.0, 38.0, step=1.0,
                  unit="%", help="Remove the sampled screen colour reflected onto edges"),
        ParamSpec("cleanup", "Matte cleanup", "float", 0.0, 100.0, 12.0, step=1.0,
                  unit="%", help="Remove speckles and close small holes in the matte"),
        ParamSpec("choke", "Erode / expand", "float", -30.0, 30.0, 0.0, step=0.5,
                  unit=" px", pixels=True,
                  help="Positive pulls the matte inward; negative grows it outward"),
        ParamSpec("transparency", "Transparency", "float", 0.0, 100.0, 100.0, step=1.0,
                  unit="%", help="How much of the keyed background is removed"),
        ParamSpec("sample_margin", "Sample edge", "float", 0.0, 30.0, 8.0, step=1.0,
                  unit="%", help="Width of the border band used to estimate the screen"),
        ParamSpec("auto_key", "Auto sample", "bool", default=True,
                  help="Estimate the screen colour from the border; off falls back to green"),
        ParamSpec("matte_view", "Matte view", "choice", default="composite",
                  choices=MATTE_MODES, choice_labels=MATTE_LABELS,
                  help="Inspect the key or the spill result instead of the composite"),
        ParamSpec("quality", "Quality", "choice", default="balanced",
                  choices=QUALITIES, choice_labels=QUALITY_LABELS,
                  help="Draft solves at half resolution; High adds a final cleanup pass"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.tolerance = Value(30.0)
        self.similarity = Value(24.0)
        self.edge_softness = Value(1.5)
        self.spill = Value(38.0)
        self.cleanup = Value(12.0)
        self.choke = Value(0.0)
        self.transparency = Value(100.0)
        self.sample_margin = Value(8.0)
        self.auto_key = Value(True)
        self.matte_view = Value("composite")
        self.quality = Value("balanced")
        # Kept for the inherited manual-key fallback and for project compatibility.
        self.key_r = Value(DEFAULT_KEY_RGB[0])
        self.key_g = Value(DEFAULT_KEY_RGB[1])
        self.key_b = Value(DEFAULT_KEY_RGB[2])
        self._source_layer = None

    def apply_layer(self, layer):
        """Make the current layer available to auto sampling, then run the soft key."""
        self._source_layer = layer
        try:
            return super().apply_layer(layer)
        finally:
            self._source_layer = None

    def _key_rgb(self, frame):
        if not bool(self.auto_key.get()) or self._source_layer is None:
            return super()._key_rgb(frame)
        layer = self._source_layer
        if layer is None or layer.ndim != 3 or layer.shape[2] < 4:
            return super()._key_rgb(frame)
        height, width = layer.shape[:2]
        if height < 2 or width < 2:
            return super()._key_rgb(frame)
        margin = max(1, int(round(min(height, width) * max(0.0, _num(self.sample_margin,
                                                                    frame)) / 100.0)))
        pixels = layer[:, :, :3][layer[:, :, 3] >= 128]
        if pixels.size == 0:
            return super()._key_rgb(frame)
        # The border is a better estimate than the whole frame: a foreground object in the
        # middle must not pull the sampled screen towards its own colour.
        border = np.concatenate([
            layer[:margin, :, :3].reshape(-1, 3),
            layer[-margin:, :, :3].reshape(-1, 3),
            layer[:, :margin, :3].reshape(-1, 3),
            layer[:, -margin:, :3].reshape(-1, 3),
        ], axis=0)
        border_alpha = np.concatenate([
            layer[:margin, :, 3].reshape(-1),
            layer[-margin:, :, 3].reshape(-1),
            layer[:, :margin, 3].reshape(-1),
            layer[:, -margin:, 3].reshape(-1),
        ], axis=0)
        samples = border[border_alpha >= 128]
        if samples.size < 3:
            samples = pixels
        # The layer is BGR; ChromaKeyEffect._key_rgb speaks RGB and reverses internally.
        return np.median(samples.astype(np.float32), axis=0)[::-1]
