"""Animation Graph Editor: value and speed curves for the selected clip.

The editor is a view over the same channels the inspector and the preview use
(``model.channels``), so a curve edited here is by definition the curve that renders.
Value mode draws each channel's value over time; speed mode draws the derivative of
that same curve, which is what acceleration and deceleration actually are. Both modes
edit the keyframe's easing handles, so switching the view never changes the animation.

Interactions: click/shift-click to select keyframes, drag a box to select many, drag a
keyframe to retime or revalue it, drag a handle to bend the segment, wheel to zoom,
middle-drag or Ctrl+wheel to pan, Delete to remove, Ctrl+C/Ctrl+V to copy and paste.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath,
                         QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QInputDialog, QLabel,
                             QMenu, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from .. import crashlog, tokens
from ..icons import icon
from ..model import channels as CH
from ..model import easing
from ..model.keyframes import AnimatedValue

RULER_H = 20
AXIS_W = 56          # gutter holding the value labels, so they never sit on the curves
HANDLE_PX = 5.0
HIT = 8.0
SNAP_PX = 7.0


def _axis_text(value, step, unit):
    """An axis label with just enough decimals for the spacing between ticks.

    Two fixed decimals turned every tick into ``625.93px``: unreadable, and wide enough
    to collide with the ruler. The precision follows the tick spacing instead, so a
    175-unit step reads ``700 px`` and a 0.5-unit step still shows its decimals.
    """
    step = abs(float(step))
    decimals = 0 if step >= 10 else 1 if step >= 1 else 2
    text = f"{float(value):,.{decimals}f}"
    return f"{text} {unit}" if unit else text


def curve_icon(points, colour=None, size=26):
    """Tiny easing preview for the preset menus (timeline and Graph Editor)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(colour or tokens.COLORS["accent"]), 1.4)
    painter.setPen(pen)
    path = QPainterPath()
    for index, (x, y) in enumerate(points):
        # y grows downwards on screen; curves may overshoot, so leave headroom.
        point = QPointF(3 + x * (size - 6), size - 3 - y * (size - 6))
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


# Backwards-compatible private alias: older call sites use the underscore name.
_curve_icon = curve_icon


