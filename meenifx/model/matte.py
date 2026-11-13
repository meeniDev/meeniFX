"""Track-matte settings: use another clip as this clip's transparency source.

The source is stored by clip id, never by a copied layer, so the matte follows edits to
the source and survives save/load.  Rendering happens in the engine's normal compositor,
which means preview, export and nested compositions all share the same result.
"""

from __future__ import annotations

import copy

MODES = ("alpha", "alpha_inverted", "luma", "luma_inverted")
MODE_LABELS = {
    "alpha": "Alpha Matte",
    "alpha_inverted": "Inverted Alpha",
    "luma": "Luma Matte",
    "luma_inverted": "Inverted Luma",
}
DEFAULTS = {"enabled": False, "source_id": 0, "mode": "alpha", "invert": False}


def _meta(clip):
    if not isinstance(getattr(clip, "transform_meta", None), dict):
        clip.transform_meta = {}
    return clip.transform_meta


def ensure(clip):
    meta = _meta(clip)
    raw = meta.get("matte")
    if not isinstance(raw, dict):
        raw = {}
    out = copy.deepcopy(DEFAULTS)
    for name in DEFAULTS:
        if name in raw:
            out[name] = raw[name]
    out["enabled"] = bool(out["enabled"])
    try:
        out["source_id"] = max(0, int(out.get("source_id") or 0))
    except (TypeError, ValueError):
        out["source_id"] = 0
    out["mode"] = str(out.get("mode") or "alpha")
    if out["mode"] not in MODES:
        out["mode"] = "alpha"
    out["invert"] = bool(out.get("invert"))
    meta["matte"] = out
    return out


def set_source(clip, source_id, mode="alpha"):
    settings = ensure(clip)
    settings["source_id"] = max(0, int(source_id or 0))
    settings["mode"] = mode if mode in MODES else "alpha"
    settings["enabled"] = bool(settings["source_id"])
    return settings


def clear(clip):
    settings = ensure(clip)
    settings.update(DEFAULTS)
    return settings


def is_active(clip):
    settings = ensure(clip)
    return bool(settings.get("enabled") and settings.get("source_id"))


def base_mode(mode):
    text = str(mode or "alpha")
    return text[:-len("_inverted")] if text.endswith("_inverted") else text


def inverted(settings):
    """Effective inversion: the legacy mode and the explicit switch are XORed once.

    Older projects can carry both an ``alpha_inverted`` mode and ``invert``; treating
    them as a toggle pair preserves exactly what those projects rendered instead of
    inverting a second time. New edits normalise the mode to its base value.
    """
    mode_inverted = str((settings or {}).get("mode") or "alpha").endswith("_inverted")
    return bool(mode_inverted) != bool((settings or {}).get("invert"))
