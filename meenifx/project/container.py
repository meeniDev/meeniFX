"""The .mfxpf container: a ZIP archive of separate, versioned entries.

Layout
    manifest.json            format name, version, entry list
    meta.json                name, timestamps, size/fps/duration/counts (read alone for the start window)
    settings.json            project settings
    media.json               media references (id, type, relative + absolute path, duration, size, fps, bins)
    timeline/tracks.json     tracks
    timeline/clips.json      clips, columnar
    timeline/effects.json    effects per clip, columnar
    timeline/keyframes.json  keyframes, columnar
    timeline/markers.json    markers and loop region
    ui/layout.json           saved UI / timeline view state
    ui/window_state.bin      Qt dock/window state
    thumbs/project.jpg       project thumbnail
    thumbs/media/<id>.jpg    media thumbnails

JSON entries are deflated; images are stored as-is (already compressed). Original media is never embedded.
"""

import json
import os
import time
import zipfile

FORMAT_NAME = "mfxpf"
FORMAT_VERSION = 5

JSON_ENTRIES = {
    "meta": "meta.json",
    "settings": "settings.json",
    "media": "media.json",
    "tracks": "timeline/tracks.json",
    "clips": "timeline/clips.json",
    "effects": "timeline/effects.json",
    "keyframes": "timeline/keyframes.json",
    "markers": "timeline/markers.json",
    "ui": "ui/layout.json",
}
REQUIRED = ("meta", "settings", "media", "tracks", "clips", "effects", "keyframes", "markers")
WINDOW_STATE_ENTRY = "ui/window_state.bin"
PROJECT_THUMB = "thumbs/project.jpg"
MEDIA_THUMB_DIR = "thumbs/media/"
COMPRESSED_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


class ProjectFormatError(Exception):
    """The file is not a valid or readable .mfxpf project."""


# {from_version: function(sections: dict) -> sections for from_version + 1}
MIGRATIONS = {}


def _migrate_1_to_2(sections):
    """Format 2 splits video and audio into separate linked clips.

    A format 1 clip played both streams itself, so every old clip is marked "both": it
    keeps its own audio and is not given an extra audio partner when the project opens.
    """
    clips = sections.get("clips") or {}
    count = len(clips.get("id", []))
    clips.setdefault("stream", ["both"] * count)
    clips.setdefault("link", [0] * count)
    sections["clips"] = clips
    return sections


MIGRATIONS[1] = _migrate_1_to_2


def _migrate_2_to_3(sections):
    """Format 3 stores clip transforms, puppet pins and easing, all with defaults.

    Old clips have no transform: an identity transform keeps them exactly as they were,
    and their effect keyframes are untouched. Nothing has to be invented.
    """
    clips = sections.get("clips") or {}
    count = len(clips.get("id", []))
    clips.setdefault("transform", [{} for _ in range(count)])
    clips.setdefault("meta", [{} for _ in range(count)])
    sections["clips"] = clips
    effects = sections.get("effects") or {}
    rows = len(effects.get("clip", []))
    effects.setdefault("pins", [[] for _ in range(rows)])
    effects.setdefault("data", [{} for _ in range(rows)])
    sections["effects"] = effects
    keyframes = sections.get("keyframes") or {}
    keyframes.setdefault("kinds", ["effect"] * len(keyframes.get("owners", [])))
    sections["keyframes"] = keyframes
    return sections


MIGRATIONS[2] = _migrate_2_to_3


def _migrate_3_to_4(sections):
    """Format 4 stores generated text clips. Old projects simply have no text clips."""
    clips = sections.get("clips") or {}
    count = len(clips.get("id", []))
    clips.setdefault("text", [None for _ in range(count)])
    sections["clips"] = clips
    return sections


MIGRATIONS[3] = _migrate_3_to_4


def _migrate_4_to_5(sections):
    """Format 5 adds per-clip audio gain (dB).

    Old clips had no gain control, so 0 dB (unity) keeps every project sounding exactly
    as it did; the new text channels (fill opacity, blur, reveal) default themselves in
    the model when an old text clip is read.
    """
    clips = sections.get("clips") or {}
    count = len(clips.get("id", []))
    clips.setdefault("gain", [0.0 for _ in range(count)])
    sections["clips"] = clips
    return sections


MIGRATIONS[4] = _migrate_4_to_5


def migrate(sections, version):
    while version < FORMAT_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ProjectFormatError(f"No migration from format version {version}")
        sections = step(sections)
        version += 1
    return sections


