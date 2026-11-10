"""Generated text clips: content, typography and animation live in the clip.

A text clip is an ordinary video clip whose picture is drawn instead of decoded: it has
the same transform channels, effects, fades and export path as any other clip. Two
things make it special:

* the visible string is a **held** value, so `{"keys": {0: "Chapter 1", 90: "Chapter 2"}}`
  changes the words at frame 90 without splitting the clip;
* every numeric typography parameter can be an ``AnimatedValue`` like any other
  channel, addressed as ``text.<name>`` (see :mod:`meenifx.model.channels`).

Everything here is toolkit-free so the renderer and the inspector share one definition.
"""

from __future__ import annotations

import copy

from .keyframes import AnimatedValue

TEXT_PREFIX = "text."
CONTENT = "content"

DEFAULTS = {
    "text": "Text",
    "keys": {},
    "font": "Segoe UI",
    "size": 96.0,
    "bold": False,
    "italic": False,
    "underline": False,
    "strikethrough": False,
    "color": "#FFFFFF",
    "stroke_color": "#000000",
    "stroke_width": 0.0,
    # Outline opacity is independent of the fill and of the clip's own opacity.
    "stroke_opacity": 100.0,
    # Two-stop linear gradient across the whole text block (0 = solid fill).
    "gradient": False,
    "gradient_color": "#7DD3FC",
    "gradient_angle": 90.0,
    "shadow_color": "#000000",
    "shadow_opacity": 0.55,
    "shadow_offset_x": 4.0,
    "shadow_offset_y": 4.0,
    "shadow_blur": 6.0,
    # Fill opacity belongs to the glyphs only; the shadow and the background plate keep
    # their own opacity, and the clip itself uses the universal transform opacity.
    "fill_opacity": 100.0,
    # Whole-glyph blur (used by the Blur In/Out presets); 0 = sharp.
    "blur": 0.0,
    # Per-character reveal: -1 shows every character, 0 hides the text, N shows N glyphs.
    "reveal": -1.0,
    "bg_color": "#000000",
    "bg_opacity": 0.0,
    "bg_padding": 24.0,
    "bg_radius": 0.0,
    "align": "center",
    # Block anchor: where the whole paragraph sits vertically on the frame.
    "anchor": "center",
    "line_spacing": 1.15,
    "letter_spacing": 0.0,
    "word_spacing": 0.0,
    # Uppercase/lowercase conversion happens before layout, so metrics follow the text.
    "case": "none",
    "offset_x": 0.0,
    "offset_y": 0.0,
    "wrap": 0.0,
}

# Parameters that can carry keyframes (channel ids are "text.<name>").
NUMERIC = ("size", "stroke_width", "stroke_opacity", "shadow_offset_x", "shadow_offset_y",
           "shadow_blur", "shadow_opacity", "fill_opacity", "blur", "reveal",
           "bg_opacity", "bg_padding", "bg_radius", "line_spacing", "letter_spacing",
           "word_spacing", "gradient_angle", "offset_x", "offset_y", "wrap")

# label, unit, minimum, maximum, step, decimals, slider span
SPECS = {
    "size": ("Size", "px", 4.0, 400.0, 0.5, 1, 200.0),
    "stroke_width": ("Stroke", "px", 0.0, 40.0, 0.1, 1, 20.0),
    "stroke_opacity": ("Outline opacity", "%", 0.0, 100.0, 1.0, 0, 100.0),
    "gradient_angle": ("Gradient angle", "\u00b0", -180.0, 180.0, 1.0, 0, 180.0),
    "shadow_offset_x": ("Shadow X", "px", -200.0, 200.0, 0.5, 1, 100.0),
    "shadow_offset_y": ("Shadow Y", "px", -200.0, 200.0, 0.5, 1, 100.0),
    # Every opacity is labelled with what it affects: this one never changes the text.
    "shadow_blur": ("Shadow blur", "px", 0.0, 80.0, 0.5, 1, 40.0),
    "shadow_opacity": ("Shadow opacity", "%", 0.0, 100.0, 1.0, 0, 100.0),
    "fill_opacity": ("Fill opacity", "%", 0.0, 100.0, 1.0, 0, 100.0),
    "blur": ("Text blur", "px", 0.0, 80.0, 0.5, 1, 40.0),
    "reveal": ("Letters shown", "", -1.0, 4000.0, 1.0, 0, 200.0),
    "bg_opacity": ("Background opacity", "%", 0.0, 100.0, 1.0, 0, 100.0),
    "bg_padding": ("Padding", "px", 0.0, 200.0, 1.0, 0, 60.0),
    "bg_radius": ("Corner radius", "px", 0.0, 200.0, 1.0, 0, 60.0),
    "line_spacing": ("Line gap", "x", 0.5, 3.0, 0.01, 2, 1.0),
    "letter_spacing": ("Tracking", "px", -40.0, 80.0, 0.5, 1, 40.0),
    "word_spacing": ("Word spacing", "px", -40.0, 200.0, 0.5, 1, 80.0),
    "offset_x": ("Block X", "px", -2000.0, 2000.0, 1.0, 1, 800.0),
    "offset_y": ("Block Y", "px", -2000.0, 2000.0, 1.0, 1, 800.0),
    "wrap": ("Wrap", "px", 0.0, 4000.0, 4.0, 0, 1000.0),
}

