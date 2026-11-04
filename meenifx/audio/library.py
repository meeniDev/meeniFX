"""Decoded audio store: WAV is decoded on a worker, other formats by Qt's media backend.

Decoded arrays are float32 (frames, 2) at the library sample rate and shared by the
mixer and the waveform generator.
"""

import os
import wave

import numpy as np
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioDecoder, QAudioFormat

from .mix import resample, to_stereo

SAMPLE_RATE = 48000
MAX_CACHED = 12


class _JobSignals(QObject):
    done = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class _WavJob(QRunnable):
    """Read a .wav file with the standard library, off the UI thread."""

    def __init__(self, path, sample_rate):
        super().__init__()
        self.path = path
        self.sample_rate = sample_rate
        self.signals = _JobSignals()

    def run(self):
        try:
            with wave.open(self.path, "rb") as handle:
                channels = handle.getnchannels()
                width = handle.getsampwidth()
                rate = handle.getframerate()
                raw = handle.readframes(handle.getnframes())
            if width == 1:
                data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
            elif width == 2:
                data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            elif width == 3:
                packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
                ints = packed[:, 0] | (packed[:, 1] << 8) | (packed[:, 2] << 16)
                ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
                data = ints.astype(np.float32) / float(1 << 23)
            else:
                data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
            data = data[: (data.size // max(1, channels)) * max(1, channels)].reshape(-1, channels)
            data = to_stereo(resample(data, rate, self.sample_rate))
            self.signals.done.emit(self.path, data)
        except Exception as exc:
            self.signals.failed.emit(self.path, str(exc))


class AudioLibrary(QObject):
    """Decode-and-cache audio per source path; emits loaded/failed."""

    loaded = pyqtSignal(str)
    failed = pyqtSignal(str, str)

    def __init__(self, parent=None, sample_rate=SAMPLE_RATE):
        super().__init__(parent)
        self.sample_rate = int(sample_rate)
        self._cache = {}
        self._order = []
        self._pending = set()
        self._decoders = {}
        self._pool = QThreadPool.globalInstance()

    # ------------------------------------------------------------------ lookups
    def cached(self, path):
        if not path:
            return None
        data = self._cache.get(path)
        if data is not None and path in self._order:
            self._order.remove(path)
            self._order.append(path)
        return data

    def set_cached(self, path, samples):
        """Install samples decoded elsewhere (used when an import already decoded them)."""
        if samples is None:
            return
        self._cache[path] = to_stereo(samples)
        self._order.append(path)
        while len(self._order) > MAX_CACHED:
            stale = self._order.pop(0)
            self._cache.pop(stale, None)
        self._pending.discard(path)
        self.loaded.emit(path)

    def duration_seconds(self, path):
        data = self.cached(path)
        if data is None or data.size == 0:
            return 0.0
        return data.shape[0] / float(self.sample_rate)

    def is_pending(self, path):
        return path in self._pending

    def request(self, path):
        """Return cached audio immediately, otherwise start decoding and return None."""
        if not path or not os.path.isfile(path):
            return None
        cached = self.cached(path)
        if cached is not None:
            return cached
        if path in self._pending:
            return None
        self._pending.add(path)
        if os.path.splitext(path)[1].lower() == ".wav":
            job = _WavJob(path, self.sample_rate)
            job.signals.done.connect(self.set_cached)
            job.signals.failed.connect(self._on_failed)
            self._pool.start(job)
        else:
            self._decode_with_qt(path)
        return None

    # ------------------------------------------------------------- Qt decoding
    def _decode_with_qt(self, path):
        """Decode with QAudioDecoder (mp3/m4a/aac/ogg and audio inside video files).

        Deletion is deferred out of the signal handler: destroying a QAudioDecoder from
        inside its own finished/error signal aborts the process on Windows.
        """
        decoder = QAudioDecoder(self)
        audio_format = QAudioFormat()
        audio_format.setSampleRate(self.sample_rate)
        audio_format.setChannelCount(2)
        # Int16 is the format every backend supports; Float requests come back empty.
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        decoder.setAudioFormat(audio_format)
        decoder.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
        chunks = []
        state = {"finished": False}

        def copy_buffer(buffer):
            """PyQt6 hands out a void pointer, not a buffer object."""
            count = int(buffer.byteCount())
            pointer = buffer.constData()
            try:
                pointer.setsize(count)
                return bytes(pointer)
            except Exception:
                return bytes(memoryview(pointer)[:count])

        def drain():
            while True:
                buffer = decoder.read()
                if not buffer.isValid() or buffer.byteCount() <= 0:
                    return
                chunks.append(copy_buffer(buffer))

        def retire():
            self._decoders.pop(path, None)
            decoder.stop()
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, decoder.deleteLater)

        def on_buffer():
            if state["finished"]:
                return
            try:
                drain()
            except Exception:
                pass

        def on_finished():
            if state["finished"]:
                return
            state["finished"] = True
            try:
                drain()
                if not chunks:
                    self._on_failed(path, "No audio stream found in this file.")
                    return
                raw = b"".join(chunks)
                fmt = decoder.audioFormat()
                sample_format = fmt.sampleFormat()
                if sample_format == QAudioFormat.SampleFormat.Float:
                    data = np.frombuffer(raw, dtype="<f4").astype(np.float32)
                elif sample_format == QAudioFormat.SampleFormat.Int16:
                    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                elif sample_format == QAudioFormat.SampleFormat.Int32:
                    data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
                elif sample_format == QAudioFormat.SampleFormat.UInt8:
                    data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
                else:
                    self._on_failed(path, "Unsupported audio sample format.")
                    return
                channels = max(1, fmt.channelCount())
                data = data[: (data.size // channels) * channels].reshape(-1, channels)
                data = to_stereo(resample(data, fmt.sampleRate() or self.sample_rate, self.sample_rate))
                self.set_cached(path, data)
            except Exception as exc:
                self._on_failed(path, str(exc))
            finally:
                retire()

        def on_error():
            if state["finished"]:
                return
            state["finished"] = True
            try:
                error = decoder.error()
                if error != QAudioDecoder.Error.NoError:
                    self._on_failed(path, decoder.errorString() or "Qt could not decode this audio file.")
            finally:
                retire()

        decoder.bufferReady.connect(on_buffer)
        decoder.finished.connect(on_finished)
        decoder.error.connect(on_error)
        self._decoders[path] = decoder
        decoder.start()

    # ------------------------------------------------------------------ errors
    def _on_failed(self, path, message):
        self._pending.discard(path)
        self._decoders.pop(path, None)
        self.failed.emit(path, message)
