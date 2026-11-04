"""Capture sources and the recording session.

Each source owns its own QMediaCaptureSession + QMediaRecorder, so one failing or unplugged device
never takes the others down. All sources are started in the same event-loop turn and share one
monotonic clock; pause/resume closes the current segment file of every source and opens the next
one together, which keeps them aligned and keeps every finished file valid after a crash.
"""

import os
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtMultimedia import (QCamera, QMediaCaptureSession, QMediaDevices, QMediaFormat,
                                QMediaRecorder, QScreenCapture, QWindowCapture)

from . import devices
from .disk import STOP_SPACE_MB, free_mb, friendly_error

STATE_STOPPED = QMediaRecorder.RecorderState.StoppedState
STATE_RECORDING = QMediaRecorder.RecorderState.RecordingState
EXTENSION = {"webcam": ".mkv", "screen": ".mkv", "mic": ".wav", "system": ".wav"}
LABEL = {"webcam": "Webcam", "screen": "Screen", "mic": "Voice", "system": "System sound"}


def recording_name(kind, stamp, segment=0, segments=1, ext=None):
    """"Webcam 2026-11-02 14-30-05.mkv" (plus " part 2" after a pause)."""
    suffix = f" part {segment + 1}" if segments > 1 or segment else ""
    return f"{LABEL[kind]} {stamp}{suffix}{ext or EXTENSION[kind]}"


@dataclass
class Segment:
    path: str
    offset: float        # seconds of recorded time before this segment
    length: float        # wall-clock seconds this segment ran
    muxed: bool = False  # the sound of this video segment is inside the file
    audio_paths: list = field(default_factory=list)   # the WAVs that went into it


@dataclass
class RecordingResult:
    stamp: str
    folder: str
    start_frame: int
    segments: dict = field(default_factory=dict)   # kind -> [Segment]
    warnings: list = field(default_factory=list)
    voice_over: bool = False


