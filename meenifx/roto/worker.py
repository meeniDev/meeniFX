"""Background worker for Roto Brush propagation.

Segmentation must never run on the GUI thread: a full clip is thousands of GrabCut and
optical-flow passes. The worker owns the source decoding, writes each finished mask to the
disk cache as it goes (so a cancel keeps the frames that really finished) and reports
progress, completion and failures through Qt signals.
"""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from .model import DEFAULT_MODEL, DownloadCancelled, download_model, model_path
from .propagate import propagate_seed


class PropagationWorker(QThread):
    """Analyses a clip in the background; one job per run."""

    progressed = pyqtSignal(int, int)      # done, total
    completed = pyqtSignal(list)           # analysed source frames
    failed = pyqtSignal(str)

    def __init__(self, engine, clip, key, seed_frame, seed_matte, frames, settings,
                 cache, mask_refiner=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.clip = clip
        self.key = str(key)
        self.seed_frame = int(seed_frame)
        self.seed_matte = np.asarray(seed_matte, np.float32)
        self.frames = [int(frame) for frame in frames]
        self.settings = dict(settings or {})
        self.cache = cache
        self.mask_refiner = mask_refiner
        self._stop = False
        self.total = len(self.frames)

    def stop(self):
        self._stop = True

    def stopped(self):
        return self._stop

    def _reader(self):
        entry = self.engine.media.load(self.clip.source_path)
        if entry is None or entry.frames is None:
            raise RuntimeError("The clip's source could not be read.")
        frames = entry.frames
        last = max(0, len(frames) - 1)

        def read(source_frame):
            index = max(0, min(last, int(source_frame)))
            return frames[index]
        return read

    def run(self):
        try:
            read = self._reader()
            analysed = []

            def on_mask(frame, matte):
                self.cache.store(self.clip, frame, matte, self.key)
                analysed.append(int(frame))

            def on_progress(done, total):
                self.progressed.emit(int(done), int(total))

            propagate_seed(self.seed_matte, self.seed_frame, self.frames, read,
                           self.settings, on_mask=on_mask,
                           should_stop=self.stopped, on_progress=on_progress,
                           mask_refiner=self.mask_refiner)
            self.completed.emit(sorted(set(analysed)))
        except Exception as error:                     # noqa: BLE001 - reported to the UI
            self.failed.emit(str(error))


class SubjectScanWorker(QThread):
    """Automatic subject analysis for a whole clip (Smart Key).

    Roto Brush needs a painted seed before it can propagate. Smart Key has no strokes, so
    this worker segments the seed frame itself and then carries that matte outward through
    every source frame, re-running the model on each one. Finished masks go straight to the
    shared cache, so cancelling keeps the frames that really finished.
    """

    progressed = pyqtSignal(int, int)
    completed = pyqtSignal(list)
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, engine, clip, key, seed_frame, frames, settings, cache, analyzer,
                 parent=None):
        super().__init__(parent)
        self.engine = engine
        self.clip = clip
        self.key = str(key)
        self.seed_frame = int(seed_frame)
        self.frames = [int(frame) for frame in frames]
        self.settings = dict(settings or {})
        self.cache = cache
        self.analyzer = analyzer
        self._stop = False
        self.total = len(self.frames) + 1

    def stop(self):
        self._stop = True

    def stopped(self):
        return self._stop

    def _reader(self):
        entry = self.engine.media.load(self.clip.source_path)
        if entry is None or entry.frames is None:
            raise RuntimeError("The clip's source could not be read.")
        frames = entry.frames
        last = max(0, len(frames) - 1)

        def read(source_frame):
            index = max(0, min(last, int(source_frame)))
            return frames[index]
        return read

    def run(self):
        try:
            if self.analyzer is None:
                raise RuntimeError("The subject segmenter is not available.")
            read = self._reader()
            seed = self.analyzer.segment_frame(read(self.seed_frame), self.seed_frame, None)
            if seed is None:
                raise RuntimeError("The AI model found no subject, or it is not installed.")
            self.cache.store(self.clip, self.seed_frame, seed, self.key)
            analysed = [self.seed_frame]
            self.progressed.emit(1, self.total)
            if self._stop:
                self.cancelled.emit()
                return

            def on_mask(frame, matte):
                self.cache.store(self.clip, int(frame), matte, self.key)
                analysed.append(int(frame))

            def refine(source_frame, prior, rgb):
                return self.analyzer.segment_frame(rgb, int(source_frame), prior)

            propagate_seed(
                seed, self.seed_frame, self.frames, read, self.settings,
                on_mask=on_mask, should_stop=self.stopped,
                on_progress=lambda done, _total: self.progressed.emit(done + 1, self.total),
                mask_refiner=refine)
            if self._stop:
                self.cancelled.emit()
            else:
                # The last propagation callback can race the thread finishing. Deliver a
                # definite final count so the card never freezes one frame short.
                self.progressed.emit(self.total, self.total)
                self.completed.emit(sorted(set(analysed)))
        except Exception as error:                     # noqa: BLE001 - reported to the UI
            self.failed.emit(str(error))


class ModelDownloadWorker(QThread):
    """Download the optional segmentation weights without blocking the editor."""

    progressed = pyqtSignal(int, int)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = False

    def stop(self):
        self._stop = True

    def stopped(self):
        return self._stop

    def run(self):
        try:
            path = download_model(DEFAULT_MODEL,
                                  on_progress=lambda done, total:
                                  self.progressed.emit(int(done), int(total)),
                                  should_stop=self.stopped)
            self.completed.emit(path)
        except DownloadCancelled:
            self.cancelled.emit()
        except Exception as error:                     # noqa: BLE001 - reported to the UI
            self.failed.emit(str(error))
