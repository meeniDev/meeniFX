"""Shared animation helpers: gain math, envelope geometry and text animation presets.

Everything here is toolkit-free and works on the same channels the inspector, the
timeline, the graph editor, the preview and the exporter already use
(:mod:`meenifx.model.channels`). Nothing in this module paints or stores a second copy
of an animation: a text preset *is* a set of real keyframes, so it can be retimed,
re-eased or deleted from any surface afterwards.

Three groups of helpers live here:

* **audio gain** - dB <-> linear conversion with a configurable ceiling and a silent
  floor, shared by the mixer, the inspector and the timeline envelope;
* **envelopes** - the value <-> height mapping the timeline uses for the universal Clip
  Opacity line and the audio gain line;
* **text animations** - the preset table and the generator that turns a preset into
  keyframes on ``transform.*`` and ``text.*`` channels.
"""

from __future__ import annotations

import copy

from . import channels as CH
from .easing import preset_spec

# --------------------------------------------------------------- audio gain (dB)
# The gain line maps its whole height onto this range. Anything at or below the silent
# floor is rendered as true silence (gain 0), which is what "reduce to silence" means.
GAIN_FLOOR_DB = -60.0
GAIN_CEILING_DB = 12.0      # +12 dB of headroom; Playback preferences can change it
GAIN_DEFAULT_DB = 0.0


def set_gain_ceiling(db):
    """Change the boost headroom (clamped to a safe 0..24 dB). Returns the new value."""
    global GAIN_CEILING_DB
    try:
        value = float(db)
    except (TypeError, ValueError):
        return GAIN_CEILING_DB
    GAIN_CEILING_DB = max(0.0, min(24.0, value))
    return GAIN_CEILING_DB


def db_to_gain(db):
    """Linear amplitude for a dB value; the silent floor and below become exact silence."""
    try:
        value = float(db)
    except (TypeError, ValueError):
        value = GAIN_DEFAULT_DB
    if value <= GAIN_FLOOR_DB:
        return 0.0
    return float(10.0 ** (value / 20.0))


def gain_to_db(gain):
    """dB for a linear amplitude (0 -> the silent floor)."""
    try:
        value = float(gain)
    except (TypeError, ValueError):
        return GAIN_FLOOR_DB
    if value <= 1e-6:
        return GAIN_FLOOR_DB
    import math
    return max(GAIN_FLOOR_DB, min(GAIN_CEILING_DB, 20.0 * math.log10(value)))


def clamp_gain_db(db, ceiling=None):
    """Keep a gain value inside the safe range, allowing the floor for silence."""
    ceiling = GAIN_CEILING_DB if ceiling is None else float(ceiling)
    try:
        value = float(db)
    except (TypeError, ValueError):
        value = GAIN_DEFAULT_DB
    return max(GAIN_FLOOR_DB, min(max(0.0, ceiling), value))


def gain_text(db):
    """Display string: ``-inf`` at silence, else ``+3.0 dB`` / ``-6.0 dB``."""
    try:
        value = float(db)
    except (TypeError, ValueError):
        value = GAIN_DEFAULT_DB
    if value <= GAIN_FLOOR_DB + 1e-6:
        return "-\u221e dB"
    return f"{value:+.1f} dB"


# ------------------------------------------------------------------ envelopes
def value_to_fraction(value, channel):
    """0..1 height fraction for an envelope value (1 = top of the clip)."""
    if channel == "audio.gain":
        span = max(1e-6, GAIN_CEILING_DB - GAIN_FLOOR_DB)
        return max(0.0, min(1.0, (float(value) - GAIN_FLOOR_DB) / span))
    return max(0.0, min(1.0, float(value) / 100.0))


def fraction_to_value(fraction, channel):
    """Inverse of :func:`value_to_fraction`, in the channel's own unit."""
    fraction = max(0.0, min(1.0, float(fraction)))
    if channel == "audio.gain":
        return GAIN_FLOOR_DB + fraction * (GAIN_CEILING_DB - GAIN_FLOOR_DB)
    return fraction * 100.0


def envelope_text(value, channel):
    """Human readable envelope label: percent for opacity, dB for gain."""
    if channel == "audio.gain":
        return gain_text(value)
    return f"{float(value):.0f}%"


