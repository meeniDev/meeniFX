"""Vector paths shared by custom shapes, clip masks and motion paths.

A path is a plain JSON-safe dictionary so it can live inside a clip's
``transform_meta`` and travel through undo snapshots and ``.mfxpf`` without a second
serializer.  Coordinates are project pixels for masks/motion paths and project pixels
inside the generated shape layer for custom shapes.

Each point has an absolute position plus two *relative* cubic handles::

    {"x": 120.0, "y": 80.0, "in": [-24, 0], "out": [24, 0], "smooth": True}

``in`` is the handle that controls the segment arriving at the point and ``out`` the
segment leaving it.  ``smooth`` is a behaviour flag used by the pen tool (dragging one
handle mirrors the other); playback only reads the two offsets.

Held path keyframes are stored in ``keys`` as ``{frame: {"points": [...], "closed": ...}}``.
When consecutive keys have the same point count they are linearly interpolated, which is
enough to animate a mask or shape without introducing a second interpolation engine.
"""

from __future__ import annotations

import copy
import math

HANDLE_FIELDS = ("in", "out")


def _number(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def point(x, y, in_handle=None, out_handle=None, smooth=False):
    return {
        "x": _number(x),
        "y": _number(y),
        "in": [_number((in_handle or (0.0, 0.0))[0]),
               _number((in_handle or (0.0, 0.0))[1])],
        "out": [_number((out_handle or (0.0, 0.0))[0]),
                _number((out_handle or (0.0, 0.0))[1])],
        "smooth": bool(smooth),
    }


def normalise_point(raw):
    if isinstance(raw, dict):
        return point(raw.get("x", 0.0), raw.get("y", 0.0), raw.get("in"), raw.get("out"),
                     raw.get("smooth", False))
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return point(raw[0], raw[1])
    return point(0.0, 0.0)


def points(raw):
    values = raw.get("points") if isinstance(raw, dict) else raw
    if not isinstance(values, (list, tuple)):
        return []
    return [normalise_point(item) for item in values]


def normalise(raw, closed=None):
    """A complete path dictionary from anything a project or the UI may hand us."""
    source = raw if isinstance(raw, dict) else {"points": raw}
    out = {
        "points": points(source),
        "closed": bool(source.get("closed", False) if closed is None else closed),
    }
    keys = source.get("keys")
    if isinstance(keys, dict):
        clean = {}
        for frame, key in keys.items():
            try:
                frame = int(frame)
            except (TypeError, ValueError):
                continue
            if frame < 0:
                continue
            if isinstance(key, dict):
                clean[frame] = {"points": points(key),
                                "closed": bool(key.get("closed", out["closed"]))}
            else:
                clean[frame] = {"points": points(key), "closed": out["closed"]}
        if clean:
            out["keys"] = clean
    return out


def blank(closed=False):
    return {"points": [], "closed": bool(closed)}


def clone(path):
    out = normalise(path)
    return copy.deepcopy(out)


def key_frames(path):
    return sorted(int(frame) for frame in (normalise(path).get("keys") or {}))


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_handle(a, b, t):
    return [_lerp(a[0], b[0], t), _lerp(a[1], b[1], t)]


def _lerp_points(left, right, t):
    out = []
    for first, second in zip(left, right):
        out.append(point(_lerp(first["x"], second["x"], t),
                         _lerp(first["y"], second["y"], t),
                         _lerp_handle(first["in"], second["in"], t),
                         _lerp_handle(first["out"], second["out"], t),
                         bool(first.get("smooth"))))
    return out


def at(path, frame=0):
    """Evaluate a path at a clip-local frame (held or index-wise interpolated)."""
    data = normalise(path)
    keys = data.get("keys") or {}
    if not keys:
        return data
    frames = sorted(int(value) for value in keys)
    frame = int(frame)
    if frame <= frames[0]:
        key = keys[frames[0]]
        return {"points": copy.deepcopy(key["points"]), "closed": key["closed"]}
    if frame >= frames[-1]:
        key = keys[frames[-1]]
        return {"points": copy.deepcopy(key["points"]), "closed": key["closed"]}
    for first, second in zip(frames, frames[1:]):
        if not (first <= frame <= second):
            continue
        left, right = keys[first], keys[second]
        if second == first:
            return {"points": copy.deepcopy(left["points"]), "closed": left["closed"]}
        if len(left["points"]) != len(right["points"]):
            # A point was added/removed between keys: holding is safer than pairing
            # mismatched indices and folding the path onto itself.
            return {"points": copy.deepcopy(left["points"]), "closed": left["closed"]}
        t = (frame - first) / float(second - first)
        return {"points": _lerp_points(left["points"], right["points"], t),
                "closed": bool(left["closed"] if t < 0.5 else right["closed"])}
    return data


def set_key(path, frame, current=None, closed=None):
    """Store a held/interpolated path keyframe; returns the normalised path."""
    data = normalise(path)
    frame = max(0, int(frame))
    value = at(data, frame) if current is None else normalise(current)
    data.setdefault("keys", {})[frame] = {
        "points": copy.deepcopy(value["points"] if "points" in value else points(value)),
        "closed": bool(data["closed"] if closed is None else closed),
    }
    return data


def remove_key(path, frame):
    data = normalise(path)
    keys = data.get("keys") or {}
    return keys.pop(int(frame), None) is not None


def keyed(path, frame):
    data = normalise(path)
    return int(frame) in (data.get("keys") or {})


def set_points(path, values, closed=None):
    data = normalise(path)
    data["points"] = points(values)
    if closed is not None:
        data["closed"] = bool(closed)
    return data


def toggle_smooth(raw, index):
    data = normalise(raw)
    if not 0 <= int(index) < len(data["points"]):
        return data, False
    item = data["points"][int(index)]
    item["smooth"] = not bool(item.get("smooth"))
    if not item["smooth"]:
        item["in"] = [0.0, 0.0]
        item["out"] = [0.0, 0.0]
    return data, item["smooth"]


def move_point(raw, index, x, y):
    data = normalise(raw)
    if 0 <= int(index) < len(data["points"]):
        data["points"][int(index)]["x"] = _number(x)
        data["points"][int(index)]["y"] = _number(y)
    return data


def set_handle(raw, index, side, dx, dy, mirror=True):
    data = normalise(raw)
    index = int(index)
    if not 0 <= index < len(data["points"]):
        return data
    item = data["points"][index]
    side = "in" if side == "in" else "out"
    item[side] = [_number(dx), _number(dy)]
    if mirror and item.get("smooth"):
        other = "out" if side == "in" else "in"
        item[other] = [-item[side][0], -item[side][1]]
    return data


def segments(raw, frame=None):
    """Cubic segments ``[(p0, c0, c1, p1)]`` for a path at a frame."""
    data = at(raw, frame) if frame is not None else normalise(raw)
    pts = data["points"]
    if len(pts) < 2:
        return []
    out = []
    count = len(pts) - 1 + (1 if data.get("closed") and len(pts) > 2 else 0)
    for index in range(count):
        first = pts[index]
        second = pts[(index + 1) % len(pts)]
        c0 = (first["x"] + first["out"][0], first["y"] + first["out"][1])
        c1 = (second["x"] + second["in"][0], second["y"] + second["in"][1])
        out.append(((first["x"], first["y"]), c0, c1, (second["x"], second["y"])))
    return out


def _cubic(p0, c0, c1, p1, t):
    mt = 1.0 - t
    return (mt ** 3 * p0[0] + 3 * mt * mt * t * c0[0]
            + 3 * mt * t * t * c1[0] + t ** 3 * p1[0],
            mt ** 3 * p0[1] + 3 * mt * mt * t * c0[1]
            + 3 * mt * t * t * c1[1] + t ** 3 * p1[1])


def sampled(raw, frame=None, steps=16):
    """Flatten a path to a polyline for OpenCV and QPainterPath."""
    data = at(raw, frame) if frame is not None else normalise(raw)
    pts = data["points"]
    if not pts:
        return []
    if len(pts) == 1:
        return [(pts[0]["x"], pts[0]["y"])]
    out = [(pts[0]["x"], pts[0]["y"])]
    for p0, c0, c1, p1 in segments(data):
        for step in range(1, max(2, int(steps)) + 1):
            out.append(_cubic(p0, c0, c1, p1, step / float(steps)))
    if data.get("closed") and len(out) > 1:
        out[-1] = out[0]
    return out


def bounds(raw, frame=None):
    values = sampled(raw, frame) if isinstance(raw, (dict, list, tuple)) else []
    if not values:
        return None
    xs = [value[0] for value in values]
    ys = [value[1] for value in values]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def length(raw, frame=None):
    values = sampled(raw, frame)
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(values, values[1:]))


def point_along(raw, ratio):
    """Point and tangent angle at ``ratio`` of a path's arc length."""
    values = sampled(raw)
    if not values:
        return (0.0, 0.0), 0.0
    if len(values) == 1:
        return values[0], 0.0
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(values, values[1:])]
    total = sum(lengths)
    if total <= 1e-9:
        return values[0], 0.0
    target = max(0.0, min(1.0, float(ratio))) * total
    walked = 0.0
    for index, length_value in enumerate(lengths):
        if walked + length_value >= target or index == len(lengths) - 1:
            local = (target - walked) / max(1e-9, length_value)
            a, b = values[index], values[index + 1]
            return ((a[0] + (b[0] - a[0]) * local,
                     a[1] + (b[1] - a[1]) * local),
                    math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])))
        walked += length_value
    return values[-1], 0.0
