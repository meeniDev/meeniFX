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

from . import path as PATHS
from .keyframes import AnimatedValue

SHAPE_PREFIX = "shape."

KINDS = ("rectangle", "rounded_rectangle", "ellipse", "polygon", "star", "line", "path")

# Icon name per kind, so the toolbar menu, the inspector and the Add menu all draw the
# same glyph for the same shape.
ICONS = {
    "rectangle": "shape_rectangle",
    "rounded_rectangle": "shape_rounded",
    "ellipse": "shape_ellipse",
    "polygon": "shape_polygon",
    "star": "shape_star",
    "line": "shape_line",
    "path": "shape_polygon",
}

LABELS = {
    "rectangle": "Rectangle",
    "rounded_rectangle": "Rounded Rectangle",
    "ellipse": "Ellipse",
    "polygon": "Polygon",
    "star": "Star",
    "line": "Line",
    "path": "Custom Path",
}

DEFAULTS = {
    "kind": "rectangle",
    "fill_color": "#4C9AFF",
    "fill_opacity": 100.0,
    "gradient_enabled": False,
    "gradient_type": "linear",
    "gradient_angle": 0.0,
    "gradient_scale": 100.0,
    "gradient_offset": 0.0,
    # Semicolon-separated positions and colours also accepted by the inspector.
    "gradient_stops": [{"position": 0.0, "color": "#4C9AFF"},
                       {"position": 100.0, "color": "#7A5CFF"}],
    "stroke_enabled": True,
    "stroke_color": "#FFFFFF",
    "stroke_width": 2.0,
    "stroke_opacity": 100.0,
    # center | inside | outside: where the stroke sits relative to the path.
    "stroke_align": "center",
    "stroke_dash": 0.0,
    "stroke_gap": 0.0,
    "stroke_join": "miter",
    "stroke_miter_limit": 4.0,
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
    # Custom paths use project pixels inside the full-frame generated layer.
    "path": {"points": [], "closed": False},
}

NUMERIC = ("fill_opacity", "stroke_width", "stroke_opacity", "width", "height", "radius",
           "sides", "points", "inner_ratio", "gradient_angle", "gradient_scale",
           "gradient_offset", "stroke_dash", "stroke_gap", "stroke_miter_limit")

COLOR_FIELDS = ("fill_color", "stroke_color")

ALIGNMENTS = ("center", "inside", "outside")
CAPS = ("butt", "round", "square")
JOINS = ("miter", "round", "bevel")
GRADIENT_TYPES = ("linear", "radial")

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
    "gradient_angle": ("Gradient angle", "\u00b0", -360.0, 360.0, 1.0, 0, 360.0),
    "gradient_scale": ("Gradient scale", "%", 1.0, 400.0, 1.0, 0, 300.0),
    "gradient_offset": ("Gradient position", "%", -100.0, 100.0, 1.0, 0, 200.0),
    "stroke_dash": ("Dash length", "px", 0.0, 200.0, 1.0, 0, 50.0),
    "stroke_gap": ("Dash gap", "px", 0.0, 200.0, 1.0, 0, 50.0),
    "stroke_miter_limit": ("Miter limit", "", 1.0, 20.0, 0.5, 1, 10.0),
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
    stops = data.get("gradient_stops")
    if isinstance(stops, str):
        data["gradient_stops"] = parse_stops(stops)
    elif not isinstance(stops, list) or len(stops) < 2:
        data["gradient_stops"] = copy.deepcopy(DEFAULTS["gradient_stops"])
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


def parse_stops(value):
    """Parse ``0:#112233; 55:#ff8800; 100:white`` into a validated stop list."""
    if isinstance(value, list):
        raw = value
    else:
        raw = []
        for part in str(value or "").split(";"):
            position, _sep, colour = part.partition(":")
            if colour:
                raw.append({"position": position, "color": colour.strip()})
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        position = item.get("position", 0.0)
        if not isinstance(position, dict):
            try:
                position = max(0.0, min(100.0, float(position)))
            except (TypeError, ValueError):
                position = 0.0
        colour = item.get("color")
        if not isinstance(colour, dict):
            colour = str(colour or "#ffffff").strip()
        out.append({"position": position, "color": colour})
    if len(out) < 2:
        return copy.deepcopy(DEFAULTS["gradient_stops"])
    return sorted(out, key=lambda item: stop_position_at(item, 0))


def stops_text(settings):
    data = ensure_shape(settings) if hasattr(settings, "shape") else settings
    return ";".join(f"{int(round(stop_position_at(stop, 0)))}:{stop_color_at(stop, 0)}"
                    for stop in (data or {}).get("gradient_stops") or () if isinstance(stop, dict))


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


