"""One channel vocabulary for every animatable property of a clip.

The inspector's diamonds, the timeline's keyframe rows, the graph editor, the preview
overlay and the project file all address animation through these ids:

* ``transform.<channel>``           - position, scale, rotation, anchor, opacity
* ``fx.<effect_id>.<param>``        - an effect parameter
* ``fx.<effect_id>.pin.<pin>.x|y``  - one puppet-warp pin position
* ``text.<param>``                  - a text clip's typography (``text.content`` = words)

Keeping the ids in the model (instead of the UI) means a property can be animated from
any surface and every other surface sees it immediately.
"""

from __future__ import annotations

from . import animation as ANIM
from . import shape as SHAPE
from . import text as TXT
from . import transform as T
from .keyframes import AnimatedValue, Keyframe

TRANSFORM_PREFIX = "transform."
AUDIO_PREFIX = "audio."
PIN_MARK = ".pin."
GAIN = "gain"


def transform_channel(name):
    return TRANSFORM_PREFIX + name


def gain_channel():
    """The audio clip's own gain, in dB (negative and positive, silence at the floor)."""
    return AUDIO_PREFIX + GAIN


def text_channel(name):
    return TXT.text_channel(name)


def text_content_channel():
    return TXT.content_channel()


def shape_channel(name):
    return SHAPE.shape_channel(name)


def effect_channel(effect_id, name):
    return f"fx.{effect_id}.{name}"


def pin_channel(effect_id, pin_id, axis):
    return f"fx.{effect_id}{PIN_MARK}{pin_id}.{axis}"


def parse(channel):
    """('transform', None, name) | ('effect', effect_id, param) | ('pin', effect_id, 'id.axis')."""
    text = str(channel or "")
    if text.startswith(TRANSFORM_PREFIX):
        return "transform", None, text[len(TRANSFORM_PREFIX):]
    if text.startswith(AUDIO_PREFIX):
        return "audio", None, text[len(AUDIO_PREFIX):]
    if text.startswith(TXT.TEXT_PREFIX):
        return "text", None, text[len(TXT.TEXT_PREFIX):]
    if text.startswith(SHAPE.SHAPE_PREFIX):
        return "shape", None, text[len(SHAPE.SHAPE_PREFIX):]
    if text.startswith("fx."):
        rest = text[3:]
        effect_id, _sep, tail = rest.partition(".")
        if PIN_MARK.strip(".") in tail and tail.startswith("pin."):
            return "pin", effect_id, tail[4:]
        return "effect", effect_id, tail
    return "transform", None, text


def pin_parts(channel):
    """(pin_id, 'x'|'y') for a pin channel."""
    kind, _effect_id, tail = parse(channel)
    if kind != "pin":
        return None, None
    pin_id, _sep, axis = tail.rpartition(".")
    try:
        pin_id = int(pin_id)
    except ValueError:
        return None, None
    return pin_id, axis


def _entry(clip, effect_id):
    for entry in getattr(clip, "effects", ()) or ():
        if entry.get("effect_id") == effect_id:
            return entry
    return None


def pins_of(clip, effect_id):
    """The pin list of an effect entry (created on demand)."""
    entry = _entry(clip, effect_id)
    if entry is None:
        return []
    pins = entry.get("pins")
    if not isinstance(pins, list):
        entry["pins"] = pins = []
    return pins


def find_pin(clip, effect_id, pin_id):
    for pin in pins_of(clip, effect_id):
        if int(pin.get("id", -1)) == int(pin_id):
            return pin
    return None


def next_pin_id(clip, effect_id):
    pins = pins_of(clip, effect_id)
    return max((int(pin.get("id", 0)) for pin in pins), default=0) + 1


