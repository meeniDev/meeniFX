"""Shared drawing for the Face, Hand and Blob trackers.

The three trackers detect different things, but a user expects one visual language:
the same box, label, point and connector controls no matter which detector produced the
object.  This module owns that language.  It deliberately has no effect or GUI imports so
it can be tested on its own, and it never invents detections - callers pass the objects
their real detector returned.

All colours are OpenCV BGR tuples.  The renderer returns the same frame object when there
is nothing to draw, which keeps a detector-less preview free of an extra copy.
"""

import os
import re
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


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
    # 0..1 detector confidence, used by {confidence} in labels.
    confidence: float = 1.0
    # Pixels per frame, filled by TrackStabilizer; used by {speed} in labels.
    speed: float = 0.0
    # Extra label variables ("side" for a hand, for example).
    extra: dict = field(default_factory=dict)


# A preset is a starting point, not a lock.  Choosing one writes these values into the
# ordinary effect parameters; the renderer always reads those parameters, so every
# checkbox and dropdown keeps working afterwards.
TRACKING_STYLES = {
    "custom": {},
    "clean": {"show_box": True, "show_contour": False, "show_points": False,
              "show_circle": False, "show_labels": True, "show_numbers": False,
              "connector_mode": "none", "glow": False, "opacity": 100,
              "box_style": "corners"},
    "minimal": {"show_box": True, "show_contour": False, "show_points": False,
                "show_circle": False, "show_labels": False, "show_numbers": False,
                "connector_mode": "none", "glow": False, "opacity": 100,
                "box_style": "corners"},
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


def resolve_style(preset, manual=None, defaults=None):
    """The effective style for a tracker.

    Presets are applied by the properties panel when the user picks one: their values
    are written into the ordinary parameters.  Rendering therefore uses ``manual`` for
    both Custom and named looks.  A preset must never become a hidden second source of
    truth that ignores a checkbox changed afterwards.

    ``defaults`` is for callers that only set the Look key (API/tests): in that case a
    named preset fills values that are still at their defaults.  A value the user has
    changed is never overwritten, and the properties panel switches the Look to Custom
    on the first manual change.
    """
    manual = dict(manual or {})
    name = str(preset or "custom").strip().lower()
    if defaults is None or name not in TRACKING_STYLES or name == "custom":
        return manual
    style = dict(manual)
    for key, value in TRACKING_STYLES[name].items():
        if key not in style or style[key] == defaults.get(key):
            style[key] = value
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


# --------------------------------------------------------------------- labels
LABEL_VARIABLES = ("id", "x", "y", "width", "height", "speed", "confidence")
_VARIABLE = re.compile(r"\{([a-zA-Z_]+)\}")


def label_values(detection, index, width=0, height=0):
    """The values available to ``{name}`` in a label, in the detection's pixels."""
    x, y, w, h = [int(round(float(value))) for value in detection.box]
    values = {
        "id": int(detection.track_id or index + 1),
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "speed": int(round(float(detection.speed or 0.0))),
        "confidence": int(round(float(detection.confidence) * 100.0)),
    }
    values.update({str(key): value for key, value in (detection.extra or {}).items()})
    return values


def format_label(template, detection, index, width=0, height=0):
    """Replace ``{id}``-style tokens in a label template.

    Unknown tokens are left untouched, so typing ``{colour}`` is visible instead of
    silently disappearing.  Empty templates stay empty.
    """
    text = str(template or "")
    if not text:
        return ""

    def replace(match):
        key = match.group(1)
        values = label_values(detection, index, width, height)
        return str(values[key]) if key in values else match.group(0)

    return _VARIABLE.sub(replace, text)


_FONT_CACHE = {}
_FONT_FILES = None
_FONT_DIRS = (
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.path.expanduser("~"), ".fonts"),
    "/usr/share/fonts/truetype",
    "/System/Library/Fonts",
)


def _font_files():
    """(stem, path) for every usable TrueType/OpenType font, scanned once."""
    global _FONT_FILES
    if _FONT_FILES is not None:
        return _FONT_FILES
    found = []
    for directory in _FONT_DIRS:
        if not os.path.isdir(directory):
            continue
        for root, _dirs, names in os.walk(directory):
            for name in names:
                if name.lower().endswith((".ttf", ".ttc", ".otf")):
                    found.append((os.path.splitext(name)[0].lower(), os.path.join(root, name)))
    _FONT_FILES = found
    return found


