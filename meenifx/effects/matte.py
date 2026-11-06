"""Simple Choker: grow or shrink a layer's visible alpha edge.

The matte is turned into a signed Euclidean distance field around its 50 % contour. Moving
that contour inwards or outwards and rebuilding a one-pixel antialiased ramp around it chokes
the edge at any value, including fractional pixels, instead of leaving the stair-stepping a
morphology kernel produces at small amounts.

Sign convention: **positive Choke pulls the edge inward, so the visible matte shrinks;
negative Choke pushes it outward, so the matte grows.**

Colour is never scaled by alpha: layers here carry straight (non-premultiplied) BGRA, so
painting colour into a transparent pixel cannot darken a composited edge. When the matte
grows, the new ring takes the nearest opaque colour, which is what keeps a grown edge free
of the black rim that copying RGB=0 would produce around a rendered shape or text.
"""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number

# Edge quality is the accuracy of the distance field, the axis that decides where a
# fractional choke lands. Fast solves at half resolution (cheapest, edges rounded by up to
# half a working pixel), Balanced and Precise solve at layer resolution with a 3x3 and a
# 5x5 chamfer mask. All three keep the same geometry, so switching never jumps a look.
_QUALITY = {
    "fast": (cv2.DIST_MASK_3, 0.5),
    "balanced": (cv2.DIST_MASK_3, 1.0),
    "precise": (cv2.DIST_MASK_5, 1.0),
}

# Results are cached per parameter set and matte pixels: a still frame, a paused playhead
# and the exporter all ask for the same answer, so the distance transform runs once.
_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_CACHE_LIMIT = 3
_CACHE_BUDGET = 64 * 1024 * 1024
_CACHE_BYTES = 0


def clear_cache():
    """Drop cached edge results (tests and cache audits use this)."""
    global _CACHE_BYTES
    _CACHE.clear()
    _CACHE_BYTES = 0


def _remember(key, value):
    global _CACHE_BYTES
    _CACHE[key] = value
    _CACHE_BYTES += value[0].nbytes + value[1].nbytes
    while _CACHE and (len(_CACHE) > _CACHE_LIMIT or _CACHE_BYTES > _CACHE_BUDGET):
        if len(_CACHE) == 1:
            # A single result larger than the budget is still better kept than recomputed
            # on the next preview frame; the next distinct key evicts it.
            break
        _old_key, old = _CACHE.popitem(last=False)
        _CACHE_BYTES -= old[0].nbytes + old[1].nbytes