def stored(clip, channel):
    """The raw stored value (number, bool or AnimatedValue), or None."""
    kind, effect_id, name = parse(channel)
    if kind == "transform":
        return T.ensure_transform(clip).get(name)
    if kind == "audio":
        return getattr(clip, GAIN, ANIM.GAIN_DEFAULT_DB)
    if kind == "text":
        if name == TXT.CONTENT:
            return TXT.content_at(clip, 0)
        return TXT.stored(clip, name)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.color_at(clip, name, 0)
        return SHAPE.stored(clip, name)
    entry = _entry(clip, effect_id)
    if entry is None:
        return None
    if kind == "effect":
        return entry.get("params", {}).get(name)
    pin_id, axis = pin_parts(channel)
    pin = find_pin(clip, effect_id, pin_id) if pin_id is not None else None
    return None if pin is None else pin.get(axis)


def value_at(clip, channel, frame):
    """Evaluated value at a clip-local frame."""
    kind, _effect_id, name = parse(channel)
    if kind == "text":
        if name == TXT.CONTENT:
            return TXT.content_at(clip, frame)
        return TXT.value_at(clip, name, frame)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.color_at(clip, name, frame)
        return SHAPE.value_at(clip, name, frame)
    if kind == "audio":
        raw = getattr(clip, GAIN, ANIM.GAIN_DEFAULT_DB)
        return raw.value_at(frame) if isinstance(raw, AnimatedValue) else raw
    if kind == "transform" and name in ("position_x", "position_y"):
        x, y = T.position_at(clip, frame)
        return x if name == "position_x" else y
    raw = stored(clip, channel)
    if isinstance(raw, AnimatedValue):
        return raw.value_at(frame)
    return raw


