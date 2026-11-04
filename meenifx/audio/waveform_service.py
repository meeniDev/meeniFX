"""Waveform service: decodes on demand, computes peaks off-thread and caches them.

The UI only ever calls ``peaks()`` (non-blocking cache read) and ``request()`` (start
work if needed). Results arrive through the ``ready`` signal, so the timeline can draw
a placeholder and repaint when the peaks land.
"""

import copy
import os

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from .library import AudioLibrary
from .waveform import WaveformCache, compute_peaks

from . import processed_waveform


class _PeaksSignals(QObject):
    done = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class _PeaksJob(QRunnable):
    """Compute the multi-resolution peaks for already decoded samples."""

    def __init__(self, path, samples):
        super().__init__()
        self.path = path
        self.samples = samples
        self.signals = _PeaksSignals()

    def run(self):
        try:
            self.signals.done.emit(self.path, compute_peaks(self.samples))
        except Exception as exc:            # pragma: no cover - defensive
            self.signals.failed.emit(self.path, str(exc))


class _ProcessedSignals(QObject):
    done = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)


class _ProcessedJob(QRunnable):
    """Peaks of a clip after its audio chain - the expensive, honest waveform."""

    def __init__(self, key, entries, samples, rate, fps):
        super().__init__()
        self.key = key
        self.entries = entries
        self.samples = samples
        self.rate = rate
        self.fps = fps
        self.signals = _ProcessedSignals()

    def run(self):
        try:
            levels = processed_waveform.build_processed_levels(self.entries, self.samples,
                                                               self.rate, self.fps)
            self.signals.done.emit(self.key, levels)
        except Exception as exc:            # pragma: no cover - defensive
            self.signals.failed.emit(self.key, str(exc))


class WaveformService(QObject):
    """Owns waveform peaks for every source that needs one."""

    ready = pyqtSignal(str)
    failed = pyqtSignal(str, str)
    # (path, signature): the processed envelope for one clip state is ready to draw.
    processed_ready = pyqtSignal(str, str)

    def __init__(self, library=None, cache_dir="", parent=None):
        super().__init__(parent)
        self.library = library or AudioLibrary(parent=self)
        self.cache = WaveformCache(cache_dir) if cache_dir else None
        self._pool = QThreadPool.globalInstance()
        self._computing = set()
        self._missing = set()
        self._processed_jobs = set()
        self.library.loaded.connect(self._on_library_loaded)
        self.library.failed.connect(self._on_library_failed)

    # ------------------------------------------------------------------ lookups
    def peaks(self, path):
        """Cached peaks for a path (``None`` while they are still being computed)."""
        if not path or self.cache is None:
            return None
        return self.cache.get(path)

    def samples(self, path):
        """Decoded audio for a path, requesting a decode when needed (never blocks)."""
        if not path:
            return None
        data = self.library.cached(path)
        if data is None:
            self.request(path)
        return data

    def is_busy(self, path):
        return bool(path) and (path in self._computing or self.library.is_pending(path))

    # ------------------------------------------------------------------- work
    def request(self, path):
        """Ask for peaks for `path`; cheap when they already exist or are pending."""
        if not path or not os.path.isfile(path):
            return
        if self._known_missing(path):
            return
        if self.library.cached(path) is None:
            # Audio must still be decoded for playback even when peaks are on disk:
            # the library then hands them back to us through _on_library_loaded.
            if self.library.is_pending(path):
                return
            self._computing.add(path)
            self.library.request(path)
            return
        if self.cache is None or path in self._computing or self.cache.has(path):
            return
        self._computing.add(path)
        self._compute(path)

    # ---------------------------------------------------- processed waveforms
    def processed(self, path, signature, rate=48000):
        """Cached processed peaks for this exact clip state, or None while computing."""
        return processed_waveform.cached(path, signature, rate)

    def request_processed(self, path, signature, entries, rate=48000, fps=30.0):
        """Ask for processed peaks; cheap when they exist or are already running.

        The work needs decoded samples, so a missing decode is requested first and this
        call is retried by the caller when `processed_ready` says the decode landed.
        """
        if not path:
            return False
        key = processed_waveform.cache_key(path, signature, rate)
        if processed_waveform.cached(path, signature, rate) is not None:
            return True
        if key in self._processed_jobs:
            return False
        samples = self.library.cached(path)
        if samples is None:
            self.request(path)
            return False
        self._processed_jobs.add(key)
        job = _ProcessedJob(key, copy.deepcopy(list(entries or ())), samples, int(rate), float(fps))
        job.signals.done.connect(self._on_processed_done)
        job.signals.failed.connect(self._on_processed_failed)
        self._pool.start(job)
        return False

    def _on_processed_done(self, key, levels):
        self._processed_jobs.discard(key)
        if levels:
            processed_waveform.store(key[0], key[1], key[2], levels)
            self.processed_ready.emit(str(key[0]), str(key[1]))

    def _on_processed_failed(self, key, message):
        self._processed_jobs.discard(key)
        self.failed.emit(str(key[0]), message)

    def invalidate(self, path):
        """Forget a cached result (used after a re-import of the same path)."""
        self._computing.discard(path)
        if self.cache is not None:
            self.cache.forget(path)

    def _known_missing(self, path):
        return path in getattr(self, "_missing", set())

    def _compute(self, path):
        samples = self.library.cached(path)
        if samples is None:
            self._computing.discard(path)
            return
        job = _PeaksJob(path, samples)
        job.signals.done.connect(self._on_done)
        job.signals.failed.connect(self._on_failed)
        self._pool.start(job)

    def _on_done(self, path, levels):
        self._computing.discard(path)
        if levels and self.cache is not None:
            self.cache.put(path, levels)
        self.ready.emit(path)

    def _on_failed(self, path, message):
        self._computing.discard(path)
        self._missing.add(path)
        self.failed.emit(path, message)

    # -------------------------------------------------------- library bridging
    def _on_library_loaded(self, path):
        if path in self._computing:
            self._compute(path)

    def _on_library_failed(self, path, message):
        if path in self._computing:
            self._computing.discard(path)
            self.failed.emit(path, message)
