"""Tracked-region masks and the picture effects applied inside them.

A tracker can restrict an effect to the object it found: invert a hand, blur a face,
pixelate a blob.  This module owns the mask geometry (expand, feather and temporal
smoothing support) and the small, real effect set used by every tracker.  It never
invents a region: callers pass a detection and its mask is derived from the contour or
box that the detector returned.

Effects are applied on the mask's bounding crop, not the whole frame, and are composited
back through the feathered mask.  That keeps an invert or pixelate inside one face cheap
even on a 4K frame.
"""

import cv2
import numpy as np

REGION_EFFECTS = ("none", "invert", "blur", "pixelate", "glow", "color")
REGION_LABELS = ("None", "Invert", "Blur", "Pixelate", "Glow", "Colour pop")

# A face's custom stack uses the same vocabulary, optionally with an amount:
# "blur:60, pixelate:40".
def parse_stack(text):
    stack = []
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, _sep, amount = part.partition(":")
        name = name.strip().lower().replace(" ", "_")
        if name not in REGION_EFFECTS or name == "none":
            continue
        try:
            value = float(amount) if amount else 50.0
        except ValueError:
            value = 50.0
        stack.append((name, max(1.0, min(100.0, value))))
    return stack


def _contour_mask(shape, contour):
    height, width = shape[:2]
    mask = np.zeros((height, width), np.uint8)
    try:
        points = np.asarray(contour, dtype=np.float32).reshape(-1, 2)
    except (TypeError, ValueError):
        return mask
    if len(points) < 3:
        return mask
    cv2.fillPoly(mask, [np.round(points).astype(np.int32)], 255, lineType=cv2.LINE_AA)
    return mask


def _box_mask(shape, box, ellipse=False):
    height, width = shape[:2]
    mask = np.zeros((height, width), np.uint8)
    x, y, w, h = [int(round(float(value))) for value in box]
    if ellipse:
        cv2.ellipse(mask, (x + w // 2, y + h // 2), (max(1, w // 2), max(1, h // 2)),
                    0, 0, 360, 255, -1, cv2.LINE_AA)
    else:
        cv2.rectangle(mask, (x, y), (x + w, y + h), 255, -1, cv2.LINE_AA)
    return mask


def region_mask(shape, box, contour=None, expand=0, feather=0, ellipse=False):
    """A 0..255 mask for one tracked region.

    ``expand`` grows (positive) or shrinks (negative) the region in pixels.  ``feather``
    softens the edge, which is what keeps a tracked blur from showing a hard rectangle.
    """
    mask = _contour_mask(shape, contour) if contour is not None else np.zeros(shape[:2], np.uint8)
    if not mask.any():
        mask = _box_mask(shape, box, ellipse=ellipse)
    expand = int(round(float(expand or 0)))
    if expand:
        size = max(1, abs(expand) * 2 + 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        mask = (cv2.dilate(mask, kernel, iterations=1) if expand > 0
                else cv2.erode(mask, kernel, iterations=1))
    feather = int(round(float(feather or 0)))
    if feather > 0:
        mask = cv2.GaussianBlur(mask, (0, 0), max(0.5, feather / 2.0))
    return mask


def _blur_radius(amount, scale=1.0):
    return max(1.0, float(amount) / 100.0 * 18.0 * max(0.2, float(scale)))


def apply_effect(patch, name, amount, scale=1.0):
    """One real picture effect on a crop; returns a new image."""
    name = str(name or "none").strip().lower()
    if name == "none" or patch.size == 0:
        return patch
    amount = max(1.0, min(100.0, float(amount)))
    if name == "invert":
        strength = amount / 100.0
        return (patch.astype(np.float32) * (1.0 - strength)
                + (255 - patch).astype(np.float32) * strength).astype(np.uint8)
    if name == "blur":
        radius = _blur_radius(amount, scale)
        return cv2.GaussianBlur(patch, (0, 0), radius)
    if name == "pixelate":
        block = max(2, int(round(amount / 100.0 * max(patch.shape[:2]) / 7.0)))
        small = cv2.resize(patch, (max(1, patch.shape[1] // block),
                                   max(1, patch.shape[0] // block)),
                           interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (patch.shape[1], patch.shape[0]),
                          interpolation=cv2.INTER_NEAREST)
    if name == "glow":
        blur = cv2.GaussianBlur(patch, (0, 0), _blur_radius(amount, scale) * 1.6)
        return cv2.addWeighted(patch, 1.0, blur, amount / 100.0 * 1.2, 0).astype(np.uint8)
    if name == "color":
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (1.0 + amount / 100.0), 0, 255)
        hsv[:, :, 2] = np.clip(hsv[:, :, 2] + amount * 0.35, 0, 255)
        return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
    return patch


def apply_stack(image, mask, stack, scale=1.0):
    """Apply ``[(effect, amount)]`` inside ``mask`` on a copy of ``image``."""
    stack = [(name, amount) for name, amount in stack or ()
             if str(name).lower() not in ("", "none")]
    if not stack:
        return image
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return image
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    patch = image[y0:y1, x0:x1]
    local = mask[y0:y1, x0:x1]
    affected = patch.copy()
    for name, amount in stack:
        affected = apply_effect(affected, name, amount, scale)
    alpha = local.astype(np.float32)[:, :, None] / 255.0
    image[y0:y1, x0:x1] = (affected.astype(np.float32) * alpha
                           + patch.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)
    return image
