"""Screen recording through ffmpeg, so the mouse pointer is in the picture.

Qt captures the screen with DXGI desktop duplication, which hands over a pointer-free
desktop image, and QScreenCapture has no cursor option at all: a Qt screen recording can
never show the mouse. The picture is therefore recorded by the ffmpeg build the app
already ships - gdigrab draws the pointer itself - while the Qt capture keeps running for
the recorder panel's live preview, so the card still shows the real screen.

The class mirrors the parts of :class:`SourceCapture` the recording session uses
(``configure``/``begin``/``end`` and the ``failed``/``stopped`` signals), which keeps the
session, the muxer and the timeline insertion unchanged. Segments are ordinary Matroska
files, so a pause simply closes one file and opens the next.
"""

from __future__ import annotations

import os
import subprocess
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..audio.export_mix import ffmpeg_exe
from .capture import SourceCapture
from .disk import friendly_error

FPS = 30
# How long ffmpeg gets to close the file after being asked to quit before it is killed.
# The recording session waits 6 s for a source to report back, so this must stay below it.
QUIT_TIMEOUT_MS = 4500


class ScreenRecorder(QObject):
    """One screen source: ffmpeg records it, a Qt capture previews it."""

    failed = pyqtSignal(str, str)      # kind, short message
    stopped = pyqtSignal(str)

    kind = "screen"

    def __init__(self, parent=None):
        super().__init__(parent)
        # Preview only: this session never records (no recorder is attached to it here).
        self.preview = SourceCapture("screen", self)
        self.preview.failed.connect(self.failed)
        self.target = None                 # QScreen or QCapturableWindow
        self.mode = "screen"               # screen | window
        self.show_cursor = True
        self.size = (0, 0)                 # physical pixels, screen mode
        self.origin = (0, 0)               # physical top-left of that screen
        self.bitrate_mbps = 0
        self._process = None
        self._path = ""
        self._stopping = False
        self._deadline = 0.0
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._check)

    # ------------------------------------------------------------- selection
    @property
    def device(self):
        return self.preview.device

    def use_screen(self, screen):
        self.release_target()
        self.mode, self.target = "screen", screen
        if screen is not None:
            ratio = screen.devicePixelRatio()
            geometry = screen.geometry()
            # gdigrab works in physical pixels; keep both dimensions even for yuv420p.
            width = int(round(geometry.width() * ratio))
            height = int(round(geometry.height() * ratio))
            self.size = (max(2, width - width % 2), max(2, height - height % 2))
            self.origin = (int(round(geometry.x() * ratio)), int(round(geometry.y() * ratio)))
        self.preview.use_screen(screen)
        return True

    def use_window(self, window):
        self.release_target()
        self.mode, self.target = "window", window
        self.size = (0, 0)
        self.origin = (0, 0)
        self.preview.use_window(window)
        return True

    def release_target(self):
        """Drop the previous screen/window without touching the recording or the preview."""
        self.target = None

    def set_preview(self, widget):
        self.preview.set_preview(widget)

    def start_capture(self):
        self.preview.start_capture()

    def stop_capture(self):
        self._stop_process()
        self.preview.stop_capture()

    def release(self):
        self._stop_process()
        self.preview.release()
        self.target = None

    # ------------------------------------------------------------- recording
    def configure(self, bitrate_mbps=0):
        self.bitrate_mbps = max(0, int(bitrate_mbps or 0))

    def begin(self, path):
        self._path = path
        self._stopping = False
        command = self.command(path)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE, creationflags=flags)
        except OSError as exc:
            self._process = None
            self.failed.emit(self.kind, f"The screen recorder could not start: {exc}")
            return
        self._poll.start()

    def end(self):
        """Ask ffmpeg to quit: it closes the file itself, which is what makes it playable."""
        process = self._process
        if process is None or process.poll() is not None:
            QTimer.singleShot(0, lambda: self.stopped.emit(self.kind))
            return
        self._stopping = True
        self._deadline = time.monotonic() + QUIT_TIMEOUT_MS / 1000.0
        try:
            process.stdin.write(b"q")
            process.stdin.flush()
            process.stdin.close()
        except (OSError, ValueError):
            pass
        self._poll.start()

    def is_recording(self):
        return self._process is not None and self._process.poll() is None

    # ---------------------------------------------------------------- ffmpeg
    def command(self, path):
        width, height = self.size
        command = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
        if self.mode == "window" and self.target is not None:
            title = str(self.target.description() or "")
            command += ["-f", "gdigrab", "-draw_mouse", "1" if self.show_cursor else "0",
                        "-framerate", str(FPS), "-i", f"title={title}"]
        else:
            x, y = getattr(self, "origin", (0, 0))
            size = f"{width}x{height}" if width and height else "1920x1080"
            command += ["-f", "gdigrab", "-draw_mouse", "1" if self.show_cursor else "0",
                        "-framerate", str(FPS), "-offset_x", str(x), "-offset_y", str(y),
                        "-video_size", size, "-i", "desktop"]
        # A screen needs more bits than a camera for the same area: text and UI edges.
        bits = (self.bitrate_mbps * 1_000_000 if self.bitrate_mbps
                else int((width or 1920) * (height or 1080) * FPS * 0.20))
        # Packets are flushed and the encoder does not look ahead, so a crash leaves a
        # playable segment behind instead of an empty file - the same promise the Qt
        # path made by using Matroska.
        command += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                    "-flush_packets", "1", "-pix_fmt", "yuv420p",
                    "-g", str(FPS * 2), "-b:v", str(bits), path]
        return command

    # ------------------------------------------------------------------ state
    def _stop_process(self):
        process, self._process = self._process, None
        self._poll.stop()
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.write(b"q")
            process.stdin.flush()
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    def _check(self):
        process = self._process
        if process is None:
            self._poll.stop()
            return
        if process.poll() is None:
            if self._stopping and time.monotonic() > self._deadline:
                process.kill()
            return
        self._poll.stop()
        self._process = None
        error = ""
        try:
            text = (process.stderr.read() or b"").decode("utf-8", "replace").strip()
            error = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
            process.stderr.close()
        except (OSError, ValueError):
            pass
        written = os.path.isfile(self._path) and os.path.getsize(self._path) > 0
        if written:
            # Also reached when ffmpeg gave up by itself: the segment that exists is kept
            # and the session is told the source is done, exactly like the Qt path.
            self._stopping = False
            self.stopped.emit(self.kind)
            return
        self._stopping = False
        message = friendly_error(self.kind, error) if error else ""
        self.failed.emit(self.kind, message or "The screen recording could not be written.")
