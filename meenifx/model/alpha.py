"""Clip-level alpha inversion.

Masks and track mattes shape the intermediate coverage first; this switch inverts that
finished clip alpha immediately afterwards, before opacity and blending.  It is stored in
``transform_meta`` like a track matte, so it is undoable, serialized with the project and
identical in preview and export (both use the engine's one compositor).
"""

from __future__ import annotations

import numpy as np


def inverted(clip):
    meta = getattr(clip, "transform_meta", None)
    return bool((meta or {}).get("alpha_invert", False))


def set_inverted(clip, value):
    if not isinstance(getattr(clip, "transform_meta", None), dict):
        clip.transform_meta = {}
    clip.transform_meta["alpha_invert"] = bool(value)
    return bool(value)


def apply(layer, clip=None, force=None):
    """Invert a BGRA layer's coverage in place and return it."""
    if layer is None or getattr(layer, "ndim", 0) != 3 or layer.shape[2] < 4:
        return layer
    value = inverted(clip) if force is None else bool(force)
    if not value:
        return layer
    layer[:, :, 3] = (255 - layer[:, :, 3].astype(np.uint16)).astype(np.uint8)
    return layer
