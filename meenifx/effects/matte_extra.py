"""Matte Choker: a softer, iterative companion to Simple Choker.

It blurs the alpha edge, shifts its 50 % contour with a signed distance field and rebuilds
the transition band.  Positive Choke contracts, negative Choke expands.  Colour is preserved
and fully transparent pixels stay transparent; the effect is a no-op on an opaque layer.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec
from .creative_utils import num


class MatteChokerEffect(LayerEffect):
    key = "matte_choker"
    name = "Matte Choker"
    category = "Matte / Keying"
    NOTE = ("Needs an alpha edge: transparent images, text, shapes, a key or a track matte. "
            "An opaque clip has no edge to choke.")

    PARAMS = (
        ParamSpec("choke", "Choke", "float", -40.0, 40.0, 5.0, step=0.5, unit=" px",
                  pixels=True, help="Positive contracts the matte; negative expands it"),
        ParamSpec("feather", "Feather", "float", 0.0, 40.0, 3.0, step=0.5, unit=" px",
                  pixels=True, help="Soften the edge before it is choked"),
        ParamSpec("edge_softness", "Edge softness", "float", 0.5, 40.0, 2.0, step=0.5,
                  unit=" px", pixels=True,
                  help="Width of the rebuilt transition band"),
        ParamSpec("iterations", "Iterations", "int", 1, 6, 2,
                  help="Repeat the choke for a stronger, smoother edge"),
        ParamSpec("close_holes", "Close holes", "bool", default=False,
                  help="Fill small transparent specks inside the matte"),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_layer(self, layer):
        frame = self.local_frame
        if layer is None or getattr(layer, "ndim", 0) != 3 or layer.shape[2] < 4:
            return layer
        alpha = layer[:, :, 3]
        if int(alpha.max()) == 0 or int(alpha.min()) == 255:
            return layer
        choke = num(self.choke, frame) * max(0.02, float(self.param_scale))
        feather = max(0.0, num(self.feather, frame) * max(0.02, float(self.param_scale)))
        softness = max(0.5, num(self.edge_softness, frame) * max(0.02, float(self.param_scale)))
        if abs(choke) < 0.01 and feather < 0.01:
            return layer
        working = alpha.astype(np.float32) / 255.0
        if feather > 0.01:
            working = np.clip(cv2.GaussianBlur(working, (0, 0), feather / 2.0,
                                              borderType=cv2.BORDER_REPLICATE), 0, 1)
        for _ in range(max(1, int(num(self.iterations, frame)))):
            working = self._choke_once(working, choke / max(1, int(num(self.iterations, frame))),
                                       softness)
        if bool(self.close_holes.get()):
            solid = (working >= 0.5).astype(np.uint8)
            contours, _hierarchy = cv2.findContours(solid, cv2.RETR_EXTERNAL,
                                                    cv2.CHAIN_APPROX_SIMPLE)
            filled = np.zeros_like(solid)
            cv2.drawContours(filled, contours, -1, 1, -1)
            working = np.maximum(working, filled.astype(np.float32) * 0.999)
        out = layer.copy()
        out[:, :, 3] = np.clip(np.rint(working * 255.0), 0, 255).astype(np.uint8)
        return out

    @staticmethod
    def _choke_once(alpha, choke, softness):
        height, width = alpha.shape
        reach = max(2, int(math.ceil(max(0.0, -choke) + softness + 2.0)))
        solid = (alpha >= 0.5).astype(np.uint8) * 255
        padded = cv2.copyMakeBorder(solid, reach, reach, reach, reach,
                                    cv2.BORDER_CONSTANT, value=0)
        inverted = (padded == 0).astype(np.uint8)
        inside = cv2.distanceTransform(padded, cv2.DIST_L2, cv2.DIST_MASK_5)
        outside = cv2.distanceTransform(inverted, cv2.DIST_L2, cv2.DIST_MASK_5)
        distance = (inside - outside)[reach:reach + height, reach:reach + width]
        old_edge = np.clip(distance + 0.5, 0.0, 1.0)
        new_edge = np.clip((distance - choke) / max(1.0, softness) + 0.5, 0.0, 1.0)
        edge = np.clip(alpha * (new_edge / np.maximum(old_edge, 1e-3)), 0.0, 1.0)
        if choke < 0.0:
            # Growth must invent coverage outside the original matte; otherwise a
            # negative choke only softens the pixels that were already visible.
            growth = np.clip(new_edge - old_edge, 0.0, 1.0) * float(alpha.max())
            edge = np.maximum(edge, growth)
        return edge