def as_number(value):
    """`value` as a finite float, or None when it is not a plottable number.

    Bools are deliberately not numbers here: a flag is a step, not a curve. NaN and
    infinity are rejected too - a curve editor cannot place them, and drawing them
    produced paths Qt could not print.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def numeric_channel(clip, channel, resolver=None):
    """True when this channel can be drawn as a curve at all.

    Text words, choice parameters (a blend mode, a colour preset) and booleans have no
    numeric range. The Graph Editor used to try to plot them and took the process down
    with a ``float("warm")`` inside ``paintEvent``; this is the one predicate that both
    the editor and the project loader ask.
    """
    meta = spec(clip, channel, resolver)
    if not isinstance(meta, dict) or meta.get("kind") not in ("float", "int"):
        return False
    return as_number(value_at(clip, channel, 0)) is not None


def numeric_value_at(clip, channel, frame):
    """Evaluated value as a finite float, or None when the channel is not numeric."""
    return as_number(value_at(clip, channel, frame))


def is_animated(clip, channel):
    kind, _effect_id, name = parse(channel)
    if kind == "text":
        if name == TXT.CONTENT:
            return bool(TXT.content_keys(clip))
        return TXT.is_animated(clip, name)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.color_is_animated(clip, name)
        return SHAPE.is_animated(clip, name)
    return isinstance(stored(clip, channel), AnimatedValue)


def set_value(clip, channel, value, frame=0, auto_key=False):
    """Write a value through the same rules the inspector and the preview use."""
    kind, effect_id, name = parse(channel)
    if kind == "transform":
        return T.set_channel(clip, name, value, frame, auto_key)
    if kind == "audio":
        return _set_gain(clip, value, frame, auto_key)
    if kind == "text":
        if name == TXT.CONTENT:
            return TXT.set_content(clip, value, frame, auto_key)
        return TXT.set_value(clip, name, value, frame, auto_key)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.set_color(clip, name, value, frame, auto_key)
        return SHAPE.set_value(clip, name, value, frame, auto_key)
    entry = _entry(clip, effect_id)
    if entry is None:
        return False
    frame = int(frame or 0)
    if kind == "effect":
        params = entry.setdefault("params", {})
        current = params.get(name)
        if isinstance(current, AnimatedValue):
            key = current.key_at(frame)
            if key is not None:
                key.value = value
                return True
            if auto_key:
                current.set_keyframe(frame, value)
                return True
            if isinstance(current.keyframes[0].value, (int, float)) and isinstance(value, (int, float)):
                delta = value - current.value_at(frame)
                for item in current.keyframes:
                    item.value = item.value + delta
            return False
        if auto_key and isinstance(value, (int, float)):
            params[name] = _animated_from(current, frame, value)
            return True
        params[name] = value
        return False
    pin_id, axis = pin_parts(channel)
    pin = find_pin(clip, effect_id, pin_id) if pin_id is not None else None
    if pin is None:
        return False
    current = pin.get(axis)
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
        pin[axis] = _animated_from(current, frame, float(value))
        return True
    pin[axis] = float(value)
    return False


def _animated_from(old_value, frame, new_value):
    """AnimatedValue that keeps the old pose at the clip start and the new one here.

    Auto-Keyframe must not silently rewrite the animation that already exists before
    the playhead, so the previous value is kept as a keyframe at frame 0.
    """
    animated = AnimatedValue(base_value=old_value)
    if isinstance(old_value, (int, float)) and int(frame) > 0:
        animated.set_keyframe(0, old_value)
    animated.set_keyframe(int(frame), new_value)
    return animated


def animate(clip, channel, frame):
    """Turn a property into an animated one, keyed at `frame`."""
    kind, effect_id, name = parse(channel)
    if kind == "transform":
        return T.animate_channel(clip, name, frame)
    if kind == "audio":
        current = getattr(clip, GAIN, ANIM.GAIN_DEFAULT_DB)
        if isinstance(current, AnimatedValue):
            return current
        animated = AnimatedValue(base_value=current)
        animated.set_keyframe(int(frame), current)
        clip.gain = animated
        return animated
    if kind == "text":
        return TXT.animate(clip, name, frame)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            if SHAPE.color_is_animated(clip, name):
                return SHAPE.color_at(clip, name, frame)
            SHAPE.set_color(clip, name, SHAPE.color_at(clip, name, frame), frame, True)
            return SHAPE.color_at(clip, name, frame)
        return SHAPE.animate(clip, name, frame)
    entry = _entry(clip, effect_id)
    if entry is None:
        return None
    frame = int(frame)
    if kind == "effect":
        params = entry.setdefault("params", {})
        current = params.get(name)
        if isinstance(current, AnimatedValue):
            return current
        value = current
        animated = AnimatedValue(base_value=value)
        animated.set_keyframe(frame, value)
        params[name] = animated
        return animated
    pin_id, axis = pin_parts(channel)
    pin = find_pin(clip, effect_id, pin_id) if pin_id is not None else None
    if pin is None:
        return None
    current = pin.get(axis)
    if isinstance(current, AnimatedValue):
        return current
    animated = AnimatedValue(base_value=current)
    animated.set_keyframe(frame, current)
    pin[axis] = animated
    return animated


def unanimate(clip, channel, frame):
    kind, effect_id, name = parse(channel)
    if kind == "transform":
        return T.unanimate_channel(clip, name, frame)
    if kind == "audio":
        current = getattr(clip, GAIN, ANIM.GAIN_DEFAULT_DB)
        if isinstance(current, AnimatedValue):
            clip.gain = float(current.value_at(frame))
        return
    if kind == "text":
        return TXT.unanimate(clip, name, frame)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            colour = SHAPE.color_at(clip, name, frame)
            SHAPE.ensure_shape(clip)[name] = colour
            return
        return SHAPE.unanimate(clip, name, frame)
    entry = _entry(clip, effect_id)
    if entry is None:
        return
    if kind == "effect":
        params = entry.setdefault("params", {})
        current = params.get(name)
        if isinstance(current, AnimatedValue):
            params[name] = current.value_at(frame)
        return
    pin_id, axis = pin_parts(channel)
    pin = find_pin(clip, effect_id, pin_id) if pin_id is not None else None
    if pin is not None and isinstance(pin.get(axis), AnimatedValue):
        pin[axis] = float(pin[axis].value_at(frame))


def key_times(clip, channel):
    kind, _effect_id, name = parse(channel)
    if kind == "text":
        if name == TXT.CONTENT:
            return TXT.content_keys(clip)
        return TXT.key_times(clip, name)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.color_keys(clip, name)
        return SHAPE.key_times(clip, name)
    raw = stored(clip, channel)
    return [key.time for key in raw.keyframes] if isinstance(raw, AnimatedValue) else []


def has_key(clip, channel, frame):
    kind, _effect_id, name = parse(channel)
    if kind == "text":
        if name == TXT.CONTENT:
            return TXT.has_content_key(clip, frame)
        return int(frame) in TXT.key_times(clip, name)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.has_color_key(clip, name, frame)
        return int(frame) in SHAPE.key_times(clip, name)
    raw = stored(clip, channel)
    return isinstance(raw, AnimatedValue) and raw.key_at(int(frame)) is not None


def ensure_key(clip, channel, frame):
    """Add a keyframe at `frame` if there is none (never removes one). Returns True on add."""
    kind, _effect_id, name = parse(channel)
    if kind == "text" and name == TXT.CONTENT:
        if TXT.has_content_key(clip, frame):
            return False
        TXT.toggle_content_key(clip, frame)
        return True
    if kind == "shape" and name in SHAPE.COLOR_FIELDS:
        if SHAPE.has_color_key(clip, name, frame):
            return False
        SHAPE.toggle_color_key(clip, name, frame)
        return True
    frame = max(0, int(frame))
    raw = stored(clip, channel)
    if isinstance(raw, AnimatedValue):
        if raw.key_at(frame) is not None:
            return False
        raw.set_keyframe(frame, raw.value_at(frame))
        return True
    animate(clip, channel, frame)
    return True


def remove_key(clip, channel, frame):
    """Delete one keyframe from any channel (text words included)."""
    kind, _effect_id, name = parse(channel)
    if kind == "text" and name == TXT.CONTENT:
        return TXT.remove_content_key(clip, frame)
    if kind == "shape" and name in SHAPE.COLOR_FIELDS:
        return SHAPE.remove_color_key(clip, name, frame)
    raw = stored(clip, channel)
    return bool(isinstance(raw, AnimatedValue) and raw.remove_keyframe(frame))


def toggle_key(clip, channel, frame):
    """Diamond: add a key at `frame`, or delete the existing one."""
    kind, effect_id, name = parse(channel)
    if kind == "transform":
        return T.toggle_keyframe(clip, name, frame)
    if kind == "text":
        if name == TXT.CONTENT:
            return TXT.toggle_content_key(clip, frame)
        return TXT.toggle_key(clip, name, frame)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.toggle_color_key(clip, name, frame)
        return SHAPE.toggle_key(clip, name, frame)
    frame = int(frame)
    raw = stored(clip, channel)
    if not isinstance(raw, AnimatedValue):
        animate(clip, channel, frame)
        return True
    if raw.remove_keyframe(frame):
        return False
    raw.set_keyframe(frame, raw.value_at(frame))
    return True


def set_key_time(clip, channel, old_time, new_time):
    """Move one keyframe in time (live drag); returns the frame it landed on."""
    kind, _effect_id, name = parse(channel)
    if kind == "text":
        if name == TXT.CONTENT:
            return TXT.move_content_key(clip, old_time, new_time)
        return TXT.set_key_time(clip, name, old_time, new_time)
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            return SHAPE.move_color_key(clip, name, old_time, new_time)
        return SHAPE.set_key_time(clip, name, old_time, new_time)
    raw = stored(clip, channel)
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


def spec(clip, channel, resolver=None):
    """Display metadata for a channel: label, unit, range, step, decimals, group."""
    kind, effect_id, name = parse(channel)
    if kind == "transform":
        label, unit, minimum, maximum, step, decimals = T.SPECS.get(name, (name, "", -1e9, 1e9, 1.0, 2))
        group = "Transform"
        if name == "opacity":
            # One name for one thing: this is the whole clip's composited transparency,
            # not a text fill or an effect opacity.
            label, group = "Clip Opacity", "Compositing"
        return {"label": label, "unit": unit, "minimum": minimum, "maximum": maximum,
                "step": step, "decimals": decimals, "group": group, "kind": "float"}
    if kind == "audio":
        return {"label": "Gain", "unit": " dB", "minimum": ANIM.GAIN_FLOOR_DB,
                "maximum": ANIM.GAIN_CEILING_DB, "step": 0.5, "decimals": 1,
                "group": "Audio", "kind": "float", "qty": ANIM.GAIN_CEILING_DB - ANIM.GAIN_FLOOR_DB}
    if kind == "text":
        if name == TXT.CONTENT:
            return {"label": "Text", "unit": "", "minimum": 0, "maximum": 0, "step": 1,
                    "decimals": 0, "group": "Text", "kind": "text"}
        label, unit, minimum, maximum, step, decimals, span = TXT.SPECS.get(
            name, (name.replace("_", " ").title(), "", -1e9, 1e9, 1.0, 2, 100.0))
        return {"label": label, "unit": unit, "minimum": minimum, "maximum": maximum,
                "step": step, "decimals": decimals, "group": "Text", "kind": "float",
                "qty": span}
    if kind == "shape":
        if name in SHAPE.COLOR_FIELDS:
            label = "Fill Color" if name == "fill_color" else "Stroke Color"
            return {"label": label, "unit": "", "minimum": 0, "maximum": 0, "step": 1,
                    "decimals": 0, "group": "Shape", "kind": "color"}
        label, unit, minimum, maximum, step, decimals, span = SHAPE.SPECS.get(
            name, (name.replace("_", " ").title(), "", -1e9, 1e9, 1.0, 2, 100.0))
        return {"label": label, "unit": unit, "minimum": minimum, "maximum": maximum,
                "step": step, "decimals": decimals, "group": "Shape", "kind": "float",
                "qty": span}
    runtime = resolver.get(effect_id) if isinstance(resolver, dict) else (
        resolver(effect_id) if callable(resolver) else None)
    effect_name = getattr(runtime, "name", effect_id.replace("_", " ").title())
    if kind == "effect":
        param = runtime.spec(name) if runtime is not None and hasattr(runtime, "spec") else None
        if param is not None:
            return {"label": param.label, "unit": param.unit, "minimum": param.minimum,
                    "maximum": param.maximum, "step": param.step,
                    "decimals": 0 if param.kind == "int" else 2, "group": effect_name,
                    "kind": param.kind}
        return {"label": name.replace("_", " ").title(), "unit": "", "minimum": -1e9,
                "maximum": 1e9, "step": 1.0, "decimals": 2, "group": effect_name, "kind": "float"}
    pin_id, axis = pin_parts(channel)
    return {"label": f"Pin {pin_id} {axis.upper()}", "unit": "px", "minimum": -4.0,
            "maximum": 5.0, "step": 0.002, "decimals": 3, "group": f"{effect_name} pins",
            "kind": "float", "normalized": True}


def list_channels(clip, resolver=None, include_static_effects=False):
    """Every channel of a clip, transform first, then effects in pipeline order."""
    rows = []
    if getattr(clip, "is_shape", False):
        for name in SHAPE.NUMERIC:
            channel = SHAPE.shape_channel(name)
            meta = spec(clip, channel, resolver)
            rows.append({**meta, "id": channel, "name": name,
                         "animated": SHAPE.is_animated(clip, name),
                         "value": SHAPE.value_at(clip, name, 0)})
        for name in SHAPE.COLOR_FIELDS:
            if not SHAPE.color_keys(clip, name):
                continue
            channel = SHAPE.shape_channel(name)
            meta = spec(clip, channel, resolver)
            rows.append({**meta, "id": channel, "name": name, "animated": True,
                         "value": SHAPE.color_at(clip, name, 0)})
    if getattr(clip, "is_text", False):
        for name in TXT.NUMERIC:
            channel = TXT.text_channel(name)
            meta = spec(clip, channel, resolver)
            rows.append({**meta, "id": channel, "name": name, "animated": TXT.is_animated(clip, name),
                         "value": TXT.value_at(clip, name, 0)})
        if TXT.content_keys(clip):
            channel = TXT.content_channel()
            meta = spec(clip, channel, resolver)
            rows.append({**meta, "id": channel, "name": "content", "animated": True,
                         "value": TXT.content_at(clip, 0)})
    transform = T.ensure_transform(clip)
    for name in T.TRANSFORM_CHANNELS:
        channel = transform_channel(name)
        meta = spec(clip, channel, resolver)
        rows.append({**meta, "id": channel, "name": name, "animated": is_animated(clip, channel),
                     "value": value_at(clip, channel, 0)})
    if getattr(clip, "track", None) is not None and clip.track.type == "audio":
        channel = gain_channel()
        meta = spec(clip, channel, resolver)
        rows.append({**meta, "id": channel, "name": GAIN, "animated": is_animated(clip, channel),
                     "value": value_at(clip, channel, 0)})
    for entry in getattr(clip, "effects", ()) or ():
        effect_id = entry.get("effect_id")
        for name, raw in (entry.get("params") or {}).items():
            if not isinstance(raw, AnimatedValue) and not include_static_effects:
                continue
            channel = effect_channel(effect_id, name)
            meta = spec(clip, channel, resolver)
            rows.append({**meta, "id": channel, "name": name, "animated": is_animated(clip, channel),
                         "value": raw.value_at(0) if isinstance(raw, AnimatedValue) else raw})
        for pin in entry.get("pins") or ():
            for axis in ("x", "y"):
                if not isinstance(pin.get(axis), AnimatedValue):
                    continue
                channel = pin_channel(effect_id, pin.get("id"), axis)
                meta = spec(clip, channel, resolver)
                rows.append({**meta, "id": channel, "name": f"pin{pin.get('id')}{axis}",
                             "animated": True, "value": value_at(clip, channel, 0)})
    return rows


def animated_channels(clip, resolver=None):
    return [row for row in list_channels(clip, resolver) if row["animated"]]


def all_keyframe_frames(clip):
    """Every clip-local frame carrying a keyframe in transform, effects or pins."""
    frames = set()
    for row in list_channels(clip):
        frames.update(key_times(clip, row["id"]))
    return frames


def keyframe_entries(clip):
    """[(channel, time)] for every keyframe of a clip (timeline rows, copy/paste)."""
    entries = []
    for row in list_channels(clip):
        entries.extend((row["id"], time) for time in key_times(clip, row["id"]))
    return entries


# --------------------------------------------------------------- key utilities
def key_records(clip, channel):
    """[(time, value, interpolation, in_handle, out_handle)] of a channel's keyframes.

    Text words are held values, so they come back with a ``hold`` interpolation and no
    handles; every other channel is an ``AnimatedValue``. This is the one shape the
    timeline group-move, the clipboard and the text preset generator all move around.
    """
    kind, _effect_id, name = parse(channel)
    if kind == "text" and name == TXT.CONTENT:
        return [(int(time), TXT.content_at(clip, int(time)), "hold", None, None)
                for time in TXT.content_keys(clip)]
    if kind == "shape" and name in SHAPE.COLOR_FIELDS:
        return [(int(time), SHAPE.color_at(clip, name, int(time)), "hold", None, None)
                for time in SHAPE.color_keys(clip, name)]
    raw = stored(clip, channel)
    if not isinstance(raw, AnimatedValue):
        return []
    return [(int(key.time), key.value, key.interpolation,
             key.in_value_handle, key.out_value_handle) for key in raw.keyframes]


def replace_keys(clip, channel, records, base_value=None):
    """Replace a channel's keyframes with `records` (sorted, deduplicated by time)."""
    kind, effect_id, name = parse(channel)
    rows = {}
    for row in records or ():
        time = int(row[0])
        rows[time] = row
    ordered = [rows[time] for time in sorted(rows)]
    if kind == "text" and name == TXT.CONTENT:
        data = TXT.ensure_text(clip)
        data["keys"] = {int(row[0]): str(row[1]) for row in ordered}
        return
    if kind == "shape" and name in SHAPE.COLOR_FIELDS:
        SHAPE.ensure_shape(clip)[name] = {"keys": {int(row[0]): str(row[1])
                                                   for row in ordered}}
        return
    keys = [Keyframe(int(row[0]), row[1],
                     str(row[2]) if len(row) > 2 and row[2] else "linear",
                     in_value_handle=row[3] if len(row) > 3 else None,
                     out_value_handle=row[4] if len(row) > 4 else None)
            for row in ordered]
    base = base_value
    if base is None and isinstance(stored(clip, channel), AnimatedValue):
        base = stored(clip, channel).base_value
    animated = AnimatedValue(keys, base)
    if kind == "transform":
        T.ensure_transform(clip)[name] = animated
        return
    if kind == "audio":
        clip.gain = animated
        return
    if kind == "text":
        TXT.ensure_text(clip)[name] = animated
        return
    if kind == "shape":
        SHAPE.ensure_shape(clip)[name] = animated
        return
    entry = _entry(clip, effect_id)
    if entry is None:
        return
    if kind == "effect":
        entry.setdefault("params", {})[name] = animated
        return
    pin_id, axis = pin_parts(channel)
    pin = find_pin(clip, effect_id, pin_id) if pin_id is not None else None
    if pin is not None:
        pin[axis] = animated