class GraphCanvas(QWidget):
    """Curves, keyframes and handles."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.zoom_x = 4.0          # pixels per frame
        self.zoom_y = 1.0          # multiplier on the auto value range
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag = None
        self._box = None
        self._hover = None
        self._playhead_px = None   # last painted column, so a move can erase the old one
        # Filled by paintEvent; the geometric helpers read it instead of recomputing the
        # value range per point.
        self._paint_low, self._paint_high = 0.0, 1.0

    # ------------------------------------------------------------------ geometry
    def frame_span(self):
        clip = self.panel.clip()
        duration = max(1, clip.duration) if clip is not None else 120
        return duration

    def value_range(self):
        """(low, high) of every visible curve, padded."""
        values = [value for _channel, _time, value in self.panel.sample_points()]
        if not values:
            return 0.0, 1.0
        low, high = min(values), max(values)
        speed = self.panel.mode == "speed"
        if speed:
            values = [point[1] for point in self.panel.speed_points()]
            if values:
                low, high = min(low, min(values)), max(high, max(values))
        if abs(high - low) < 1e-6:
            low, high = low - 1.0, high + 1.0
        pad = (high - low) * 0.12
        mid = (high + low) / 2.0
        half = (high - low) / 2.0 * self.zoom_y + pad
        return mid - half - self.pan_y * 0.01, mid + half - self.pan_y * 0.01

    def x_of(self, frame):
        return AXIS_W + (frame - self.panel.start_frame()) * self.zoom_x - self.pan_x

    def frame_at(self, x):
        return self.panel.start_frame() + (x - AXIS_W + self.pan_x) / max(1e-6, self.zoom_x)

    def y_of(self, value):
        low, high = self.value_range()
        rect = self.curve_rect()
        span = max(1e-6, high - low)
        return rect.bottom() - (value - low) / span * rect.height()

    def value_at(self, y):
        low, high = self.value_range()
        rect = self.curve_rect()
        span = max(1e-6, high - low)
        return low + (rect.bottom() - y) / max(1e-6, rect.height()) * span

    def curve_rect(self):
        """The plotting area: right of the value gutter, below the ruler."""
        return QRectF(AXIS_W, RULER_H, max(1, self.width() - AXIS_W),
                      max(1, self.height() - RULER_H))

    def fit(self, only_selected=False):
        clip = self.panel.clip()
        if clip is None:
            return
        rect = self.curve_rect()
        entries = self.panel.key_entries()
        if only_selected and self.panel.selection:
            entries = [entry for entry in entries if (entry[0], entry[1]) in self.panel.selection]
        if not entries:
            entries = self.panel.key_entries()
        times = [time for _channel, time, _value in entries] or [0, clip.duration]
        span = max(1, max(times) - min(times))
        self.zoom_x = max(0.2, min(80.0, (rect.width() - 40) / span))
        self.pan_x = min(times) * self.zoom_x - 20
        self.zoom_y = 1.0
        self.pan_y = 0.0
        self.update()

    # -------------------------------------------------------------------- painting
    # Every Qt override below is guarded: an exception here is fatal to the process
    # (PyQt6 aborts on it), so a bad frame logs once and skips a repaint instead.
    @crashlog.guard("graph editor")
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(tokens.COLORS["panel_alt"]))
        rect = self.curve_rect()
        clip = self.panel.clip()
        if clip is None:
            painter.setPen(QColor(tokens.COLORS["muted"]))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "No clip selected")
            painter.end()
            return
        # One geometry pass per repaint. `y_of` used to rebuild the value range for every
        # single point of every curve, and that range re-sampled every curve - quadratic
        # work that made one repaint of a real project take tens of seconds.
        visible = list(self.panel.visible_channels())
        samples = {channel: self.sample_channel(channel) for channel in visible}
        low, high = self._range_of(samples, visible)
        self._paint_low, self._paint_high = low, high
        self._paint_grid(painter, rect, low, high)
        painter.save()
        painter.setClipRect(rect)
        for channel in visible:
            colour = self.panel.channel_colour(channel)
            if self.panel.mode == "speed":
                self._paint_speed(painter, samples.get(channel), colour)
            else:
                self._paint_curve(painter, samples.get(channel), colour)
        self._paint_keys(painter, visible)
        if self._box is not None:
            painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(124, 156, 255, 40))
            painter.drawRect(self._box)
        painter.restore()
        self._paint_playhead(painter)
        painter.end()

    def _range_of(self, samples, visible):
        """Padded value window for the curves about to be drawn (speed mode included)."""
        values = []
        for channel in visible:
            frames, series = samples.get(channel, ((), ()))
            values.extend(series)
        if not values:
            return 0.0, 1.0
        low, high = min(values), max(values)
        if self.panel.mode == "speed":
            for channel in visible:
                speeds = self._speed_values(samples.get(channel))
                if speeds:
                    low, high = min(low, min(speeds)), max(high, max(speeds))
        if abs(high - low) < 1e-6:
            low, high = low - 1.0, high + 1.0
        pad = (high - low) * 0.12
        mid = (high + low) / 2.0
        half = (high - low) / 2.0 * self.zoom_y + pad
        return mid - half - self.pan_y * 0.01, mid + half - self.pan_y * 0.01

    def _y(self, value):
        """Value -> pixel, using the range this repaint computed once."""
        low, high = self._paint_low, self._paint_high
        rect = self.curve_rect()
        span = max(1e-6, high - low)
        return rect.bottom() - (value - low) / span * rect.height()

    def _speed_values(self, sample):
        frames, values = sample if sample else ((), ())
        if len(frames) < 2:
            return []
        fps = max(1.0, float(self.panel.engine.timeline.fps))
        return [(values[index] - values[index - 1]) / max(1e-6, frames[index] - frames[index - 1]) * fps
                for index in range(1, len(frames))]

    def _paint_grid(self, painter, rect, low, high):
        painter.setFont(QFont("Segoe UI", 8))
        # Resolve the unit/decimals once: every axis label belongs to the active channel.
        # Passing the channel *id* here used to crash the whole editor, so the id is
        # turned into a metadata row before it reaches the formatter.
        meta = self.panel.channel_meta(self.panel.active_channel())
        unit = meta.get("unit") if isinstance(meta, Mapping) else ""
        # ---- value gutter: its own plate, labels right-aligned against the plot edge
        painter.fillRect(QRectF(0, RULER_H, AXIS_W, self.height() - RULER_H),
                         QColor(tokens.COLORS["panel"]))
        # A short dock still has to read: fewer value lines beat overlapping labels.
        steps = 4 if rect.height() >= 110 else 2 if rect.height() >= 55 else 1
        tick = (high - low) / steps
        for index in range(steps + 1):
            y = rect.top() + rect.height() * index / steps
            painter.setPen(QPen(QColor(47, 59, 77, 120), 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            value = high - tick * index
            label = QRectF(2, min(max(y - 7, RULER_H + 1), self.height() - 16),
                           AXIS_W - 8, 14)
            painter.setPen(QColor(tokens.COLORS["muted"]))
            painter.drawText(label, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             _axis_text(value, tick, unit))
        # ---- time ruler: one band with centred frame labels and tick marks
        painter.fillRect(QRectF(AXIS_W, 0, self.width() - AXIS_W, RULER_H),
                         QColor(tokens.COLORS["panel"]))
        step = max(1, int(self.panel.ruler_step(self.zoom_x)))
        start = int(self.frame_at(AXIS_W) // step) * step
        for frame in range(start, int(self.frame_at(self.width())) + step, step):
            if frame < 0:
                continue
            x = self.x_of(frame)
            painter.setPen(QPen(QColor(47, 59, 77, 90), 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
            painter.drawLine(QPointF(x, RULER_H - 4), QPointF(x, RULER_H))
            painter.setPen(QColor(tokens.COLORS["muted"]))
            painter.drawText(QRectF(x - 26, 1, 52, RULER_H - 6),
                             int(Qt.AlignmentFlag.AlignCenter), str(frame))
        # ---- frame: ruler rule, plot border and the gutter separator
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
        painter.drawLine(QPointF(AXIS_W, RULER_H), QPointF(self.width(), RULER_H))
        painter.drawRect(rect)
        painter.drawLine(QPointF(AXIS_W, RULER_H), QPointF(AXIS_W, self.height()))

    def _clip_local(self, frame):
        return frame - self.panel.start_frame()

    def sample_channel(self, channel, max_points=2400):
        """(frames, values) of a channel, sampled densely enough to look smooth.

        The old painter sampled once per frame and drew straight lines between those
        points. At 4 px/frame that is invisible, but as soon as the user zoomed in the
        polygon corners showed up and a smooth curve looked segmented. Sampling every
        half pixel of screen space (capped, so a huge zoom cannot explode the point
        count) and letting the painter antialias gives a genuinely smooth curve - and it
        is the *real* motion: every sample is `CH.value_at`, the same call the preview
        and the exporter use.
        """
        clip = self.panel.clip()
        if clip is None:
            return [], []
        duration = max(1, int(clip.duration))
        # Only the frames that can appear on screen are sampled, at most one point per
        # pixel: sampling the whole clip and then drawing it off-view was wasted work on
        # long clips, and a fixed count made a wide canvas coarse and a narrow one slow.
        left = self.curve_rect().left()
        first = max(0.0, min(float(duration), self.frame_at(left)))
        last = max(0.0, min(float(duration), self.frame_at(max(left + 1, self.width()))))
        if last <= first:
            first, last = 0.0, float(duration)
        budget = max(2, min(int(max_points), max(2, self.width())))
        count = int(max(2, min(budget, (last - first) * max(1e-6, float(self.zoom_x)) / 0.5 + 2)))
        step = (last - first) / float(count - 1)
        frames, values = [], []
        for index in range(count):
            frame = first + index * step
            value = CH.numeric_value_at(clip, channel, frame)
            if value is None:
                # A channel that turns non-numeric mid-curve cannot be drawn; drop the
                # whole curve rather than half a path.
                return [], []
            frames.append(frame)
            values.append(value)
        return frames, values

    def _curve_polygon(self, sample):
        """Screen positions of one sampled curve, in a single pass.

        A polyline instead of a QPainterPath of thousands of ``lineTo`` calls: same
        antialiased result, a fraction of the cost, and no per-point range lookup.
        """
        frames, values = sample if sample else ((), ())
        if len(frames) < 2:
            return None
        start = self.panel.start_frame()
        return QPolygonF([QPointF(self.x_of(start + frame), self._y(value))
                          for frame, value in zip(frames, values)])

    def _paint_curve(self, painter, sample, colour):
        polygon = self._curve_polygon(sample)
        if polygon is None:
            return
        painter.setPen(QPen(colour, 1.8))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(polygon)

    def _paint_speed(self, painter, sample, colour):
        """Velocity of the same curve, in channel units per second."""
        frames, values = sample if sample else ((), ())
        if len(frames) < 2:
            return
        fps = max(1.0, float(self.panel.engine.timeline.fps))
        start = self.panel.start_frame()
        points = []
        for index in range(1, len(frames)):
            speed = (values[index] - values[index - 1]) / max(1e-6, frames[index] - frames[index - 1]) * fps
            points.append(QPointF(self.x_of(start + frames[index]), self._y(speed)))
        painter.setPen(QPen(colour, 1.6, Qt.PenStyle.DashLine))
        painter.drawPolyline(QPolygonF(points))

    def _paint_keys(self, painter, visible=None):
        clip = self.panel.clip()
        if clip is None:
            return
        visible = set(visible if visible is not None else self.panel.visible_channels())
        for channel, time, value in self.panel.key_entries():
            if channel not in visible:
                continue
            colour = self.panel.channel_colour(channel)
            selected = (channel, time) in self.panel.selection
            point = QPointF(self.x_of(self.panel.start_frame() + time), self._y(value))
            painter.setPen(QPen(QColor("#ffffff") if selected else colour, 1.2))
            painter.setBrush(QBrush(QColor(tokens.COLORS["accent"] if selected else tokens.COLORS["panel"])))
            painter.drawPolygon(QPolygonF([QPointF(point.x(), point.y() - 4.5),
                                          QPointF(point.x() + 4.5, point.y()),
                                          QPointF(point.x(), point.y() + 4.5),
                                          QPointF(point.x() - 4.5, point.y())]))
            if selected:
                # Every selected Bezier keyframe shows its own handles, whichever
                # channel it belongs to - that is what makes them editable in a
                # multi-channel selection.
                self._paint_handles(painter, channel, time, point)

    def _paint_handles(self, painter, channel, time, point):
        clip = self.panel.clip()
        stored = CH.stored(clip, channel)
        if not isinstance(stored, AnimatedValue):
            return
        keys = stored.keyframes
        index = next((i for i, key in enumerate(keys) if key.time == time), None)
        if index is None:
            return
        painter.setPen(QPen(QColor(tokens.COLORS["muted"]), 1.0))
        # Outgoing handle (this key to the next one) and incoming (previous to this).
        if index + 1 < len(keys):
            target = self._handle_point(keys[index], keys[index + 1], "out")
            if target is not None:
                painter.drawLine(point, target)
                painter.setBrush(QBrush(QColor(tokens.COLORS["muted"])))
                painter.drawRect(QRectF(target.x() - 2.5, target.y() - 2.5, 5, 5))
        if index > 0:
            target = self._handle_point(keys[index - 1], keys[index], "in")
            if target is not None:
                painter.drawLine(point, target)
                painter.setBrush(QBrush(QColor(tokens.COLORS["muted"])))
                painter.drawEllipse(target, 2.6, 2.6)

    def _handle_point(self, left, right, side):
        """Widget position of one easing handle of a segment."""
        x1, y1, x2, y2 = easing.normalized_handles(left, right)
        if side == "out":
            nx, ny = x1, y1
        else:
            nx, ny = x2, y2
        dt = max(1e-6, right.time - left.time)
        dv = float(right.value) - float(left.value)
        frame = left.time + nx * dt
        value = float(left.value) + ny * dv
        return QPointF(self.x_of(self.panel.start_frame() + frame), self._y(value))

    def _paint_playhead(self, painter):
        """Red line through the curves, with a head that is never cut off by an edge.

        The head used to be centred on the true x, so at the first frame (and while
        panned past the left edge) half of it fell outside the canvas and the plot
        border drew straight through it. The line keeps the exact frame; only the head
        is nudged inside, and it is drawn on top of the border.
        """
        frame = self.panel.session.playhead
        x = self.x_of(frame)
        rect = self.curve_rect()
        colour = QColor(tokens.COLORS["red"])
        if rect.left() <= x <= rect.right():
            painter.setPen(QPen(colour, 1.2))
            painter.drawLine(QPointF(x, RULER_H + 1), QPointF(x, self.height()))
        head_x = min(max(x, rect.left() + 5), rect.right() - 5)
        painter.setPen(QPen(colour, 1.0))
        painter.setBrush(colour)
        painter.drawPolygon(QPolygonF([QPointF(head_x - 5, RULER_H - 9),
                                       QPointF(head_x + 5, RULER_H - 9),
                                       QPointF(head_x, RULER_H + 1)]))
        self._playhead_px = int(round(x))

    def playhead_moved(self):
        """Repaint just the columns the playhead left and entered.

        Playback advances the playhead every tick, so this has to be cheap: a full repaint
        of a five-channel graph costs tens of milliseconds, while these two narrow bands
        (plus the ruler strip that holds the head) are a fraction of that.
        """
        x = int(round(self.x_of(self.panel.session.playhead)))
        old = self._playhead_px
        self._playhead_px = x
        if old is None or old == x:
            self.update()
            return
        band = QRect()
        for column in (old, x):
            band = band.united(QRect(column - 6, 0, 13, self.height()))
        self.update(band)

    # ------------------------------------------------------------------ interaction
    @crashlog.guard("graph editor")
    def mousePressEvent(self, event):
        clip = self.panel.clip()
        if clip is None:
            return
        pos = event.position()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._drag = {"kind": "pan", "origin": pos, "pan": (self.pan_x, self.pan_y)}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        if pos.y() <= RULER_H:
            # The ruler is the playhead's track: pressing jumps there, dragging scrubs.
            self._drag = {"kind": "scrub"}
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self._scrub_to(pos.x())
            return
        handle = self._handle_at(pos)
        if handle is not None:
            channel, time, side = handle
            self.panel.session.begin_gesture()
            self._drag = {"kind": "handle", "channel": channel, "time": time, "side": side}
            return
        hit = self._key_at(pos)
        modifiers = event.modifiers()
        additive = bool(modifiers & (Qt.KeyboardModifier.ShiftModifier
                                     | Qt.KeyboardModifier.ControlModifier))
        if hit is not None:
            channel, time = hit
            entry = (channel, time)
            if additive and entry in self.panel.selection:
                # Ctrl/Shift-click toggles: clicking a chosen key again drops it.
                self.panel.select_keys(set(self.panel.selection) - {entry})
                self.update()
                return
            if additive:
                self.panel.select_keys({entry}, additive=True)
            elif entry not in self.panel.selection:
                self.panel.select_keys({entry})
            values = {}
            for ch, t in self.panel.selection:
                value = CH.numeric_value_at(clip, ch, t)
                if value is not None:
                    values[(ch, t)] = value
            # The channels are snapshotted, so every mouse move re-applies the whole drag
            # from the keys as they were when the button went down. Moving them in place
            # made the second move look for keys that had already left their old frame.
            records = {}
            for channel, _time in values:
                if channel not in records:
                    records[channel] = [tuple(row) for row in CH.key_records(clip, channel)]
            self._drag = {"kind": "keys", "origin": pos, "clip": clip.id,
                          "keys": [(ch, t) for ch, t in self.panel.selection if (ch, t) in values],
                          "base": set(self.panel.selection),
                          "values": values, "records": records}
            self.panel.session.begin_gesture()
            self.update()
            return
        playhead_x = self.x_of(self.panel.session.playhead)
        if abs(pos.x() - playhead_x) <= 4.0:
            # Grabbing the red line itself scrubs, the way the timeline's playhead does.
            self._drag = {"kind": "scrub"}
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
        # Empty space: a plain click deselects and moves the playhead, a drag draws a
        # selection box. Both gestures start here so a click never becomes a box.
        self._drag = {"kind": "empty", "origin": pos, "modifiers": modifiers,
                      "moved": False}
        self.panel.select_keys(set())
        self.update()

    @crashlog.guard("graph editor")
    def mouseMoveEvent(self, event):
        pos = event.position()
        drag = self._drag
        if drag is None:
            handle = self._handle_at(pos)
            if pos.y() <= RULER_H:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor if handle else Qt.CursorShape.ArrowCursor)
            self._hover = handle
            return
        kind = drag["kind"]
        if kind == "pan":
            self.pan_x = drag["pan"][0] - (pos.x() - drag["origin"].x())
            self.pan_y = drag["pan"][1] + (pos.y() - drag["origin"].y())
            self.update()
            return
        if kind == "scrub":
            self._scrub_to(pos.x())
            self.update()
            return
        if kind == "box":
            self._box = QRectF(drag["origin"], pos).normalized()
            self.update()
            return
        if kind == "empty":
            if not drag["moved"] and (pos - drag["origin"]).manhattanLength() > 3:
                drag["moved"] = True
            if drag["moved"]:
                self._box = QRectF(drag["origin"], pos).normalized()
                self.update()
            return
        if kind == "keys":
            self._move_keys(drag, pos)
            return
        if kind == "handle":
            self._move_handle(drag, pos)
            return

    @crashlog.guard("graph editor")
    def mouseReleaseEvent(self, event):
        drag = self._drag
        self._drag = None
        if drag is None:
            return
        if drag["kind"] in ("pan", "scrub"):
            self.unsetCursor()
        if drag["kind"] == "box" and self._box is not None:
            self._select_in_box(self._box)
            self._box = None
            self.update()
            return
        if drag["kind"] == "empty":
            if self._box is not None and drag.get("moved"):
                self._select_in_box(self._box)
                self._box = None
            else:
                self._box = None
                self.panel.session.set_playhead(
                    max(0, int(round(self.frame_at(event.position().x())))))
            self.update()
            return
        if drag["kind"] in ("keys", "handle"):
            label = "Move keyframe" if drag["kind"] == "keys" else "Edit easing curve"
            self.panel.session.commit_gesture(label)
            self.panel.refresh_preview()
        self.update()

    def _move_keys(self, drag, pos):
        """Move the selected keys in time and value as one coherent group.

        Every move is rebuilt from the snapshot taken when the drag started, so dragging
        is continuous: the keys follow the pointer instead of jumping once and stalling.
        The whole group is written per channel afterwards, so a move can never land on an
        order that loses a key to a collision; keys sharing a frame collapse into one
        (CH.replace_keys deduplicates), and the time snaps to the playhead when it comes
        within a few pixels.
        """
        clip = self.panel.clip()
        if clip is None or clip.id != drag.get("clip"):
            return
        frame_delta = (pos.x() - drag["origin"].x()) / max(1e-6, self.zoom_x)
        value_delta = self.value_at(pos.y()) - self.value_at(drag["origin"].y())
        playhead_local = self.panel.session.playhead - self.panel.start_frame()
        playhead_x = self.x_of(self.panel.session.playhead)
        limit = max(0, int(clip.duration) - 1)
        moved = {}
        for channel, records in drag.get("records", {}).items():
            rows = []
            for row in records:
                # Never name this `time`: it shadowed the module this method needs below.
                key_time = int(row[0])
                if (channel, key_time) not in drag["values"]:
                    rows.append(tuple(row))
                    continue
                target = max(0, min(limit, int(round(key_time + frame_delta))))
                if abs(self.x_of(self.panel.start_frame() + target) - playhead_x) <= SNAP_PX:
                    target = max(0, min(limit, int(playhead_local)))
                value = row[1]
                start = drag["values"].get((channel, key_time))
                if abs(value_delta) > 1e-9 and start is not None \
                        and isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = start + value_delta
                moved[(channel, key_time)] = target
                rows.append((target, value) + tuple(row[2:]))
            if rows:
                CH.replace_keys(clip, channel, rows)
        if not moved:
            return
        self.panel.selection = {(channel, moved.get((channel, key_time), key_time))
                                for channel, key_time in drag.get("base", self.panel.selection)}
        self.panel.session.set_key_selection([(clip.id, channel, key_time)
                                              for channel, key_time in self.panel.selection])
        # Re-rendering the picture costs far more than the edit; a live drag only needs it
        # often enough to look continuous, and the release always refreshes once more.
        now = time.monotonic()
        if now - drag.get("painted", 0.0) >= 0.03:
            drag["painted"] = now
            self.panel.refresh_preview()
        self.update()

    def _scrub_to(self, x):
        """Put the playhead under a widget x, kept inside the project."""
        frame = int(round(self.frame_at(x)))
        duration = int(getattr(self.panel.session.timeline, "duration", 0) or 0)
        frame = max(0, min(max(0, duration - 1), frame))
        self.panel.session.set_playhead(frame)

    def _move_handle(self, drag, pos):
        clip = self.panel.clip()
        channel, time, side = drag["channel"], drag["time"], drag["side"]
        stored = CH.stored(clip, channel)
        if not isinstance(stored, AnimatedValue):
            return
        keys = stored.keyframes
        index = next((i for i, key in enumerate(keys) if key.time == time), None)
        if index is None:
            return
        if side == "out":
            if index + 1 >= len(keys):
                return
            left, right = keys[index], keys[index + 1]
        else:
            if index == 0:
                return
            left, right = keys[index - 1], keys[index]
        dt = max(1e-6, right.time - left.time)
        dv = float(right.value) - float(left.value)
        frame = self.frame_at(pos.x()) - self.panel.start_frame()
        value = float(self.value_at(pos.y()))
        nx = min(1.0, max(0.0, (frame - left.time) / dt))
        ny = (value - float(left.value)) / dv if abs(dv) > 1e-9 else 0.33
        ny = max(-2.0, min(3.0, ny))
        if side == "out":
            left.out_value_handle = (nx, ny)
        else:
            right.in_value_handle = (nx, ny)
        if self.panel.unified:
            # Continuous Bézier: mirror the handle on the keyframe's other side.
            if side == "out" and index > 0:
                previous = keys[index - 1]
                previous.in_value_handle = (1.0 - nx, 1.0 - ny)
            elif side == "in" and index + 1 < len(keys):
                following = keys[index + 1]
                following.out_value_handle = (1.0 - nx, 1.0 - ny)
        if left.interpolation in ("linear", "hold"):
            left.interpolation = "bezier"
        self.panel.refresh_preview()
        self.update()

    def _key_at(self, pos):
        clip = self.panel.clip()
        if clip is None:
            return None
        for channel, time, value in self.panel.key_entries():
            if channel not in self.panel.visible_channels():
                continue
            point = QPointF(self.x_of(self.panel.start_frame() + time), self.y_of(value))
            if (point - pos).manhattanLength() <= HIT * 1.6:
                return channel, time
        return None

    def _handle_at(self, pos):
        clip = self.panel.clip()
        if clip is None:
            return None
        for channel in self.panel.visible_channels():
            stored = CH.stored(clip, channel)
            if not isinstance(stored, AnimatedValue):
                continue
            keys = stored.keyframes
            if not any((channel, key.time) in self.panel.selection for key in keys):
                continue
            for index in range(len(keys)):
                if (channel, keys[index].time) not in self.panel.selection:
                    continue
                if index + 1 < len(keys):
                    point = self._handle_point(keys[index], keys[index + 1], "out")
                    if point is not None and (point - pos).manhattanLength() <= HIT * 1.5:
                        return channel, keys[index].time, "out"
                if index > 0:
                    point = self._handle_point(keys[index - 1], keys[index], "in")
                    if point is not None and (point - pos).manhattanLength() <= HIT * 1.5:
                        return channel, keys[index].time, "in"
        return None

    def _channel_near(self, pos):
        """The visible channel whose curve passes closest to the pointer."""
        clip = self.panel.clip()
        if clip is None:
            return None
        best, best_distance = None, HIT
        for channel in self.panel.visible_channels():
            value = CH.numeric_value_at(clip, channel,
                                        self.frame_at(pos.x()) - self.panel.start_frame())
            if value is None:
                continue
            distance = abs(self.y_of(value) - pos.y())
            if distance <= best_distance:
                best, best_distance = channel, distance
        return best

    def _select_in_box(self, box):
        chosen = set()
        for channel, time, value in self.panel.key_entries():
            if channel not in self.panel.visible_channels():
                continue
            point = QPointF(self.x_of(self.panel.start_frame() + time), self.y_of(value))
            if box.contains(point):
                chosen.add((channel, time))
        self.panel.select_keys(chosen, additive=True)

    def reset_view(self):
        """Back to the default zoom, then frame the whole animation."""
        self.zoom_x = 4.0
        self.zoom_y = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.fit()

    @crashlog.guard("graph editor")
    def wheelEvent(self, event):
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y() / 120.0
            anchor = self.value_at(event.position().y())
            self.zoom_y = max(0.2, min(8.0, self.zoom_y * (1.15 ** delta)))
            # Keep the value under the pointer in place while zooming vertically.
            self.pan_y += (anchor - self.value_at(event.position().y())) * 100.0
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            self.pan_x += event.angleDelta().y() / 120.0 * 40.0
        else:
            steps = event.angleDelta().y() / 120.0
            anchor = self.frame_at(event.position().x())
            self.zoom_x = max(0.15, min(400.0, self.zoom_x * (1.15 ** steps)))
            # Invert x_of (which starts at the value gutter) so the anchor frame stays
            # under the pointer instead of drifting by the gutter's width.
            self.pan_x = (AXIS_W + (anchor - self.panel.start_frame()) * self.zoom_x
                          - event.position().x())
        self.update()

    @crashlog.guard("graph editor")
    def mouseDoubleClickEvent(self, event):
        """Double-click a curve to add a keyframe that starts on the curve."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        clip = self.panel.clip()
        if clip is None:
            return
        pos = event.position()
        if self._key_at(pos) is not None or self._handle_at(pos) is not None:
            return
        channel = self._channel_near(pos)
        if channel is None:
            return
        frame = max(0, min(max(0, clip.duration - 1),
                           int(round(self.frame_at(pos.x()) - self.panel.start_frame()))))
        if self.panel.session.add_keyframe(clip.id, channel, frame):
            self.panel.select_keys({(channel, frame)})
            self.panel.refresh_preview()
            self.update()

    @crashlog.guard("graph editor")
    def contextMenuEvent(self, event):
        clip = self.panel.clip()
        if clip is None:
            return
        pos = QPointF(event.pos())
        menu = QMenu(self)
        hit = self._key_at(pos)
        if hit is not None:
            entry = (hit[0], hit[1])
            if entry not in self.panel.selection:
                self.panel.select_keys({entry})
            entries = self.panel.selected_entries()
            self.panel.fill_easing_menu(menu, entries)
            menu.addSeparator()
            menu.addAction("Copy", self.panel.copy_selected)
            paste = menu.addAction("Paste", self.panel.paste_selected)
            paste.setEnabled(self.panel.session.has_key_clipboard())
            menu.addAction("Duplicate", self.panel.duplicate_selected)
            menu.addAction("Delete", self.panel.delete_selected)
            menu.addSeparator()
            menu.addAction("Add Keyframe Here", lambda: self.panel.add_key_at(hit[0],
                                                                              hit[1]))
        else:
            channel = self._channel_near(pos)
            if channel is not None:
                frame = max(0, min(max(0, clip.duration - 1),
                                   int(round(self.frame_at(pos.x()) - self.panel.start_frame()))))
                menu.addAction("Add Keyframe Here",
                               lambda: self.panel.add_key_at(channel, frame))
        if menu.actions():
            menu.exec(event.globalPos())

    @crashlog.guard("graph editor")
    def keyPressEvent(self, event):
        modifiers = event.modifiers()
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.panel.delete_selected()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.panel.select_keys(set())
            self._drag = None
            self._box = None
            self.update()
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_A:
                self.panel.select_keys({(channel, time) for channel, time, _value
                                        in self.panel.key_entries()})
                return
            if event.key() == Qt.Key.Key_C:
                self.panel.copy_selected()
                return
            if event.key() == Qt.Key.Key_V:
                self.panel.paste_selected()
                return
            if event.key() == Qt.Key.Key_D:
                self.panel.duplicate_selected()
                return
            if event.key() == Qt.Key.Key_Z:
                session = self.panel.session
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    session.stack.redo() if hasattr(session.stack, "redo") else None
                else:
                    session.stack.undo() if hasattr(session.stack, "undo") else None
                self.panel.refresh_preview()
                self.update()
                return
        super().keyPressEvent(event)