def resolve_font_file(family, weight="regular", size=16):
    """A font file for the requested family, falling back to a bundled-style default."""
    stem = re.sub(r"[^a-z0-9]", "", str(family or "").lower())
    bold = str(weight or "").strip().lower() in ("bold", "semibold", "heavy", "black")
    candidates = _font_files()
    if stem:
        matches = [(name, path) for name, path in candidates if stem in name]
        if matches:
            def rank(item):
                name = item[0]
                italic = "italic" in name or "oblique" in name
                heavy = ("bold" in name or name.endswith("b") or "bd" in name
                         or "black" in name or "heavy" in name or "semibold" in name)
                return (italic, 0 if heavy == bold else 1, len(name))
            matches.sort(key=rank)
            return matches[0][1]
    fallbacks = (("segoeuib", "seguisb", "arialbd", "calibrib", "dejavusans-bold")
                 if bold else ("segoeui", "arial", "calibri", "dejavusans"))
    lowered = {name: path for name, path in candidates}
    for name in fallbacks:
        for key, path in lowered.items():
            if name in key:
                return path
    return None


def _font(family, weight, size, scale=1.0):
    size = max(6, int(round(float(size) * max(0.1, float(scale)))))
    key = (str(family or ""), str(weight or ""), size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    path = resolve_font_file(family, weight, size)
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)
    except (OSError, ValueError, TypeError):
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow before 10.1 has no sized default font
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _colour(text, fallback=(255, 255, 255)):
    """A BGR tuple from ``#RRGGBB`` or ``#AARRGGBB``; fallback on nonsense."""
    value = str(text or "").strip().lstrip("#")
    try:
        if len(value) == 8:
            _a, r, g, b = (int(value[index:index + 2], 16) for index in (0, 2, 4, 6))
            return (b, g, r)
        if len(value) == 6:
            r, g, b = (int(value[index:index + 2], 16) for index in (0, 2, 4))
            return (b, g, r)
    except ValueError:
        pass
    return tuple(int(v) for v in fallback)


def _rgba(text, fallback=(0, 0, 0), opacity=100):
    b, g, r = _colour(text, fallback)
    alpha = max(0, min(255, int(round(float(opacity) * 2.55))))
    return (r, g, b, alpha)


_ANCHOR_XY = {
    "top_left": (0.0, 0.0), "top_center": (0.5, 0.0), "top_right": (1.0, 0.0),
    "middle_left": (0.0, 0.5), "center": (0.5, 0.5), "middle_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0), "bottom_center": (0.5, 1.0), "bottom_right": (1.0, 1.0),
}


def _label_rect(box, size, style):
    """Top-left of the label plate for a position/anchor/offset combination."""
    x, y, w, h = box
    tw, th = size
    position = str(style.get("label_position") or "above").lower()
    anchor = _ANCHOR_XY.get(str(style.get("label_anchor") or "top_left").lower(),
                            (0.0, 0.0))
    gap = max(0, int(style.get("label_padding", 4) or 0))
    ax, ay = x + w * anchor[0], y + h * anchor[1]
    align = str(style.get("label_align") or "left").lower()
    if position == "below":
        left = ax
        top = ay + gap
    elif position == "left":
        left = ax - tw - gap
        top = ay
    elif position == "right":
        left = ax + gap
        top = ay
    elif position == "inside":
        left = ax + gap
        top = ay + gap
    else:  # above
        left = ax
        top = ay - th - gap
    if align == "center":
        left -= tw / 2.0
    elif align == "right":
        left -= tw
    left += float(style.get("label_offset_x", 0) or 0)
    top += float(style.get("label_offset_y", 0) or 0)
    return int(round(left)), int(round(top))


