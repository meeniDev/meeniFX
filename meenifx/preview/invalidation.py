"""Deciding which cached preview frames a model change invalidates.

A rendered frame depends on every clip that covers it. Re-rendering the whole timeline
after each tiny edit throws away good work, so the app keeps a digest per clip and only
drops the frame ranges of clips whose digest changed. A clip that moved, resized, got a
new effect value or a new keyframe changes its digest; everything else keeps its cache.

The digest is a compact string of the properties that reach the renderer. It is not a
snapshot and never touches the undo stack.
"""

from __future__ import annotations

import hashlib

from ..model.keyframes import AnimatedValue


def _number(value):
    """A key's value as text: numbers formatted, anything else reproduced exactly.

    A curve can legally hold non-numeric values (a text clip's words, a colour preset
    saved by an older build). Formatting one with ``{:.6f}`` raised, and because this
    runs from a model-change slot it could take down the process: the digest must
    describe every value without ever assuming it is a number.
    """
    try:
        return "{:.6f}".format(float(value))
    except (TypeError, ValueError):
        return repr(value)


def _animated(value):
    """Stable text for a value that may be animated (keys, values, curves)."""
    if not isinstance(value, AnimatedValue):
        return repr(value)
    keys = ";".join(
        "{:.3f}={}|{}|{}|{}".format(
            float(key.time), _number(key.value), key.interpolation,
            key.out_value_handle, key.in_value_handle)
        for key in value.keyframes)
    return f"A[{keys}]"


def _params(entry):
    params = entry.get("params") or {}
    parts = []
    for name in sorted(params):
        parts.append(f"{name}={_animated(params[name])}")
    pins = entry.get("pins") or ()
    pin_parts = []
    for pin in pins:
        pin_parts.append("{}:{}/{}:{}".format(
            pin.get("id"), _animated(pin.get("x")), _animated(pin.get("y")),
            int(bool(pin.get("locked")))))
    return ",".join(parts) + "|" + ",".join(pin_parts)


def _effects(clip):
    return ";".join(
        f"{entry.get('effect_id')}:{int(bool(entry.get('enabled', True)))}:{_params(entry)}"
        for entry in (getattr(clip, "effects", None) or ()))


def _transform(clip):
    transform = getattr(clip, "transform", None) or {}
    return ",".join(f"{name}={_animated(transform[name])}" for name in sorted(transform))


def _text(clip):
    data = getattr(clip, "text", None)
    if not isinstance(data, dict):
        return ""
    keys = data.get("keys") or {}
    text_keys = ";".join(f"{frame}:{value!r}" for frame, value in sorted(keys.items()))
    settings = ",".join(f"{name}={data.get(name)!r}" for name in sorted(data) if name != "keys")
    return text_keys + "|" + settings


def clip_digest(clip):
    """One string that changes exactly when this clip's rendered contribution changes."""
    if clip is None:
        return ""
    raw = "|".join((
        str(getattr(clip, "source_path", "") or ""),
        str(getattr(clip, "name", "")),
        str(int(getattr(clip, "timeline_start", 0))),
        str(int(getattr(clip, "duration", 0))),
        str(getattr(clip, "source_in", 0)),
        str(getattr(clip, "source_out", 0)),
        str(round(float(getattr(clip, "speed", 1.0) or 1.0), 6)),
        str(bool(getattr(clip, "reverse", False))),
        str(bool(getattr(clip, "loop", False))),
        str(bool(getattr(clip, "freeze", False))),
        str(getattr(clip, "source_offset", 0)),
        str(round(float(getattr(clip, "volume", 1.0) or 1.0), 5)),
        str(int(getattr(clip, "fade_in", 0))),
        str(int(getattr(clip, "fade_out", 0))),
        str(round(float(getattr(clip, "fade_in_curve", 0.0) or 0.0), 5)),
        str(round(float(getattr(clip, "fade_out_curve", 0.0) or 0.0), 5)),
        _transform(clip),
        _effects(clip),
        _text(clip),
    ))
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=12).hexdigest()


def timeline_digests(timeline):
    """{clip_id: (digest, first_frame, last_frame)} for every clip on the timeline."""
    result = {}
    for track in timeline.tracks:
        quiet = getattr(track, "muted", False) and track.type == "audio"
        hidden = not bool(getattr(track, "visible", True))
        for clip in track.clips:
            digest = clip_digest(clip)
            if hidden:
                # A hidden track contributes nothing to the picture, so its cached frames
                # must be dropped the moment it is hidden (this is why a hidden text
                # clip used to keep showing: its own digest never changed).
                digest += ":hidden"
            result[clip.id] = (digest, int(clip.timeline_start), int(clip.end), quiet)
    return result


def changed_ranges(old, new):
    """Frame ranges whose picture may have changed between two digest maps.

    A clip that appears, disappears or changes invalidates its own span - plus one frame
    on each side so a fade or a transform that reaches past the clip edges is refreshed.
    """
    ranges = []
    for clip_id in set(old) | set(new):
        before = old.get(clip_id)
        after = new.get(clip_id)
        if before is not None and after is not None and before[0] == after[0] \
                and before[3] == after[3]:
            continue
        span = None
        for entry in (before, after):
            if entry is not None:
                start, end = entry[1], entry[2]
                span = (start, end) if span is None else (min(span[0], start), max(span[1], end))
        if span is not None:
            ranges.append((max(0, span[0] - 1), span[1] + 1))
    return ranges


def fingerprint(timeline):
    """Stable hash of the whole timeline, used to validate a disk cache on reopen."""
    digests = timeline_digests(timeline)
    raw = "|".join(f"{clip_id}:{digests[clip_id][0]}" for clip_id in sorted(digests))
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()