# Common fallback families, kept for old projects and for code paths without Qt. The
# inspector and renderer use the installed-font registry, not this list.
FAMILIES = ("Segoe UI", "Arial", "Calibri", "Verdana", "Tahoma", "Trebuchet MS", "Georgia",
            "Times New Roman", "Cambria", "Courier New", "Consolas", "Impact", "Comic Sans MS",
            "DejaVu Sans", "DejaVu Serif", "Liberation Sans")

ALIGNMENTS = ("left", "center", "right", "justify")
ANCHORS = ("top", "center", "bottom")
CASES = ("none", "upper", "lower", "title")


def blank():
    """A fresh settings dict for a new text clip."""
    return copy.deepcopy(DEFAULTS)


def ensure_text(clip):
    """Fill a clip's text settings with defaults, keeping anything already stored."""
    if getattr(clip, "text", None) is None:
        clip.text = {}
    data = clip.text
    for name, value in DEFAULTS.items():
        if name not in data:
            data[name] = copy.deepcopy(value)
    keys = data.get("keys")
    if not isinstance(keys, dict):
        data["keys"] = {}
    else:
        # Keep the dict object when it is already normalised: callers hold a reference to
        # it while editing (renaming a key, typing), and rebuilding it would silently
        # drop those writes.
        converted, dirty = {}, False
        for frame, text in keys.items():
            try:
                number = int(frame)
            except (TypeError, ValueError):
                dirty = True
                continue
            converted[number] = str(text)
            if not isinstance(frame, int) or number != frame:
                dirty = True
        if dirty or len(converted) != len(keys):
            data["keys"] = converted
    return data


def is_text_clip(clip):
    """True for a generated text clip (it has text settings and no media file)."""
    data = getattr(clip, "text", None)
    return isinstance(data, dict) and bool(data)


def text_channel(name):
    return TEXT_PREFIX + name


def content_channel():
    return TEXT_PREFIX + CONTENT


# --------------------------------------------------------------------- content keys
def content_keys(clip):
    """Frames that carry a text keyframe, sorted."""
    return sorted(ensure_text(clip)["keys"])


def content_at(clip, frame):
    """The words shown at a clip-local frame; keys hold until the next one."""
    data = ensure_text(clip)
    keys = data["keys"]
    if not keys:
        return str(data.get("text", ""))
    frame = int(frame)
    candidates = [time for time in keys if time <= frame]
    if not candidates:
        return str(keys[min(keys)])
    return str(keys[max(candidates)])


def set_content(clip, value, frame=0, auto_key=False):
    """Write the words. Returns True when a keyframe was created or updated.

    With Auto-Keyframe on (or once the text is already animated) the value is stored as
    a held key at `frame`; otherwise it replaces the clip's single static string.
    """
    data = ensure_text(clip)
    keys = data["keys"]
    frame = max(0, int(frame))
    value = "" if value is None else str(value)
    if keys or auto_key:
        keys[frame] = value
        if not keys:
            keys[0] = value
        return True
    data["text"] = value
    return False


def has_content_key(clip, frame):
    return int(frame) in ensure_text(clip)["keys"]


def toggle_content_key(clip, frame):
    """Diamond button: key the current words here, or drop the key again."""
    data = ensure_text(clip)
    keys = data["keys"]
    frame = max(0, int(frame))
    if frame in keys:
        del keys[frame]
        return False
    keys[frame] = content_at(clip, frame)
    return True


