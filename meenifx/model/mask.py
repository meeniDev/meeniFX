"""Clip masks stored on a clip's ordinary transform metadata.

A mask is one vector path, a coordinate space, an operation and edge controls. Keeping
the list in ``clip.transform_meta`` means it is already covered by undo snapshots, copying
a clip, pre-compositions and the project serializer; no second persistence format is
needed.

Two spaces exist. **Layer space** (new masks) stores the path as project pixels *before*
the clip transform, so the mask follows position, scale, rotation and anchor. **Composition
space** stores project pixels applied after the transform, so the mask stays fixed in the
frame. A mask written before spaces existed has no ``space`` key and is therefore read as
composition space, which keeps old projects looking exactly as they did.

Everything the UI sees (the Pen, the overlay, the properties card) works in project space;
:func:`project_path` and :func:`stored_path` convert at the edge, so editing a rotated or
scaled clip stays intuitive.
"""

from __future__ import annotations

import copy

from . import path as PATHS

MODES = ("add", "subtract", "intersect", "difference")
SPACES = ("layer", "composition")
SPACE_LABELS = {"layer": "Layer Space", "composition": "Composition Space"}
MODE_LABELS = {
    "add": "Add",
    "subtract": "Subtract",
    "intersect": "Intersect",
    "difference": "Difference",
}
DEFAULTS = {
    "mode": "add",
    # New masks are layer space; legacy dicts without the key are read as composition
    # space in _normalise(), so an existing project never silently changes behaviour.
    "space": "layer",
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
    out["space"] = str(raw.get("space") or "composition").lower()
    if out["space"] not in SPACES:
        out["space"] = "composition"
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


def add(clip, points, closed=True, mode="add", name=None, space="layer"):
    meta = _meta(clip)
    values = masks(clip)
    mask_id = next_id(clip)
    if str(space).lower() not in SPACES:
        space = "layer"
    mask = _normalise({"id": mask_id, "name": name or f"Mask {mask_id}",
                       "mode": mode, "space": space,
                       "path": {"points": points, "closed": bool(closed)}})
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


def matrix_for(clip, frame, project_size):
    """3x3 layer-space -> project-space matrix at a frame, or None."""
    if not project_size:
        return None
    from . import transform as T
    try:
        return T.layer_delta(T.evaluate(clip, int(frame)), project_size)
    except Exception:        # a half-built clip in a test must never stop a render
        return None


def project_path(clip, mask, frame, project_size=None):
    """One mask's path at a frame, in project pixels (evaluated, not stored)."""
    path = PATHS.at(mask.get("path") or {}, frame)
    if str(mask.get("space") or "composition") != "layer" or not project_size:
        return path
    matrix = matrix_for(clip, frame, project_size)
    return path if matrix is None else PATHS.transform(path, matrix)


def stored_path(clip, mask, path, frame, project_size=None):
    """Convert an edited project-space path into the mask's own stored space.

    The conversion uses the transform at `frame`, so a gesture keeps its appearance for
    the frame it was made on; other frames of an animated transform follow the clip.
    """
    value = PATHS.normalise(path)
    if str(mask.get("space") or "composition") != "layer" or not project_size:
        return value
    matrix = matrix_for(clip, frame, project_size)
    if matrix is None:
        return value
    import numpy as np
    try:
        inverse = np.linalg.inv(np.asarray(matrix, dtype=np.float64))
    except np.linalg.LinAlgError:
        return value
    return PATHS.transform(value, inverse)


def set_space(clip, mask, space, frame=0, project_size=None):
    """Convert a mask between layer and composition space, keeping its look.

    Every key is mapped with the transform at `frame`, the playhead where the user made
    the switch: that frame is preserved exactly, and a clip whose transform is animated
    will differ by that animation elsewhere (reported in the card's tooltip).
    """
    target = str(space or "").lower()
    source = str(mask.get("space") or "composition")
    if target not in SPACES or target == source:
        return False
    matrix = matrix_for(clip, frame, project_size)
    if matrix is None:
        mask["space"] = target
        return True
    import numpy as np
    if target == "composition":
        mask["path"] = PATHS.transform(mask.get("path") or {}, matrix)
    else:
        try:
            inverse = np.linalg.inv(np.asarray(matrix, dtype=np.float64))
        except np.linalg.LinAlgError:
            mask["space"] = target
            return True
        mask["path"] = PATHS.transform(mask.get("path") or {}, inverse)
    mask["space"] = target
    return True


def at(clip, frame=0, project_size=None):
    """A copy of every visible mask at a clip-local frame, in project pixels."""
    out = []
    for raw in _stored(clip):
        mask = _normalise(raw)
        if not mask.get("visible", True):
            continue
        resolved = copy.deepcopy(mask)
        resolved["path"] = project_path(clip, mask, frame, project_size)
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
    """Move a whole mask, including every stored path key, in its own stored pixels."""
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
