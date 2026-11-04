"""Hardware capability detection and a hardware-accelerated MP4 writer.

Nothing here claims acceleration it has not checked: the OpenCL device name comes from
OpenCV, the NVENC/QSV/AMF encoder is verified with a tiny real encode before it is used
for an export, and OpenCV's hardware video *decode* reports what the driver actually
gave us (on many builds that is 0 = software decode, which is then reported as such).

The writer speaks raw BGR24 to the bundled ffmpeg so the rest of the pipeline is
unchanged: frames are still produced by the same compositor, just compressed on the GPU.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading

import numpy as np

_LOCK = threading.Lock()
_CACHE = {"nvenc": None, "ffmpeg": None}
ENCODERS = ("h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf")


def ffmpeg_exe():
    """Path to the ffmpeg binary we ship with (imageio's bundled build), or None."""
    with _LOCK:
        if _CACHE["ffmpeg"] is not None:
            return _CACHE["ffmpeg"] or None
    exe = None
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
    if not exe or not os.path.exists(exe):
        exe = shutil.which("ffmpeg")
    with _LOCK:
        _CACHE["ffmpeg"] = exe or ""
    return exe


def _encoder_list(exe):
    try:
        completed = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True,
                                   text=True, timeout=20)
        return completed.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def nvenc_available(recheck=False):
    """True only when a real NVENC encode succeeds (the encoder can be listed but unusable)."""
    with _LOCK:
        if _CACHE["nvenc"] is not None and not recheck:
            return bool(_CACHE["nvenc"])
    exe = ffmpeg_exe()
    result = False
    if exe:
        listed = _encoder_list(exe)
        result = "h264_nvenc" in listed and _try_encode(exe, "h264_nvenc")
    with _LOCK:
        _CACHE["nvenc"] = result
    return result


def _try_encode(exe, encoder):
    # 320x240: NVENC refuses frames below its minimum supported size (a 128x64 probe made
    # a perfectly capable GPU look unavailable).
    frame = np.zeros((240, 320, 3), np.uint8)
    target = os.path.join(tempfile.mkdtemp(), "probe.mp4")
    command = [exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
               "-pix_fmt", "bgr24", "-s", "320x240", "-r", "10", "-i", "-", "-an",
               "-c:v", encoder, "-pix_fmt", "yuv420p", target]
    try:
        completed = subprocess.run(command, input=frame.tobytes() * 3, capture_output=True,
                                   timeout=25)
        return completed.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(target)
        except OSError:
            pass


def decode_acceleration():
    """OpenCV's own answer for hardware video decoding: the enum value it actually uses."""
    import cv2
    try:
        return int(cv2.VIDEO_ACCELERATION_NONE)
    except AttributeError:
        return 0


def description():
    """One-line summary for Preferences and the overlay."""
    from ..preview.backend import gpu
    parts = [gpu.summary()]
    parts.append("NVENC encode" if nvenc_available() else "software encode")
    return " | ".join(parts)


class FFmpegVideoWriter:
    """Drop-in replacement for ``cv2.VideoWriter`` that encodes on the GPU (NVENC).

    Frames are written as raw BGR24 into ffmpeg's stdin. ``used_hardware`` says whether
    NVENC actually produced the file, so the UI can report the truth after an export.
    """

    def __init__(self, path, width, height, fps, encoder="h264_nvenc", crf=None):
        self.path = path
        self.width = int(width); self.height = int(height)
        self.fps = max(1.0, float(fps))
        self.encoder = encoder
        self.used_hardware = False
        self.error = ""
        self._process = None
        exe = ffmpeg_exe()
        if not exe:
            raise RuntimeError("ffmpeg is not available")
        command = [exe, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "rawvideo", "-pix_fmt", "bgr24",
                   "-s", f"{self.width}x{self.height}", "-r", f"{self.fps:g}", "-i", "-",
                   "-an", "-c:v", encoder]
        if encoder.endswith("nvenc"):
            command += ["-preset", "p5", "-rc", "vbr", "-cq", str(int(crf or 20)), "-b:v", "0"]
        elif encoder.endswith("qsv"):
            command += ["-global_quality", str(int(crf or 20))]
        else:
            command += ["-crf", str(int(crf or 20))]
        command += ["-pix_fmt", "yuv420p", path]
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.used_hardware = encoder.endswith("nvenc")

    def isOpened(self):
        return self._process is not None and self._process.poll() is None

    def write(self, frame):
        if self._process is None or frame is None:
            return
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            import cv2
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        if frame.ndim == 2:
            import cv2
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        try:
            self._process.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
        except (BrokenPipeError, OSError) as exc:
            self.error = str(exc)

    def release(self):
        if self._process is None:
            return
        try:
            self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self._process.kill()
        finally:
            if self._process.stderr is not None:
                try:
                    message = self._process.stderr.read().decode("utf-8", "replace")
                    if message.strip():
                        self.error = message.strip()[:400]
                except (OSError, ValueError):
                    pass
                self._process.stderr.close()
            self._process = None
