"""One animation per behaviour, with its variants behind it.

The shipped library stores one file per *variant* (``clip_in_slide``, ``clip_out_slide``,
``text_in_slide``...). Browsing those 81 files as 81 tiles is what made the old panel feel
duplicated: "Slide In Left", "Slide In Right", "Slide Out Left"... are one animation with a
mode and a direction.

This module folds the library into *families* (one per program) and resolves the file for a
(mode, direction, media) triple, so the browser can show one Slide card with an In/Out/Loop
switch. Nothing is renamed on disk and no preset is lost: a variant that exists is still
exactly the same JSON the old panel applied.
"""

from __future__ import annotations

import re

from . import preset_library as LIB

MODES = ("in", "out", "loop")
MODE_LABELS = {"in": "In", "out": "Out", "loop": "Loop", "both": "Both"}
MEDIA_LABELS = {"clip": "Clip", "text": "Text"}
CATEGORY_FOR = {
    ("clip", "in"): "clip_in", ("clip", "out"): "clip_out", ("clip", "loop"): "clip_loop",
    ("text", "in"): "text_in", ("text", "out"): "text_out", ("text", "loop"): "text_loop",
}
# Behaviour groups for the browser's category chips (by program).
BEHAVIOURS = {
    "fade": "Fade", "stagger_fade": "Fade", "flash": "Fade", "strobe": "Fade",
    "flicker": "Fade", "blur": "Fade",
    "slide": "Motion", "whip": "Motion", "zoom": "Motion", "punch_zoom": "Motion",
    "spiral": "Motion", "tracking": "Motion", "wipe": "Motion", "float": "Motion",
    "drift": "Motion",
    "scale": "Scale", "pop": "Scale", "bounce": "Scale", "pulse": "Scale",
    "zoom_breath": "Scale", "heartbeat": "Scale",
    "rotate": "Rotation", "spin": "Rotation", "swing": "Rotation", "flip": "Rotation",
    "wave": "Rotation", "wave_in": "Rotation",
    "typewriter": "Text", "scramble": "Text", "kinetic": "Text", "color_cycle": "Text",
    "shake": "Distortion", "jitter": "Distortion", "wiggle": "Distortion",
    "glitch_reveal": "Distortion", "glitch_stutter": "Distortion", "scatter": "Distortion",
    "camera_shake": "Distortion", "sway": "Distortion", "drop": "Motion",
}
_SUFFIX = re.compile(r"\s+(?:in|out|loop)$", re.IGNORECASE)


def base_name(preset):
    """'Slide In Left' -> 'Slide'; 'Camera shake' stays 'Camera shake'."""
    name = str((preset or {}).get("name") or (preset or {}).get("id") or "Animation").strip()
    return _SUFFIX.sub("", name) or name


def _direction(preset):
    return str(((preset or {}).get("args") or {}).get("dir") or "").strip().lower()


def _media_mode(preset):
    category = str((preset or {}).get("category") or "")
    if not category:
        return None, None
    media = "text" if category.startswith("text_") else "clip"
    mode = category.split("_", 1)[1] if "_" in category else "in"
    return media, mode


def families(library=None):
    """Family dicts in name order: {key, name, behaviour, modes, directions, media, variants}."""
    library = LIB.load() if library is None else library
    groups = {}
    for preset in library.values():
        program = str(preset.get("program") or "")
        media, mode = _media_mode(preset)
        if not program or media is None or preset.get("instances"):
            # Clips saved as their own animation are not a behaviour with variants.
            continue
        family = groups.get(program)
        if family is None:
            family = groups[program] = {
                "key": program, "program": program, "name": base_name(preset),
                "behaviour": BEHAVIOURS.get(program, "Other"),
                "modes": set(), "directions": set(), "media": set(), "variants": {},
                "keywords": set(),
            }
        family["variants"][(media, mode, _direction(preset))] = preset
        family["modes"].add(mode)
        family["media"].add(media)
        if _direction(preset):
            family["directions"].add(_direction(preset))
        for word in preset.get("keywords") or ():
            family["keywords"].add(str(word).lower())
        family["keywords"].update({program.replace("_", " "), family["name"].lower()})
    out = []
    for family in groups.values():
        family["modes"] = [mode for mode in MODES if mode in family["modes"]]
        family["directions"] = sorted(family["directions"])
        family["media"] = [media for media in ("clip", "text") if media in family["media"]]
        family["keywords"] = sorted(family["keywords"])
        out.append(family)
    return sorted(out, key=lambda item: (item["name"].lower(), item["key"]))


def directions(family, media="clip", mode="in"):
    """Which directions this medium and mode actually have."""
    variants = (family or {}).get("variants") or {}
    found = sorted({d for (m, c, d) in variants if m == media and c == mode and d})
    if not found:
        found = sorted({d for (m, c, d) in variants if c == mode and d})
    return found


def variant(family, media="clip", mode="in", direction=""):
    """The preset for one variant.

    Falling back across *media* is fine; falling back from Out to In is not, because it
    would silently apply an entrance when an exit was asked for. So the mode is only
    swapped when the family has no such mode at all, and the direction is dropped before
    the medium is.
    """
    variants = (family or {}).get("variants") or {}
    direction = str(direction or "").strip().lower()
    modes = family.get("modes") or []
    mode_keys = [mode] if not modes or mode in modes else list(modes)
    # Text and clip variants are written for different things: a text entrance must not
    # be applied to a picture clip just because the picture has no matching entrance.
    media_keys = [media]
    if media not in (family.get("media") or []):
        media_keys += [key for key in ("clip", "text") if key != media]
    direction_keys = [direction, ""] if direction else [""]
    for mode_key in mode_keys:
        for media_key in media_keys:
            for direction_key in direction_keys:
                preset = variants.get((media_key, mode_key, direction_key))
                if preset is not None:
                    return preset
        for media_key in media_keys:
            for (m, c, _d), preset in variants.items():
                if (m, c) == (media_key, mode_key):
                    return preset
    return None


def default_mode(family):
    """Loops stay loops; entrances that only exist as exits start on Out."""
    modes = (family or {}).get("modes") or []
    if "in" in modes:
        return "in"
    return modes[0] if modes else "in"


def summary(family):
    """Short line for a card: 'In · Out · 4 directions'."""
    parts = [MODE_LABELS.get(mode, mode.title()) for mode in (family.get("modes") or [])]
    directions = family.get("directions") or []
    if directions:
        parts.append(f"{len(directions)} directions")
    if "text" in (family.get("media") or []):
        parts.append("Text + clips" if "clip" in family["media"] else "Text only")
    return " · ".join(parts)
