"""Looks: one-click effect bundles, shipped as JSON and shareable as .mfxlook files.

A look is a versioned document listing a stack of ordinary effects with tuned values::

    {"format": "mfxlook", "version": 1, "id": "vhs_tape", "name": "VHS Tape",
     "description": "Worn tape: soft chroma, scanlines and grain",
     "stack": [{"effect_id": "vhs_crt", "params": {"scanlines": 45}},
               {"effect_id": "film_grain", "params": {"amount": 18}}]}

Applying one inserts those entries as a normal, individually editable effect stack plus
a "look" group card whose Strength scales the whole stack. Nothing about a look is
magic at render time: the engine only reads the group card's strength.
"""

from __future__ import annotations

import copy
import json
import os
import re

FORMAT = "mfxlook"
VERSION = 1
RESERVED_STACK_KEYS = ("effect_id", "enabled", "params", "mix", "look_id", "name")


def shipped_folder():
    """The looks that ship with the app (read-only)."""
    from ..config import ASSETS_ROOT
    return str(ASSETS_ROOT / "looks")


def user_folder():
    """The user's own looks, outside the bundle so an upgrade never deletes them."""
    from ..config import user_config_dir
    return os.path.join(user_config_dir(), "looks")


def _slug(text):
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    return cleaned or "look"


def _static_params(params):
    """JSON-safe parameter values: animations keep their base value, not their curve."""
    from .keyframes import AnimatedValue
    out = {}
    for name, value in (params or {}).items():
        if isinstance(value, AnimatedValue):
            base = value.base_value
            if base is None:
                base = value.keyframes[0].value if value.keyframes else 0
            out[str(name)] = base
        elif isinstance(value, (int, float, bool, str)) or value is None:
            out[str(name)] = value
    return out


def normalise(payload, fallback_id=""):
    """A validated look dict, or None when the file is not a look at all."""
    if not isinstance(payload, dict):
        return None
    if payload.get("format") not in (None, FORMAT):
        return None
    try:
        version = int(payload.get("version") or VERSION)
    except (TypeError, ValueError):
        version = VERSION
    if version > VERSION:
        return None
    raw_stack = payload.get("stack")
    if not isinstance(raw_stack, (list, tuple)) or not raw_stack:
        return None
    stack = []
    for item in raw_stack:
        if not isinstance(item, dict) or not item.get("effect_id"):
            continue
        entry = {"effect_id": str(item["effect_id"]),
                 "enabled": bool(item.get("enabled", True)),
                 "params": _static_params(item.get("params"))}
        # A look inside a look would be a recursion the renderer does not need.
        if entry["effect_id"] == "look":
            continue
        stack.append(entry)
    if not stack:
        return None
    look_id = _slug(payload.get("id") or fallback_id or payload.get("name"))
    return {"format": FORMAT, "version": version, "id": look_id,
            "name": str(payload.get("name") or look_id.replace("_", " ").title()),
            "description": str(payload.get("description") or ""),
            "stack": stack}


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_folder(folder, into, seen):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        look = normalise(_read_file(os.path.join(folder, name)), fallback_id=name[:-5])
        if look is None or look["id"] in seen:
            continue
        look["path"] = os.path.join(folder, name)
        look["builtin"] = folder == shipped_folder()
        seen.add(look["id"])
        into[look["id"]] = look


_CACHE = {"key": None, "items": {}}


def _cache_key():
    stamps = []
    for folder in (shipped_folder(), user_folder()):
        try:
            stamps.append((folder, len(os.listdir(folder))))
        except OSError:
            stamps.append((folder, -1))
    return tuple(stamps)


def load(force=False):
    """{id: look} for the whole library (user looks override shipped ones)."""
    key = _cache_key()
    if not force and _CACHE["key"] == key:
        return _CACHE["items"]
    items, seen = {}, set()
    _load_folder(user_folder(), items, seen)
    _load_folder(shipped_folder(), items, seen)
    _CACHE["key"], _CACHE["items"] = key, items
    return items


def all_looks():
    """Shipped and user looks, in name order."""
    return sorted(load().values(), key=lambda look: (look["name"].lower(), look["id"]))


def get(look_id):
    return load().get(str(look_id or ""))


def stack_for(look):
    """Deep copy of a look's effect entries, ready to be inserted into a clip."""
    return copy.deepcopy((look or {}).get("stack") or [])


def from_effects(effects):
    """A stack built from a clip's current effects (group cards are not copied).

    Keyframes become their current base value: a look is a recipe of values, and a
    look file stays readable and editable by hand.
    """
    stack = []
    for entry in effects or ():
        if str(entry.get("effect_id") or "") == "look":
            continue
        if not entry.get("effect_id"):
            continue
        stack.append({"effect_id": str(entry["effect_id"]),
                      "enabled": bool(entry.get("enabled", True)),
                      "params": _static_params(entry.get("params"))})
    return stack


def write_user_look(name, stack, description=""):
    """Store a stack as a user look. Returns (path, look) or (None, None)."""
    look = normalise({"name": name, "description": description, "stack": stack,
                      "id": _slug(name)})
    if look is None:
        return None, None
    folder = user_folder()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{look['id']}.json")
    stored = {key: value for key, value in look.items() if key not in ("path", "builtin")}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=1, sort_keys=True)
    load(force=True)
    look["path"], look["builtin"] = path, False
    return path, look


def import_file(path, rename=None):
    """Copy a .mfxlook/.json file into the user library. Returns (path, look)."""
    look = normalise(_read_file(path), fallback_id=os.path.splitext(os.path.basename(str(path)))[0])
    if look is None:
        return None, None
    if rename:
        look["name"] = str(rename)
        look["id"] = _slug(rename)
    existing = load()
    base_id = look["id"]
    run = 2
    while look["id"] in existing and existing[look["id"]].get("path") != path:
        look["id"] = f"{base_id}_{run}"
        run += 1
    return _write_with_id(look)


def _write_with_id(look):
    folder = user_folder()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{look['id']}.json")
    stored = {key: value for key, value in look.items() if key not in ("path", "builtin")}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=1, sort_keys=True)
    load(force=True)
    look["path"], look["builtin"] = path, False
    return path, look


def export_file(look_id, path):
    """Write one look to a .mfxlook file."""
    look = get(look_id)
    if look is None:
        return None
    stored = {key: value for key, value in look.items() if key not in ("path", "builtin")}
    path = str(path)
    if not path.lower().endswith(".mfxlook"):
        path = os.path.splitext(path)[0] + ".mfxlook"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=1, sort_keys=True)
    return path