class SimpleChokerEffect(LayerEffect):
    """Choke a matte in or out, keeping its antialiasing and its colours."""

    key = "simple_choker"
    name = "Simple Choker"
    category = "Matte / Keying"
    NOTE = ("Works on an alpha edge: shapes, text, transparent PNGs and mattes. Fully "
            "opaque footage has no edge to choke, so the picture is left untouched.")

    PARAMS = (
        ParamSpec("choke", "Choke", "float", -40.0, 40.0, 6.0, step=0.5, unit=" px",
                  pixels=True,
                  help="Positive pulls the matte inward, negative pushes it outward"),
        ParamSpec("softness", "Edge softness", "float", 0.0, 40.0, 0.0, step=0.5, unit=" px",
                  pixels=True, help="Width of the transition band at the new edge"),
        ParamSpec("quality", "Edge quality", "choice",
                  choices=("fast", "balanced", "precise"),
                  choice_labels=("Fast", "Balanced", "Precise"), default="balanced",
                  advanced=True,
                  help="Distance-field accuracy; Fast solves at half resolution"),
        ParamSpec("preserve_rgb", "Preserve original RGB", "bool", default=True,
                  advanced=True,
                  help="Keep the source colour under the matte; turn off to rebuild colour "
                       "at partly transparent edges"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.choke = Value(6.0)
        self.softness = Value(0.0)
        self.quality = Value("balanced")
        self.preserve_rgb = Value(True)
        # Why the last render was left alone, so a caller can explain an inactive effect.
        self.last_note = ""

    # ---------------------------------------------------------------- the effect
    def apply_layer(self, layer):
        self.last_note = ""
        if layer is None or getattr(layer, "ndim", 0) != 3 or layer.shape[2] < 4:
            return layer
        if layer.size == 0:
            return layer
        frame = int(self.local_frame)
        scale = max(1e-6, float(self.param_scale))
        choke = float(param_number(self.choke, frame)) * scale
        softness = max(0.0, float(param_number(self.softness, frame)) * scale)
        quality = str(self.quality.get() or "balanced")
        preserve = bool(self.preserve_rgb.get())
        if abs(choke) < 0.01 and softness < 0.01:
            # Neutral: return the caller's array untouched, byte for byte.
            return layer

        alpha = layer[:, :, 3]
        if int(alpha.max()) == 0:
            self.last_note = "The layer is fully transparent, so there is no edge to choke."
            return layer
        if int(alpha.min()) == 255:
            self.last_note = ("This layer has no transparency, so there is no alpha edge to "
                              "choke. Mask it or apply the choker to a matte first.")
            return layer

        rows = np.flatnonzero(alpha.any(axis=1))
        cols = np.flatnonzero(alpha.any(axis=0))
        if not rows.size or not cols.size:
            return layer

        # Work only around the matte, with enough transparent margin for the edge to move
        # outward. Growth is computed against that padding, so the distance field never
        # believes the crop is the end of the picture.
        margin = int(math.ceil(max(0.0, -choke) + softness + 2.0))
        height, width = alpha.shape
        y0 = max(0, int(rows[0]) - margin)
        y1 = min(height, int(rows[-1]) + 1 + margin)
        x0 = max(0, int(cols[0]) - margin)
        x1 = min(width, int(cols[-1]) + 1 + margin)
        crop = np.ascontiguousarray(layer[y0:y1, x0:x1])

        key = (crop.shape, round(choke, 4), round(softness, 4), quality, preserve,
               hashlib.blake2b(crop.data, digest_size=12).digest())
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            colour, edge = cached
        else:
            colour, edge = self._choke(crop, choke, softness, quality, preserve)
            _remember(key, (colour, edge))

        out = layer.copy()
        region = out[y0:y1, x0:x1]
        region[:, :, :3] = colour
        region[:, :, 3] = edge
        return out

    def _choke(self, crop, choke, softness, quality, preserve):
        """(colour, alpha) crops for one matte, both uint8 and already the final values."""
        alpha = crop[:, :, 3]
        height, width = alpha.shape
        # 0/255, not 0/1: the Fast path averages this mask down before thresholding, and a
        # 0/1 mask averages to zero, which would report an empty matte and choke everything.
        solid = np.where(alpha >= 128, 255, 0).astype(np.uint8)
        mask_size, resolution = _QUALITY.get(quality, _QUALITY["balanced"])
        reach = max(2, int(math.ceil(max(0.0, -choke) + softness + 3.0)))
        # The border makes the end of the layer a real edge: a full-frame matte is choked
        # against the frame, and a growth that leaves the frame simply has nowhere to go.
        mask = cv2.copyMakeBorder(solid, reach, reach, reach, reach,
                                  cv2.BORDER_CONSTANT, value=0)
        if resolution < 1.0:
            mask = cv2.resize(mask, (max(1, int(round(mask.shape[1] * resolution))),
                                     max(1, int(round(mask.shape[0] * resolution)))),
                              interpolation=cv2.INTER_AREA)
            mask = (mask >= 128).astype(np.uint8)
        # `1 - mask` on a 0/255 mask wraps in uint8, so the inverse is built explicitly.
        inverted = (mask == 0).astype(np.uint8)
        inside = cv2.distanceTransform(mask, cv2.DIST_L2, mask_size)
        outside = cv2.distanceTransform(inverted, cv2.DIST_L2, mask_size)
        distance = inside - outside
        if resolution < 1.0:
            # Reduced-grid distances are in working pixels; scale them back to layer pixels.
            distance = cv2.resize(distance, (width + 2 * reach, height + 2 * reach),
                                  interpolation=cv2.INTER_LINEAR) / resolution
        distance = distance[reach:reach + height, reach:reach + width]

        # The source edge is reconstructed from the same field, so a neutral choke leaves
        # the original ramp exactly as it was and only the requested move changes alpha.
        ramp = max(1.0, softness)
        old_edge = np.clip(distance + 0.5, 0.0, 1.0)
        new_edge = np.clip((distance - choke) / ramp + 0.5, 0.0, 1.0)
        if softness > 1.0:
            new_edge = new_edge * new_edge * (3.0 - 2.0 * new_edge)

        source = alpha.astype(np.float32) / 255.0
        edge_alpha = np.clip(source * (new_edge / np.maximum(old_edge, 1e-3)), 0.0, 1.0)
        if choke < 0.0:
            # Growth invents coverage outside the source matte, at the opacity the matte
            # itself reaches: a half-transparent layer grows at half opacity instead of
            # turning into a solid rim.
            peak = float(source.max())
            edge_alpha = np.maximum(edge_alpha, np.clip(new_edge - old_edge, 0.0, 1.0) * peak)
        edge = np.clip(np.rint(edge_alpha * 255.0), 0, 255).astype(np.uint8)

        if choke >= 0.0 and preserve:
            return np.ascontiguousarray(crop[:, :, :3]), edge
        bleed = self._bleed(crop, solid, reach)
        # Only pixels the source matte does not fully cover are rebuilt, so a preserved
        # picture stays pixel-identical wherever it was visible.
        keep = (source > 0.0) if preserve else (source >= 0.999)
        colour = np.where(keep[:, :, None], crop[:, :, :3], bleed)
        return np.ascontiguousarray(colour), edge

    @staticmethod
    def _bleed(crop, solid, reach):
        """Nearest opaque colour smeared outward over `reach` pixels.

        A normalised box blur of the premultiplied colour: transparent pixels receive the
        average of the opaque pixels within reach and never a black contribution, so a grown
        edge carries the colour next to it rather than a dark halo.
        """
        size = (int(reach) * 2 + 1, int(reach) * 2 + 1)
        cover = cv2.blur(solid.astype(np.float32), size)
        weighted = cv2.blur(crop[:, :, :3].astype(np.float32) * solid[:, :, None].astype(np.float32),
                            size)
        return np.clip(weighted / np.maximum(cover, 1e-3)[:, :, None], 0, 255).astype(np.uint8)
