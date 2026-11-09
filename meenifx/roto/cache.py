"""Disk cache for analysed Roto Brush masks.

Frame-by-frame masks are far too big for the project JSON (a minute of 720p is tens of
megabytes), so they live beside the preview cache as one 8-bit PNG per source frame:

    <user config>/meenifx-roto/<clip>/<frame>.png
    <user config>/meenifx-roto/<clip>/meta.json

`meta.json` carries the *key*: a digest of the base frame, every stroke and the analysis
parameters. Changing a stroke or the search radius changes the key, so stale masks are
never shown, the folder is reset instead of being silently mislabelled. The project keeps
only small metadata (key, analysed range, frozen flag) and never the pixels, so a project
reopened without its cache simply reports which frames need analysing again.

Nothing here touches the source media: freezing is a cache, not a render.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np

from ..config import user_config_dir

CACHE_VERSION = 4
MAX_MASK_EDGE = 640
# Parameters that change the *raw* segmentation. Feather, expand/shrink, edge refinement
# and the preview mode are applied after the cache, so moving those sliders must not throw
# masks away. Backend and subject choice do change the mask and invalidate it.
ANALYSIS_PARAMS = ("backend", "refine", "quality", "search_radius", "tolerance")
_META = "meta.json"


def cache_root(root=None):
    if root:
        return Path(root)
    override = os.environ.get("MEENIFX_ROTO_CACHE", "").strip()
    if override:
        return Path(override)
    return Path(user_config_dir()) / "meenifx-roto"


def mask_size(rect_width, rect_height, max_edge=MAX_MASK_EDGE):
    """Bounded analysis size for a picture rectangle, keeping its aspect ratio."""
    width = max(1.0, float(rect_width))
    height = max(1.0, float(rect_height))
    factor = min(1.0, float(max_edge) / max(width, height))
    return max(8, int(round(width * factor))), max(8, int(round(height * factor)))


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def mask_key(entry):
    """Digest of everything that changes the raw matte of a clip."""
    entry = entry or {}
    params = entry.get("params") or {}
    try:
        from .model import model_signature
        model = model_signature()
    except Exception:                                  # noqa: BLE001 - never break render
        model = "unknown"
    materials = {
        "version": CACHE_VERSION,
        "effect": str(entry.get("effect_id") or ""),
        "base": int(entry.get("base_frame", 0) or 0),
        "strokes": entry.get("strokes") or [],
        "analysis": {name: params.get(name) for name in ANALYSIS_PARAMS},
        "subject_point": entry.get("subject_point"),
        "model": model,
        "seed": bool(params.get("auto_seed", False)),
    }
    return hashlib.blake2b(_canonical(materials).encode("utf-8"), digest_size=16).hexdigest()


def _safe_name(clip, scope="roto_brush"):
    source = str(getattr(clip, "source_path", "") or "")
    identity = f"{scope}|{getattr(clip, 'id', '')}|{source}"
    return hashlib.blake2b(identity.encode("utf-8"), digest_size=10).hexdigest()


class MaskCache:
    """Per-clip mask store. Safe to read from the render thread and write from a worker.

    `scope` keeps two effects on the same clip apart. Roto Brush and Smart Key share the
    storage format, propagation and model, but they must never clear each other's masks,
    so each writes into its own folder even though both effects point at the same clip.
    """

    def __init__(self, root=None, scope="roto_brush"):
        self.root = cache_root(root)
        self.scope = str(scope or "roto_brush")

    # ------------------------------------------------------------------ locations
    def folder(self, clip):
        return self.root / _safe_name(clip, self.scope)

    def _meta_path(self, clip):
        return self.folder(clip) / _META

    def _frame_path(self, clip, frame):
        return self.folder(clip) / f"{int(frame):06d}.png"

    # ------------------------------------------------------------------ metadata
    def read_meta(self, clip):
        try:
            with open(self._meta_path(clip), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or int(data.get("version", 0) or 0) != CACHE_VERSION:
            return {}
        return data

    def write_meta(self, clip, meta):
        folder = self.folder(clip)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            payload = dict(meta or {})
            payload["version"] = CACHE_VERSION
            temporary = self._meta_path(clip).with_suffix(".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
            os.replace(temporary, self._meta_path(clip))
        except OSError:
            pass

    def configure(self, clip, key, size):
        """Make the folder belong to `key`; a different key drops every stored mask."""
        meta = self.read_meta(clip)
        if meta.get("key") != key:
            self.clear(clip)
            meta = {"key": key, "size": [int(size[0]), int(size[1])], "frames": [],
                    "frozen": False}
            self.write_meta(clip, meta)
        return meta

    # ------------------------------------------------------------------ masks
    def load(self, clip, frame, key):
        """A stored raw matte as float32 0..1, or None when it is missing or stale."""
        meta = self.read_meta(clip)
        if not meta or meta.get("key") != key:
            return None
        path = self._frame_path(clip, frame)
        if not path.is_file():
            return None
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return None
        return image.astype(np.float32) / 255.0

    def store(self, clip, frame, matte, key, size=None):
        if matte is None:
            return None
        meta = self.read_meta(clip)
        if not meta or meta.get("key") != key:
            meta = self.configure(clip, key, size or (matte.shape[1], matte.shape[0]))
        folder = self.folder(clip)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = self._frame_path(clip, frame)
            temporary = path.with_suffix(".tmp.png")
            cv2.imwrite(str(temporary), np.clip(np.asarray(matte, np.float32) * 255.0,
                                                0, 255).astype(np.uint8))
            os.replace(temporary, path)
        except (OSError, cv2.error):
            return None
        frames = sorted({int(value) for value in (meta.get("frames") or [])} | {int(frame)})
        meta["frames"] = frames
        if size is not None:
            meta["size"] = [int(size[0]), int(size[1])]
        self.write_meta(clip, meta)
        return path

    def frames(self, clip, key):
        meta = self.read_meta(clip)
        if not meta or meta.get("key") != key:
            return []
        return sorted({int(value) for value in (meta.get("frames") or [])})

    def has(self, clip, frame, key):
        meta = self.read_meta(clip)
        return (bool(meta) and meta.get("key") == key
                and int(frame) in {int(value) for value in (meta.get("frames") or [])}
                and self._frame_path(clip, frame).is_file())

    # ------------------------------------------------------------------ lifecycle
    def clear(self, clip):
        shutil.rmtree(self.folder(clip), ignore_errors=True)

    def set_frozen(self, clip, frozen):
        meta = self.read_meta(clip)
        if not meta:
            return False
        meta["frozen"] = bool(frozen)
        self.write_meta(clip, meta)
        return True

    def frozen(self, clip):
        return bool(self.read_meta(clip).get("frozen", False))

    def summary(self, clip, key):
        """(stored frame count, frozen) for the panel's status line."""
        meta = self.read_meta(clip)
        if not meta or meta.get("key") != key:
            return 0, False
        return len(meta.get("frames") or []), bool(meta.get("frozen", False))
