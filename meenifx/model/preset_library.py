"""The animation preset library: JSON files, shipped and per-user.

A preset file is a small, versioned JSON document::

    {"format": "mfxpreset", "version": 1, "id": "text_in_pop", "name": "Pop",
     "category": "text_in", "program": "pop", "args": {"from": 0.3},
     "knobs": {"unit": "letter", "stagger": 0.6, "easing": "Back Out"},
     "keywords": ["entrance", "bounce"]}

The programs themselves live in :mod:`meenifx.model.presets` (code, not data), so a preset
file can never carry something the compositor cannot run. The shipped folder is read-only;
the user folder (``%APPDATA%\\meeniFX\\Studios\\presets``) wins on id clashes and is where
"Save as preset" and "Import" write.

A clip's own animation can also be exported as a preset: those files keep their
``instances`` list instead of a single program, and :func:`meenifx.model.presets.from_payload`
replays them on another clip.
"""

from __future__ import annotations

import copy
import json
import os

from . import presets as P

FORMAT = "mfxpreset"
VERSION = 1

CATEGORY_KEYS = ("text_in", "text_out", "text_loop", "clip_in", "clip_out", "clip_loop")
CATEGORY_TITLES = {"text_in": "Text In", "text_out": "Text Out", "text_loop": "Text Loop",
                   "clip_in": "Clip In", "clip_out": "Clip Out", "clip_loop": "Clip Loop"}
# (category, kind, allows per-unit animation) - the tab builds its menus from this.
CATEGORY_INFO = {
    "text_in": ("in", "text", True), "text_out": ("out", "text", True),
    "text_loop": ("loop", "text", True),
    "clip_in": ("in", "clip", False), "clip_out": ("out", "clip", False),
    "clip_loop": ("loop", "clip", False),
}
DEFAULT_KEYWORDS = ()


def shipped_folder():
    """The library that ships with the app (read-only)."""
    from ..config import ASSETS_ROOT
    return str(ASSETS_ROOT / "presets")


def user_folder():
    """The user's own presets; outside the bundle so an upgrade never deletes them."""
    from ..config import user_config_dir
    return os.path.join(user_config_dir(), "presets")


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def normalise(payload, fallback_id=""):
    """A validated preset dict, or None when the file is not a preset at all."""
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
    program = payload.get("program")
    instances = payload.get("instances")
    if not program and not instances:
        return None
    if program and program not in P.PROGRAMS:
        return None
    category = payload.get("category") or "text_in"
    if category not in CATEGORY_INFO:
        return None
    preset_id = str(payload.get("id") or fallback_id or payload.get("name") or "preset")
    # A preset saved from a clip stores its knobs on the instance; the library entry keeps
    # a copy so the browser preview and the Animation card can read them directly.
    knobs = dict(payload.get("knobs") or {})
    args = dict(payload.get("args") or {})
    saved = instances or ()
    if not payload.get("program") and saved and isinstance(saved[0], dict):
        # The entry names the first instance's program so previews and the knobs card have
        # something concrete; applying the preset replays every instance (session.apply_preset).
        program = saved[0].get("program") or "fade"
        args = dict(saved[0].get("args") or {})
    if not knobs and saved and isinstance(saved[0], dict):
        knobs = dict(saved[0].get("knobs") or {})
    return {"format": FORMAT, "version": version,
            "id": preset_id, "name": str(payload.get("name") or preset_id.replace("_", " ").title()),
            "category": category, "program": program or "",
            "args": args,
            "knobs": {name: value for name, value in knobs.items() if name in P.DEFAULT_KNOBS},
            "keywords": [str(word) for word in payload.get("keywords") or DEFAULT_KEYWORDS],
            "instances": copy.deepcopy(instances) if instances else None}


def _load_folder(folder, into, seen):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        payload = normalise(_read_file(os.path.join(folder, name)), fallback_id=name[:-5])
        if payload is None or payload["id"] in seen:
            continue
        payload["path"] = os.path.join(folder, name)
        payload["builtin"] = folder == shipped_folder()
        seen.add(payload["id"])
        into[payload["id"]] = payload


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
    """{id: preset} for the whole library (user presets override shipped ones)."""
    key = _cache_key()
    if not force and _CACHE["key"] == key:
        return _CACHE["items"]
    items, seen = {}, set()
    # The user folder is loaded first so its versions win on an id clash.
    _load_folder(user_folder(), items, seen)
    _load_folder(shipped_folder(), items, seen)
    _CACHE["key"], _CACHE["items"] = key, items
    return items


def by_category(library=None):
    """{category: [preset, ...]} in a stable order (name, then id)."""
    library = load() if library is None else library
    grouped = {key: [] for key in CATEGORY_KEYS}
    for preset in library.values():
        grouped[preset["category"]].append(preset)
    for rows in grouped.values():
        rows.sort(key=lambda item: (item["name"].lower(), item["id"]))
    return grouped


def search(term, library=None):
    """Case-insensitive match on name, id and keywords."""
    library = load() if library is None else library
    term = str(term or "").strip().lower()
    if not term:
        return list(library.values())
    found = []
    for preset in library.values():
        haystack = " ".join([preset["name"], preset["id"], " ".join(preset["keywords"])]).lower()
        if term in haystack:
            found.append(preset)
    found.sort(key=lambda item: (item["name"].lower(), item["id"]))
    return found


def write_user_preset(payload):
    """Write a preset into the user folder. Returns (path, preset) or (None, None)."""
    preset = normalise(payload)
    if preset is None:
        return None, None
    folder = user_folder()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{preset['id']}.json")
    stored = {key: value for key, value in preset.items() if key not in ("path", "builtin")}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=1, sort_keys=True)
    load(force=True)
    preset["path"], preset["builtin"] = path, False
    return path, preset


def import_file(path, rename=None):
    """Copy a .mfxpreset/.json file into the user library. Returns (path, preset)."""
    payload = normalise(_read_file(path), fallback_id=os.path.splitext(os.path.basename(path))[0])
    if payload is None:
        return None, None
    if rename:
        payload["name"] = str(rename)
    run = 2
    base_id = payload["id"]
    existing = load()
    while payload["id"] in existing and existing[payload["id"]].get("path") != path:
        payload["id"] = f"{base_id}_{run}"
        run += 1
    return write_user_preset(payload)


def export_file(preset_id, path):
    """Write one preset (or a clip's saved instances) to a .mfxpreset file."""
    preset = load().get(preset_id)
    if preset is None:
        return None
    stored = {key: value for key, value in preset.items() if key not in ("path", "builtin")}
    if not str(path).lower().endswith(".mfxpreset"):
        path = os.path.splitext(str(path))[0] + ".mfxpreset"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=1, sort_keys=True)
    return path


def preset_spec(preset, unit=None):
    """The (program, args, knobs, category) tuple :func:`presets.add` wants."""
    category, _kind, allows_units = CATEGORY_INFO.get(preset["category"], ("in", "clip", False))
    program = preset.get("program", "")
    args = preset.get("args") or {}
    knobs = dict(preset.get("knobs") or {})
    saved = preset.get("instances") or ()
    if not program and saved:
        # A preset saved from a clip carries whole instances; preview and knobs follow the
        # first one, and applying the preset replays all of them (see session.apply_preset).
        program = saved[0].get("program", "fade")
        args = saved[0].get("args") or {}
        knobs = dict(saved[0].get("knobs") or {})
    if allows_units and unit:
        knobs["unit"] = unit
    elif not allows_units:
        knobs.pop("unit", None)
    return category, program or "fade", args, knobs
