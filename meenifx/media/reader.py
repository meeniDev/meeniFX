"""Decoders and tile loading: nothing here touches widgets."""

import os
import threading
from collections import OrderedDict

import cv2
import imageio.v2 as imageio

from ..config import DEFAULT_TILE_PATHS


def load_default_tiles():
    images = []
    for path in DEFAULT_TILE_PATHS:
        img = cv2.imread(str(path))
        if img is None:
            raise FileNotFoundError(f"Missing tile image: {path}")
        images.append(img)
    return images


STREAM_BYTES = 500_000_000   # videos that would decode to more than this stay on disk
THUMB_STEP_SECONDS = 2.0

# Hardware decoding is requested (never assumed): the value OpenCV reports afterwards is
# what the Preferences dialog and the overlay show, so "software decode" stays visible.
_HW = {"mode": "auto", "acceleration": 0}


def set_hardware_decode(mode):
    _HW["mode"] = mode if mode in ("auto", "on", "off") else "auto"


def decode_acceleration():
    """0 = software decode, otherwise OpenCV's VIDEO_ACCELERATION_* value actually used."""
    return int(_HW.get("acceleration", 0))


def _open_capture(path):
    """Open a video, asking for hardware decoding when the preference allows it."""
    if _HW["mode"] != "off" and hasattr(cv2, "CAP_PROP_HW_ACCELERATION"):
        cap = cv2.VideoCapture()
        try:
            if cap.open(str(path), cv2.CAP_FFMPEG,
                        [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY]):
                _HW["acceleration"] = int(cap.get(cv2.CAP_PROP_HW_ACCELERATION) or 0)
                return cap
        except Exception:
            pass
        finally:
            if not cap.isOpened():
                cap.release()
        _HW["acceleration"] = 0
    return cv2.VideoCapture(str(path))


class StreamFrames:
    """Read-only frame sequence backed by the file: sequential reads are cheap, seeks are cached.

    Behaves like a list of BGR frames (len, index) without holding the video in memory.
    A background pass keeps one small thumbnail every couple of seconds for the timeline.
    """

    CACHE = 24

    def __init__(self, path, frame_count, fps):
        self.path = path
        self.count = max(1, int(frame_count))
        self.fps = float(fps)
        self._lock = threading.Lock()
        self._cap = _open_capture(path)
        self._next = 0
        self._recent = OrderedDict()
        self._first = None
        self.thumb_step = max(1, int(round(self.fps * THUMB_STEP_SECONDS)))
        self.thumbs = {}
        self._thumb_thread = None

    def __len__(self):
        return self.count

    def __bool__(self):
        return True

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.count))]
        index = int(index)
        if index < 0:
            index += self.count
        index = max(0, min(index, self.count - 1))
        with self._lock:
            frame = self._recent.get(index)
            if frame is not None:
                self._recent.move_to_end(index)
                return frame
            frame = self._read(index)
            if frame is None:
                frame = self._recent.get(max(self._recent), None) if self._recent else self._first
            if frame is None:
                raise IndexError(index)
            self._recent[index] = frame
            while len(self._recent) > self.CACHE:
                self._recent.popitem(last=False)
            if index == 0:
                self._first = frame
            return frame

    def _read(self, index):
        cap = self._cap
        if index != self._next:
            if 0 < index - self._next <= 12:
                while self._next < index and cap.grab():
                    self._next += 1
            else:
                cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                self._next = index
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        self._next = index + 1
        return frame

    def start_thumbnails(self, height=96):
        """Build the coarse thumbnail strip on a separate capture so playback is never blocked."""
        if self._thumb_thread is not None:
            return
        def work():
            cap = _open_capture(self.path)
            try:
                index = 0
                while index < self.count:
                    if index % self.thumb_step == 0:
                        ok, frame = cap.read()
                        if not ok:
                            break
                        h, w = frame.shape[:2]
                        self.thumbs[index // self.thumb_step] = cv2.resize(
                            frame, (max(1, int(round(height * w / h))), height), interpolation=cv2.INTER_AREA)
                    elif not cap.grab():
                        break
                    index += 1
            finally:
                cap.release()
        self._thumb_thread = threading.Thread(target=work, daemon=True)
        self._thumb_thread.start()

    def thumb_for(self, index):
        if not self.thumbs:
            return None
        slot = index // self.thumb_step
        while slot >= 0 and slot not in self.thumbs:
            slot -= 1
        return self.thumbs.get(max(slot, 0))

    def close(self):
        with self._lock:
            self._cap.release()


def read_video_frames(path):
    cap = _open_capture(path)
    if not cap.isOpened():
        raise RuntimeError("Unable to open video file.")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = 30.0
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if count > 0 and count * width * height * 3 > STREAM_BYTES:
        cap.release()
        stream = StreamFrames(path, count, fps)
        stream.start_thumbnails()
        return stream, float(fps)

    frames = []
    try:
        while cap.isOpened():
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            frames.append(frame)
    finally:
        cap.release()

    return frames, float(fps)


def _gif_fps(meta):
    fps = None
    if isinstance(meta, dict):
        if "fps" in meta and meta["fps"]:
            fps = float(meta["fps"])
        elif "duration" in meta and meta["duration"]:
            duration_val = float(meta["duration"])
            if duration_val > 0:
                if duration_val > 10:
                    fps = 1000.0 / duration_val
                else:
                    fps = 1.0 / duration_val
    if not fps or fps <= 0:
        fps = 12.0
    return float(fps)


def read_gif_frames(path):
    reader = imageio.get_reader(path)
    fps = _gif_fps(reader.get_meta_data())

    frames = []
    try:
        for arr in reader:
            if arr.shape[-1] == 4:
                arr = arr[:, :, :3]
            frame_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            frames.append(frame_bgr)
    finally:
        reader.close()

    return frames, fps


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def probe_media(path):
    """Cheap header read: returns (kind, frame_count, fps) without decoding every frame."""
    ext = os.path.splitext(path.lower())[1]
    if ext in IMAGE_EXTS:
        return "image", 1, 1.0
    if ext == ".gif":
        reader = imageio.get_reader(path)
        try:
            return "video", int(len(reader)), _gif_fps(reader.get_meta_data())
        finally:
            reader.close()
    cap = _open_capture(path)
    try:
        if not cap.isOpened():
            raise RuntimeError("Unable to open video file.")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return "video", max(1, count), float(fps)
    finally:
        cap.release()


def read_media(path):
    """Fully decode a media file. Returns (kind, frames, fps)."""
    ext = os.path.splitext(path.lower())[1]
    if ext in IMAGE_EXTS:
        frame = cv2.imread(path)
        if frame is None:
            raise RuntimeError("Unable to read image.")
        return "image", [frame], 1.0
    if ext == ".gif":
        frames, fps = read_gif_frames(path)
        return "video", frames, fps
    frames, fps = read_video_frames(path)
    return "video", frames, fps