def _key_selection(session):
    """The shared key selection as [(clip_id, channel, time)], skipping malformed rows.

    A stale entry (a deleted clip, a channel id that is not a string) must not be able to
    raise inside a signal handler, so it is dropped here instead of being trusted.
    """
    rows = []
    for row in getattr(session, "key_selection", ()) or ():
        if _is_key_entry(row):
            rows.append(row)
    return rows


def _is_key_entry(row):
    """True for a (clip_id, channel, time) triple whose parts can be used as numbers."""
    if not isinstance(row, (tuple, list)) or len(row) != 3:
        return False
    clip_id, channel, time = row
    if not isinstance(channel, str) or not channel:
        return False
    for value in (clip_id, time):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
    return True


def _format_value(value, channel_meta):
    """Axis label for one value, using a channel metadata row (never a channel id).

    Rows come from ``model.channels.list_channels`` and carry unit/decimals, so a
    position reads ``120.00 px`` while opacity reads ``75.00%``. Anything that is not a
    mapping has no metadata and falls back to a plain number, which keeps a stray label
    from taking the editor down.
    """
    if not isinstance(channel_meta, Mapping):
        return f"{value:g}"
    try:
        decimals = max(0, int(channel_meta.get("decimals", 2)))
    except (TypeError, ValueError):
        decimals = 2
    unit = channel_meta.get("unit") or ""
    return f"{value:.{decimals}f}{unit}"


