"""Preview frame cache: a RAM LRU over a disposable disk tier.

The cache stores exactly what the compositor produced (BGR uint8 at a given preview
scale), keyed by ``(frame, scale)``. Nothing else knows how a frame was made, so a
frame is either there and usable, or it is not - which is what makes the timeline
indicator honest.

* RAM is always used and bounded by a byte budget (least recently used frames go first).
* The disk tier lives in ``<project>/Cache/preview`` and is only written by background
  rendering; it is regenerable at any time and never enters the ``.mfxpf`` file.
* ``pending`` marks frames a worker is rendering right now, so the indicator can show
  "rendering" for real work instead of guessing.
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

MANIFEST = "manifest.json"
CACHE_VERSION = 1


def scale_key(scale):
    """Quantised scale so 0.5 and 0.5000001 share cache entries."""
    return int(round(float(scale) * 1000.0))


class PreviewCache(QObject):
    """Thread-safe two-tier cache of rendered preview frames."""

    changed = pyqtSignal()          # any frame appeared/disappeared; the UI coalesces

    def __init__(self, ram_budget_mb=1024, disk_budget_mb=4096, parent=None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._ram = OrderedDict()            # (frame, skey) -> ndarray
        self._ram_bytes = 0
        self._disk_frames = set()            # (frame, skey) present on disk
        self._disk_index = {}                # (frame, skey) -> (path, bytes)
        self._pending = set()
        self._folder = ""
        self._ram_budget = int(max(0, ram_budget_mb)) * 1024 * 1024
        self._disk_budget = int(max(0, disk_budget_mb)) * 1024 * 1024
        self._hits = 0
        self._misses = 0
        self._disk_hits = 0

    # ------------------------------------------------------------------ budgets
    def set_budgets(self, ram_budget_mb=None, disk_budget_mb=None):
        with self._lock:
            if ram_budget_mb is not None:
                self._ram_budget = int(max(0, ram_budget_mb)) * 1024 * 1024
            if disk_budget_mb is not None:
                self._disk_budget = int(max(0, disk_budget_mb)) * 1024 * 1024
            self._trim_ram()
            self._trim_disk()

    @property
    def ram_budget(self):
        return self._ram_budget

    @property
    def folder(self):
        return self._folder

    # -------------------------------------------------------------------- disk
    def open_folder(self, folder, fingerprint=""):
        """Point the disk tier at a project cache folder; reuse only matching frames."""
        with self._lock:
            self._folder = str(folder or "")
            self._disk_frames.clear()
            self._disk_index.clear()
            if not self._folder:
                return
            try:
                os.makedirs(self._folder, exist_ok=True)
            except OSError:
                self._folder = ""
                return
            manifest = self._read_manifest()
            if manifest.get("fingerprint") not in (None, "", fingerprint):
                # The project changed since these frames were rendered: start clean. RAM
                # must go too - those frames belong to a different timeline.
                self._wipe_disk()
                self._ram.clear()
                self._ram_bytes = 0
                self._pending.clear()
            self._scan_disk()

    def write_manifest(self, fingerprint, extra=None):
        with self._lock:
            if not self._folder:
                return
            payload = {"version": CACHE_VERSION, "fingerprint": str(fingerprint)}
            if extra:
                payload.update(extra)
            try:
                with open(os.path.join(self._folder, MANIFEST), "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
            except OSError:
                pass

    def _read_manifest(self):
        try:
            with open(os.path.join(self._folder, MANIFEST), encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _scan_disk(self):
        try:
            names = os.listdir(self._folder)
        except OSError:
            return
        for name in names:
            if not name.endswith(".npy"):
                continue
            try:
                stem = name[:-4].split("_")
                frame = int(stem[0][1:])
                skey = int(stem[1][1:])
            except (IndexError, ValueError):
                continue
            path = os.path.join(self._folder, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            key = (frame, skey)
            self._disk_frames.add(key)
            self._disk_index[key] = (path, size)
        self._trim_disk()

    def _path_for(self, frame, skey):
        return os.path.join(self._folder, f"f{int(frame):07d}_s{int(skey):07d}.npy")

    def _wipe_disk(self):
        """Delete every cache file in the folder, indexed or not.

        The index is emptied before a folder switch, so removing only indexed files would
        leave stale frames behind and a mismatched manifest would silently keep them.
        """
        removed = 0
        try:
            names = os.listdir(self._folder) if self._folder else []
        except OSError:
            names = []
        for name in names:
            if not (name.endswith(".npy") or name == MANIFEST):
                continue
            try:
                os.remove(os.path.join(self._folder, name))
                removed += 1
            except OSError:
                pass
        self._disk_index.clear()
        self._disk_frames.clear()
        self._wiped = removed
        return removed

    # --------------------------------------------------------------------- api
    def get(self, frame, scale):
        """The cached frame, or None. 'None' also records a miss for the hit rate."""
        key = (int(frame), scale_key(scale))
        with self._lock:
            image = self._ram.get(key)
            if image is not None:
                self._ram.move_to_end(key)
                self._hits += 1
                return image
            entry = self._disk_index.get(key)
            if entry is None:
                self._misses += 1
                return None
            try:
                image = np.load(entry[0])
            except (OSError, ValueError):
                self._disk_index.pop(key, None)
                self._disk_frames.discard(key)
                self._misses += 1
                return None
            self._disk_hits += 1
            self._hits += 1
            self._insert_ram(key, image)
            return image

    def has(self, frame, scale):
        key = (int(frame), scale_key(scale))
        with self._lock:
            return key in self._ram or key in self._disk_frames

    def state(self, frame, scale):
        """'pending' | 'cached' | 'missing' for the timeline indicator."""
        key = (int(frame), scale_key(scale))
        with self._lock:
            if key in self._ram or key in self._disk_frames:
                return "cached"
            if key in self._pending:
                return "pending"
            return "missing"

    def put(self, frame, scale, image, to_disk=False):
        if image is None or not isinstance(image, np.ndarray):
            return
        key = (int(frame), scale_key(scale))
        with self._lock:
            self._insert_ram(key, image)
            self._pending.discard(key)
            if to_disk and self._folder and self._disk_budget > 0:
                self._write_disk(key, image)
        self.changed.emit()

    def mark_pending(self, frame, scale):
        with self._lock:
            self._pending.add((int(frame), scale_key(scale)))

    def mark_pending_many(self, frames, scale):
        """Mark a queued prerender range as "rendering" so the bar turns yellow, not red."""
        key = scale_key(scale)
        with self._lock:
            for frame in frames:
                self._pending.add((int(frame), key))

    def clear_pending(self, frame=None, scale=None):
        with self._lock:
            if frame is None:
                self._pending.clear()
            else:
                self._pending.discard((int(frame), scale_key(scale)))

    def pending_count(self):
        with self._lock:
            return len(self._pending)

    # ------------------------------------------------------------- invalidation
    def invalidate_frames(self, frames):
        """Drop these frames at every scale (RAM and disk)."""
        wanted = {int(f) for f in frames}
        if not wanted:
            return 0
        with self._lock:
            removed = 0
            for key in [k for k in self._ram if k[0] in wanted]:
                self._ram.pop(key, None)
                removed += 1
            self._recount_ram()
            # An invalidated frame is not being rendered any more, so it must read as
            # "needs render" (red) rather than staying yellow forever.
            self._pending = {k for k in self._pending if k[0] not in wanted}
            for key in [k for k in self._disk_index if k[0] in wanted]:
                path, _size = self._disk_index.pop(key)
                self._disk_frames.discard(key)
                try:
                    os.remove(path)
                except OSError:
                    pass
        if removed:
            self.changed.emit()
        return removed

    def invalidate_ranges(self, ranges):
        """Drop frames inside [(first, last)] inclusive ranges (changed clips only)."""
        frames = set()
        for first, last in ranges or ():
            first, last = int(min(first, last)), int(max(first, last))
            if last - first > 20000:
                last = first + 20000
            frames.update(range(max(0, first), max(0, last) + 1))
        return self.invalidate_frames(frames)

    def clear(self, disk=True):
        with self._lock:
            self._ram.clear()
            self._ram_bytes = 0
            self._pending.clear()
            if disk:
                self._wipe_disk()
        self.changed.emit()

    # ------------------------------------------------------------------ memory
    @staticmethod
    def _size_of(image):
        return int(getattr(image, "nbytes", 0) or 0)

    def _insert_ram(self, key, image):
        old = self._ram.pop(key, None)
        if old is not None:
            self._ram_bytes -= self._size_of(old)
        self._ram[key] = image
        self._ram_bytes += self._size_of(image)
        self._trim_ram()

    def _recount_ram(self):
        self._ram_bytes = sum(self._size_of(image) for image in self._ram.values())

    def _trim_ram(self):
        while self._ram and self._ram_bytes > max(0, self._ram_budget):
            key, image = self._ram.popitem(last=False)
            self._ram_bytes -= self._size_of(image)

    def _write_disk(self, key, image):
        path = self._path_for(*key)
        try:
            np.save(path, image)
            size = os.path.getsize(path)
        except (OSError, ValueError):
            return
        self._disk_index[key] = (path, size)
        self._disk_frames.add(key)
        self._trim_disk()

    def _trim_disk(self):
        if self._disk_budget <= 0:
            return
        total = sum(size for _path, size in self._disk_index.values())
        while self._disk_index and total > self._disk_budget:
            key = next(iter(self._disk_index))
            path, size = self._disk_index.pop(key)
            self._disk_frames.discard(key)
            total -= size
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------------- stats
    def stats(self):
        with self._lock:
            disk_bytes = sum(size for _path, size in self._disk_index.values())
            lookups = self._hits + self._misses
            return {
                "ram_frames": len(self._ram),
                "ram_bytes": self._ram_bytes,
                "ram_budget": self._ram_budget,
                "disk_frames": len(self._disk_index),
                "disk_bytes": disk_bytes,
                "disk_budget": self._disk_budget,
                "pending": len(self._pending),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / lookups) if lookups else 0.0,
            }

    def reset_stats(self):
        with self._lock:
            self._hits = self._misses = self._disk_hits = 0
