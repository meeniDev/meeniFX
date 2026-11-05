"""Shared drawing for the Face, Hand and Blob trackers.

The three trackers detect different things, but a user expects one visual language:
the same box, label, point and connector controls no matter which detector produced the
object.  This module owns that language.  It deliberately has no effect or GUI imports so
it can be tested on its own, and it never invents detections - callers pass the objects
their real detector returned.

All colours are OpenCV BGR tuples.  The renderer returns the same frame object when there
is nothing to draw, which keeps a detector-less preview free of an extra copy.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class Detection:
    """One real detection in frame pixels."""

    box: tuple
    contour: object = None
    # Optional explicit points.  When absent, corner points are derived from the box.
    points: list = field(default_factory=list)
    track_id: int = 0
    # A style override per detection is useful for trackers that want the same object to
    # keep one colour while other objects cycle through a palette.
    color: tuple = None


# Presets are resolved by the effect before drawing.  ``custom`` is the only one that
# reads the individual checkboxes; the named presets are a coherent starting style.
TRACKING_STYLES = {
    "custom": {},
    "clean": {"show_box": True, "show_contour": False, "show_points": False,
              "show_circle": False, "show_labels": True, "show_numbers": False,
              "connector_mode": "none", "glow": False, "opacity": 100,
              "box_style": "corners"},
    "minimal": {"show_box": True, "show_contour": False, "show_points": False,
                "show_circle": False, "show_labels": False, "show_numbers": False,
                "connector_mode": "none", "glow": False, "opacity": 100,
                "box_style": "rectangle"},
    "numbered": {"show_box": False, "show_contour": False, "show_points": False,
                 "show_circle": False, "show_labels": False, "show_numbers": True,
                 "connector_mode": "none", "glow": False, "opacity": 100,
                 "box_style": "rectangle"},
    "neon": {"show_box": False, "show_contour": False, "show_points": True,
             "show_circle": False, "show_labels": False, "show_numbers": False,
             "connector_mode": "nearest", "glow": True, "opacity": 90,
             "box_style": "rectangle"},
    "hud": {"show_box": True, "show_contour": True, "show_points": True,
            "show_circle": False, "show_labels": True, "show_numbers": True,
            "connector_mode": "distance", "glow": True, "opacity": 95,
            "box_style": "corners"},
    "points": {"show_box": False, "show_contour": False, "show_points": True,
               "show_circle": False, "show_labels": False, "show_numbers": False,
               "connector_mode": "sequential", "glow": False, "opacity": 100,
               "box_style": "rectangle"},
}

STYLE_KEYS = ("show_box", "show_contour", "show_points", "show_circle", "show_labels",
              "show_numbers", "connector_mode", "glow", "opacity", "box_style")


def resolve_style(preset, manual=None):
    """The effective style for ``preset``.

    A named preset is authoritative so it stays predictable; ``custom`` uses the values
    the UI checkboxes hold.  Connector distance/count are always manual because they
    depend on the footage, not on the look.
    """
    manual = dict(manual or {})
    name = str(preset or "custom").strip().lower()
    if name not in TRACKING_STYLES or name == "custom":
        return manual
    style = dict(manual)
    style.update(TRACKING_STYLES[name])
    return style


def _as_box(detection):
    try:
        x, y, w, h = [int(round(float(value))) for value in detection.box]
    except (TypeError, ValueError):
        return None
    return x, y, max(1, w), max(1, h)


def _center(box):
    x, y, w, h = box
    return x + w // 2, y + h // 2


def _corners(box, length=0.18):
    x, y, w, h = box
    dx = max(2, int(round(min(w, h) * length)))
    dy = dx
    return [
        ((x, y), (x + dx, y)), ((x, y), (x, y + dy)),
        ((x + w, y), (x + w - dx, y)), ((x + w, y), (x + w, y + dy)),
        ((x, y + h), (x + dx, y + h)), ((x, y + h), (x, y + h - dy)),
        ((x + w, y + h), (x + w - dx, y + h)), ((x + w, y + h), (x + w, y + h - dy)),
    ]


def _draw_box(overlay, box, color, thickness, style):
    x, y, w, h = box
    if style == "corners":
        for start, end in _corners(box):
            cv2.line(overlay, start, end, color, thickness, cv2.LINE_AA)
    elif style == "ellipse":
        cv2.ellipse(overlay, (x + w // 2, y + h // 2), (max(1, w // 2), max(1, h // 2)),
                    0, 0, 360, color, thickness, cv2.LINE_AA)
    else:
        cv2.rectangle(overlay, (x, y), (x + w, y + h), color, thickness, cv2.LINE_AA)


def _draw_text(overlay, text, x, y, color, scale, thickness):
    """Text with a dark plate so it stays readable over any picture."""
    scale = max(0.32, float(scale))
    thickness = max(1, int(thickness))
    if not text:
        return
    (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    top = max(0, int(y) - th - baseline - 3)
    left = max(0, int(x))
    cv2.rectangle(overlay, (left, top), (left + tw + 6, top + th + baseline + 4),
                  (18, 18, 18), -1)
    cv2.putText(overlay, text, (left + 3, top + th + 1), cv2.FONT_HERSHEY_SIMPLEX,
                scale, color, thickness, cv2.LINE_AA)


def _sample_points(points, limit=24):
    points = points or []
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    return points[::step]


def _contour_points(contour, limit=24):
    if contour is None:
        return []
    try:
        flat = np.asarray(contour, dtype=np.float32).reshape(-1, 2)
    except (TypeError, ValueError):
        return []
    return _sample_points([(int(round(x)), int(round(y))) for x, y in flat], limit)


def _detection_points(detection, box):
    if detection.points:
        return _sample_points([(int(round(x)), int(round(y))) for x, y in detection.points])
    contour_points = _contour_points(detection.contour)
    if contour_points:
        return contour_points
    x, y, w, h = box
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _connector_pairs(boxes, mode, limit, max_connections):
    """Pairs of centres to connect, capped so 80 blobs cannot make 3160 lines."""
    count = len(boxes)
    if count < 2 or mode == "none":
        return []
    centres = [_center(box) for box in boxes]
    pairs = []
    cap = max(1, int(max_connections)) * count
    for left in range(count):
        lx, ly = centres[left]
        candidates = []
        for right in range(count):
            if right == left:
                continue
            rx, ry = centres[right]
            distance = float(np.hypot(rx - lx, ry - ly))
            if limit > 0 and distance > limit:
                continue
            candidates.append((distance, right))
        if mode == "sequential":
            # Track order is meaningful for blobs and hands; use it even when a gap is
            # longer than the colour-neighbour radius.
            if left + 1 < count:
                pairs.append((left, left + 1))
        elif mode == "nearest":
            if candidates:
                pairs.append((left, min(candidates)[1]))
        else:  # distance: all neighbours within the radius, nearest first
            candidates.sort()
            for _distance, right in candidates[:max(1, int(max_connections))]:
                pairs.append((left, right))
        if len(pairs) >= cap:
            break
    # Draw each undirected pair once; direction does not change the picture.
    seen = set()
    unique = []
    for left, right in pairs:
        key = (min(left, right), max(left, right))
        if key in seen:
            continue
        seen.add(key)
        unique.append((left, right))
    return unique[:cap]


def draw_tracking(frame, detections, *, color=(0, 255, 255), thickness=2, opacity=100,
                  show_box=True, show_contour=False, show_points=False, show_circle=False,
                  show_labels=True, show_numbers=False, label_text="", box_style="rectangle",
                  connector_mode="none", connector_distance=25, max_connections=2,
                  glow=False, colors=None, label_scale=0.55):
    """Draw real detections onto a copy of ``frame`` using the shared visual language.

    ``connector_distance`` is a percentage of the shorter frame edge.  ``opacity`` is 0..100.
    ``colors`` may hold one BGR colour per detection; otherwise ``color`` is used.
    """
    detections = list(detections or ())
    if not detections:
        return frame
    boxes = [_as_box(detection) for detection in detections]
    valid = [(detection, box) for detection, box in zip(detections, boxes) if box is not None]
    if not valid:
        return frame
    detections = [item[0] for item in valid]
    boxes = [item[1] for item in valid]

    height, width = frame.shape[:2]
    thickness = max(1, int(round(thickness)))
    alpha = max(0.0, min(1.0, float(opacity) / 100.0))
    base = frame.copy() if alpha < 0.999 or glow else frame
    overlay = base.copy()
    colour_list = list(colors or [])
    labels_on = bool(show_labels)
    numbers_on = bool(show_numbers)

    def colour_for(index):
        if index < len(colour_list) and colour_list[index] is not None:
            return tuple(int(v) for v in colour_list[index])
        return tuple(int(v) for v in color)

    # Glow is a wide, blurred underlay.  It is deliberately drawn only when requested:
    # a blurred full-frame copy on every preview frame is not free.
    glow_layer = None
    if glow:
        # A black canvas keeps the blurred halo from dimming the whole picture; adding it
        # below only brightens the tracking lines.
        glow_layer = np.zeros_like(overlay)

    pairs = _connector_pairs(boxes, str(connector_mode or "none").strip().lower(),
                             max(0.0, float(connector_distance) / 100.0) * max(width, height),
                             max_connections)
    if glow_layer is not None:
        for left, right in pairs:
            cv2.line(glow_layer, _center(boxes[left]), _center(boxes[right]),
                     colour_for(left), max(3, thickness * 3), cv2.LINE_AA)
        # Glow also belongs behind the shapes, otherwise turning it on with no connectors
        # would quietly do nothing.
        for index, (detection, box) in enumerate(zip(detections, boxes)):
            colour = detection.color or colour_for(index)
            if show_box:
                _draw_box(glow_layer, box, colour, max(3, thickness * 3),
                          str(box_style or "rectangle").lower())
            if show_circle:
                radius = max(2, int(round(max(box[2], box[3]) / 2.0)))
                cv2.circle(glow_layer, (box[0] + box[2] // 2, box[1] + box[3] // 2),
                           radius, colour, max(3, thickness * 3), cv2.LINE_AA)
        glow_layer = cv2.GaussianBlur(glow_layer, (0, 0), max(1.0, thickness * 1.6))

    # Draw connectors first so boxes and labels sit on top.
    connector_mode = str(connector_mode or "none").strip().lower()
    for left, right in pairs:
        if connector_mode == "sequential":
            # Arrows show the tracker's order, not just proximity.
            cv2.arrowedLine(overlay, _center(boxes[left]), _center(boxes[right]),
                            colour_for(left), thickness, cv2.LINE_AA, tipLength=0.22)
        else:
            cv2.line(overlay, _center(boxes[left]), _center(boxes[right]),
                     colour_for(left), thickness, cv2.LINE_AA)
        if connector_mode == "distance":
            # A distance readout makes the "all within range" mode visibly different from
            # the single nearest link and is useful when tuning the range.
            ax, ay = _center(boxes[left])
            bx, by = _center(boxes[right])
            cv2.putText(overlay, str(int(round(float(np.hypot(bx - ax, by - ay))))),
                        ((ax + bx) // 2, (ay + by) // 2 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, colour_for(left),
                        max(1, thickness // 2), cv2.LINE_AA)

    for index, (detection, box) in enumerate(zip(detections, boxes)):
        colour = detection.color or colour_for(index)
        x, y, w, h = box
        if show_box:
            _draw_box(overlay, box, colour, thickness, str(box_style or "rectangle").lower())
        if show_circle:
            radius = max(2, int(round(max(w, h) / 2.0)))
            centre = (x + w // 2, y + h // 2)
            cv2.circle(overlay, centre, radius, colour, thickness, cv2.LINE_AA)
            cv2.circle(overlay, centre, max(2, thickness + 1), colour, -1, cv2.LINE_AA)
        if show_contour and detection.contour is not None:
            cv2.drawContours(overlay, [detection.contour], -1, colour, thickness, cv2.LINE_AA)
        elif show_contour:
            # A tracker with no contour still gets a meaningful outline rather than a
            # control that silently does nothing.
            cv2.ellipse(overlay, (x + w // 2, y + h // 2), (max(1, w // 2), max(1, h // 2)),
                        0, 0, 360, colour, thickness, cv2.LINE_AA)
        if show_points:
            for point in _detection_points(detection, box):
                cv2.circle(overlay, point, max(3, thickness * 2 + 1), colour, -1, cv2.LINE_AA)
        if labels_on:
            name = (label_text or detection.__class__.__name__ or "Object").strip()
            # Callers pass a friendly name; keep the ID because several objects can share it.
            text = name if not detection.track_id else f"{name} {detection.track_id}"
            _draw_text(overlay, text, x, y, colour, label_scale, max(1, thickness // 2))
        if numbers_on:
            text = str(detection.track_id or index + 1)
            _draw_text(overlay, text, x + 4, y + h, colour, label_scale * 0.9,
                       max(1, thickness // 2))

    if glow_layer is not None:
        overlay = cv2.addWeighted(overlay, 1.0, glow_layer, 0.55, 0)
    if alpha < 0.999:
        overlay = cv2.addWeighted(overlay, alpha, frame, 1.0 - alpha, 0)
    return overlay
