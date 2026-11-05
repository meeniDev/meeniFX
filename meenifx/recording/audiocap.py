"""Microphone and system-sound recording built on the WASAPI backend.

One capture thread per source opens the endpoint, resamples the endpoint's mix format to
the requested rate, applies the source gain and writes 16-bit WAV. The thread never touches
Qt: the GUI polls `level`, and failures arrive through the same `failed`/`stopped` signals
the video sources use, so `RecordingSession` treats all sources the same way.

Timing: WASAPI delivers exactly the frames the audio engine consumed, including silence
while nothing is playing, so the WAV length follows the endpoint clock. The system clock is
what the video timestamps use, so the resampler slowly nudges its ratio to follow wall time:
over a long recording the audio cannot slide out of sync with the picture.
"""

import os
import struct
import threading
import time

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import wasapi

RATES = (48000, 44100, 32000, 22050)
CHANNELS = (2, 1)
MAX_DRIFT = 0.030          # seconds of audio-vs-clock drift tolerated before correcting
LEVEL_FLOOR_DB = 55.0
# A packet whose device position is this far ahead of what we have written means the
# endpoint skipped captured time (loopback devices often produce nothing while the
# speakers are silent). The hole is written as silence so later speech stays where it
# was recorded instead of sliding towards the beginning of the file.
GAP_THRESHOLD = 0.010
# Anything longer than this is a suspend, a clock jump or a broken timestamp, not a gap in
# the capture; padding it would allocate a huge block and shift everything after it.
MAX_GAP = 30.0


def silence_gap(position_seconds, start_seconds, produced_frames, rate,
                threshold=GAP_THRESHOLD, limit=MAX_GAP):
    """Output frames of silence to insert before a packet.

    `position_seconds` is the endpoint's QPC timestamp for the packet and `start_seconds`
    the QPC time the stream was started, both on the same clock as perf_counter. Comparing
    the audio written so far with the device clock keeps leading silence, skipped packets
    and intermittent quiet passages in their real places. Ordinary buffering (a negative
    or tiny difference) produces no padding, and neither does a jump beyond `limit`.
    """
    rate = max(1, int(rate))
    gap = float(position_seconds) - float(start_seconds) - float(produced_frames) / rate
    if gap <= threshold or gap > limit:
        return 0
    return int(round(gap * rate))


def endpoints(loopback=False, refresh=False):
    """Audio endpoints for a picker: microphones, or output devices for system sound."""
    try:
        return wasapi.list_endpoints(loopback, refresh=refresh)
    except Exception:
        return []


def pick_endpoint(loopback, saved_id):
    """The saved endpoint if it is still plugged in, else the Windows default, else first."""
    found = endpoints(loopback, refresh=True)
    for endpoint in found:
        if endpoint.id == saved_id:
            return endpoint
    default_id = wasapi.default_endpoint_id(loopback)
    for endpoint in found:
        if endpoint.id == default_id:
            return endpoint
    return found[0] if found else None


def endpoint_available(endpoint):
    if endpoint is None:
        return False
    return any(item.id == endpoint.id for item in endpoints(endpoint.loopback, refresh=True))


def friendly_failure(kind, message=""):
    name = "Microphone" if kind == "mic" else "System audio"
    detail = f" ({message})" if message and message not in ("", "0x00000000") else ""
    return f"{name} could not be opened{detail}"


class WaveSink:
    """Streaming 16-bit PCM WAV: the header is patched when the file is closed."""

    HEADER = 44

    def __init__(self, path, rate, channels):
        self.path = path
        self.rate, self.channels = int(rate), int(channels)
        self.frames = 0
        self._file = open(path, "wb")
        self._file.write(b"\0" * self.HEADER)

    def write(self, samples):
        """samples: float32 (n, channels) in -1..1."""
        if len(samples) == 0:
            return
        pcm = np.clip(samples, -1.0, 1.0)
        raw = (pcm * 32767.0).astype("<i2", copy=False).tobytes()
        self._file.write(raw)
        self.frames += len(samples)

    def close(self):
        if self._file is None:
            return self.frames
        size = self.frames * self.channels * 2
        header = struct.pack("<4sI4s4sIHHIIHH4sI",
                             b"RIFF", 36 + size, b"WAVE", b"fmt ", 16, 1, self.channels,
                             self.rate, self.rate * self.channels * 2, self.channels * 2, 16,
                             b"data", size)
        self._file.seek(0)
        self._file.write(header)
        self._file.close()
        self._file = None
        return self.frames


