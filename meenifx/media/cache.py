import os
from collections import OrderedDict

import cv2

from .proxy import proxy_path
from .reader import probe_media, read_media


def entry_size(entry, default=None):
    """(width, height) of an entry's first frame, or `default` when it has none.

    ``entry.frames`` is a lazy reader for a file that can have been moved, renamed or
    hold no decodable frame; indexing it raises IndexError. The overlay paints and the
    inspector rebuilds with this size on every redraw, so asking must never raise.
    """
    if entry is None or getattr(entry, "frames", None) is None:
        return default
    try:
        height, width = entry.frames[0].shape[:2]
    except (IndexError, KeyError, TypeError, AttributeError, ValueError):
        return default
    if not width or not height:
        return default
    return int(width), int(height)


class MediaEntry:
    def __init__(self, kind, frame_count, fps):
        self.kind = kind
        self.frame_count = frame_count
        self.fps = fps
        self.frames = None


class MediaCache:
    """Probes media headers cheaply, decodes frames on demand and serves timeline thumbnails."""

    THUMB_LIMIT = 500

    def __init__(self):
        self._entries = {}
        self._thumbs = OrderedDict()
        # Proxy playback is preview-only: `_proxy_suspended` is set while exporting so a
        # reduced-resolution stand-in can never reach the final file.
        self._proxy_enabled = False
        self._proxy_width = 1280
        self._proxy_folder = ""
        self._proxy_suspended = False
        self._resolved = {}

    def set_proxy(self, enabled=None, width=None, folder=None):
        if enabled is not None:
            self._proxy_enabled = bool(enabled)
        if width is not None:
            self._proxy_width = int(width)
        if folder is not None and str(folder) != self._proxy_folder:
            self._proxy_folder = str(folder)
            self._resolved.clear()

    def suspend_proxy(self, suspended):
        self._proxy_suspended = bool(suspended)

    def proxy_source(self, path, suspendable=True):
        """The file to decode for the *preview*: the proxy when one exists, else the source."""
        if not path or not self._proxy_enabled or self._proxy_folder == "":
            return path
        if suspendable and self._proxy_suspended:
            return path
        cached = self._resolved.get(path)
        if cached is not None:
            return cached
        candidate = proxy_path(path, self._proxy_folder, self._proxy_width)
        resolved = candidate if candidate and os.path.exists(candidate) else path
        self._resolved[path] = resolved
        return resolved

    def entry(self, path):
        entry = self._entries.get(path)
        if entry is None:
            if not path or not os.path.exists(path):
                return None
            try:
                entry = MediaEntry(*probe_media(path))
            except Exception:
                return None
            self._entries[path] = entry
        return entry

    def is_loaded(self, path):
        entry = self._entries.get(path)
        return entry is not None and entry.frames is not None

    def load(self, path):
        entry = self.entry(path)
        if entry is None:
            return None
        if entry.frames is None:
            kind, frames, fps = read_media(self.proxy_source(path))
            if not frames:
                raise RuntimeError("No frames found in source.")
            entry.kind, entry.frames, entry.fps = kind, frames, fps
            entry.frame_count = len(frames)
        return entry

    def length_in_timeline_frames(self, path, timeline_fps):
        """Number of timeline frames a source spans (None for stills, which have no limit)."""
        entry = self.entry(path)
        if entry is None or entry.kind == "image":
            return None
        return max(1, int(entry.frame_count * timeline_fps / entry.fps))

    def source_index(self, entry, timeline_frame_offset, timeline_fps):
        if entry.kind == "image":
            return 0
        index = int(timeline_frame_offset * entry.fps / timeline_fps)
        return max(0, min(index, len(entry.frames) - 1))

    def frame_for_clip(self, clip, frame, timeline_fps):
        entry = self.load(clip.source_path)
        if entry is None:
            return None
        source_index = clip.source_frame_at(frame)
        return entry.frames[self.source_index(entry, source_index, entry.fps)]

    def thumbnail(self, clip, tile_offset, timeline_fps, height, width_limit):
        """Return a PhotoImage for a clip tile, or None if the media isn't decoded yet."""
        entry = self._entries.get(clip.source_path)
        if entry is None or entry.frames is None:
            return None
        index = self.source_index(entry, clip.source_frame_at(clip.timeline_start + tile_offset), entry.fps)
        key = (clip.source_path, index, height, width_limit)
        thumbnail = self._thumbs.get(key)
        if thumbnail is not None:
            self._thumbs.move_to_end(key)
            return thumbnail
        frame = entry.frames[index] if not hasattr(entry.frames, "thumb_for") else entry.frames.thumb_for(index)
        if frame is None:
            return None
        h, w = frame.shape[:2]
        tile_w = max(1, int(round(height * w / h)))
        thumbnail = cv2.resize(frame, (tile_w, height), interpolation=cv2.INTER_AREA)
        if width_limit and width_limit < tile_w:
            thumbnail = thumbnail[:, :max(1, width_limit)].copy()
        self._thumbs[key] = thumbnail
        while len(self._thumbs) > self.THUMB_LIMIT:
            self._thumbs.popitem(last=False)
        return thumbnail

    def tile_width(self, clip, height):
        size = entry_size(self._entries.get(clip.source_path))
        if size is None:
            return int(height * 16 / 9)
        w, h = size
        return max(1, int(round(height * w / h)))

    def source_size(self, path, default=None):
        """(width, height) of a source's first frame, or `default` when none decodes."""
        return entry_size(self._entries.get(path), default)