class SourceCapture(QObject):
    """One capture source (webcam, screen or microphone) with its own recorder."""

    failed = pyqtSignal(str, str)      # kind, short message
    stopped = pyqtSignal(str)

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.session = QMediaCaptureSession(self)
        self.recorder = QMediaRecorder(self)
        self.session.setRecorder(self.recorder)
        self.camera = None
        self.screen_capture = None
        self.window_capture = None
        self.audio_input = None
        self.device = None
        self.mode = None
        self.active = False
        self.size = (0, 0)
        self.extension = EXTENSION[kind]
        self.recorder.errorChanged.connect(self._recorder_error)
        # recorderStateChanged cannot be connected from PyQt6 (enum argument), so the state is polled.
        self._last_state = self.recorder.recorderState()
        self._poll = QTimer(self)
        self._poll.setInterval(30)
        self._poll.timeout.connect(self._check_state)
        self._poll.start()

    # -------------------------------------------------------------- setup
    def use_camera(self, device, mode):
        self.release()
        self.device, self.mode = device, mode
        if device is None:
            return False
        self.camera = QCamera(device, self)
        if mode is not None and mode.format is not None:
            self.camera.setCameraFormat(mode.format)
        self.camera.errorChanged.connect(self._camera_error)
        self.session.setCamera(self.camera)
        self.size = (mode.width, mode.height) if mode else (1280, 720)
        return True

    def use_screen(self, screen):
        self.release()
        self.screen_capture = QScreenCapture(self)
        self.screen_capture.setScreen(screen)
        self.screen_capture.errorChanged.connect(self._screen_error)
        self.session.setScreenCapture(self.screen_capture)
        ratio = screen.devicePixelRatio()
        self.size = (int(screen.size().width() * ratio), int(screen.size().height() * ratio))
        self.device = screen
        return True

    def use_window(self, window):
        self.release()
        self.window_capture = QWindowCapture(self)
        self.window_capture.setWindow(window)
        self.window_capture.errorChanged.connect(self._screen_error)
        self.session.setWindowCapture(self.window_capture)
        self.device = window
        self.size = (0, 0)
        return True

    def set_preview(self, widget):
        self.session.setVideoOutput(widget)

    def release(self):
        self.stop_capture()
        self.session.setCamera(None)
        self.session.setScreenCapture(None)
        self.session.setWindowCapture(None)
        for obj in (self.camera, self.screen_capture, self.window_capture):
            if obj is not None:
                obj.deleteLater()
        self.camera = self.screen_capture = self.window_capture = None
        self.device = self.mode = None

    def start_capture(self):
        """Switch the device on (live preview); recording is a separate step."""
        for obj in (self.camera, self.screen_capture, self.window_capture):
            if obj is not None:
                obj.start()
        self.active = True

    def stop_capture(self):
        if self.recorder.recorderState() != STATE_STOPPED:
            self.recorder.stop()
        for obj in (self.camera, self.screen_capture, self.window_capture):
            if obj is not None and obj.isActive():
                obj.stop()
        self.active = False

    # ---------------------------------------------------------- recording
    def configure(self, bitrate_mbps=0):
        fmt = QMediaFormat()
        # Qt's FFmpeg backend cannot mux H.264 into Matroska, and an interrupted mp4 is unreadable
        # (tested), so Matroska + MPEG-4 part 2 is used: crash-safe, but needs a higher bitrate.
        fmt.setFileFormat(QMediaFormat.FileFormat.Matroska)
        fmt.setVideoCodec(QMediaFormat.VideoCodec.MPEG4)
        self.recorder.setMediaFormat(fmt)
        self.recorder.setEncodingMode(QMediaRecorder.EncodingMode.AverageBitRateEncoding)
        fps = self.mode.fps if (self.kind == "webcam" and self.mode) else 30
        self.recorder.setVideoFrameRate(fps)   # constant frame rate even for variable-rate webcams
        w, h = self.size
        if w and h:
            self.recorder.setVideoResolution(w, h)
        if bitrate_mbps > 0:
            bits = bitrate_mbps * 1_000_000
        else:
            per_pixel = 0.20 if self.kind == "screen" else 0.12   # screen text needs more bits
            bits = int((w or 1920) * (h or 1080) * fps * per_pixel)
        self.recorder.setVideoBitRate(bits)
        self.recorder.setQuality(QMediaRecorder.Quality.HighQuality)

    def begin(self, path):
        self.recorder.setOutputLocation(QUrl.fromLocalFile(path))
        self.recorder.record()

    def end(self):
        if self.recorder.recorderState() == STATE_STOPPED:
            QTimer.singleShot(0, lambda: self.stopped.emit(self.kind))
        else:
            self.recorder.stop()

    def is_recording(self):
        return self.recorder.recorderState() == STATE_RECORDING

    # ------------------------------------------------------------ signals
    def _check_state(self):
        state = self.recorder.recorderState()
        if state != self._last_state:
            self._last_state = state
            if state == STATE_STOPPED:
                self.stopped.emit(self.kind)

    def _recorder_error(self):
        if self.recorder.error() != QMediaRecorder.Error.NoError:
            self.failed.emit(self.kind, friendly_error(self.kind, self.recorder.errorString()))

    def _camera_error(self):
        if self.camera is not None and self.camera.error() != QCamera.Error.NoError:
            self.failed.emit(self.kind, friendly_error(self.kind, self.camera.errorString()))

    def _screen_error(self):
        obj = self.screen_capture or self.window_capture
        if obj is not None and obj.error() != type(obj.error()).NoError:
            self.failed.emit(self.kind, friendly_error(self.kind, obj.errorString()))


