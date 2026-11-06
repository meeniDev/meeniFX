"""Generated shape clips: a vector primitive that renders at every resolution.

A shape clip is an ordinary video clip whose picture is drawn from parameters instead of
decoded from a file, exactly like a text clip: it has the same transform channels, effects,
fades, keyframes, fades, split and export path. Nothing here is rasterised at creation
time, so the preview at 35 % and the export at 100 % draw the same geometry.

Colours are held keys (``{"keys": {frame: "#rrggbb"}}``) like text words, because a colour
is a step, not a number to interpolate between. Every measurement is an ``AnimatedValue``
like any other channel and is addressed as ``shape.<name>``.
"""

from __future__ import annotations

import copy

from .keyframes import AnimatedValue

SHAPE_PREFIX = "shape."

KINDS = ("rectangle", "rounded_rectangle", "ellipse", "polygon", "star", "line")

# Icon name per kind, so the toolbar menu, the inspector and the Add menu all draw the
# same glyph for the same shape.
ICONS = {
    "rectangle": "shape_rectangle",
    "rounded_rectangle": "shape_rounded",
    "ellipse": "shape_ellipse",
    "polygon": "shape_polygon",
    "star": "shape_star",
    "line": "shape_line",
}

LABELS = {
    "rectangle": "Rectangle",
    "rounded_rectangle": "Rounded Rectangle",
    "ellipse": "Ellipse",
    "polygon": "Polygon",
    "star": "Star",
    "line": "Line",
}

DEFAULTS = {
    "kind": "rectangle",
    "fill_color": "#4C9AFF",
    "fill_opacity": 100.0,
    "stroke_enabled": True,
    "stroke_color": "#FFFFFF",
    "stroke_width": 2.0,
    "stroke_opacity": 100.0,
    # center | inside | outside: where the stroke sits relative to the path.
    "stroke_align": "center",
    "width": 320.0,
    "height": 180.0,
    "radius": 24.0,
    "sides": 6.0,
    "points": 5.0,
    "inner_ratio": 50.0,
    # line only: butt | round | square, plus optional arrowheads.
    "cap": "butt",
    "arrow_start": False,
    "arrow_end": False,
}

NUMERIC = ("fill_opacity", "stroke_width", "stroke_opacity", "width", "height", "radius",
           "sides", "points", "inner_ratio")

COLOR_FIELDS = ("fill_color", "stroke_color")

ALIGNMENTS = ("center", "inside", "outside")
CAPS = ("butt", "round", "square")

# label, unit, minimum, maximum, step, decimals, slider span
SPECS = {
    "width": ("Width", "px", 1.0, 6000.0, 1.0, 0, 900.0),
    "height": ("Height", "px", 1.0, 6000.0, 1.0, 0, 900.0),
    "radius": ("Corner radius", "px", 0.0, 2000.0, 1.0, 0, 200.0),
    "sides": ("Sides", "", 3.0, 24.0, 1.0, 0, 12.0),
    "points": ("Points", "", 3.0, 24.0, 1.0, 0, 12.0),
    "inner_ratio": ("Inner radius", "%", 5.0, 95.0, 1.0, 0, 90.0),
    "fill_opacity": ("Fill opacity", "%", 0.0, 100.0, 1.0, 0, 100.0),
    "stroke_opacity": ("Stroke opacity", "%", 0.0, 100.0, 1.0, 0, 100.0),
    "stroke_width": ("Stroke width", "px", 0.0, 200.0, 0.5, 1, 40.0),
}


def blank(kind="rectangle"):
    """A fresh settings dict for a new shape clip."""
    data = copy.deepcopy(DEFAULTS)
    if kind in KINDS:
        data["kind"] = kind
    return data


def ensure_shape(clip):
    """Fill a clip's shape settings with defaults, keeping anything already stored."""
    if getattr(clip, "shape", None) is None:
        clip.shape = {}
    data = clip.shape
    for name, value in DEFAULTS.items():
        if name not in data:
            data[name] = copy.deepcopy(value)
    for name in COLOR_FIELDS:
        stored = data.get(name)
        if isinstance(stored, dict):
            keys = stored.get("keys")
            if not isinstance(keys, dict):
                stored["keys"] = {}
            else:
                converted = {int(frame): str(colour) for frame, colour in keys.items()}
                if converted != keys:
                    stored["keys"] = converted
    return data