def move_content_key(clip, old_time, new_time):
    """Move one text key in time (live drag); returns the frame it landed on."""
    keys = ensure_text(clip)["keys"]
    old_time, new_time = int(old_time), max(0, int(new_time))
    if old_time not in keys:
        return None
    if new_time == old_time:
        return new_time
    keys[new_time] = keys.pop(old_time)
    return new_time


def remove_content_key(clip, frame):
    keys = ensure_text(clip)["keys"]
    return keys.pop(int(frame), None) is not None


# ------------------------------------------------------------------- numeric values
def stored(clip, name):
    return ensure_text(clip).get(name)


def is_animated(clip, name):
    return isinstance(stored(clip, name), AnimatedValue)


def value_at(clip, name, frame=0):
    raw = stored(clip, name)
    if isinstance(raw, AnimatedValue):
        return raw.value_at(frame)
    return raw


def set_value(clip, name, value, frame=0, auto_key=False):
    """Set a numeric typography parameter, honouring the Auto-Keyframe rules."""
    data = ensure_text(clip)
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
    data = ensure_text(clip)
    current = data.get(name)
    if isinstance(current, AnimatedValue):
        return current
    animated = AnimatedValue(base_value=float(current or 0.0))
    animated.set_keyframe(max(0, int(frame)), float(current or 0.0))
    data[name] = animated
    return animated


def unanimate(clip, name, frame=0):
    data = ensure_text(clip)
    current = data.get(name)
    if isinstance(current, AnimatedValue):
        data[name] = float(current.value_at(frame))


def key_times(clip, name):
    raw = stored(clip, name)
    return [key.time for key in raw.keyframes] if isinstance(raw, AnimatedValue) else []


def toggle_key(clip, name, frame):
    data = ensure_text(clip)
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


# ------------------------------------------------------------------------ render spec
def resolve(clip, frame):
    """Every setting of a text clip evaluated at a frame, ready for the renderer."""
    data = ensure_text(clip)
    frame = int(frame)
    spec = {
        "text": content_at(clip, frame),
        "font": str(data.get("font") or DEFAULTS["font"]),
        "bold": bool(data.get("bold")),
        "italic": bool(data.get("italic")),
        "underline": bool(data.get("underline")),
        "strikethrough": bool(data.get("strikethrough")),
        "color": str(data.get("color") or "#FFFFFF"),
        "stroke_color": str(data.get("stroke_color") or "#000000"),
        "shadow_color": str(data.get("shadow_color") or "#000000"),
        "bg_color": str(data.get("bg_color") or "#000000"),
        "gradient": bool(data.get("gradient")),
        "gradient_color": str(data.get("gradient_color") or "#7DD3FC"),
        "align": str(data.get("align") or "center"),
        "anchor": str(data.get("anchor") or "center"),
        "case": str(data.get("case") or "none"),
    }
    for name in NUMERIC:
        spec[name] = float(value_at(clip, name, frame) or 0.0)
    spec["shadow_opacity"] = max(0.0, min(100.0, spec["shadow_opacity"])) / 100.0
    spec["bg_opacity"] = max(0.0, min(100.0, spec["bg_opacity"])) / 100.0
    spec["fill_opacity"] = max(0.0, min(100.0, spec["fill_opacity"])) / 100.0
    spec["blur"] = max(0.0, spec["blur"])
    spec["reveal"] = int(round(spec["reveal"]))
    spec["line_spacing"] = max(0.5, min(3.0, spec["line_spacing"]))
    spec["stroke_opacity"] = max(0.0, min(100.0, spec["stroke_opacity"])) / 100.0
    if spec["align"] not in ALIGNMENTS:
        spec["align"] = "center"
    if spec["anchor"] not in ANCHORS:
        spec["anchor"] = "center"
    if spec["case"] not in CASES:
        spec["case"] = "none"
    return spec


def clone_settings(source, target):
    """Copy another text clip's styling (not its animation) onto `target`."""
    data = ensure_text(source)
    for name in DEFAULTS:
        if name in ("text", "keys"):
            continue
        target_value = data.get(name)
        if isinstance(target_value, AnimatedValue):
            target_value = target_value.value_at(0)
        ensure_text(target)[name] = target_value
    return target