class RateConverter:
    """Linear resampling with a carried tail, so block edges never click.

    `step` is how many input samples one output sample consumes. The capture thread nudges
    it by a fraction of a percent towards the wall clock, which cancels clock drift without
    an audible pitch change.
    """

    def __init__(self, in_rate, out_rate, channels):
        self.in_rate, self.out_rate, self.channels = int(in_rate), int(out_rate), int(channels)
        self.nominal = self.in_rate / float(self.out_rate)
        self.step = self.nominal
        self.pos = 0.0
        self.carry = None
        self.prev_len = 0

    def nudge(self, drift, tolerance=MAX_DRIFT):
        """Aim the ratio at the wall clock: `drift` is produced seconds minus real seconds."""
        if abs(drift) <= tolerance:
            target = self.nominal
        else:
            # Positive drift means the audio is ahead of the wall clock, so each output sample
            # has to consume more input samples. Bounded to ±0.3 %: inaudible, but enough to
            # hold a multi-hour recording together.
            target = self.nominal * (1.0 + max(-0.003, min(0.003, drift * 0.5)))
        self.step += (target - self.step) * 0.08

    def push(self, block):
        block = np.asarray(block, dtype=np.float32)
        empty = np.zeros((0, self.channels), dtype=np.float32)
        if self.carry is None:
            data, self.pos = block, 0.0
        else:
            data = np.vstack([self.carry, block])
            # The carried sample is index 0 of the new block, so the read position moves
            # back by everything that came before it.
            self.pos = max(0.0, self.pos - max(0, self.prev_len - 1))
        count = len(data)
        self.prev_len = count
        if count < 2:
            if count:
                self.carry = data[-1:]
            return empty
        positions = np.arange(self.pos, count - 1.0, self.step, dtype=np.float64)
        if positions.size == 0:
            self.carry = data[-1:]
            return empty
        index = positions.astype(np.int64)
        frac = (positions - index).astype(np.float32)[:, None]
        out = data[index] * (1.0 - frac) + data[index + 1] * frac
        self.pos = float(positions[-1] + self.step)
        self.carry = data[-1:]
        return out.astype(np.float32, copy=False)


def fit_channels(block, channels):
    """Match the endpoint's channel count to the recording channel count."""
    have = block.shape[1]
    if have == channels:
        return block
    if channels == 1:
        return block.mean(axis=1, keepdims=True)
    if have == 1:
        return np.repeat(block, channels, axis=1)
    return block[:, :channels]


def level_of(block):
    """RMS in a 0..1 scale that moves on quiet speech (about -55 dB to full scale)."""
    if block.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    if rms <= 1e-6:
        return 0.0
    db = 20.0 * np.log10(rms)
    return float(max(0.0, min(1.0, (db + LEVEL_FLOOR_DB) / LEVEL_FLOOR_DB)))


class _CaptureThread(threading.Thread):
    """Reads one endpoint until asked to stop, writing a WAV when a path is given."""

    def __init__(self, endpoint, path, rate, channels, gain, record=True):
        super().__init__(daemon=True, name=f"audio-{endpoint.id[-8:]}")
        self.endpoint = endpoint
        self.path = path
        self.rate, self.channels = int(rate), int(channels)
        self.gain = float(gain)
        self.record = bool(record) and bool(path)
        self.level = 0.0
        self.error = ""
        self.frames = 0
        self.dropped = 0
        self.padded = 0
        self.device_format = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    def stop(self):
        self._stop.set()

    def wait_ready(self, timeout=3.0):
        return self._ready.wait(timeout)

    def run(self):
        sink = None
        capture = wasapi.WasapiCapture(self.endpoint.id, self.endpoint.loopback)
        try:
            wasapi.com_initialize()
            info = capture.open()
            self.device_format = info
            converter = RateConverter(info.rate, self.rate, self.channels)
            if self.record:
                sink = WaveSink(self.path, self.rate, self.channels)
            capture_started = time.perf_counter_ns() / 1e9
            capture.start()
            self._ready.set()
            started = time.monotonic()
            silence = np.zeros((0, info.channels), dtype=np.float32)
            last_check = started
            while not self._stop.is_set():
                pending = capture.next_packet_frames()
                if pending <= 0:
                    time.sleep(0.004)
                    continue
                block, flags, position = capture.read_packet()
                if flags & wasapi.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                    self.dropped += 1
                # Keep the endpoint's timeline: any captured time the driver did not hand
                # over is written as silence, so the first audible words are not pulled
                # towards the start of the recording.
                gap = silence_gap(position / 10_000_000.0, capture_started,
                                  self.frames, self.rate)
                if gap > 0:
                    pad = converter.push(np.zeros((gap, info.channels), dtype=np.float32))
                    if sink is not None:
                        sink.write(pad)
                    self.frames += len(pad)
                    self.padded += gap
                if len(block) == 0:
                    block = silence
                block = fit_channels(block, self.channels)
                if self.gain != 1.0:
                    block = block * self.gain
                out = converter.push(block)
                if sink is not None:
                    sink.write(out)
                self.frames += len(out)
                self.level = level_of(out)
                now = time.monotonic()
                if now - last_check > 0.25:
                    last_check = now
                    produced = self.frames / float(self.rate)
                    converter.nudge(produced - (now - started))
        except wasapi.WasapiError as exc:
            self.error = str(exc)
        except Exception as exc:                      # a lost device raises all sorts here
            self.error = str(exc) or exc.__class__.__name__
        finally:
            self._ready.set()
            try:
                capture.close()
            except Exception:
                pass
            if sink is not None:
                self.frames = sink.close()
            self.level = 0.0


