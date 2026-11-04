"""Project media library: items (links to files on disk) organised in simple bins."""

import os
from dataclasses import dataclass, asdict, fields

from ..model.timeline import IMAGE_EXTENSIONS

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}


def media_type_for(path):
    ext = os.path.splitext(path or "")[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return "video"


def norm_path(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def relative_to(path, base_dir):
    """Path relative to base_dir, or None when that is impossible (e.g. another drive)."""
    try:
        return os.path.relpath(path, base_dir).replace("\\", "/")
    except ValueError:
        return None


@dataclass
class MediaItem:
    id: str
    type: str
    name: str
    path: str = ""
    rel_path: str = ""
    duration: float = 0.0  # seconds
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bin: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class MediaLibrary:
    def __init__(self):
        self.items = {}   # id -> MediaItem, insertion ordered
        self.bins = {}    # id -> name
        self._next_item = 1
        self._next_bin = 1
        self._by_path = {}

    def __len__(self):
        return len(self.items)

    def _new_id(self):
        while f"m{self._next_item}" in self.items:
            self._next_item += 1
        mid = f"m{self._next_item}"
        self._next_item += 1
        return mid

    def by_path(self, path):
        item_id = self._by_path.get(norm_path(path))
        return self.items.get(item_id) if item_id else None

    def reindex(self):
        self._by_path = {norm_path(i.path): i.id for i in self.items.values() if i.path}

    def set_path(self, item_id, path):
        item = self.items[item_id]
        self._by_path.pop(norm_path(item.path), None)
        item.path = os.path.abspath(path)
        self._by_path[norm_path(item.path)] = item_id

    def add(self, path, duration=0.0, width=0, height=0, fps=0.0, name=None, media_type=None, bin_id=""):
        """Add a file (or return the existing item for that path)."""
        existing = self.by_path(path)
        if existing is not None:
            return existing
        item = MediaItem(self._new_id(), media_type or media_type_for(path), name or os.path.basename(path),
                         os.path.abspath(path), "", float(duration or 0), int(width or 0), int(height or 0),
                         float(fps or 0), bin_id if bin_id in self.bins else "")
        self.items[item.id] = item
        self._by_path[norm_path(item.path)] = item.id
        return item

    def remove(self, item_id):
        item = self.items.pop(item_id, None)
        if item is not None:
            self._by_path.pop(norm_path(item.path), None)
        return item

    def add_bin(self, name):
        bid = f"b{self._next_bin}"
        self._next_bin += 1
        self.bins[bid] = name or "Bin"
        return bid

    def remove_bin(self, bin_id):
        self.bins.pop(bin_id, None)
        for item in self.items.values():
            if item.bin == bin_id:
                item.bin = ""

    def move_to_bin(self, item_ids, bin_id):
        bin_id = bin_id if bin_id in self.bins else ""
        for iid in item_ids:
            if iid in self.items:
                self.items[iid].bin = bin_id

    def to_dict(self, project_dir=None):
        items = []
        for item in self.items.values():
            d = item.to_dict()
            d["rel_path"] = (relative_to(item.path, project_dir) or "") if project_dir and item.path else ""
            items.append(d)
        return {"items": items, "bins": [{"id": k, "name": v} for k, v in self.bins.items()]}

    @classmethod
    def from_dict(cls, data, project_dir=None):
        """Rebuild the library. The relative path wins when it exists, else the absolute fallback is kept."""
        lib = cls()
        for b in data.get("bins", []):
            lib.bins[str(b["id"])] = str(b.get("name", "Bin"))
        for d in data.get("items", []):
            item = MediaItem.from_dict(d)
            if project_dir and item.rel_path:
                candidate = os.path.normpath(os.path.join(project_dir, item.rel_path))
                if os.path.exists(candidate) or not item.path:
                    item.path = candidate
            if item.bin not in lib.bins:
                item.bin = ""
            lib.items[item.id] = item
        digits = [int(i[1:]) for i in lib.items if i[1:].isdigit()]
        lib._next_item = max(digits, default=0) + 1
        bdigits = [int(i[1:]) for i in lib.bins if i[1:].isdigit()]
        lib._next_bin = max(bdigits, default=0) + 1
        lib.reindex()
        return lib

    def missing(self):
        return [i for i in self.items.values() if i.path and not os.path.exists(i.path)]
