"""Brush strokes: what one stroke is, how it is resampled and how it becomes masks.

A stroke is plain JSON on the effect entry, exactly like puppet pins:

    {"frame": 12, "mode": "foreground", "radius": 0.05,
     "points": [[0.41, 0.30], [0.44, 0.52], ...]}

Coordinates are normalised inside the *picture* (0..1 across its width and height), not
the project frame and not the timeline. The engine hands layer effects the picture
rectangle inside the project-sized layer, so a stroke keeps pointing at the same part of
the subject when the clip is moved, scaled or rotated. A `frame` is a *source* frame
index, so trims, source offsets, speed and reverse playback cannot shift the strokes.

The three modes are hints, never paint: they only seed or correct the segmenter.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

MODES = ("foreground", "background", "refine")
DEFAULT_RADIUS = 0.05
# Points are inserted every 35% of a brush radius, so a fast drag is still continuous.
SPACING_FACTOR = 0.35
# A stroke is capped so a runaway drag cannot grow the project JSON without limit.
MAX_POINTS = 4000


def stroke_mode(stroke):
    mode = str((stroke or {}).get("mode") or "foreground").strip().lower()
    return mode if mode in MODES else "foreground"


def stroke_radius(stroke):
    try:
        radius = float((stroke or {}).get("radius", DEFAULT_RADIUS))
    except (TypeError, ValueError):
        radius = DEFAULT_RADIUS
    if not math.isfinite(radius):
        radius = DEFAULT_RADIUS
    return max(0.002, min(0.5, radius))


def _point(point):
    try:
        x, y = float(point[0]), float(point[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))


def stroke_points(stroke):
    """Every valid point of one stroke, clamped into the picture."""
    points = []
    for raw in (stroke or {}).get("points") or ():
        point = _point(raw)
        if point is not None:
            points.append(point)
    return points


def stroke_frame(stroke, base_frame=0):
    """The source frame a stroke belongs to; legacy strokes were painted on the base."""
    try:
        frame = int((stroke or {}).get("frame"))
    except (TypeError, ValueError):
        return int(base_frame or 0)
    return max(0, frame)


def interpolate(points, spacing):
    """Resample a polyline so the segmenter sees an unbroken stroke.

    `spacing` is in the same normalised units as the points. Mouse events arrive at
    whatever rate the platform reports; without this, a fast drag leaves a dotted line.
    """
    if not points:
        return []
    if len(points) < 2:
        return list(points)
    spacing = max(1e-4, float(spacing))
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        distance = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = max(1, min(512, int(math.ceil(distance / spacing))))
        for index in range(1, steps + 1):
            t = index / float(steps)
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        if len(out) >= MAX_POINTS:
            return out[:MAX_POINTS]
    return out


def strokes_for_frame(strokes, frame, base_frame=0):
    """Strokes that belong to one source frame.

    A stroke painted on the base frame is the reference; a stroke painted anywhere else
    is a correction for that one frame. Frames with neither have no hints of their own
    and must come from the mask cache (propagation) instead of being faked.
    """
    frame = int(frame)
    base = int(base_frame or 0)
    out = []
    for stroke in strokes or ():
        if not isinstance(stroke, dict):
            continue
        if stroke_frame(stroke, base) == frame:
            out.append(stroke)
    return out


def rasterise(strokes, width, height, rect, points_override=None):
    """(foreground, background, refine) uint8 masks in layer pixels.

    `rect` is (x, y, w, h): where the picture sits inside the layer. Strokes are
    normalised to that rectangle, which is what keeps them glued to the transformed
    picture at every zoom level and preview quality.
    """
    foreground = np.zeros((height, width), np.uint8)
    background = np.zeros((height, width), np.uint8)
    refine = np.zeros((height, width), np.uint8)
    if not width or not height:
        return foreground, background, refine
    x0, y0, rect_w, rect_h = (float(value) for value in rect)
    overlap = max(1.0, min(rect_w, rect_h))
    for stroke in strokes or ():
        if not isinstance(stroke, dict):
            continue
        mode = stroke_mode(stroke)
        points = points_override if points_override is not None else stroke_points(stroke)
        if not points:
            continue
        radius = stroke_radius(stroke)
        radius_px = max(1.0, radius * overlap)
        target = {"foreground": foreground, "background": background,
                  "refine": refine}[mode]
        pixels = [(x0 + point[0] * rect_w, y0 + point[1] * rect_h) for point in points]
        dense = interpolate(pixels, spacing=radius_px * SPACING_FACTOR)
        thickness = max(1, int(round(radius_px * 2.0)))
        for index in range(max(0, len(dense) - 1)):
            a = (int(round(dense[index][0])), int(round(dense[index][1])))
            b = (int(round(dense[index + 1][0])), int(round(dense[index + 1][1])))
            cv2.line(target, a, b, 255, thickness, cv2.LINE_AA)
        for point in (dense[0], dense[-1]):
            cv2.circle(target, (int(round(point[0])), int(round(point[1]))),
                       int(round(radius_px)), 255, -1, cv2.LINE_AA)
    # A background stroke is an explicit correction: it wins where the two overlap.
    foreground[background > 0] = 0
    refine[background > 0] = 0
    refine[foreground > 0] = 0
    return foreground, background, refine