def set_static(clip, channel, value):
    """Write a plain, unanimated value (used when a generated animation is removed)."""
    kind, effect_id, name = parse(channel)
    if kind == "transform":
        T.ensure_transform(clip)[name] = value
        return
    if kind == "audio":
        clip.gain = ANIM.clamp_gain_db(value)
        return
    if kind == "text":
        if name == TXT.CONTENT:
            data = TXT.ensure_text(clip)
            data["keys"] = {}
            data["text"] = "" if value is None else str(value)
            return
        TXT.ensure_text(clip)[name] = float(value)
        return
    if kind == "shape":
        SHAPE.ensure_shape(clip)[name] = (str(value) if name in SHAPE.COLOR_FIELDS
                                         else float(value))
        return
    entry = _entry(clip, effect_id)
    if entry is None:
        return
    if kind == "effect":
        entry.setdefault("params", {})[name] = value
        return
    pin_id, axis = pin_parts(channel)
    pin = find_pin(clip, effect_id, pin_id) if pin_id is not None else None
    if pin is not None:
        pin[axis] = float(value)


def _set_gain(clip, value, frame, auto_key):
    """Write the audio gain, honouring animation and the Auto-Keyframe rule."""
    frame = max(0, int(frame or 0))
    value = ANIM.clamp_gain_db(value)
    current = getattr(clip, GAIN, ANIM.GAIN_DEFAULT_DB)
    if isinstance(current, AnimatedValue):
        key = current.key_at(frame)
        if key is not None:
            key.value = value
            return True
        if auto_key:
            current.set_keyframe(frame, value)
            return True
        delta = value - float(current.value_at(frame))
        for item in current.keyframes:
            item.value = float(item.value) + delta
        return False
    if auto_key:
        animated = AnimatedValue(base_value=float(current or 0.0))
        if frame > 0:
            animated.set_keyframe(0, float(current or 0.0))
        animated.set_keyframe(frame, value)
        clip.gain = animated
        return True
    clip.gain = value
    return False