# ------------------------------------------------------- text animation presets
# (preset name, slot) for the two combo boxes in the Text card.
TEXT_IN_PRESETS = ("Fade In", "Slide In Left", "Slide In Right", "Slide In Up",
                   "Slide In Down", "Scale In", "Pop In", "Typewriter", "Blur In",
                   "Rotation In", "Bounce In")
TEXT_OUT_PRESETS = ("Fade Out", "Slide Out Left", "Slide Out Right", "Slide Out Up",
                    "Slide Out Down", "Scale Out", "Blur Out", "Rotation Out")
SLIDE_DIRECTIONS = ("Left", "Right", "Up", "Down", "Up Left", "Up Right", "Down Left", "Down Right")
TEXT_IN_PRESETS += tuple("Slide In " + direction for direction in SLIDE_DIRECTIONS[4:])
TEXT_OUT_PRESETS += tuple("Slide Out " + direction for direction in SLIDE_DIRECTIONS[4:])
TEXT_PRESETS = TEXT_IN_PRESETS + TEXT_OUT_PRESETS
SLOTS = ("in", "out")

# Channels a preset may touch. They are ordinary channels: the transform channels the
# preview already warps with, plus three text channels the text renderer reads.
PRESET_CHANNELS = ("transform.opacity", "transform.position_x", "transform.position_y",
                   "transform.scale_x", "transform.scale_y", "transform.rotation",
                   "text.reveal", "text.blur")

DEFAULT_DURATION_SECONDS = 0.5
DEFAULT_EASING = "Smooth"