# ------------------------------------------------------------ gradient stops
# A stop field is either a plain value or ``{"keys": {frame: value}}``. Positions
# interpolate numerically; colours interpolate their RGBA channels, so a gradient stop can
# fade (alpha) and change hue smoothly instead of snapping between held colours.
def stop_channel(index, field):
    return SHAPE_PREFIX + f"gradient_stop.{int(index)}.{field}"


def parse_stop_channel(name):
    """(stop index, 'position'|'color') for a gradient-stop channel, else None."""
    text = str(name or "")
    if not text.startswith("gradient_stop."):
        return None
    index_text, _sep, field = text[len("gradient_stop."):].partition(".")
    if field not in ("position", "color"):
        return None
    try:
        return int(index_text), field
    except (TypeError, ValueError):
        return None


def stop_indexes(clip):
    return list(range(len(ensure_shape(clip).get("gradient_stops") or ())))


def stop_value_raw(clip, index, field):
    stops = ensure_shape(clip).get("gradient_stops") or []
    if not 0 <= int(index) < len(stops) or not isinstance(stops[int(index)], dict):
        return None
    return stops[int(index)].get(field)


def stop_field_keys(clip, index, field):
    raw = stop_value_raw(clip, index, field)
    if not isinstance(raw, dict):
        return []
    return sorted(int(frame) for frame in (raw.get("keys") or {}))


def _stop_number(raw, frame, fallback=0.0):
    if not isinstance(raw, dict):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return fallback
    keys = raw.get("keys") or {}
    if not keys:
        return fallback
    times = sorted(int(time) for time in keys)
    frame = int(frame)
    if frame <= times[0]:
        return float(keys[times[0]])
    if frame >= times[-1]:
        return float(keys[times[-1]])
    for first, second in zip(times, times[1:]):
        if first <= frame <= second:
            span = float(second - first)
            ratio = (frame - first) / span if span > 0 else 0.0
            return float(keys[first]) + (float(keys[second]) - float(keys[first])) * ratio
    return fallback


def _colour_parts(colour):
    """(r, g, b, a) 0..255 from #RGB, #RRGGBB, #AARRGGBB (Qt HexArgb) or a name."""
    text = str(colour or "#000000").strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        alpha = 255
        if len(digits) == 8:
            try:
                alpha = int(digits[0:2], 16)
            except ValueError:
                alpha = 255
            digits = digits[2:]
        if len(digits) == 6:
            try:
                return (int(digits[0:2], 16), int(digits[2:4], 16),
                        int(digits[4:6], 16), alpha)
            except ValueError:
                pass
    try:
        from PIL import ImageColor
        red, green, blue = ImageColor.getrgb(text)[:3]
        return (int(red), int(green), int(blue), 255)
    except Exception:
        return (0, 0, 0, 255)


def _colour_text(parts):
    red, green, blue, alpha = (max(0, min(255, int(round(value)))) for value in parts)
    if alpha < 255:
        return f"#{alpha:02X}{red:02X}{green:02X}{blue:02X}"
    return f"#{red:02X}{green:02X}{blue:02X}"


def mix_colours(first, second, ratio):
    """Blend two colours in RGBA space (smooth hue and opacity interpolation)."""
    start, end = _colour_parts(first), _colour_parts(second)
    ratio = max(0.0, min(1.0, float(ratio)))
    return _colour_text(start[i] + (end[i] - start[i]) * ratio for i in range(4))


def _stop_colour(raw, frame, fallback="#FFFFFF"):
    if not isinstance(raw, dict):
        return str(raw or fallback)
    keys = raw.get("keys") or {}
    if not keys:
        return fallback
    times = sorted(int(time) for time in keys)
    frame = int(frame)
    if frame <= times[0]:
        return str(keys[times[0]])
    if frame >= times[-1]:
        return str(keys[times[-1]])
    for first, second in zip(times, times[1:]):
        if first <= frame <= second:
            span = float(second - first)
            ratio = (frame - first) / span if span > 0 else 0.0
            return mix_colours(str(keys[first]), str(keys[second]), ratio)
    return fallback


def stop_position_at(stop, frame=0):
    value = _stop_number((stop or {}).get("position") if isinstance(stop, dict) else stop,
                         frame, 0.0)
    return max(0.0, min(100.0, value))


def stop_color_at(stop, frame=0):
    return _stop_colour((stop or {}).get("color") if isinstance(stop, dict) else stop,
                        frame, "#FFFFFF")


def resolve_stops(clip, frame=0):
    """Every gradient stop evaluated at a frame, sorted by position."""
    data = ensure_shape(clip)
    out = []
    for stop in data.get("gradient_stops") or ():
        if not isinstance(stop, dict):
            continue
        out.append({"position": stop_position_at(stop, frame),
                    "color": stop_color_at(stop, frame)})
    if len(out) < 2:
        return copy.deepcopy(DEFAULTS["gradient_stops"])
    return sorted(out, key=lambda item: item["position"])


