"""Real playback measurements: displayed FPS, dropped frames, render time, cache use.

Everything here is counted from what actually happened - a frame shown in the preview,
a tick that advanced the playhead without producing a new picture, a render that took
measurably longer than a frame interval. The overlay just prints these numbers.
"""

from __future__ import annotations

import time
from collections import deque

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter

from .. import tokens


class PlaybackMetrics:
    """Counters for the performance overlay (pure Python, no Qt widgets)."""

    WINDOW = 2.0      # seconds of history for the FPS estimate

    def __init__(self):
        self.reset()

    def reset(self):
        self.frames_shown = 0
        self.frames_dropped = 0
        self.ticks = 0
        self.render_ms = 0.0          # last interactive render
        self.render_ms_ema = 0.0
        self._shown_times = deque(maxlen=240)
        self._play_started = None

    # ---------------------------------------------------------------- samples
    def note_tick(self, frame):
        """One playback tick happened (the clock asked for `frame`)."""
        self.ticks += 1
        self._last_tick_frame = frame

    def note_drop(self, count=1):
        self.frames_dropped += max(0, int(count))

    def note_shown(self, clock=None):
        self.frames_shown += 1
        now = time.perf_counter() if clock is None else clock
        self._shown_times.append(now)

    def note_render(self, milliseconds):
        self.render_ms = float(milliseconds)
        alpha = 0.2
        self.render_ms_ema = (float(milliseconds) if not self.render_ms_ema
                              else (1 - alpha) * self.render_ms_ema + alpha * milliseconds)

    # ------------------------------------------------------------------ readout
    def fps(self):
        """Displayed frames per second over the recent window (0 when idle)."""
        if len(self._shown_times) < 2:
            return 0.0
        span = self._shown_times[-1] - self._shown_times[0]
        if span <= 0.001:
            return 0.0
        return (len(self._shown_times) - 1) / span

    def snapshot(self, *, target_fps=30, scale=1.0, cache=None, backend=None,
                 renderer=None, resolution=(0, 0)):
        data = {
            "fps": self.fps(),
            "target_fps": float(target_fps),
            "dropped": self.frames_dropped,
            "shown": self.frames_shown,
            "render_ms": self.render_ms,
            "render_ms_ema": self.render_ms_ema,
            "scale": float(scale),
            "resolution": tuple(resolution),
            "backend": backend.summary() if backend is not None else "n/a",
        }
        if cache is not None:
            stats = cache.stats()
            data["cache_frames"] = stats["ram_frames"] + stats["disk_frames"]
            data["cache_mb"] = stats["ram_bytes"] / (1024 * 1024)
            data["cache_hit_rate"] = stats["hit_rate"]
            data["cache_pending"] = stats["pending"]
        if renderer is not None:
            rendered = renderer.stats()
            data["queue"] = rendered["queued"]
            data["render_avg_ms"] = rendered["average_ms"]
        return data


class PerfOverlay:
    """Paints the numbers on top of the preview picture (no separate window)."""

    def __init__(self, provider):
        self.provider = provider      # callable -> dict from PlaybackMetrics.snapshot
        self.visible = False

    def paint(self, painter: QPainter, rect: QRectF):
        if not self.visible:
            return
        data = self.provider() or {}
        lines = [
            f"Preview {data.get('fps', 0.0):4.1f} fps  /  target {data.get('target_fps', 0):.0f}"
            f"  dropped {data.get('dropped', 0)}",
            f"Render {data.get('render_ms', 0.0):5.1f} ms  (avg {data.get('render_avg_ms', 0.0):5.1f})"
            f"  scale {data.get('scale', 1.0) * 100:.0f}%",
            f"Cache {data.get('cache_frames', 0)} frames  {data.get('cache_mb', 0.0):.0f} MB"
            f"  hit {data.get('cache_hit_rate', 0.0) * 100:.0f}%",
            f"{data.get('backend', 'CPU')}   {data.get('resolution', (0, 0))[0]}"
            f"x{data.get('resolution', (0, 0))[1]}",
        ]
        painter.save()
        painter.setFont(QFont("Consolas", 8))
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(line) for line in lines) + 16
        height = metrics.height() * len(lines) + 10
        box = QRectF(rect.left() + 8, rect.top() + 8, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(8, 11, 16, 190))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor(tokens.COLORS.get("text", "#e6edf7")))
        y = box.top() + 4 + metrics.ascent()
        for line in lines:
            painter.drawText(QRectF(box.left() + 8, y - metrics.ascent(),
                                    box.width() - 12, metrics.height()),
                             int(Qt.AlignmentFlag.AlignLeft), line)
            y += metrics.height()
        painter.restore()