def is_shape_clip(clip):
    return getattr(clip, "shape", None) is not None and bool(clip.shape)


def shape_channel(name):
    return SHAPE_PREFIX + name


# ----------------------------------------------------------------------- colours
def is_color_animated(clip, name):
    return isinstance(ensure_shape(clip).get(name), dict)


# The channels module asks every property source for the same names; keep one spelling.
color_is_animated = is_color_animated


def color_keys(clip, name):
    """Frames carrying a held colour key, sorted ([] for a static colour)."""
    stored = ensure_shape(clip).get(name)
    if not isinstance(stored, dict):
        return []
    return sorted(int(frame) for frame in (stored.get("keys") or {}))


def color_at(clip, name, frame=0):
    """The colour shown at a clip-local frame; keys hold until the next one."""
    stored = ensure_shape(clip).get(name)
    fallback = DEFAULTS.get(name, "#FFFFFF")
    if not isinstance(stored, dict):
        return str(stored or fallback)
    keys = stored.get("keys") or {}
    if not keys:
        return fallback
    frame = int(frame)
    candidates = [int(time) for time in keys if int(time) <= frame]
    if not candidates:
        return str(keys[min(int(time) for time in keys)])
    return str(keys[max(candidates)])


def has_color_key(clip, name, frame):
    return int(frame) in color_keys(clip, name)


def set_color(clip, name, value, frame=0, auto_key=False):
    """Write a colour; keyed when the colour is (or becomes) animated."""
    data = ensure_shape(clip)
    frame = max(0, int(frame))
    current = data.get(name)
    if isinstance(current, dict) or auto_key:
        if not isinstance(current, dict):
            keys = {}
            if frame > 0:
                keys[0] = str(current or DEFAULTS.get(name, "#FFFFFF"))
            current = {"keys": keys}
            data[name] = current
        current.setdefault("keys", {})[frame] = str(value)
        return True
    data[name] = str(value)
    return False


def toggle_color_key(clip, name, frame):
    """Diamond: key the colour at `frame`, or drop the key again."""
    data = ensure_shape(clip)
    frame = max(0, int(frame))
    current = data.get(name)
    if not isinstance(current, dict):
        data[name] = {"keys": {frame: str(current or DEFAULTS.get(name, "#FFFFFF"))}}
        return True
    keys = current.setdefault("keys", {})
    if frame in keys:
        del keys[frame]
        return False
    keys[frame] = color_at(clip, name, frame)
    return True


def remove_color_key(clip, name, frame):
    current = ensure_shape(clip).get(name)
    if not isinstance(current, dict):
        return False
    return current.setdefault("keys", {}).pop(int(frame), None) is not None


def move_color_key(clip, name, old_time, new_time):
    current = ensure_shape(clip).get(name)
    if not isinstance(current, dict):
        return None
    keys = current.setdefault("keys", {})
    old_time, new_time = int(old_time), max(0, int(new_time))
    if old_time not in keys:
        return None
    keys[new_time] = keys.pop(old_time)
    return new_time


# ----------------------------------------------------------------- numeric values
def stored(clip, name):
    return ensure_shape(clip).get(name)


def is_animated(clip, name):
    return isinstance(stored(clip, name), AnimatedValue)


def value_at(clip, name, frame=0):
    raw = stored(clip, name)
    if isinstance(raw, AnimatedValue):
        return raw.value_at(frame)
    return raw