def default_duration(fps, clip_duration):
    """Half a second, at least one frame and never more than a third of the clip."""
    frames = int(round(max(1.0, float(fps)) * DEFAULT_DURATION_SECONDS))
    return max(1, min(frames, max(1, int(clip_duration) // 3 or 1)))


def _text_length(clip, frame):
    from . import text as TXT
    return max(0, len(str(TXT.content_at(clip, frame))))


def _slide_distance(context):
    width, height = (context or {}).get("project_size") or (1920, 1080)
    return max(120.0, float(width) * 0.45), max(80.0, float(height) * 0.35)


def _plan(preset, slot, length, easing, base, context):
    """[(channel, [(offset, value, interpolation, in_handle, out_handle)])] for one preset."""
    length = max(1, int(length))
    dx, dy = _slide_distance(context)
    spec = preset_spec(easing) if easing else None
    interp = spec["interpolation"] if spec else "ease_in_out"
    handles = spec["handles"] if spec else None
    keys = {}

    def add(channel, rows, curve=None):
        keys[channel] = [(int(offset), float(value), curve or interp, None, None)
                         for offset, value in rows]

    def with_handles(channel):
        """Attach the easing curve to each segment: out handle left, in handle right."""
        if not handles or channel not in keys:
            return
        rows = keys[channel]
        x1, y1, x2, y2 = handles
        for index in range(len(rows) - 1):
            rows[index] = (rows[index][0], rows[index][1], rows[index][2],
                           rows[index][3], (float(x1), float(y1)))
            rows[index + 1] = (rows[index + 1][0], rows[index + 1][1], rows[index + 1][2],
                               (float(x2), float(y2)), rows[index + 1][4])

    opacity = base.get("transform.opacity", 100.0)
    x = base.get("transform.position_x", 0.0)
    y = base.get("transform.position_y", 0.0)
    scale_x = base.get("transform.scale_x", 100.0)
    scale_y = base.get("transform.scale_y", 100.0)
    rotation = base.get("transform.rotation", 0.0)
    blur = base.get("text.blur", 0.0)
    reveal = _text_length_fn(context)

    if preset == "Fade In":
        add("transform.opacity", [(0, 0.0), (length, opacity)])
    elif preset == "Fade Out":
        add("transform.opacity", [(0, opacity), (length, 0.0)])
    elif preset.startswith(("Slide In ", "Slide Out ")) and preset.rsplit(" ", 2)[-2:] in (
            ["Up", "Left"], ["Up", "Right"], ["Down", "Left"], ["Down", "Right"]):
        vertical, horizontal = preset.split()[-2:]
        vx = -dx if horizontal == "Left" else dx
        vy = -dy if vertical == "Up" else dy
        if slot == "in":
            # In directions identify the edge/corner the text arrives from.
            add("transform.position_x", [(0, x + vx), (length, x)])
            add("transform.position_y", [(0, y + vy), (length, y)])
        else:
            add("transform.position_x", [(0, x), (length, x + vx)])
            add("transform.position_y", [(0, y), (length, y + vy)])
    elif preset == "Slide In Left":
        add("transform.position_x", [(0, x - dx), (length, x)])
    elif preset == "Slide In Right":
        add("transform.position_x", [(0, x + dx), (length, x)])
    elif preset == "Slide In Up":
        add("transform.position_y", [(0, y + dy), (length, y)])
    elif preset == "Slide In Down":
        add("transform.position_y", [(0, y - dy), (length, y)])
    elif preset == "Slide Out Left":
        add("transform.position_x", [(0, x), (length, x - dx)])
    elif preset == "Slide Out Right":
        add("transform.position_x", [(0, x), (length, x + dx)])
    elif preset == "Slide Out Up":
        add("transform.position_y", [(0, y), (length, y - dy)])
    elif preset == "Slide Out Down":
        add("transform.position_y", [(0, y), (length, y + dy)])
    elif preset in ("Scale In", "Pop In", "Bounce In"):
        start = 0.0 if preset != "Bounce In" else 35.0
        add("transform.scale_x", [(0, start), (int(length * 0.7), 112.0), (length, scale_x)])
        add("transform.scale_y", [(0, start), (int(length * 0.7), 112.0), (length, scale_y)])
        add("transform.opacity", [(0, 0.0), (length, opacity)])
        if preset == "Bounce In":
            # A real bounce curve on the first segment, then a settle.
            keys["transform.scale_x"][0] = (0, start, "fn:bounce_out", None, None)
            keys["transform.scale_y"][0] = (0, start, "fn:bounce_out", None, None)
    elif preset == "Scale Out":
        add("transform.scale_x", [(0, scale_x), (length, 0.0)])
        add("transform.scale_y", [(0, scale_y), (length, 0.0)])
        add("transform.opacity", [(0, opacity), (length, 0.0)])
    elif preset == "Typewriter":
        # Per-character reveal: the text renderer draws only the first N glyphs, so this
        # is a real animation of a real channel that stays editable and exports.
        keys["text.reveal"] = [(0, 0.0, "linear", None, None),
                               (length, float(reveal), "linear", None, None)]
    elif preset == "Blur In":
        add("text.blur", [(0, max(18.0, blur + 18.0)), (length, blur)])
        add("transform.opacity", [(0, 0.0), (length, opacity)])
    elif preset == "Blur Out":
        add("text.blur", [(0, blur), (length, max(18.0, blur + 18.0))])
        add("transform.opacity", [(0, opacity), (length, 0.0)])
    elif preset == "Rotation In":
        add("transform.rotation", [(0, rotation - 25.0), (length, rotation)])
        add("transform.opacity", [(0, 0.0), (length, opacity)])
    elif preset == "Rotation Out":
        add("transform.rotation", [(0, rotation), (length, rotation + 25.0)])
        add("transform.opacity", [(0, opacity), (length, 0.0)])
    for channel in keys:
        with_handles(channel)
    return [(channel, rows) for channel, rows in keys.items()]


def _text_length_fn(context):
    return int((context or {}).get("text_length") or 0)


def apply_text_animation(clip, slot, preset, duration=None, easing=DEFAULT_EASING,
                         delay=0, fps=30, context=None):
    """Generate (or clear) a text preset on `clip`.

    The preset's previous keys are removed first, so changing the duration, the easing
    or the preset itself never stacks duplicate animation. Every key written here is an
    ordinary keyframe of a normal channel - retime it, re-ease it or delete it from the
    timeline or the graph editor and the preset simply follows.

    Returns True when the clip changed.
    """
    from . import text as TXT

    if slot not in SLOTS:
        raise ValueError(f"Unknown text animation slot: {slot}")
    settings = TXT.ensure_text(clip)
    animation = settings.get("animation")
    if not isinstance(animation, dict):
        animation = {}
        settings["animation"] = animation
    previous = animation.get(slot if isinstance(animation.get(slot), dict) else "")
    if isinstance(previous, dict):
        _drop_generated(clip, previous)
    changed = bool(previous)

    if not preset or preset in ("None", "none"):
        animation.pop(slot, None)
        return changed

    frame_size = (context or {}).get("project_size") or (1920, 1080)
    length = max(1, int(duration if duration is not None else default_duration(fps, clip.duration)))
    # The last generated key has to stay inside the clip's own frames, otherwise it can
    # not be grabbed or retimed on the timeline.
    length = min(length, max(1, clip.duration - 1))
    delay = max(0, int(delay or 0))
    offset = delay if slot == "in" else max(0, clip.duration - length - delay)
    offset = min(offset, max(0, clip.duration - 1 - length))

    base = {}
    other = animation.get("out" if slot == "in" else "in") or {}
    resting = dict(other.get("base") or {})
    if isinstance(previous, dict):
        resting.update(previous.get("base") or {})
    for channel in PRESET_CHANNELS:
        # Frame zero can be the offscreen/transparent start of the In preset.
        # Out and subsequent edits must use the stored resting pose instead.
        value = resting.get(channel, CH.value_at(clip, channel, 0))
        base[channel] = 100.0 if value is None else float(value)
    plan_context = dict(context or {})
    plan_context.setdefault("project_size", frame_size)
    plan_context["text_length"] = _text_length(clip, offset)
    plan = _plan(preset, slot, length, easing, base, plan_context)

    generated = {}
    for channel, rows in plan:
        absolute = [(offset + row[0], *row[1:]) for row in rows]
        generated[channel] = [time for time, *_rest in absolute]
        _merge_keys(clip, channel, absolute, base.get(channel))
    animation[slot] = {"preset": preset, "duration": length, "easing": easing,
                       "delay": delay, "start": offset, "keys": generated,
                       "base": {channel: base.get(channel) for channel, _rows in plan}}
    return True


def clears_text_animation(clip, slot):
    """Remove one slot's generated keys and its settings entry."""
    from . import text as TXT
    settings = TXT.ensure_text(clip)
    animation = settings.get("animation")
    if not isinstance(animation, dict) or not isinstance(animation.get(slot), dict):
        return False
    _drop_generated(clip, animation[slot])
    animation.pop(slot, None)
    return True


def text_animation(clip, slot):
    """The stored settings of one slot, or {} (never None)."""
    from . import text as TXT
    settings = TXT.ensure_text(clip)
    animation = settings.get("animation")
    if isinstance(animation, dict) and isinstance(animation.get(slot), dict):
        return animation[slot]
    return {}


def _drop_generated(clip, slot_meta):
    """Remove exactly the keys this preset generated, restoring the resting value."""
    for channel, times in (slot_meta.get("keys") or {}).items():
        remove = {int(time) for time in times}
        records = CH.key_records(clip, channel)
        keep = [row for row in records if int(row[0]) not in remove]
        base = (slot_meta.get("base") or {}).get(channel)
        if keep:
            CH.replace_keys(clip, channel, keep)
        elif base is not None:
            CH.set_static(clip, channel, float(base))
        else:
            CH.unanimate(clip, channel, 0)


def _merge_keys(clip, channel, rows, base):
    """Write generated keys, keeping every key that belongs to something else."""
    remove = {int(row[0]) for row in rows}
    existing = [row for row in CH.key_records(clip, channel) if int(row[0]) not in remove]
    merged = existing + [(int(row[0]), row[1], row[2], row[3], row[4]) for row in rows]
    CH.replace_keys(clip, channel, merged, base)


def clone_animation_settings(source, target):
    """Copy a text clip's animation settings and regenerate its keys (used by presets)."""
    from . import text as TXT
    settings = TXT.ensure_text(source)
    animation = settings.get("animation")
    if not isinstance(animation, dict) or not animation:
        return False
    for slot, meta in animation.items():
        if isinstance(meta, dict):
            apply_text_animation(target, slot, meta.get("preset"), meta.get("duration"),
                                 meta.get("easing", DEFAULT_EASING), meta.get("delay", 0))
    return True


def animation_records(clip):
    """JSON-friendly copy of the stored animation settings (saving/documentation)."""
    from . import text as TXT
    animation = TXT.ensure_text(clip).get("animation")
    return copy.deepcopy(animation) if isinstance(animation, dict) else {}