def draw_label(overlay, text, box, style, color, height=None, width=None):
    """Draw one styled label plate onto an OpenCV BGR overlay in place."""
    text = str(text or "")
    if not text:
        return
    frame_h, frame_w = overlay.shape[:2]
    font = _font(style.get("label_font", "Segoe UI"), style.get("label_weight", "regular"),
                 style.get("label_size", 18), style.get("label_scale", 1.0))
    outline = max(0, int(round(float(style.get("label_outline", 0) or 0))))
    padding = max(0, int(round(float(style.get("label_padding", 4) or 0))))
    radius = max(0, int(round(float(style.get("label_radius", 4) or 0))))

    probe = Image.new("RGBA", (1, 1))
    measure = ImageDraw.Draw(probe)
    try:
        left, top, right, bottom = measure.textbbox((0, 0), text, font=font,
                                                    stroke_width=outline)
        tw, th = right - left, bottom - top
    except (AttributeError, ValueError):
        tw, th = max(1, len(text) * 8), 16
    plate_w, plate_h = int(tw + padding * 2 + 2), int(th + padding * 2 + 2)
    label = Image.new("RGBA", (max(1, plate_w), max(1, plate_h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(label)
    background = _rgba(style.get("label_background", "#000000"), (0, 0, 0),
                       style.get("label_background_opacity", 60))
    if background[3] > 0:
        draw.rounded_rectangle((0, 0, plate_w - 1, plate_h - 1), radius=radius,
                               fill=background)
    b, g, r = _colour(style.get("label_color", "#FFFFFF"), color or (255, 255, 255))
    outline_b, outline_g, outline_r = _colour(style.get("label_outline_color", "#000000"),
                                              (0, 0, 0))
    draw.text((padding + 1, padding + 1), text, font=font, fill=(r, g, b, 255),
              stroke_width=outline, stroke_fill=(outline_r, outline_g, outline_b, 255))

    left, top = _label_rect(box, (plate_w, plate_h), style)
    if top < 0:
        top = 0
    if left < 0:
        left = 0
    if left + plate_w > frame_w:
        left = max(0, frame_w - plate_w)
    if top + plate_h > frame_h:
        top = max(0, frame_h - plate_h)
    patch = np.asarray(label, dtype=np.uint8)[:, :, [2, 1, 0, 3]].copy()
    alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
    region = overlay[top:top + plate_h, left:left + plate_w]
    if region.size == 0:
        return
    region[:] = (patch[:, :, :3].astype(np.float32) * alpha
                 + region.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)


def draw_landmarks(overlay, detection, color=(0, 255, 255), point_size=3, line_width=2,
                   connections=(), connections_color=None, show_points=True):
    """Draw a topological skeleton (a hand's 21 landmarks, for example)."""
    points = [(int(round(x)), int(round(y))) for x, y in (detection.points or ())]
    if not points:
        return
    line_colour = tuple(int(v) for v in (connections_color or color))
    if show_points:
        for point in points:
            cv2.circle(overlay, point, max(1, int(point_size)), color, -1, cv2.LINE_AA)
    for first, second in connections or ():
        if 0 <= int(first) < len(points) and 0 <= int(second) < len(points):
            cv2.line(overlay, points[int(first)], points[int(second)], line_colour,
                     max(1, int(line_width)), cv2.LINE_AA)


def _connector_pairs(boxes, mode, limit, max_connections, custom_links=""):
    """Pairs of indices to connect, capped so 80 blobs cannot make 3160 lines."""
    count = len(boxes)
    if count < 2 or mode in ("none", "", None):
        return []
    centres = [_center(box) for box in boxes]
    pairs = []
    cap = max(1, int(max_connections)) * count

    if mode == "custom":
        for part in re.split(r"[;,\s]+", str(custom_links or "")):
            if not part:
                continue
            first, _sep, second = part.partition("-")
            try:
                left, right = int(first) - 1, int(second) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= left < count and 0 <= right < count and left != right:
                pairs.append((left, right))
        seen = set()
        unique = []
        for left, right in pairs:
            key = (min(left, right), max(left, right))
            if key not in seen:
                seen.add(key)
                unique.append((left, right))
        return unique[:cap]

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
            # Track order is meaningful for blobs and hands, but the distance control is
            # still the user's way to stop a long gap from becoming a line.
            if left + 1 < count:
                right = left + 1
                rx, ry = centres[right]
                distance = float(np.hypot(rx - lx, ry - ly))
                if limit <= 0 or distance <= limit:
                    pairs.append((left, right))
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


def _curve_points(start, end, bend):
    """Quadratic samples bending perpendicular to the connector (bend in pixels)."""
    if abs(float(bend)) < 1.0:
        return [start, end]
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length = max(1.0, float(np.hypot(dx, dy)))
    cx = (ax + bx) / 2.0 - dy / length * float(bend)
    cy = (ay + by) / 2.0 + dx / length * float(bend)
    samples = []
    for step in range(17):
        t = step / 16.0
        samples.append((int(round((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t * t * bx)),
                        int(round((1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t * t * by))))
    return samples


def _draw_connectors(overlay, boxes, pairs, colors, *, thickness, colour_text, opacity,
                     glow, curve, pulses, pulse_speed, labels, frame_index=0):
    """Styled connectors, composited as one RGBA layer so opacity is real transparency."""
    if not pairs:
        return overlay
    height, width = overlay.shape[:2]
    layer = np.zeros_like(overlay)
    alpha = np.zeros((height, width), np.uint8)
    texts = []
    base_colour = _colour(colour_text, (0, 255, 255)) if str(colour_text or "").strip() else None

    for left, right in pairs:
        colour = base_colour or tuple(int(v) for v in colors[left])
        ax, ay = _center(boxes[left])
        bx, by = _center(boxes[right])
        bend = float(curve) / 100.0 * max(1.0, np.hypot(bx - ax, by - ay)) * 0.5
        points = np.asarray(_curve_points((ax, ay), (bx, by), bend), np.int32)
        cv2.polylines(layer, [points], False, colour, thickness, cv2.LINE_AA)
        cv2.polylines(alpha, [points], False, 255, thickness, cv2.LINE_AA)
        if pulses:
            distance = max(1.0, float(np.hypot(bx - ax, by - ay)))
            phase = (float(frame_index) * max(0.5, float(pulse_speed)) / 12.0) % 1.0
            for pulse in range(3):
                t = (phase + pulse / 3.0) % 1.0
                index = min(len(points) - 1, int(round(t * (len(points) - 1))))
                px, py = int(points[index][0]), int(points[index][1])
                radius = max(2, thickness + 1)
                cv2.circle(layer, (px, py), radius, (255, 255, 255), -1, cv2.LINE_AA)
                cv2.circle(alpha, (px, py), radius, 255, -1, cv2.LINE_AA)
        if labels:
            mx, my = (ax + bx) // 2, (ay + by) // 2
            texts.append((str(int(round(float(np.hypot(bx - ax, by - ay))))),
                          mx + 2, my - 2, colour))

    strength = max(0.0, min(1.0, float(opacity) / 100.0))
    if strength <= 0.001:
        return overlay
    blend_alpha = alpha.astype(np.float32)[:, :, None] / 255.0 * strength
    if glow:
        halo = cv2.GaussianBlur(layer, (0, 0), max(1.0, thickness * 1.8))
        halo_alpha = cv2.GaussianBlur(alpha, (0, 0), max(1.0, thickness * 1.8))
        glow_alpha = halo_alpha.astype(np.float32)[:, :, None] / 255.0 * strength
        overlay[:] = (halo.astype(np.float32) * glow_alpha
                      + overlay.astype(np.float32) * (1.0 - glow_alpha)).astype(np.uint8)
    overlay[:] = (layer.astype(np.float32) * blend_alpha
                  + overlay.astype(np.float32) * (1.0 - blend_alpha)).astype(np.uint8)
    # Distance readouts are drawn after compositing: text has no alpha mask here, and
    # multiplying it by the connector's own coverage would erase it.
    for text, mx, my, colour in texts:
        _draw_text(overlay, text, mx, my, colour, 0.42, max(1, thickness // 2))
    return overlay


def draw_tracking(frame, detections, *, color=(0, 255, 255), thickness=2, opacity=100,
                  show_box=True, show_contour=False, show_points=False, show_circle=False,
                  show_labels=False, show_numbers=False, label_text="", box_style="rectangle",
                  connector_mode="none", connector_distance=25, max_connections=2,
                  glow=False, colors=None, label_scale=0.55, label_style=None,
                  connector_color="", connector_thickness=None, connector_opacity=100,
                  connector_glow=False, connector_curve=0, connector_pulses=False,
                  connector_pulse_speed=4, connector_labels=False, custom_links="",
                  frame_index=0, label_values_extra=None, landmark=None, skeleton=False,
                  skeleton_connections=(), point_size=None, line_width=None,
                  skeleton_color=""):
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
    label_style = dict(label_style or {})
    label_style.setdefault("label_scale", label_scale)

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

    mode = str(connector_mode or "none").strip().lower()
    if mode == "center" and len(boxes) > 1:
        pairs = []
        cx = int(round(sum(_center(box)[0] for box in boxes) / float(len(boxes))))
        cy = int(round(sum(_center(box)[1] for box in boxes) / float(len(boxes))))
        for index in range(len(boxes)):
            ax, ay = _center(boxes[index])
            cv2.line(overlay, (ax, ay), (cx, cy), colour_for(index), thickness, cv2.LINE_AA)
            if glow_layer is not None:
                cv2.line(glow_layer, (ax, ay), (cx, cy), colour_for(index),
                         max(3, thickness * 3), cv2.LINE_AA)
        cv2.circle(overlay, (cx, cy), max(2, thickness + 1), colour_for(0), -1, cv2.LINE_AA)
    else:
        pairs = _connector_pairs(boxes, mode,
                                 max(0.0, float(connector_distance) / 100.0) * max(width, height),
                                 max_connections, custom_links)
        if pairs:
            overlay = _draw_connectors(
                overlay, boxes, pairs,
                [colour_for(index) for index in range(len(boxes))],
                thickness=max(1, int(connector_thickness or thickness)),
                colour_text=connector_color, opacity=connector_opacity,
                glow=bool(connector_glow), curve=connector_curve,
                pulses=bool(connector_pulses), pulse_speed=connector_pulse_speed,
                labels=bool(connector_labels), frame_index=frame_index)

        # Glow also belongs behind the shapes, otherwise turning it on with no connectors
        # would quietly do nothing.
        if glow_layer is not None:
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
        if skeleton and detection.points:
            draw_landmarks(overlay, detection, color=colour,
                           point_size=point_size if point_size is not None else thickness * 2 + 1,
                           line_width=line_width if line_width is not None else thickness,
                           connections=skeleton_connections,
                           connections_color=_colour(skeleton_color, colour)
                           if str(skeleton_color or "").strip() else None,
                           show_points=show_points)
        elif show_points:
            for point in _detection_points(detection, box):
                cv2.circle(overlay, point, max(3, thickness * 2 + 1), colour, -1, cv2.LINE_AA)
        if labels_on:
            # Labels are explicit text, not identity badges.  An empty field draws
            # nothing; Numbers is the separate control for track IDs.
            name = format_label(label_text, detection, index, width, height).strip()
            if name:
                draw_label(overlay, name, box, label_style, colour, height, width)
        if numbers_on:
            text = str(detection.track_id or index + 1)
            _draw_text(overlay, text, x + 4, y + h, colour, label_scale * 0.9,
                       max(1, thickness // 2))

    if glow_layer is not None:
        overlay = cv2.addWeighted(overlay, 1.0, glow_layer, 0.55, 0)
    if alpha < 0.999:
        overlay = cv2.addWeighted(overlay, alpha, frame, 1.0 - alpha, 0)
    return overlay


class TrackStabilizer:
    """Persistent IDs and speeds across frames, so connectors never jump.

    Detections are matched to the nearest previous centroid; unmatched ones get the next
    unused ID.  The match radius is generous by default because a hand can move a long way
    between frames, but it stops two unrelated objects from swapping identities.
    """

    def __init__(self, max_missing=8):
        self._tracks = {}
        self._next_id = 1
        self._frame = -1
        self.max_missing = max(1, int(max_missing))

    def reset(self):
        self._tracks = {}
        self._next_id = 1
        self._frame = -1

    def update(self, detections, frame=0, max_distance=0.0):
        detections = list(detections or ())
        frame = int(frame)
        if frame < self._frame:
            # Seeking backwards restarts identity matching, rather than matching future
            # positions to an older frame.
            self.reset()
        self._frame = frame
        radius = float(max_distance) if max_distance > 0 else 0.0
        claimed = set()
        for detection in detections:
            box = _as_box(detection)
            if box is None:
                continue
            cx, cy = _center(box)
            best_id, best_distance = None, None
            for track_id, track in self._tracks.items():
                if track_id in claimed:
                    continue
                tx, ty, _seen = track
                distance = float(np.hypot(cx - tx, cy - ty))
                if radius and distance > radius:
                    continue
                if best_distance is None or distance < best_distance:
                    best_id, best_distance = track_id, distance
            if best_id is None:
                best_id = self._next_id
                self._next_id += 1
                speed = 0.0
            else:
                tx, ty, seen = self._tracks[best_id]
                span = max(1, frame - seen)
                speed = float(np.hypot(cx - tx, cy - ty)) / span
            claimed.add(best_id)
            self._tracks[best_id] = (cx, cy, frame)
            detection.track_id = best_id
            detection.speed = speed
        stale = [track_id for track_id, track in self._tracks.items()
                 if frame - track[2] > self.max_missing]
        for track_id in stale:
            del self._tracks[track_id]
        return detections