def set_value(clip, name, value, frame=0, auto_key=False):
    """Set a numeric shape parameter, honouring the Auto-Keyframe rules."""
    data = ensure_shape(clip)
    current = data.get(name)
    frame = max(0, int(frame))
    if isinstance(current, AnimatedValue):
        key = current.key_at(frame)
        if key is not None:
            key.value = float(value)
            return True
        if auto_key:
            current.set_keyframe(frame, float(value))
            return True
        delta = float(value) - float(current.value_at(frame))
        for item in current.keyframes:
            item.value = float(item.value) + delta
        return False
    if auto_key:
        animated = AnimatedValue(base_value=float(current or 0.0))
        if frame > 0:
            animated.set_keyframe(0, float(current or 0.0))
        animated.set_keyframe(frame, float(value))
        data[name] = animated
        return True
    data[name] = float(value)
    return False


def animate(clip, name, frame=0):
    data = ensure_shape(clip)
    current = data.get(name)
    if isinstance(current, AnimatedValue):
        return current
    animated = AnimatedValue(base_value=float(current or 0.0))
    animated.set_keyframe(max(0, int(frame)), float(current or 0.0))
    data[name] = animated
    return animated


def unanimate(clip, name, frame=0):
    data = ensure_shape(clip)
    current = data.get(name)
    if isinstance(current, AnimatedValue):
        data[name] = float(current.value_at(frame))


def key_times(clip, name):
    raw = stored(clip, name)
    return [key.time for key in raw.keyframes] if isinstance(raw, AnimatedValue) else []


def toggle_key(clip, name, frame):
    data = ensure_shape(clip)
    raw = data.get(name)
    frame = max(0, int(frame))
    if not isinstance(raw, AnimatedValue):
        animate(clip, name, frame)
        return True
    if raw.remove_keyframe(frame):
        return False
    raw.set_keyframe(frame, raw.value_at(frame))
    return True


def set_key_time(clip, name, old_time, new_time):
    raw = stored(clip, name)
    if not isinstance(raw, AnimatedValue):
        return None
    key = raw.key_at(old_time)
    if key is None:
        return None
    target = max(0, int(new_time))
    if target == key.time:
        return target
    clash = raw.key_at(target)
    if clash is not None and clash is not key:
        raw.remove_keyframe(target)
    key.time = target
    raw.keyframes.sort(key=lambda item: item.time)
    return target


# ------------------------------------------------------------------------- render
def resolve(clip, frame):
    """Every setting of a shape clip evaluated at a frame, ready for the renderer."""
    data = ensure_shape(clip)
    frame = int(frame)
    kind = str(data.get("kind") or "rectangle")
    spec = {
        "kind": kind if kind in KINDS else "rectangle",
        "fill_color": color_at(clip, "fill_color", frame),
        "stroke_color": color_at(clip, "stroke_color", frame),
        "stroke_enabled": bool(data.get("stroke_enabled")),
        "align": str(data.get("stroke_align") or "center"),
        "cap": str(data.get("cap") or "butt"),
        "arrow_start": bool(data.get("arrow_start")),
        "arrow_end": bool(data.get("arrow_end")),
    }
    if spec["align"] not in ALIGNMENTS:
        spec["align"] = "center"
    if spec["cap"] not in CAPS:
        spec["cap"] = "butt"
    for name in NUMERIC:
        spec[name] = float(value_at(clip, name, frame) or 0.0)
    spec["fill_opacity"] = max(0.0, min(100.0, spec["fill_opacity"])) / 100.0
    spec["stroke_opacity"] = max(0.0, min(100.0, spec["stroke_opacity"])) / 100.0
    spec["inner_ratio"] = max(5.0, min(95.0, spec["inner_ratio"])) / 100.0
    spec["width"] = max(1.0, spec["width"])
    spec["height"] = max(1.0, spec["height"])
    spec["radius"] = max(0.0, spec["radius"])
    spec["sides"] = max(3.0, min(24.0, spec["sides"]))
    spec["points"] = max(3.0, min(24.0, spec["points"]))
    return spec


def clone_settings(source, target):
    """Copy another shape clip's settings (not its animation) onto `target`."""
    data = ensure_shape(source)
    for name in DEFAULTS:
        value = data.get(name)
        if isinstance(value, AnimatedValue):
            value = value.value_at(0)
        elif isinstance(value, dict):
            value = copy.deepcopy(value)
        ensure_shape(target)[name] = value
    return target
