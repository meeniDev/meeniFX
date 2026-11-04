"""Disk-space checks and short, human error messages for the recorder."""

import os
import shutil

# Rough sustained write rates (MB/s) used to estimate how long the free space lasts.
RATE_MB_S = {"webcam": 2.5, "screen": 3.5, "mic": 0.2}
LOW_SPACE_MB = 500
STOP_SPACE_MB = 120

MIC_BLOCKED = ("Microphone access is blocked. Open Windows Settings > Privacy & security > Microphone "
               "and allow desktop apps to use it.")
CAMERA_BLOCKED = ("Camera access is blocked. Open Windows Settings > Privacy & security > Camera "
                  "and allow desktop apps to use it.")


def free_mb(folder):
    probe = folder
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe or ".").free / (1024 * 1024)
    except OSError:
        return float("inf")


def minutes_left(folder, kinds):
    rate = sum(RATE_MB_S.get(k, 2.0) for k in kinds) or 1.0
    return free_mb(folder) / rate / 60.0


def low_space_message(folder, kinds):
    """A warning when the disk would fill soon, else None."""
    free = free_mb(folder)
    if free == float("inf"):
        return None
    left = minutes_left(folder, kinds)
    if free < LOW_SPACE_MB or left < 10:
        return f"Low disk space: about {max(0, int(left))} min of recording left."
    return None


def friendly_error(kind, text):
    """Map a Qt/backend error string to one short sentence."""
    low = (text or "").lower()
    name = {"webcam": "Camera", "screen": "Screen capture", "mic": "Microphone"}.get(kind, "Recording")
    if "permission" in low or "access is denied" in low or "denied" in low:
        return MIC_BLOCKED if kind == "mic" else CAMERA_BLOCKED if kind == "webcam" else f"{name} access was denied."
    if "space" in low or "disk" in low or "write" in low:
        return "Disk full - recording stopped."
    if "in use" in low or "busy" in low or "exclusive" in low or "already" in low:
        return f"{name} is in use by another app."
    if "disconnect" in low or "removed" in low or "unplug" in low or "lost" in low:
        return f"{name} was disconnected."
    return f"{name} error: {text}" if text else f"{name} stopped unexpectedly."
