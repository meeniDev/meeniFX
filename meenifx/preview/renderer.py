"""The preview render thread.

One dedicated thread owns every preview render. It serves two queues:

* **interactive** - the frame under the playhead / being scrubbed. A newer request
  replaces an older one (latest wins), so a slow effect can never build a backlog.
* **background** - a range queued by "Render Preview"/"Render Selected Region" or the
  idle auto-prefetch. It is paused while the timeline plays and while an interactive
  request is waiting, so prerendering never competes with what the user is watching.

Rendering happens here, never on the GUI thread. Results are cached by the caller's
``on_result`` callback and delivered to the UI through Qt signals (queued automatically
because the receiver lives in the GUI thread).
"""

from __future__ import annotations

import atexit
import threading
import time
from collections import deque

from PyQt6.QtCore import QObject, pyqtSignal


class PreviewRenderer(QObject):
    """Serial render worker with a latest-wins interactive slot and a background queue."""

    frame_ready = pyqtSignal(int, float, object)      # frame, scale, image
    frame_failed = pyqtSignal(int, str)
    prerendered = pyqtSignal(int, float)              # frame, milliseconds
    prerender_done = pyqtSignal(int, int)             # rendered, skipped
    busy_changed = pyqtSignal(bool)

    def __init__(self, render_fn, cache=None, parent=None):
        super().__init__(parent)
        self._render_fn = render_fn
        self._cache = cache
        self._cond = threading.Condition()
        self._thread = None
        self._stop = False
        self._interactive = None          # (frame, scale)
        self._background = deque()        # (frame, scale)
        self._background_enabled = True
        self._background_paused = False
        self._background_throttle = 0.0   # seconds of rest between background frames
        self._rendered_count = 0
        self._skipped_count = 0
        self._last_ms = 0.0
        self._total_ms = 0.0
        self._count = 0
        self._busy = False

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="meeniFX-preview",
                                        daemon=True)
        self._thread.start()
        # Deterministic shutdown: an interpreter exit with a daemon thread still inside a
        # render can abort the process, and losing the session's unsaved work is worse
        # than waiting a moment for the current frame.
        atexit.register(self.stop)

    def stop(self):
        with self._cond:
            self._stop = True
            self._interactive = None
            self._background.clear()
            self._cond.notify_all()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ requests
    def request(self, frame, scale):
        """Ask for one frame now; a pending interactive request is replaced."""
        with self._cond:
            if self._interactive is not None and self._interactive[0] == int(frame):
                return
            self._interactive = (int(frame), float(scale))
            self._cond.notify()
        self._set_busy(True)

    def cancel_interactive(self):
        with self._cond:
            self._interactive = None

    def queue_range(self, frames, scale, replace=True):
        """Queue a prerender run. Returns the number of frames queued."""
        frames = [int(f) for f in frames]
        with self._cond:
            if replace:
                self._background.clear()
            already = {frame for frame, _scale in self._background}
            added = 0
            for frame in frames:
                if frame in already:
                    continue
                self._background.append((frame, float(scale)))
                already.add(frame)
                added += 1
            self._rendered_count = 0
            self._skipped_count = 0
            self._cond.notify()
        self._set_busy(True)
        return added

    def cancel_background(self):
        with self._cond:
            self._background.clear()
            self._cond.notify()
        self._set_busy(False)

    def prepare_for_playback(self):
        """Playback has priority: drop queued work and stop competing for the lock."""
        with self._cond:
            self._background.clear()
            self._background_throttle = 0.02
            self._background_paused = True
            self._cond.notify()
        self._set_busy(False)

    def finish_playback(self):
        with self._cond:
            self._background_paused = False
            self._background_throttle = 0.0
            self._cond.notify()

    def set_background_enabled(self, enabled):
        with self._cond:
            self._background_enabled = bool(enabled)
            if not enabled:
                self._background.clear()
            self._cond.notify()

    @property
    def background_pending(self):
        with self._cond:
            return len(self._background)

    # ------------------------------------------------------------------ worker
    def _set_busy(self, busy):
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)

    def _loop(self):
        while True:
            with self._cond:
                while not self._stop and self._interactive is None and not self._background:
                    self._cond.wait(0.2)
                if self._stop:
                    return
                job = None
                kind = "interactive"
                if self._interactive is not None:
                    job = self._interactive
                    self._interactive = None
                elif (self._background and self._background_enabled
                      and not self._background_paused):
                    job = self._background.popleft()
                    kind = "background"
                elif self._background:
                    # Background work exists but is paused/throttled: wake up later.
                    self._cond.wait(max(0.03, self._background_throttle))
                    continue
            if job is None:
                continue
            frame, scale = job
            self._render_one(frame, scale, kind)
            if self._stop:
                return
            if kind == "background":
                with self._cond:
                    if self._background:
                        self._skipped_count += 0
                        self._cond.wait(self._background_throttle)
                    else:
                        self.prerender_done.emit(self._rendered_count, self._skipped_count)
                        self._set_busy(False)

    def _render_one(self, frame, scale, kind):
        if self._stop:
            return
        started = time.perf_counter()
        try:
            image = self._render_fn(frame, scale)
        except Exception as exc:          # a broken effect must not kill the thread
            if kind == "interactive":
                self.frame_failed.emit(int(frame), str(exc))
                self._set_busy(False)
            else:
                self._skipped_count += 1
            return
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if self._stop:
            # Shutting down: keep the rendered frame out of the GUI thread entirely.
            return
        self._last_ms = elapsed_ms
        self._total_ms += elapsed_ms
        self._count += 1
        if self._cache is not None and image is not None:
            self._cache.put(frame, scale, image, to_disk=(kind == "background"))
        if kind == "interactive":
            self.frame_ready.emit(int(frame), float(scale), image)
            with self._cond:
                pending = self._interactive is not None
            if not pending:
                self._set_busy(False)
        else:
            self._rendered_count += 1
            self.prerendered.emit(int(frame), elapsed_ms)

    # ------------------------------------------------------------------- stats
    def stats(self):
        with self._cond:
            average = (self._total_ms / self._count) if self._count else 0.0
            return {"last_ms": self._last_ms, "average_ms": average,
                    "renders": self._count, "queued": len(self._background),
                    "paused": self._background_paused,
                    "enabled": self._background_enabled}