class AudioCapture(QObject):
    """One audio source with the same surface as a video `SourceCapture`."""

    failed = pyqtSignal(str, str)        # kind, message
    stopped = pyqtSignal(str)            # kind
    levelChanged = pyqtSignal(float)

    def __init__(self, kind, loopback=False, parent=None):
        super().__init__(parent)
        self.kind = kind                  # "mic" | "system"
        self.loopback = bool(loopback)
        self.device = None                # EndpointInfo, so `captures()` can check readiness
        self.gain = 1.0
        self.rate = 48000
        self.channels = 2
        self.error = ""
        self._thread = None
        self._recording = False
        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._publish_level)

    # ------------------------------------------------------------------ setup
    @property
    def label(self):
        return "Microphone" if self.kind == "mic" else "System audio"

    def use_endpoint(self, endpoint):
        self.device = endpoint
        return endpoint is not None

    def set_format(self, rate, channels):
        self.rate = int(rate) if int(rate) in RATES else 48000
        self.channels = int(channels) if int(channels) in CHANNELS else 2

    def set_gain(self, gain):
        self.gain = max(0.0, min(4.0, float(gain)))

    def set_volume(self, volume):
        """Compatibility with the video captures (0..1 volume)."""
        self.set_gain(volume)

    def configure(self, bitrate_mbps=0):
        """Audio has no bitrate setting here; the recorder calls this on every capture."""

    # ------------------------------------------------------------------ levels
    @property
    def level(self):
        return float(self._thread.level) if self._thread is not None else 0.0

    def _publish_level(self):
        if self._thread is not None:
            self.levelChanged.emit(self.level)

    # ------------------------------------------------------------------ capture
    def monitor(self):
        """Open the device for the level meter only; no file is written."""
        self._start(path=None, record=False)

    def begin(self, path):
        self._recording = True
        return self._start(path=path, record=True)

    def end(self):
        self._recording = False
        self._finish(report=True)

    def release(self):
        self._recording = False
        self._finish(report=False)

    def is_recording(self):
        return self._recording and self._thread is not None

    def _start(self, path, record):
        self._finish(report=False)
        if self.device is None:
            self.failed.emit(self.kind, friendly_failure(self.kind, "no device selected"))
            return False
        self.error = ""
        thread = _CaptureThread(self.device, path, self.rate, self.channels, self.gain,
                                record=record)
        self._thread = thread
        thread.start()
        thread.wait_ready()
        if thread.error:
            self._thread = None
            self.failed.emit(self.kind, friendly_failure(self.kind, thread.error))
            return False
        if record and os.path.isdir(os.path.dirname(path) or "."):
            pass
        self._poll.start()
        return True

    def _finish(self, report=False):
        thread, self._thread = self._thread, None
        self._poll.stop()
        if thread is None:
            return
        thread.stop()
        thread.join(timeout=2.0)
        if thread.error:
            self.error = thread.error
            self.failed.emit(self.kind, friendly_failure(self.kind, thread.error))
        if report and thread.record:
            if thread.frames > 0 and not thread.error:
                self.stopped.emit(self.kind)
            else:
                self.failed.emit(self.kind, f"{self.label} recorded no sound.")
        self.levelChanged.emit(0.0)


class AudioMonitor:
    """Thin adapter for the capture objects the recorder panel keeps around."""

    def __init__(self, capture):
        self.capture = capture

    def start(self):
        self.capture.monitor()

    def stop(self):
        if not self.capture.is_recording():
            self.capture.release()

    def set_gain(self, gain):
        self.capture.set_gain(gain)
