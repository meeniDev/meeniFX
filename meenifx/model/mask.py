"""Clip masks stored on a clip's ordinary transform metadata.

A mask is one vector path plus an operation and edge controls.  Keeping the list in
``clip.transform_meta`` means it is already covered by undo snapshots, copying a clip,
pre-compositions and the project serializer; no second persistence format is needed.

Masks are composition-space: the path is stored in project pixels and applied after the
clip's transform.  That makes drawing on the preview exact, and means moving a mask does
not require inverting a rotated/scaled layer transform.  The limitation (a mask does not
follow a later transform change) is reported in the properties card.
"""

from __future__ import annotations

import copy

from . import path as PATHS

MODES = ("add", "subtract", "intersect", "difference")
MODE_LABELS = {
    "add": "Add",
    "subtract": "Subtract",
    "intersect": "Intersect",
    "difference": "Difference",
}
DEFAULTS = {
    "mode": "add",
    "invert": False,
    "visible": True,
    "show_outline": True,
    "feather": 0.0,
    "expansion": 0.0,
    "opacity": 100.0,
}


def _meta(clip):
    if not isinstance(getattr(clip, "transform_meta", None), dict):
        clip.transform_meta = {}
    return clip.transform_meta


def next_id(clip):
    return max((int(mask.get("id", 0)) for mask in masks(clip)), default=0) + 1


def _normalise(raw):
    if not isinstance(raw, dict):
        raw = {}
    out = copy.deepcopy(DEFAULTS)
    for name in DEFAULTS:
        if name in raw:
            out[name] = raw[name]
    out["mode"] = str(out.get("mode") or "add")
    if out["mode"] not in MODES:
        out["mode"] = "add"
    out["invert"] = bool(out.get("invert"))
    out["visible"] = bool(out.get("visible", True))
    out["show_outline"] = bool(out.get("show_outline", True))
    for name, limit in (("feather", (0.0, 200.0)), ("expansion", (-200.0, 200.0)),
                        ("opacity", (0.0, 100.0))):
        try:
            value = float(out.get(name, DEFAULTS[name]))
        except (TypeError, ValueError):
            value = DEFAULTS[name]
        out[name] = max(limit[0], min(limit[1], value))
    out["id"] = max(1, int(raw.get("id", 0) or 1))
    out["name"] = str(raw.get("name") or f"Mask {out['id']}")
    out["path"] = PATHS.normalise(raw.get("path") or {})
    return out


def masks(clip):
    meta = _meta(clip)
    values = meta.get("masks")
    if not isinstance(values, list):
        values = []
        meta["masks"] = values
    # Normalise the stored dicts in place. Returning copies here made edits through
    # ``MASK.find`` (path keyframes, rename, visibility) silently disappear: callers
    # mutated a detached list and the project kept the old mask.
    for index, raw in enumerate(list(values)):
        clean = _normalise(raw)
        if clean != raw:
            values[index] = clean
    return values


def ensure(clip):
    return masks(clip)


def add(clip, points, closed=True, mode="add", name=None):
    meta = _meta(clip)
    values = masks(clip)
    mask_id = next_id(clip)
    mask = _normalise({"id": mask_id, "name": name or f"Mask {mask_id}",
                       "mode": mode, "path": {"points": points, "closed": bool(closed)}})
    values.append(mask)
    meta["masks"] = values
    return mask


def remove(clip, mask_id):
    meta = _meta(clip)
    before = masks(clip)
    values = [mask for mask in before if int(mask.get("id", 0)) != int(mask_id)]
    meta["masks"] = values
    return len(values) != len(before)


def find(clip, mask_id):
    for mask in masks(clip):
        if int(mask.get("id", 0)) == int(mask_id):
            return mask
    return None


def _stored(clip):
    """The stored mask list without creating or rewriting anything (render-safe)."""
    meta = getattr(clip, "transform_meta", None)
    if not isinstance(meta, dict):
        return ()
    values = meta.get("masks")
    return values if isinstance(values, list) else ()


def at(clip, frame=0):
    """A copy of every mask with its path evaluated at a clip-local frame."""
    out = []
    for raw in _stored(clip):
        mask = _normalise(raw)
        if not mask.get("visible", True):
            continue
        resolved = copy.deepcopy(mask)
        resolved["path"] = PATHS.at(mask.get("path"), frame)
        out.append(resolved)
    return out


def set_path(mask, path, frame=None):
    """Write an edited path, preserving and updating existing path keyframes."""
    current = PATHS.normalise(mask.get("path") or {})
    edited = PATHS.normalise(path)
    if frame is None or (not PATHS.key_frames(current) and not PATHS.key_frames(edited)):
        mask["path"] = edited
        return mask["path"]
    template = current if PATHS.points(current) else edited
    result = PATHS.normalise({"points": template.get("points") or [],
                              "closed": bool(template.get("closed")),
                              "keys": current.get("keys") or {}})
    result = PATHS.set_key(result, int(frame),
                           {"points": edited.get("points") or [],
                            "closed": bool(edited.get("closed"))})
    mask["path"] = result
    return result


def move(mask, dx, dy):
    """Move a whole mask, including every stored path key, in project pixels."""
    path = PATHS.normalise(mask.get("path") or {})

    def shift(value):
        value = PATHS.normalise(value)
        value["points"] = [PATHS.point(point["x"] + dx, point["y"] + dy,
                                       point.get("in"), point.get("out"),
                                       point.get("smooth")) for point in value["points"]]
        return value

    path = shift(path)
    for frame, value in list((path.get("keys") or {}).items()):
        path["keys"][frame] = shift(value)
    mask["path"] = path
    return path


def has_masks(clip):
    return bool(_stored(clip))