def stop_value_at(clip, index, field, frame=0):
    stops = ensure_shape(clip).get("gradient_stops") or []
    if not 0 <= int(index) < len(stops) or not isinstance(stops[int(index)], dict):
        return None
    stop = stops[int(index)]
    return stop_position_at(stop, frame) if field == "position" else stop_color_at(stop, frame)


def stop_is_animated(clip, index, field):
    return bool(stop_field_keys(clip, index, field))


def stop_has_key(clip, index, field, frame):
    return int(frame) in stop_field_keys(clip, index, field)


def stop_key_times(clip, index, field):
    return stop_field_keys(clip, index, field)


def stop_set_value(clip, index, field, value, frame=0, auto_key=False):
    """Write a stop field; keyed once the field is animated (or Auto-Keyframe is on)."""
    stops = ensure_shape(clip).get("gradient_stops") or []
    if not 0 <= int(index) < len(stops) or not isinstance(stops[int(index)], dict):
        return False
    stop = stops[int(index)]
    frame = max(0, int(frame))
    value = float(value) if field == "position" else str(value)
    current = stop.get(field)
    if isinstance(current, dict) or auto_key:
        if not isinstance(current, dict):
            keys = {}
            if frame > 0:
                previous = (stop_position_at({"position": current}, 0)
                            if field == "position" else str(current or "#FFFFFF"))
                keys[0] = previous
            current = {"keys": keys}
            stop[field] = current
        current.setdefault("keys", {})[frame] = value
        return True
    stop[field] = value
    return False


def stop_toggle_key(clip, index, field, frame):
    """Diamond for one stop field: add a key at `frame`, or remove the existing one."""
    stops = ensure_shape(clip).get("gradient_stops") or []
    if not 0 <= int(index) < len(stops) or not isinstance(stops[int(index)], dict):
        return False
    stop = stops[int(index)]
    frame = max(0, int(frame))
    current = stop.get(field)
    if isinstance(current, dict):
        keys = current.setdefault("keys", {})
        if frame in keys:
            removed = keys.pop(frame)
            if not keys:
                stop[field] = removed
            return False
        keys[frame] = stop_value_at(clip, index, field, frame)
        return True
    stop[field] = {"keys": {frame: current}}
    return True


def stop_remove_key(clip, index, field, frame):
    raw = stop_value_raw(clip, index, field)
    if not isinstance(raw, dict):
        return False
    keys = raw.setdefault("keys", {})
    return keys.pop(int(frame), None) is not None


def stop_clear_keys(clip, index, field, frame=0):
    raw = stop_value_raw(clip, index, field)
    if not isinstance(raw, dict):
        return
    stops = ensure_shape(clip).get("gradient_stops") or []
    stops[int(index)][field] = stop_value_at(clip, index, field, frame)


def stop_move_key(clip, index, field, old_time, new_time):
    raw = stop_value_raw(clip, index, field)
    if not isinstance(raw, dict):
        return None
    keys = raw.setdefault("keys", {})
    old_time, new_time = int(old_time), max(0, int(new_time))
    if old_time not in keys:
        return None
    keys[new_time] = keys.pop(old_time)
    return new_time


def stop_key_records(clip, index, field):
    """[(time, value, 'linear', None, None)] for the clipboard and group moves."""
    return [(int(time), stop_value_at(clip, index, field, int(time)), "linear", None, None)
            for time in stop_field_keys(clip, index, field)]


def stop_replace_keys(clip, index, field, records):
    stops = ensure_shape(clip).get("gradient_stops") or []
    if not 0 <= int(index) < len(stops) or not isinstance(stops[int(index)], dict):
        return
    keys = {int(row[0]): row[1] for row in records or ()}
    stops[int(index)][field] = {"keys": keys}


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
        "gradient_enabled": bool(data.get("gradient_enabled")),
        "gradient_type": str(data.get("gradient_type") or "linear"),
        "gradient_stops": resolve_stops(clip, frame),
        "stroke_join": str(data.get("stroke_join") or "miter"),
        "path": PATHS.at(data.get("path") or {"points": [], "closed": False}, frame),
        "align": str(data.get("stroke_align") or "center"),
        "cap": str(data.get("cap") or "butt"),
        "arrow_start": bool(data.get("arrow_start")),
        "arrow_end": bool(data.get("arrow_end")),
    }
    if spec["align"] not in ALIGNMENTS:
        spec["align"] = "center"
    if spec["cap"] not in CAPS:
        spec["cap"] = "butt"
    if spec["stroke_join"] not in JOINS:
        spec["stroke_join"] = "miter"
    if spec["gradient_type"] not in GRADIENT_TYPES:
        spec["gradient_type"] = "linear"
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
