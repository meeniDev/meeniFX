"""Rasterise and combine clip masks on a BGRA layer.

Masks are project-space polygons.  The renderer samples their cubic paths at the current
render scale, builds a coverage mask, optionally feathers and expands it, then combines it
using the ordinary After Effects-style operations: Add, Subtract, Intersect and Difference.
All arithmetic is done on a float coverage map so overlapping feathered edges do not
quantise to 0/255 and preview/export agree.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..model import path as PATHS


def _polygon(mask, width, height, scale):
    points = PATHS.sampled(mask.get("path") or {}, steps=max(8, 16))
    if len(points) < 2:
        return None
    return (np.round(np.asarray(points, np.float32) * float(scale)).astype(np.int32),
            bool((mask.get("path") or {}).get("closed", True)))


def _coverage(mask, width, height, scale):
    polygon = _polygon(mask, width, height, scale)
    if polygon is None:
        return None
    points, closed = polygon
    canvas = np.zeros((height, width), dtype=np.float32)
    if closed and len(points) >= 3:
        cv2.fillPoly(canvas, [points], 1.0, lineType=cv2.LINE_AA)
    else:
        width_px = max(1, int(round(2.0 * float(scale))))
        cv2.polylines(canvas, [points], False, 1.0, width_px, lineType=cv2.LINE_AA)

    expansion = float(mask.get("expansion", 0.0) or 0.0) * float(scale)
    if abs(expansion) >= 0.25:
        radius = max(1, int(round(abs(expansion))))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
        if expansion > 0:
            canvas = cv2.dilate(canvas, kernel)
        else:
            canvas = cv2.erode(canvas, kernel)

    feather = max(0.0, float(mask.get("feather", 0.0) or 0.0) * float(scale))
    if feather >= 0.25:
        canvas = cv2.GaussianBlur(canvas, (0, 0), feather * 0.5)
    if mask.get("invert"):
        canvas = 1.0 - canvas
    opacity = max(0.0, min(100.0, float(mask.get("opacity", 100.0) or 0.0))) / 100.0
    return np.clip(canvas * opacity, 0.0, 1.0)


def combined(masks, width, height, scale=1.0):
    """One 0..1 coverage map, or None when there are no masks."""
    result = None
    for mask in masks or ():
        current = _coverage(mask, width, height, scale)
        if current is None:
            continue
        mode = str(mask.get("mode") or "add")
        if result is None:
            if mode == "subtract":
                result = 1.0 - current
            elif mode == "intersect":
                result = current
            elif mode == "difference":
                result = current
            else:
                result = current
            continue
        if mode == "subtract":
            result = result * (1.0 - current)
        elif mode == "intersect":
            result = np.minimum(result, current)
        elif mode == "difference":
            result = np.abs(result - current)
        else:
            result = np.maximum(result, current)
    return result


def apply(layer, masks, scale=1.0):
    """Multiply a BGRA layer's alpha by the combined mask coverage."""
    if layer is None or getattr(layer, "ndim", 0) != 3 or layer.shape[2] < 4:
        return layer
    if not masks:
        return layer
    height, width = layer.shape[:2]
    coverage = combined(masks, width, height, scale)
    if coverage is None:
        return layer
    alpha = layer[:, :, 3].astype(np.float32) / 255.0
    layer[:, :, 3] = np.clip(np.rint(alpha * coverage * 255.0), 0, 255).astype(np.uint8)
    return layer