class RecordingSession(QObject):
    """Start/pause/resume/stop a set of SourceCaptures as one recording."""

    elapsed_changed = pyqtSignal(float)
    state_changed = pyqtSignal(str)            # recording | paused | idle
    finished = pyqtSignal(object)              # RecordingResult
    failed = pyqtSignal(str)                   # nothing usable was recorded
    warning = pyqtSignal(str)

    STOP_TIMEOUT_MS = 6000

    def __init__(self, captures, folder, stamp, start_frame, voice_over=False, parent=None):
        super().__init__(parent)
        self.captures = dict(captures)         # kind -> SourceCapture
        self.folder = folder
        self.stamp = stamp
        self.result = RecordingResult(stamp, folder, start_frame, voice_over=voice_over)
        self.state = "idle"
        self._segment_index = 0
        self._segment_started = 0.0
        self._recorded_before = 0.0
        self._pending = set()
        self._ending = None                    # "pause" | "stop" while waiting for the recorders
        self._abort_reason = ""
        self._paths = {}
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)
        self._disk_checked = 0.0
        self._devices_watch = QMediaDevices(self)
        self._devices_watch.videoInputsChanged.connect(self._devices_changed)
        self._devices_watch.audioInputsChanged.connect(self._devices_changed)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._finish_ending)
        for kind, cap in self.captures.items():
            cap.failed.connect(self._source_failed)
            cap.stopped.connect(self._source_stopped)

    # ----------------------------------------------------------- public
    @property
    def elapsed(self):
        if self.state == "recording":
            return self._recorded_before + (time.monotonic() - self._segment_started)
        return self._recorded_before

    def start(self, bitrate_mbps=0):
        os.makedirs(self.folder, exist_ok=True)
        for cap in self.captures.values():
            cap.configure(bitrate_mbps)
        self._begin_segment()
        self._timer.start()
        self._set_state("recording")

    def pause(self):
        if self.state == "recording" and self._ending is None:
            self._end_segment("pause")

    def resume(self):
        if self.state == "paused":
            self._segment_index += 1
            self._begin_segment()
            self._set_state("recording")

    def toggle_pause(self):
        self.resume() if self.state == "paused" else self.pause()

    def stop(self, reason=""):
        if self.state == "paused":
            self._finish_all()
            return
        if self.state == "recording" and self._ending is None:
            self._abort_reason = reason or self._abort_reason
            self._end_segment("stop")
        elif self._ending == "pause":
            self._ending = "stop"            # stop requested while a pause was settling

    # --------------------------------------------------------- internals
    def _set_state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def _begin_segment(self):
        self._segment_started = time.monotonic()
        for kind, cap in self.captures.items():
            name = recording_name(kind, self.stamp, self._segment_index)
            path = os.path.join(self.folder, name)
            self._paths[kind] = path
        # One loop turn for every source so they share the same start instant.
        for kind, cap in self.captures.items():
            cap.begin(self._paths[kind])

    def _end_segment(self, mode):
        self._ending = mode
        self._segment_wall = time.monotonic() - self._segment_started
        self._pending = set(self.captures)
        for cap in self.captures.values():
            cap.end()
        self._timeout.start(self.STOP_TIMEOUT_MS)

    def _source_stopped(self, kind):
        if self._ending is None:
            return
        self._pending.discard(kind)
        if not self._pending:
            QTimer.singleShot(250, self._finish_ending)    # let the muxers flush the file

    def _finish_ending(self):
        if self._ending is None:
            return
        self._timeout.stop()
        mode, self._ending = self._ending, None
        self._record_segment()
        if mode == "pause":
            self._recorded_before += self._segment_wall
            self._set_state("paused")
        else:
            self._recorded_before += self._segment_wall
            self._finish_all()

    def _record_segment(self):
        for kind, path in self._paths.items():
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                self.result.segments.setdefault(kind, []).append(
                    Segment(path, self._recorded_before, self._segment_wall))
        self._paths = {}

    def _finish_all(self):
        self._timer.stop()
        self._set_state("idle")
        if self._abort_reason:
            self.result.warnings.append(self._abort_reason)
        if not self.result.segments:
            self.failed.emit(self._abort_reason or "Nothing was recorded.")
        else:
            self.finished.emit(self.result)

    def _tick(self):
        self.elapsed_changed.emit(self.elapsed)
        now = time.monotonic()
        if self.state == "recording" and now - self._disk_checked > 3.0:
            self._disk_checked = now
            if free_mb(self.folder) < STOP_SPACE_MB:
                self.stop("Disk full - recording stopped.")

    def _source_failed(self, kind, message):
        if self.state == "recording":
            self.warning.emit(message)
            self.stop(message)

    def _devices_changed(self):
        if self.state != "recording":
            return
        # Audio captures notice a lost endpoint themselves (the read fails), so only the
        # camera list is watched here; a stolen microphone must not stop the picture.
        for kind, cap in self.captures.items():
            if kind != "webcam":
                continue
            device = cap.device
            if device is not None and not devices.find_by_id(devices.cameras(), devices.device_id(device)):
                self._source_failed("webcam", "Camera was disconnected.")
                return
