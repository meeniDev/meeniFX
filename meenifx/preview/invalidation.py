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


def _shape(clip):
    """Shape geometry, colours and their keys all change the generated pixels."""
    data = getattr(clip, "shape", None)
    if not isinstance(data, dict):
        return ""
    parts = []
    for name in sorted(data):
        value = data[name]
        if isinstance(value, dict):
            keys = value.get("keys") or {}
            parts.append(f"{name}=" + ",".join(
                f"{int(frame)}:{colour!r}" for frame, colour in sorted(keys.items())))
        else:
            parts.append(f"{name}={_animated(value)}")
    return ";".join(parts)


def _creative_metadata(clip):
    """Masks, track mattes and motion paths are part of the rendered picture."""
    data = getattr(clip, "transform_meta", None)
    if not isinstance(data, dict) or not data:
        return ""

    def clean(value):
        if isinstance(value, dict):
            return "{" + ",".join(f"{key!r}:{clean(item)}" for key, item in
                                    sorted(value.items(), key=lambda pair: str(pair[0]))) + "}"
        if isinstance(value, (list, tuple)):
            return "[" + ",".join(clean(item) for item in value) + "]"
        return repr(value)

    return clean(data)


def _presets(clip):
    """The preset instances are part of the picture, so they are part of the digest."""
    from ..model import presets as preset_model
    return preset_model.cache_key(clip)


def composition_digest(comp, compositions=None, seen=None):
    """Digest of one composition definition, including everything nested inside it.

    A composition clip's own properties never change when the user edits the definition,
    so the parent timeline would keep showing stale frames without this. `seen` breaks
    cycles defensively (the model already refuses to create them).
    """
    if comp is None:
        return "missing"
    seen = set(seen or ())
    if comp.id in seen:
        return f"cycle:{comp.id}"
    seen.add(comp.id)
    parts = [str(comp.id), str(comp.name), f"{comp.width}x{comp.height}",
             str(comp.fps), ",".join(str(part) for part in comp.background),
             str(bool(getattr(comp, "transparent", False))),
             str(int(getattr(comp, "declared_duration", 0) or 0))]
    nested = timeline_digests(comp.timeline, compositions, seen)
    parts.extend(f"{clip_id}:{nested[clip_id][0]}" for clip_id in sorted(nested))
    return hashlib.blake2b("|".join(parts).encode("utf-8", "replace"),
                           digest_size=12).hexdigest()


def clip_digest(clip, compositions=None, seen=None):
    """One string that changes exactly when this clip's rendered contribution changes."""
    if clip is None:
        return ""
    comp_note = ""
    if getattr(clip, "is_composition", False):
        library = compositions if compositions is not None else getattr(clip, "compositions", None)
        comp = library.get(clip.composition_id) if library is not None else None
        comp_note = "comp:" + str(clip.composition_id) + ":" + composition_digest(
            comp, library, seen)
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
        _shape(clip),
        _creative_metadata(clip),
        _presets(clip),
        # How the layer is composited changes the picture just as much as its effects.
        str(getattr(clip, "blend", "normal")),
        str(bool(getattr(clip, "adjustment", False))),
        comp_note,
    ))
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=12).hexdigest()


def timeline_digests(timeline, compositions=None, seen=None):
    """{clip_id: (digest, first_frame, last_frame)} for every clip on the timeline.

    `compositions` makes a nested clip's digest follow its definition, so editing a
    composition invalidates exactly the parent frames that show it.
    """
    # A transition changes the picture/audio of both clips in its overlap, even though
    # neither clip's own properties moved. Fold its settings into both digests so a
    # curve change invalidates exactly those cached frames.
    transition_notes = {}
    for transition in getattr(timeline, "transitions", []) or ():
        note = "|".join(str(part) for part in transition.state())
        for clip_id in (transition.out_id, transition.in_id):
            transition_notes.setdefault(clip_id, []).append(note)
    result = {}
    for track in timeline.tracks:
        quiet = getattr(track, "muted", False) and track.type == "audio"
        hidden = not bool(getattr(track, "visible", True))
        for clip in track.clips:
            digest = clip_digest(clip, compositions, seen)
            notes = transition_notes.get(clip.id)
            if notes:
                digest += ":T[" + ";".join(sorted(notes)) + "]"
            if hidden:
                # A hidden track contributes nothing to the picture, so its cached frames
                # must be dropped the moment it is hidden (this is why a hidden text
                # clip used to keep showing: its own digest never changed).
                digest += ":hidden"
            result[clip.id] = (digest, int(clip.timeline_start), int(clip.end), quiet)
    # A matte target's cached picture also depends on the source clip. Editing the
    # source must invalidate the target even when the target itself did not change.
    for track in timeline.tracks:
        for clip in track.clips:
            meta = getattr(clip, "transform_meta", None)
            raw = meta.get("matte") if isinstance(meta, dict) else None
            if not isinstance(raw, dict) or not raw.get("enabled"):
                continue
            try:
                source_id = int(raw.get("source_id", 0) or 0)
            except (TypeError, ValueError):
                source_id = 0
            if source_id and source_id in result and source_id != clip.id:
                digest, start, end, quiet = result[clip.id]
                result[clip.id] = (digest + ":matte[" + result[source_id][0] + "]",
                                   start, end, quiet)
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


def fingerprint(timeline, project_size=None, background=None, compositions=None):
    """Stable hash of the timeline and output format, for validating a disk cache on reopen.

    The same timeline rendered at another project size is a different picture, so the
    project dimensions (and the background behind transparent areas) are part of the
    fingerprint rather than only of the preview scale key.
    """
    digests = timeline_digests(timeline, compositions)
    raw = "|".join(f"{clip_id}:{digests[clip_id][0]}" for clip_id in sorted(digests))
    if project_size is not None:
        raw += f"|size={int(project_size[0])}x{int(project_size[1])}"
    if background is not None:
        raw += "|bg=" + ",".join(str(int(channel)) for channel in background)
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()
