"""Motion paths: attach a clip to a drawn path for a configurable span.

Motion settings live in ``clip.transform_meta["motion"]``.  When enabled they override the
clip's position and (optionally) add the tangent angle to its rotation.  The ordinary
position keyframes are still the fallback whenever motion is turned off, so existing
projects and the Graph Editor keep working unchanged.
"""

from __future__ import annotations

import math

from . import path as PATHS

EASINGS = ("linear", "ease_in", "ease_out", "ease_in_out")
LOOPS = ("none", "loop", "ping_pong")
MODES = ("offset", "replace")
DEFAULTS = {
    "enabled": False,
    # Existing projects have no mode: they keep the old, absolute behaviour. Newly
    # attached paths explicitly opt into "offset" in set_path(), so authored position
    # keyframes are preserved while the path contributes a displacement.
    "mode": "replace",
    "closed": False,
    "speed": 1.0,
    "duration": 0.0,
    "progress": -1.0,
    "easing": "linear",
    "orient": False,
    "rotation_offset": 0.0,
    "reverse": False,
    "loop": "none",
    "start_offset": 0.0,
    "end_offset": 0.0,
    "path": {"points": [], "closed": False},
}


def _meta(clip):
    if not isinstance(getattr(clip, "transform_meta", None), dict):
        clip.transform_meta = {}
    return clip.transform_meta


def _normalise(raw):
    """A complete settings dict built from `raw`, without touching the clip."""
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    for name in DEFAULTS:
        if name in raw:
            out[name] = raw[name]
    out["enabled"] = bool(out["enabled"])
    out["mode"] = str(out.get("mode") or "replace").lower()
    if out["mode"] not in MODES:
        out["mode"] = "replace"
    out["closed"] = bool(out.get("closed"))
    try:
        out["speed"] = max(0.01, float(out.get("speed", 1.0)))
    except (TypeError, ValueError):
        out["speed"] = 1.0
    out["easing"] = str(out.get("easing") or "linear")
    if out["easing"] not in EASINGS:
        out["easing"] = "linear"
    out["loop"] = str(out.get("loop") or "none")
    if out["loop"] not in LOOPS:
        out["loop"] = "none"
    out["orient"] = bool(out.get("orient"))
    out["reverse"] = bool(out.get("reverse"))
    for name in ("start_offset", "end_offset", "rotation_offset"):
        try:
            out[name] = float(out.get(name, 0.0) or 0.0)
        except (TypeError, ValueError):
            out[name] = 0.0
    try:
        out["duration"] = max(0.0, float(out.get("duration", 0.0) or 0.0))
    except (TypeError, ValueError):
        out["duration"] = 0.0
    try:
        out["progress"] = float(out.get("progress", -1.0))
    except (TypeError, ValueError):
        out["progress"] = -1.0
    out["path"] = PATHS.normalise(raw.get("path") or {"points": [], "closed": out["closed"]})
    out["closed"] = bool(out["path"].get("closed", out["closed"]))
    return out


def current(clip):
    """The clip's normalised motion settings, or None when it has none.

    Evaluation must never write here: the undo stack compares whole clip snapshots, so
    creating an idle ``motion`` dict while rendering would make an otherwise clean edit
    look different from the snapshot taken before it. Only explicit edits call
    :func:`ensure`.
    """
    meta = getattr(clip, "transform_meta", None)
    if not isinstance(meta, dict):
        return None
    raw = meta.get("motion")
    if not isinstance(raw, dict):
        return None
    return _normalise(raw)


def ensure(clip):
    """The stored, normalised motion settings, creating them for an explicit edit."""
    meta = _meta(clip)
    out = _normalise(meta.get("motion"))
    meta["motion"] = out
    return out


def set_path(clip, points, closed=False, enabled=True, mode="offset"):
    settings = ensure(clip)
    settings["path"] = PATHS.normalise({"points": points, "closed": bool(closed)})
    settings["closed"] = bool(closed)
    settings["enabled"] = bool(enabled)
    settings["mode"] = mode if mode in MODES else "offset"
    return settings


def clear(clip):
    settings = ensure(clip)
    settings.update(DEFAULTS)
    settings["path"] = PATHS.normalise({"points": []})
    return settings


def _ease(value, easing):
    value = max(0.0, min(1.0, float(value)))
    if easing == "ease_in":
        return value * value
    if easing == "ease_out":
        return 1.0 - (1.0 - value) ** 2
    if easing == "ease_in_out":
        return value * value * (3.0 - 2.0 * value)
    return value


def progress_at(clip, frame, duration):
    settings = current(clip)
    if not settings or not settings.get("enabled"):
        return None
    duration = int(settings.get("duration") or duration or getattr(clip, "duration", 1) or 1)
    duration = max(1, duration)
    start = max(0.0, settings.get("start_offset", 0.0))
    end = max(0.0, settings.get("end_offset", 0.0))
    span = max(1e-6, float(duration) - start - end)
    try:
        fixed = float(settings.get("progress", -1.0))
    except (TypeError, ValueError):
        fixed = -1.0
    value = fixed if fixed >= 0.0 else (float(frame) - start) / span
    value *= float(settings.get("speed", 1.0))
    if settings.get("reverse"):
        value = 1.0 - value
    loop = settings.get("loop", "none")
    if loop == "loop":
        value = value % 1.0
    elif loop == "ping_pong":
        phase = value % 2.0
        value = phase if phase <= 1.0 else 2.0 - phase
    return max(0.0, min(1.0, value))


def position_at(clip, frame, center):
    settings = current(clip)
    if not settings:
        return None, None
    ratio = progress_at(clip, frame, clip.duration)
    path = settings.get("path") or {}
    if ratio is None or len(PATHS.points(path)) < 2:
        return None, None
    ratio = _ease(ratio, settings.get("easing", "linear"))
    point, angle = PATHS.point_along(path, ratio)
    if settings.get("mode") == "offset":
        samples = PATHS.sampled(path, steps=max(8, len(PATHS.points(path)) * 12))
        origin = samples[0] if samples else point
        return (point[0] - origin[0], point[1] - origin[1]), angle
    return (point[0] - float(center[0]), point[1] - float(center[1])), angle


def angle_at(clip, frame, center):
    settings = current(clip)
    if not settings or not settings.get("enabled") or not settings.get("orient"):
        return 0.0
    _position, angle = position_at(clip, frame, center)
    return float(angle or 0.0) + float(settings.get("rotation_offset", 0.0) or 0.0)
