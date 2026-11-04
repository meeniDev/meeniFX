"""Device discovery with sensible defaults and fallback when a saved device is gone."""

from dataclasses import dataclass

from PyQt6.QtMultimedia import QMediaDevices, QWindowCapture
from PyQt6.QtWidgets import QApplication


def device_id(device):
    return bytes(device.id()).decode("utf-8", "replace")


@dataclass(frozen=True)
class CameraMode:
    width: int
    height: int
    fps: int
    format: object = None

    @property
    def key(self):
        return f"{self.width}x{self.height}@{self.fps}"

    @property
    def label(self):
        return f"{self.width}x{self.height}  {self.fps} fps"


def cameras():
    return list(QMediaDevices.videoInputs())


def microphones():
    return list(QMediaDevices.audioInputs())


def find_by_id(devices, wanted):
    for device in devices:
        if device_id(device) == wanted:
            return device
    return None


def _default(devices, default):
    if default is not None and not default.isNull() and find_by_id(devices, device_id(default)):
        return default
    return devices[0] if devices else None


def pick_camera(saved_id):
    """The saved camera if it is still plugged in, else the system default, else the first one."""
    devices = cameras()
    return find_by_id(devices, saved_id) or _default(devices, QMediaDevices.defaultVideoInput())


def pick_microphone(saved_id):
    devices = microphones()
    return find_by_id(devices, saved_id) or _default(devices, QMediaDevices.defaultAudioInput())


def camera_modes(device):
    """Resolution/fps pairs the camera supports (usable ones only), largest first."""
    if device is None or device.isNull():
        return []
    best = {}
    for fmt in device.videoFormats():
        size = fmt.resolution()
        w, h = size.width(), size.height()
        fps = int(round(fmt.maxFrameRate()))
        if w < 320 or h < 240 or fps < 15:
            continue
        best.setdefault((w, h, fps), CameraMode(w, h, fps, fmt))
    return sorted(best.values(), key=lambda m: (-m.width * m.height, -m.fps))


def default_mode(modes):
    """Prefer 1080p30, then 720p30, then the largest mode at 24-30 fps, then the first one."""
    for want in ((1920, 1080, 30), (1280, 720, 30)):
        for m in modes:
            if (m.width, m.height, m.fps) == want:
                return m
    thirty = [m for m in modes if 24 <= m.fps <= 30]
    return (thirty or modes or [None])[0]


def pick_mode(modes, saved_key):
    for m in modes:
        if m.key == saved_key:
            return m
    return default_mode(modes)


def screens():
    return list(QApplication.screens())


def pick_screen(saved_name):
    for s in screens():
        if s.name() == saved_name:
            return s
    return QApplication.primaryScreen()


def windows():
    """Capturable top-level windows that have a usable title."""
    out = []
    for w in QWindowCapture.capturableWindows():
        title = w.description().strip()
        if title and not title.startswith("Window 0x"):
            out.append(w)
    return out


def find_window(saved_id):
    for w in windows():
        if w.id() == saved_id:
            return w
    return None