class ProjectData:
    """Everything stored in a project file, as plain Python data."""

    def __init__(self):
        self.sections = {k: None for k in JSON_ENTRIES}
        self.window_state = None
        self.thumbnails = {}  # entry name (e.g. thumbs/project.jpg) -> bytes

    def __getitem__(self, key):
        return self.sections[key]

    def __setitem__(self, key, value):
        self.sections[key] = value


def _dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def write_container(path, data):
    """Write a project to `path` (caller handles atomic replace)."""
    manifest = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "created_by": "meeniFX", "entries": []}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        def put(name, payload):
            stored = name.lower().endswith(COMPRESSED_SUFFIXES) or name == WINDOW_STATE_ENTRY and len(payload) < 64
            zf.writestr(name, payload, zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
            manifest["entries"].append({"name": name, "size": len(payload)})
        for key, name in JSON_ENTRIES.items():
            if data.sections.get(key) is not None:
                put(name, _dumps(data.sections[key]))
        if data.window_state:
            put(WINDOW_STATE_ENTRY, bytes(data.window_state))
        for name, payload in data.thumbnails.items():
            put(name, payload)
        # manifest last, but it is read first (zip central directory makes order irrelevant)
        zf.writestr("manifest.json", _dumps(manifest))


def _read_json(zf, name):
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except KeyError:
        raise ProjectFormatError(f"Missing entry: {name}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProjectFormatError(f"Corrupt entry {name}: {exc}")


def _open(path):
    try:
        zf = zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ProjectFormatError(f"Not a valid meeniFX project file: {exc}")
    try:
        manifest = _read_json(zf, "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
            raise ProjectFormatError("Not a meeniFX project file")
        version = manifest.get("version")
        if not isinstance(version, int) or version < 1:
            raise ProjectFormatError("Invalid format version")
        if version > FORMAT_VERSION:
            raise ProjectFormatError(
                f"This project was saved by a newer meeniFX (format {version}); this version reads up to {FORMAT_VERSION}.")
        return zf, version
    except Exception:
        zf.close()
        raise


def read_container(path):
    """Read and validate a project; returns ProjectData migrated to the current format."""
    zf, version = _open(path)
    with zf:
        data = ProjectData()
        for key in REQUIRED:
            data.sections[key] = _read_json(zf, JSON_ENTRIES[key])
        names = set(zf.namelist())
        if JSON_ENTRIES["ui"] in names:
            data.sections["ui"] = _read_json(zf, JSON_ENTRIES["ui"])
        if WINDOW_STATE_ENTRY in names:
            data.window_state = zf.read(WINDOW_STATE_ENTRY)
        for name in names:
            if name.startswith("thumbs/"):
                data.thumbnails[name] = zf.read(name)
    data.sections = migrate(data.sections, version)
    validate(data)
    return data


def validate(data):
    s = data.sections
    for key in REQUIRED:
        if s.get(key) is None:
            raise ProjectFormatError(f"Missing section: {key}")
    clips = s["clips"]
    n = len(clips.get("id", []))
    if any(len(col) != n for col in clips.values()):
        raise ProjectFormatError("Clip columns have different lengths")
    kf = s["keyframes"]
    m = len(kf.get("time", []))
    if any(len(kf.get(k, [])) != m for k in ("owner", "value", "interp")):
        raise ProjectFormatError("Keyframe columns have different lengths")
    eff = s["effects"]
    e = len(eff.get("clip", []))
    if any(len(col) != e for col in eff.values()):
        raise ProjectFormatError("Effect columns have different lengths")
    for owner in kf.get("owner", []):
        if not isinstance(owner, int) or not 0 <= owner < len(kf.get("owners", [])):
            raise ProjectFormatError("Keyframe refers to an unknown parameter")


def read_summary(path):
    """(meta dict, thumbnail bytes or None) without loading the timeline; used by the start window."""
    zf, _version = _open(path)
    with zf:
        meta = _read_json(zf, JSON_ENTRIES["meta"])
        thumb = zf.read(PROJECT_THUMB) if PROJECT_THUMB in zf.namelist() else None
    return meta, thumb


def make_meta(name, settings, duration_frames, clip_count, extra=None):
    meta = {
        "name": name,
        "modified": time.time(),
        "width": settings.width, "height": settings.height, "fps": settings.fps,
        "duration_frames": int(duration_frames), "clips": int(clip_count),
        "app": "meeniFX Studios",
    }
    meta.update(extra or {})
    return meta