class GraphEditorPanel(QWidget):
    """Dockable Graph Editor: channel list, graph canvas and its toolbar."""

    keyframe_requested = pyqtSignal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.session = engine.session
        self.mode = "value"
        self.unified = True
        self.selection = set()      # {(channel, time)}
        self.visible = set()        # channels shown as curves; empty = all animated
        self._colours = {}
        self._building = False

        self.toolbar = QWidget()
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(tokens.SP1, tokens.SP1, tokens.SP1, tokens.SP1)
        row.setSpacing(tokens.SP1)
        # Two named animation editors, one shared model.  The buttons are a mode switch,
        # not two copies: both panels read and write the session's channel data.
        self.keyframe_mode_button = QToolButton()
        self.keyframe_mode_button.setText("Keyframe Editor")
        self.keyframe_mode_button.setToolTip("Edit animation timing and values")
        self.keyframe_mode_button.setCheckable(True)
        self.keyframe_mode_button.setFixedHeight(tokens.CONTROL_H)
        self.keyframe_mode_button.clicked.connect(lambda _checked=False:
                                                  self.keyframe_requested.emit())
        row.addWidget(self.keyframe_mode_button)
        self.graph_mode_button = QToolButton()
        self.graph_mode_button.setText("Graph Editor")
        self.graph_mode_button.setToolTip("Edit interpolation, Bezier handles and curves")
        self.graph_mode_button.setCheckable(True)
        self.graph_mode_button.setChecked(True)
        self.graph_mode_button.setFixedHeight(tokens.CONTROL_H)
        self.graph_mode_button.clicked.connect(lambda _checked=False: self.set_active(True))
        row.addWidget(self.graph_mode_button)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Value Graph", "Speed Graph"])
        self.mode_combo.setToolTip("Value shows the property over time; Speed shows how fast it changes")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode_combo)
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Apply an easing preset to the selected keyframes")
        self._fill_presets()
        self.preset_combo.activated.connect(self._preset_chosen)
        row.addWidget(self.preset_combo, 1)
        self.unified_button = QToolButton()
        self.unified_button.setProperty("role", "icon")
        self.unified_button.setText("Cont.")
        self.unified_button.setCheckable(True)
        self.unified_button.setChecked(True)
        self.unified_button.setToolTip("Continuous Bézier handles: editing one side mirrors the other")
        self.unified_button.toggled.connect(lambda on: setattr(self, "unified", bool(on)))
        row.addWidget(self.unified_button)
        for name, tip, callback in (
                ("fit", "Fit the whole animation into the view", lambda: self.canvas.fit()),
                ("reset", "Reset the view (zoom and pan)", lambda: self.canvas.reset_view()),
                ("path", "Copy the selected keyframes", self.copy_selected)):
            button = QToolButton()
            button.setProperty("role", "icon")
            button.setIcon(icon(name))
            button.setToolTip(tip)
            button.setFixedSize(tokens.HIT, tokens.HIT)
            button.clicked.connect(lambda _=False, cb=callback: cb())
            row.addWidget(button)
        paste = QToolButton()
        paste.setProperty("role", "icon")
        paste.setIcon(icon("plus"))
        paste.setToolTip("Paste keyframes onto the active channel at the playhead")
        paste.setFixedSize(tokens.HIT, tokens.HIT)
        paste.clicked.connect(self.paste_selected)
        row.addWidget(paste)
        self.fit_selected = QToolButton()
        self.fit_selected.setText("Fit sel.")
        self.fit_selected.setToolTip("Fit the selected keyframes into the view")
        self.fit_selected.clicked.connect(lambda _=False: self.canvas.fit(True))
        row.addWidget(self.fit_selected)
        self.interp_button = QToolButton()
        self.interp_button.setText("Easing")
        self.interp_button.setToolTip("Easing presets and interpolation for the selected keyframes")
        self.interp_button.clicked.connect(lambda _=False: self.show_interpolation_menu(self.interp_button))
        row.addWidget(self.interp_button)
        self.preset_menu_button = QToolButton()
        self.preset_menu_button.setProperty("role", "icon")
        self.preset_menu_button.setIcon(icon("stopwatch"))
        self.preset_menu_button.setToolTip("Save, rename or delete easing presets")
        self.preset_menu_button.setFixedSize(tokens.HIT, tokens.HIT)
        self.preset_menu_button.clicked.connect(lambda _=False: self.show_preset_menu(self.preset_menu_button))
        row.addWidget(self.preset_menu_button)

        self.channel_tree = QTreeWidget()
        self.channel_tree.setHeaderHidden(True)
        self.channel_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.channel_tree.setFixedWidth(190)
        self.channel_tree.itemClicked.connect(self._channel_clicked)
        self.channel_tree.itemChanged.connect(self._channel_toggled)
        self.canvas = GraphCanvas(self)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(tokens.SP1)
        body.addWidget(self.channel_tree)
        body.addWidget(self.canvas, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addLayout(body, 1)
        # The timeline's diamonds and this curve view share one selection, kept in the
        # session: selecting in either place selects in the other.
        self.session.key_selection_changed.connect(self._selection_from_session)
        # Playback advances the playhead with `silent=True`, which skips `playhead_changed`,
        # and the window only repaints its own timeline - so without this the curve view's
        # red line stayed frozen at the frame where playback started.
        self.session.playhead_moved.connect(self._playhead_moved)

    # ------------------------------------------------------------------- helpers
    def sizeHint(self):
        return QSize(640, 260)

    def set_active(self, active):
        """Reflect which of the two animation editors is raised, without changing data."""
        self.keyframe_mode_button.blockSignals(True)
        self.graph_mode_button.blockSignals(True)
        self.keyframe_mode_button.setChecked(not bool(active))
        self.graph_mode_button.setChecked(bool(active))
        self.keyframe_mode_button.blockSignals(False)
        self.graph_mode_button.blockSignals(False)

    def clip(self):
        clip = self.session.primary_clip()
        if clip is not None and clip.track is not None and clip.track.type == "video":
            return clip
        for clip in self.session._selected_clips():
            if clip.track is not None and clip.track.type == "video":
                return clip
        return None

    def start_frame(self):
        clip = self.clip()
        return int(clip.timeline_start) if clip is not None else 0

    def _plottable(self, clip, row):
        """Can this row be drawn as a curve? One predicate, asked everywhere.

        A curve needs a numeric range. Text words, booleans and choice parameters (a
        blend mode, a colour preset) have none; plotting them used to raise inside
        ``paintEvent`` and PyQt6 aborts the process on that, so they are filtered here
        and every value is read through ``CH.numeric_value_at``.
        """
        if row.get("kind") not in ("float", "int"):
            return False
        channel = row.get("id")
        return bool(channel) and CH.numeric_channel(clip, channel, self.engine.runtime_effects)

    def channels(self):
        clip = self.clip()
        if clip is None:
            return []
        return [row for row in CH.list_channels(clip, self.engine.runtime_effects)
                if self._plottable(clip, row)]

    def animated_channels(self):
        return [row for row in self.channels() if row["animated"]]

    def visible_channels(self):
        clip = self.clip()
        if clip is None:
            return []
        rows = [row["id"] for row in self.animated_channels()]
        if self.visible:
            return [row for row in rows if row in self.visible]
        return rows

    def active_channel(self):
        """Id of the channel the graph is labelling: tree selection, else first curve."""
        item = self.channel_tree.currentItem()
        if item is not None and item.data(0, Qt.ItemDataRole.UserRole):
            return item.data(0, Qt.ItemDataRole.UserRole)
        visible = self.visible_channels()
        return visible[0] if visible else None

    def channel_meta(self, channel):
        """The metadata row for a channel id, or None when it is not on this clip.

        The single place a channel id becomes display metadata. Callers that format a
        value ask for the row by id; the id itself is intentionally not a mapping.
        """
        if not isinstance(channel, str) or not channel:
            return None
        for row in self.channels():
            if row.get("id") == channel:
                return row
        return None

    def channel_colour(self, channel):
        palette = (tokens.COLORS["accent"], tokens.COLORS["success"], tokens.COLORS["warning"],
                   "#c792ea", "#ff6b6b", "#6bd5e0", "#e0d06b")
        if channel not in self._colours:
            self._colours[channel] = palette[len(self._colours) % len(palette)]
        return QColor(self._colours[channel])

    def key_entries(self):
        clip = self.clip()
        if clip is None:
            return []
        rows = []
        for channel in self.visible_channels():
            for time in CH.key_times(clip, channel):
                value = CH.numeric_value_at(clip, channel, time)
                if value is not None:
                    rows.append((channel, time, value))
        return rows

    def sample_points(self):
        clip = self.clip()
        if clip is None:
            return []
        rows = []
        for channel in self.visible_channels():
            for frame in range(0, max(1, clip.duration) + 1, 2):
                value = CH.numeric_value_at(clip, channel, frame)
                if value is not None:
                    rows.append((channel, frame, value))
        return rows

    def speed_points(self, only=None):
        """(timeline frame, units per second) sampled from the real curve."""
        clip = self.clip()
        if clip is None:
            return []
        fps = max(1.0, float(self.engine.timeline.fps))
        channels = [only] if only else self.visible_channels()
        rows = []
        for channel in channels:
            previous = None
            for frame in range(0, max(1, clip.duration) + 1, 2):
                value = CH.numeric_value_at(clip, channel, frame)
                if value is None:
                    continue
                if previous is not None:
                    rows.append((self.start_frame() + frame, (value - previous) * fps / 2.0))
                previous = value
        return rows

    def ruler_step(self, zoom):
        for step in (1, 2, 5, 10, 20, 30, 60, 120, 300, 600):
            if step * zoom > 46:
                return step
        return 1200

    # ------------------------------------------------------------------ refresh
    @crashlog.guard("graph editor")
    def refresh(self):
        """Rebuild the channel list when its shape changed; always repaint the graph."""
        clip = self.clip()
        signature = None if clip is None else (clip.id, tuple((row["id"], row["animated"])
                                                              for row in self.channels()))
        if signature != getattr(self, "_signature", object()):
            self._signature = signature
            self._rebuild_tree()
        self._selection_from_session()
        self.canvas.update()

    @crashlog.guard("graph editor")
    def _playhead_moved(self, _frame=0, _follow=True):
        """Follow the playhead, whether it moved by a scrub, a click or during playback."""
        self.canvas.playhead_moved()

    @crashlog.guard("graph editor")
    def _selection_from_session(self):
        """Adopt the shared keyframe selection for this panel's clip."""
        clip = self.clip()
        target = set() if clip is None else {(channel, int(time)) for clip_id, channel, time
                                             in _key_selection(self.session)
                                             if int(clip_id) == int(clip.id)}
        if target != self.selection:
            self.selection = target
            self.canvas.update()

    def focus_entries(self, entries):
        """Show the given [(clip_id, channel, time)] keys: select them and fit the curve."""
        clip = self.clip()
        if clip is None:
            return False
        entries = [row for row in (entries or ()) if _is_key_entry(row)]
        self.selection = {(channel, int(time)) for clip_id, channel, time in entries or ()
                          if int(clip_id) == int(clip.id)}
        channel = next(iter(self.selection))[0] if self.selection else None
        if channel:
            self._select_channel_item(channel)
        self.session.set_key_selection([(clip.id, ch, time) for ch, time in self.selection])
        self.canvas.fit(only_selected=bool(self.selection))
        self.canvas.update()
        return True

    def _select_channel_item(self, channel):
        for index in range(self.channel_tree.topLevelItemCount()):
            parent = self.channel_tree.topLevelItem(index)
            for child_index in range(parent.childCount()):
                item = parent.child(child_index)
                if item.data(0, Qt.ItemDataRole.UserRole) == channel:
                    self.channel_tree.setCurrentItem(item)
                    parent.setExpanded(True)
                    return

    def _rebuild_tree(self):
        self._building = True
        self.channel_tree.clear()
        groups = {}
        for row in self.channels():
            parent = groups.get(row["group"])
            if parent is None:
                parent = QTreeWidgetItem(self.channel_tree, [row["group"]])
                parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
                parent.setExpanded(True)
                groups[row["group"]] = parent
            item = QTreeWidgetItem(parent, [row["label"]])
            item.setData(0, Qt.ItemDataRole.UserRole, row["id"])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if row["id"] in self.visible_channels()
                               else Qt.CheckState.Unchecked)
            if not row["animated"]:
                item.setForeground(0, QBrush(QColor(tokens.COLORS["muted"])))
        self._building = False
        if self.channel_tree.topLevelItemCount():
            first = self.channel_tree.topLevelItem(0)
            if first.childCount():
                self.channel_tree.setCurrentItem(first.child(0))

    def _channel_clicked(self, item, _column):
        channel = item.data(0, Qt.ItemDataRole.UserRole)
        if not channel:
            return
        frame = self.session.playhead
        clip = self.clip()
        if clip is not None:
            self.session.set_playhead(max(clip.timeline_start, min(clip.end - 1, frame)))
        self.selection = {entry for entry in self.selection if entry[0] == channel}
        self.canvas.update()

    def _channel_toggled(self, item, _column):
        if self._building:
            return
        channel = item.data(0, Qt.ItemDataRole.UserRole)
        if not channel:
            return
        animated = [row["id"] for row in self.animated_channels()]
        self.visible = {row for row in self.visible_channels()}
        if item.checkState(0) == Qt.CheckState.Checked:
            self.visible.add(channel)
        else:
            self.visible.discard(channel)
        if self.visible >= set(animated):
            self.visible = set()
        self.canvas.update()

    def select_keys(self, entries, additive=False):
        entries = {(channel, int(time)) for channel, time in entries}
        self.selection = (self.selection | entries) if additive else entries
        clip = self.clip()
        if clip is not None:
            self.session.set_key_selection([(clip.id, channel, time)
                                           for channel, time in self.selection])
        self.canvas.update()

    def replace_key(self, channel, old, new):
        self.selection.discard((channel, old))
        self.selection.add((channel, new))

    def selected_entries(self):
        clip = self.clip()
        if clip is None:
            return []
        return [(clip.id, channel, time) for channel, time in sorted(self.selection)]

    # ------------------------------------------------------------------ actions
    def _mode_changed(self, index):
        self.mode = "speed" if index == 1 else "value"
        self.canvas.update()

    def _fill_presets(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Easing presets...")
        for name in easing.menu_preset_names():
            self.preset_combo.addItem(curve_icon(easing.preset_curve(name)), name)
        self.preset_combo.blockSignals(False)

    def _preset_chosen(self, index):
        if index <= 0:
            return
        name = self.preset_combo.itemText(index)
        entries = self.selected_entries()
        if entries:
            self.session.apply_easing(entries, name)
            self.refresh_preview()
        self.canvas.update()

    def copy_selected(self):
        count = self.session.copy_keyframes(self.selected_entries())
        if count:
            self.session.status.emit(f"Copied {count} keyframe(s)")

    def paste_selected(self):
        clip = self.clip()
        channel = self.active_channel()
        if clip is None or channel is None:
            return
        count = self.session.paste_keyframes(clip.id, channel)
        if count:
            self.refresh_preview()
            self.canvas.update()

    def delete_selected(self):
        entries = self.selected_entries()
        if entries:
            self.session.delete_keyframes(entries)
            self.selection = set()
            self.refresh_preview()
            self.canvas.update()

    def duplicate_selected(self):
        entries = self.selected_entries()
        if entries and self.session.duplicate_keyframes(entries):
            self.refresh_preview()
            self.canvas.update()

    def add_key_at(self, channel, frame):
        """Add a keyframe on an existing curve, starting from the curve's own value."""
        clip = self.clip()
        if clip is None:
            return False
        if not self.session.add_keyframe(clip.id, channel, int(frame)):
            return False
        self.select_keys({(channel, int(frame))})
        self.refresh_preview()
        self.canvas.update()
        return True

    def fill_easing_menu(self, menu, entries):
        """The same easing structure the timeline shows: families first, types below."""
        curve_entries = self.session.easing_entries(entries)
        for name in easing.menu_preset_names():
            action = menu.addAction(name)
            action.setIcon(curve_icon(easing.preset_curve(name)))
            action.setEnabled(bool(curve_entries))
            action.triggered.connect(lambda _=False, preset=name, rows=curve_entries:
                                     self._apply_preset_rows(preset, rows))
        menu.addSeparator()
        advanced = menu.addMenu("Advanced Interpolation")
        for label, spec, tip in easing.ADVANCED_INTERPOLATION:
            action = advanced.addAction(label)
            action.setToolTip(tip)
            action.setEnabled(bool(curve_entries))
            action.triggered.connect(lambda _=False, value=spec: self.set_interpolation(value))

    def _apply_preset_rows(self, name, entries):
        if entries and self.session.apply_easing(entries, name):
            self.refresh_preview()
            self.canvas.update()

    def set_interpolation(self, spec):
        entries = self.selected_entries()
        if entries:
            self.session.set_interpolation(entries, spec)
            self.refresh_preview()
            self.canvas.update()

    def show_interpolation_menu(self, button):
        """Friendly presets on top, interpolation types under Advanced (same as the timeline)."""
        menu = QMenu(self)
        for name in easing.menu_preset_names():
            action = menu.addAction(name)
            action.setIcon(curve_icon(easing.preset_curve(name)))
            action.triggered.connect(lambda _=False, preset=name: self.apply_preset(preset))
        menu.addSeparator()
        advanced = menu.addMenu("Advanced Interpolation")
        for label, spec, tip in easing.ADVANCED_INTERPOLATION:
            action = advanced.addAction(label)
            action.setToolTip(tip)
            action.triggered.connect(lambda _=False, value=spec: self.set_interpolation(value))
        menu.exec(button.mapToGlobal(QPoint(0, button.height())))

    def apply_preset(self, name):
        """Apply a named easing preset to the graph selection (undoable)."""
        entries = self.selected_entries()
        if not entries:
            return False
        self.session.apply_easing(entries, name)
        self.refresh_preview()
        self.canvas.update()
        return True

    def save_preset_from_selection(self):
        clip = self.clip()
        entries = self.selected_entries()
        if clip is None or not entries:
            return
        _clip_id, channel, time = entries[0]
        stored = CH.stored(clip, channel)
        if not isinstance(stored, AnimatedValue):
            return
        key = stored.key_at(time)
        if key is None:
            return
        name, ok = QInputDialog.getText(self, "Save easing preset", "Preset name:")
        if not ok or not name.strip():
            return
        handles = key.out_value_handle if isinstance(key.out_value_handle, (tuple, list)) else (0.42, 0.0)
        following = stored.keyframes[stored.keyframes.index(key) + 1:]
        other = following[0].in_value_handle if following and isinstance(following[0].in_value_handle, (tuple, list)) else (0.58, 1.0)
        easing.save_custom_preset(name.strip(), "bezier",
                                  (handles[0], handles[1], other[0], other[1]))
        self._fill_presets()

    def show_preset_menu(self, button):
        """Save the selected keyframe's curve as a preset, and manage custom ones."""
        menu = QMenu(self)
        menu.addAction("Save current easing as preset...", self.save_preset_from_selection)
        custom = list(easing.load_custom_presets())
        if custom:
            menu.addSeparator()
            rename_menu = menu.addMenu("Rename custom preset")
            delete_menu = menu.addMenu("Delete custom preset")
            for name in custom:
                rename_menu.addAction(name, lambda name=name: self._rename_preset(name))
                delete_menu.addAction(name, lambda name=name: self._delete_preset(name))
        menu.exec(button.mapToGlobal(QPoint(0, button.height())))

    def _rename_preset(self, name):
        new, ok = QInputDialog.getText(self, "Rename preset", "New name:", text=name)
        if ok and new.strip():
            easing.rename_custom_preset(name, new.strip())
            self._fill_presets()

    def _delete_preset(self, name):
        easing.delete_custom_preset(name)
        self._fill_presets()

    def refresh_preview(self):
        """Ask the window to re-render the picture and the inspector values."""
        self.session.changed.emit()
