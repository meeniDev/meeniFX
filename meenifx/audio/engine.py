"""Playback engine: a pull-mode QAudioSink fed by the pure mixer.

The audio callback owns the master clock (``clock_seconds``); the UI follows it so
picture and sound cannot drift. Segments are swapped atomically from the UI thread,
which is how mute/solo/volume edits take effect mid-playback without a restart.

Two clock quantities matter:

* ``_origin``  - timeline seconds of the first *rendered* frame of this sink session.
* ``_rendered``- frames handed to the sink so far.

The render position is ``_origin + _rendered / rate`` (ahead of the audible position
by the sink buffer), while ``clock_seconds()`` reports ``_origin + processed`` where
``processed`` comes from QAudioSink.processedUSecs(). Both stay consistent, which is
what lets a scrub seek jump the playhead without tearing the sink down.
"""

import time

import numpy as np
from PyQt6.QtCore import QIODevice, QObject, pyqtSignal
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from .mix import peak_of, render, to_pcm16


class _PullStream(QIODevice):
    """Sequential device whose readData is invoked on Qt's audio thread."""

    def __init__(self, engine):
        super().__init__()
        self._engine = engine

    def isSequential(self):
        return True

    def bytesAvailable(self):
        return 1 << 18

    def readData(self, maxlen):
        return self._engine._pull(int(maxlen))

    def writeData(self, _data):
        return 0


class AudioEngine(QObject):
    """Mixes all audible clips under the playhead and outputs them through QAudioSink."""

    level_changed = pyqtSignal(float, float)     # rms, peak
    playing_changed = pyqtSignal(bool)
    failed = pyqtSignal(str)
    BUFFER_SECONDS = 0.06

    def __init__(self, parent=None, sample_rate=48000, channels=2):
        super().__init__(parent)
        self.rate = int(sample_rate)
        self.channels = int(channels)
        self._device = QMediaDevices.defaultAudioOutput()
        self._format = self._build_format()
        self._stream = _PullStream(self)
        self._sink = None
        self._segments = ()
        self._master = 1.0
        self._playing = False
        self._origin = 0.0
        self._rendered = 0
        self._paused_at = 0.0
        self._buffer_seconds = self.BUFFER_SECONDS
        self._last_level_emit = 0.0
        self._error = "" if self.available else "No audio output device is available."

    # ----------------------------------------------------------------- format
    def _build_format(self):
        fmt = QAudioFormat()
        fmt.setChannelCount(self.channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        preferred = self._device.preferredFormat()
        rate = preferred.sampleRate() if preferred.sampleRate() > 0 else self.rate
        self.rate = int(rate)
        fmt.setSampleRate(self.rate)
        if self._device.isNull() or not self._device.isFormatSupported(fmt):
            fmt.setSampleRate(48000)
            self.rate = 48000
        return fmt

    @property
    def available(self):
        return not self._device.isNull() and self._format.isValid()

    @property
    def error_message(self):
        """Human readable reason when audio cannot play (empty when it can)."""
        return self._error

    def device_name(self):
        return "" if self._device.isNull() else self._device.description()

    # ------------------------------------------------------------------ state
    @property
    def playing(self):
        return self._playing

    def set_segments(self, segments):
        """Replace the audible snapshot (called from the UI thread)."""
        self._segments = tuple(segments or ())

    def set_master_volume(self, value):
        self._master = max(0.0, min(1.5, float(value)))

    @property
    def master_volume(self):
        return self._master

    def set_buffer_seconds(self, seconds):
        """Smaller buffers make scrubbing snappier but risk dropouts."""
        self._buffer_seconds = max(0.02, min(0.3, float(seconds)))

    def clock_seconds(self):
        """Audible timeline position in seconds (master clock for the video timer)."""
        if not self._playing or self._sink is None:
            return self._paused_at
        processed = self._sink.processedUSecs() / 1_000_000.0
        return self._origin + max(0.0, processed)

    def buffered_seconds(self):
        if not self._playing:
            return 0.0
        return max(0.0, self._origin + self._rendered / float(self.rate) - self.clock_seconds())

    # --------------------------------------------------------------- playback
    def play(self, start_seconds):
        if not self.available:
            self._error = "No audio output device is available."
            self.failed.emit(self._error)
            return False
        self.pause()
        self._origin = max(0.0, float(start_seconds))
        self._paused_at = self._origin
        self._rendered = 0
        self._sink = QAudioSink(self._device, self._format, self)
        frames = max(256, int(self.rate * self._buffer_seconds))
        self._sink.setBufferSize(frames * self.channels * 2)
        self._stream.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink.start(self._stream)
        error = self._sink.error()
        if error != QAudio.Error.NoError or self._sink.state() == QAudio.State.StoppedState:
            self._error = f"Could not start audio output ({self._describe_error(error)})."
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
            self._playing = False
            self.failed.emit(self._error)
            return False
        self._error = ""
        self._playing = True
        self.playing_changed.emit(True)
        return True

    @staticmethod
    def _describe_error(error):
        name = getattr(error, "name", None)
        return name or str(error)

    def pause(self):
        if self._sink is not None:
            position = self.clock_seconds()
            try:
                self._sink.stop()
            except Exception:
                pass
            self._sink.deleteLater()
            self._sink = None
            self._paused_at = position
        if self._playing:
            self._playing = False
            self.playing_changed.emit(False)
            self.level_changed.emit(0.0, 0.0)

    def stop(self):
        self.pause()
        self._paused_at = 0.0
        self._segments = ()

    def seek(self, seconds):
        """Reposition without changing the playing state (restarts the sink)."""
        was_playing = self._playing
        self.pause()
        self._paused_at = max(0.0, float(seconds))
        if was_playing:
            self.play(self._paused_at)

    def seek_live(self, seconds):
        """Reposition a running sink without restarting it (used while scrubbing)."""
        seconds = max(0.0, float(seconds))
        self._paused_at = seconds
        if self._playing and self._sink is not None:
            processed = self._sink.processedUSecs() / 1_000_000.0
            # Shift the rendered origin so the clock jumps to `seconds` while the audio
            # already inside the sink buffer plays out; the next pull starts just after it.
            self._origin = seconds - processed

    #  scrub helpers -----------------------------------------------------------
    def start_snippet(self, seconds):
        """Short audible burst while dragging the playhead."""
        if not self.available or not self._segments:
            return False
        self._buffer_seconds = 0.03
        started = self.play(seconds)
        self._buffer_seconds = self.BUFFER_SECONDS
        return started

    def stop_snippet(self, seconds):
        self.pause()
        self._paused_at = max(0.0, float(seconds))

    # ------------------------------------------------------------ audio thread
    def _pull(self, maxlen):
        frame_bytes = 2 * self.channels
        if not self._playing:
            return b""
        want = max(1, maxlen // frame_bytes)
        start = self._origin + self._rendered / float(self.rate)
        block = render(self._segments, start, want, self.rate, self.channels)
        if self._master != 1.0:
            block = block * float(self._master)
        self._rendered += want
        now = time.monotonic()
        if now - self._last_level_emit > 0.033:
            self._last_level_emit = now
            peak = peak_of(block)
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if block.size else 0.0
            try:
                self.level_changed.emit(rms, peak)
            except RuntimeError:
                pass
        return to_pcm16(block)
