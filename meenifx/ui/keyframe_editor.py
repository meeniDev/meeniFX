"""Dedicated Keyframe Editor: timing and values, with one shared animation model.

The panel is a view over ``model.channels`` just like the Graph Editor.  It owns no
keyframes: every diamond is read from the clip and every edit is written back through the
same channel helpers the inspector, preview and exporter use.  Selection lives in the
session, so choosing a key here selects the same key in the Graph Editor and on the
timeline.

Timing is edited by dragging a key (or a multi-row selection) horizontally.  Values are
edited in the toolbar or by double-clicking a key.  Easing and curves stay in the Graph
Editor, which is one click away in the mode switcher.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (QAbstractSpinBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                             QLabel, QMenu, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import crashlog, tokens
from ..icons import icon
from ..model import channels as CH
from .graph_editor import _format_value

RULER_H = 24
ROW_H = 24
GROUP_H = 22
LEFT_PAD = 8.0
RIGHT_PAD = 12.0
DIAMOND = 5.0
HIT = 7.0
SNAP_PX = 7.0
VALUE_COL_WIDTH = 78
LABEL_COL_WIDTH = 190


def _clamp(value, low, high):
    return max(low, min(high, value))


class KeyframeRuler(QWidget):
    """Time ruler and playhead track for the keyframe lanes."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setFixedHeight(RULER_H)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Click or drag to move the playhead; double-click to fit the animation")

    @crashlog.guard("keyframe editor")
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(tokens.COLORS["panel"]))
        clip = self.panel.clip()
        if clip is None:
            painter.setPen(QColor(tokens.COLORS["muted"]))
            painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), "No clip selected")
            painter.end()
            return
        step = self.panel.ruler_step()
        start = int(self.panel.view_start // step) * step
        end = int(self.panel.visible_end()) + step
        painter.setFont(QFont(tokens.FONT_FAMILY, 8))
        for frame in range(max(0, start), max(0, end) + step, step):
            x = self.panel.x_of(frame)
            if x < -20 or x > self.width() + 20:
                continue
            painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
            painter.drawLine(int(x), self.height() - 5, int(x), self.height())
            painter.setPen(QColor(tokens.COLORS["muted"]))
            painter.drawText(QRectF(x - 30, 1, 60, self.height() - 6),
                             int(Qt.AlignmentFlag.AlignCenter), str(frame))
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        self._paint_playhead(painter)
        painter.end()

    def _paint_playhead(self, painter):
        clip = self.panel.clip()
        if clip is None:
            return
        x = self.panel.x_of(self.panel.session.playhead - clip.timeline_start)
        if x < -6 or x > self.width() + 6:
            return
        colour = QColor(tokens.COLORS["red"])
        painter.setPen(QPen(colour, 1.1))
        painter.setBrush(colour)
        head = min(max(x, 5), self.width() - 5)
        painter.drawPolygon(QPolygonF([QPointF(head - 5, self.height() - 9),
                                       QPointF(head + 5, self.height() - 9),
                                       QPointF(head, self.height() - 2)]))
        painter.drawLine(int(x), self.height() - 2, int(x), self.height())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self.panel.begin_pan(event.position().x())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._scrub = True
            self.panel.set_playhead_at_x(event.position().x())
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, "_scrub", False):
            self.panel.set_playhead_at_x(event.position().x())
            return
        if self.panel._pan is not None:
            self.panel.pan_to(event.position().x())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._scrub = False
        if event.button() == Qt.MouseButton.MiddleButton:
            self.panel.end_pan()
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.panel.fit()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        self.panel.wheel_zoom(event)
        event.accept()


class KeyLane(QWidget):
    """One animated property: a lane of keyframe diamonds."""

    def __init__(self, panel, row, colour):
        super().__init__(panel)
        self.panel = panel
        self.row = row
        self.colour = colour
        self.setFixedHeight(ROW_H)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = None
        self._empty_press = None
        self.setToolTip(f"{row.get('group', '')} · {row.get('label', row.get('id', ''))}")

    @property
    def channel(self):
        return self.row["id"]

    def _records(self):
        clip = self.panel.clip()
        return CH.key_records(clip, self.channel) if clip is not None else []

    @crashlog.guard("keyframe editor")
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        active = self.panel.active_channel() == self.channel
        painter.fillRect(rect, QColor(tokens.COLORS["panel_soft"] if active
                                      else tokens.COLORS["panel_alt"]))
        painter.setPen(QPen(QColor(47, 59, 77, 120), 1))
        painter.drawLine(0, 0, self.width(), 0)
        mid = rect.center().y()
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1, Qt.PenStyle.DotLine))
        painter.drawLine(int(LEFT_PAD), int(mid), int(self.width() - RIGHT_PAD), int(mid))
        clip = self.panel.clip()
        if clip is None:
            painter.end()
            return
        painter.save()
        painter.setClipRect(rect)
        for record in self._records():
            frame = int(record[0])
            x = self.panel.x_of(frame)
            if x < -DIAMOND - 2 or x > self.width() + DIAMOND + 2:
                continue
            selected = (clip.id, self.channel, frame) in self.panel.session.key_selection
            hovered = self._hover == frame
            size = DIAMOND + (1.4 if hovered else 0.0)
            painter.setPen(QPen(QColor("#ffffff") if selected else self.colour, 1.3))
            painter.setBrush(QColor(tokens.COLORS["accent"] if selected
                                    else (tokens.COLORS["panel"] if not hovered else self.colour)))
            painter.drawPolygon(QPolygonF([QPointF(x, mid - size), QPointF(x + size, mid),
                                           QPointF(x, mid + size), QPointF(x - size, mid)]))
        painter.restore()
        if self.panel._marquee_rect is not None:
            offset = self.mapTo(self.panel.body, QPoint(0, 0))
            box = self.panel._marquee_rect.translated(-offset.x(), -offset.y())
            painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 1, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(124, 156, 255, 38))
            painter.drawRect(box)
        self._paint_playhead(painter)
        painter.end()

    def _paint_playhead(self, painter):
        clip = self.panel.clip()
        if clip is None:
            return
        x = self.panel.x_of(self.panel.session.playhead - clip.timeline_start)
        if -1 <= x <= self.width() + 1:
            painter.setPen(QPen(QColor(tokens.COLORS["red"]), 1.0))
            painter.drawLine(int(x), 0, int(x), self.height())

    def key_at(self, pos):
        for record in self._records():
            if abs(self.panel.x_of(int(record[0])) - pos.x()) <= HIT:
                return int(record[0])
        return None

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.MouseButton.MiddleButton:
            self.panel.begin_pan(event.position().x())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        hit = self.key_at(event.position())
        if hit is not None:
            self.panel.begin_key_drag(self.row, hit, event.position().x(), event.modifiers())
            return
        self._empty_press = event.position()
        # The panel owns the marquee so a drag can cross lane widgets and select keys
        # on several properties. Resolve the click/playhead decision on release, so a
        # marquee never scrubs the playhead while it is being drawn.
        self.panel.begin_marquee(self, event.position(), event.modifiers())

    def mouseMoveEvent(self, event):
        if self._empty_press is not None:
            self.panel.marquee_to(self, event.position())
            return
        if self.panel._drag is not None:
            self.panel.drag_to(event.position().x())
            return
        if self.panel._pan is not None:
            self.panel.pan_to(event.position().x())
            return
        hit = self.key_at(event.position())
        if hit != self._hover:
            self._hover = hit
            self.update()
        self.setCursor(Qt.CursorShape.SizeHorCursor if hit is not None else
                       Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self.panel.end_pan()
            return
        if self._empty_press is not None:
            self.panel.finish_marquee(self, event.position(), event.modifiers())
            self._empty_press = None
            return
        self.panel.end_key_drag()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self.key_at(event.position())
        if hit is not None:
            self.panel.edit_key_value(self.row, hit)
            return
        self.panel.add_key_at(self.row, self.panel.frame_at(event.position().x()))
        event.accept()

    def leaveEvent(self, event):
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        self.panel.wheel_zoom(event)
        event.accept()

    def contextMenuEvent(self, event):
        self.panel.show_context_menu(self.row, self.key_at(event.pos()),
                                     event.globalPos())

    def keyPressEvent(self, event):
        if self.panel.handle_key(event):
            return
        super().keyPressEvent(event)


class KeyframeEditorPanel(QWidget):
    """Dockable keyframe timing/value editor for the selected clip."""

    graph_requested = pyqtSignal()
    live_changed = pyqtSignal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.session = engine.session
        self.ppf = 4.0
        self.view_start = 0.0
        self.rows = []
        self.lanes = []
        self._row_widgets = []
        self._group_rows = {}
        self._group_buttons = {}
        self._collapsed = set()
        self._active_channel = None
        self._signature = object()
        self._drag = None
        self._marquee = None
        self._marquee_rect = None
        self._pan = None
        self._syncing_value = False
        self._last_live = 0.0
        self._last_width = 640
        self.ruler = None

        self._build_toolbar()
        self.ruler = KeyframeRuler(self)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(0)
        self.grid.setVerticalSpacing(0)
        self.grid.setColumnMinimumWidth(0, LABEL_COL_WIDTH)
        self.grid.setColumnMinimumWidth(1, VALUE_COL_WIDTH)
        self.grid.setColumnStretch(2, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidget(self.body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.toolbar)
        outer.addWidget(self.scroll, 1)

        self.session.key_selection_changed.connect(self._selection_changed)
        self.session.playhead_moved.connect(self._playhead_moved)
        self.session.clip_selected.connect(lambda _ids: self.refresh())
        self.refresh()

    # --------------------------------------------------------------- toolbar
    def _tool_button(self, text, tip, callback=None, checkable=False):
        button = QToolButton()
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setToolTip(tip)
        button.setCheckable(bool(checkable))
        button.setAutoRaise(True)
        button.setFixedHeight(tokens.CONTROL_H)
        if callback is not None:
            button.clicked.connect(lambda _checked=False, cb=callback: cb())
        return button

    def _build_toolbar(self):
        self.toolbar = QWidget()
        columns = QVBoxLayout(self.toolbar)
        columns.setContentsMargins(tokens.SP1, tokens.SP1, tokens.SP1, tokens.SP1)
        columns.setSpacing(tokens.SP1)
        modes = QHBoxLayout()
        modes.setSpacing(tokens.SP1)
        self.keyframe_mode_button = QToolButton()
        self.keyframe_mode_button.setText("Keyframe Editor")
        self.keyframe_mode_button.setToolTip("Edit animation timing and values")
        self.keyframe_mode_button.setCheckable(True)
        self.keyframe_mode_button.setChecked(True)
        self.keyframe_mode_button.setFixedHeight(tokens.CONTROL_H)
        self.keyframe_mode_button.clicked.connect(lambda _checked=False: self.set_active(True))
        modes.addWidget(self.keyframe_mode_button)
        self.graph_mode_button = QToolButton()
        self.graph_mode_button.setText("Graph Editor")
        self.graph_mode_button.setToolTip("Edit interpolation, Bezier handles and curves")
        self.graph_mode_button.setCheckable(True)
        self.graph_mode_button.setFixedHeight(tokens.CONTROL_H)
        self.graph_mode_button.clicked.connect(lambda _checked=False: self.graph_requested.emit())
        modes.addWidget(self.graph_mode_button)
        self.mode_buttons = (self.keyframe_mode_button, self.graph_mode_button)
        modes.addStretch(1)
        self.previous_button = self._tool_button("<", "Previous keyframe on the active property",
                                                 self.previous_key)
        self.previous_button.setToolTip("Previous keyframe")
        self.next_button = self._tool_button(">", "Next keyframe on the active property",
                                             self.next_key)
        self.next_button.setToolTip("Next keyframe")
        self.fit_button = self._tool_button("Fit", "Fit all keys, or just the selection, into view",
                                            self.fit)
        self.snap_button = self._tool_button("Snap", "Snap dragged keys to project frames and "
                                            "the playhead", checkable=True)
        self.snap_button.setChecked(True)
        for button in (self.previous_button, self.next_button, self.fit_button, self.snap_button):
            modes.addWidget(button)
        columns.addLayout(modes)

        actions = QHBoxLayout()
        actions.setSpacing(tokens.SP1)
        self.add_button = self._tool_button("+ Key", "Add a keyframe on the active property at "
                                            "the playhead", self.add_at_playhead)
        self.delete_button = self._tool_button("Delete", "Delete selected keyframes",
                                               self.delete_selected)
        self.copy_button = self._tool_button("Copy", "Copy selected keyframes", self.copy_selected)
        self.paste_button = self._tool_button("Paste", "Paste keyframes at the playhead",
                                              self.paste_selected)
        self.duplicate_button = self._tool_button("Duplicate", "Duplicate selected keyframes",
                                                  self.duplicate_selected)
        for button in (self.add_button, self.delete_button, self.copy_button,
                       self.paste_button, self.duplicate_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(QLabel("Value"))
        self.value_edit = QDoubleSpinBox()
        self.value_edit.setKeyboardTracking(False)
        self.value_edit.setDecimals(2)
        self.value_edit.setRange(-1e9, 1e9)
        self.value_edit.setSingleStep(1.0)
        self.value_edit.setFixedWidth(112)
        self.value_edit.setToolTip("Value of the selected keyframe(s); with no selection, "
                                   "sets the active property at the playhead")
        self.value_edit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.value_edit.editingFinished.connect(self.apply_value)
        actions.addWidget(self.value_edit)
        columns.addLayout(actions)

    # --------------------------------------------------------------- model
    def sizeHint(self):
        return QSize(680, 280)

    def clip(self):
        clip = self.session.primary_clip()
        if clip is None:
            selected = self.session._selected_clips()
            clip = selected[0] if selected else None
        return clip

    def channels(self):
        clip = self.clip()
        if clip is None:
            return []
        rows = []
        for row in CH.list_channels(clip, self.engine.runtime_effects):
            if row.get("kind") not in ("float", "int"):
                continue
            if not row.get("animated") and not str(row.get("id", "")).startswith(
                    (CH.TRANSFORM_PREFIX, CH.AUDIO_PREFIX, "text.")):
                continue
            if CH.numeric_value_at(clip, row["id"], 0) is None:
                continue
            rows.append(row)
        return rows

    def channel_meta(self, channel):
        if not isinstance(channel, str) or not channel:
            return None
        for row in self.channels():
            if row.get("id") == channel:
                return row
        return None

    def channel_colour(self, channel):
        palette = (tokens.COLORS["accent"], tokens.COLORS["success"], tokens.COLORS["warning"],
                   "#c792ea", "#ff6b6b", "#6bd5e0", "#e0d06b")
        ids = [row["id"] for row in self.channels()]
        index = ids.index(channel) if channel in ids else 0
        return QColor(palette[index % len(palette)])

    def active_channel(self):
        selected = self.session.selected_key_entries()
        clip = self.clip()
        for cid, channel, _time in selected:
            if clip is not None and int(cid) == int(clip.id):
                return channel
        if self._active_channel:
            return self._active_channel
        rows = self.channels()
        animated = [row["id"] for row in rows if row.get("animated")]
        return animated[0] if animated else (rows[0]["id"] if rows else None)

    def _set_active_channel(self, channel):
        if channel and channel != self._active_channel:
            self._active_channel = channel
            for lane in self.lanes:
                lane.update()
            self._sync_value_field()

    def selected_entries(self):
        clip = self.clip()
        if clip is None:
            return []
        return [(cid, channel, int(time)) for cid, channel, time
                in self.session.selected_key_entries() if int(cid) == int(clip.id)]

    def key_entries(self):
        clip = self.clip()
        if clip is None:
            return []
        return [(clip.id, row["id"], int(time)) for row in self.channels()
                for time in CH.key_times(clip, row["id"])]

    def ruler_step(self):
        for step in (1, 2, 5, 10, 20, 30, 60, 120, 300, 600):
            if step * self.ppf >= 42:
                return step
        return 1200

    def lane_width(self):
        if self.lanes:
            return max(1, self.lanes[0].width())
        return max(1, self.width() - LABEL_COL_WIDTH - VALUE_COL_WIDTH)

    def visible_end(self):
        return self.view_start + max(1.0, (self.lane_width() - LEFT_PAD - RIGHT_PAD)
                                     / max(1e-6, self.ppf))

    def x_of(self, frame):
        return LEFT_PAD + (float(frame) - self.view_start) * self.ppf

    def frame_at(self, x):
        return self.view_start + (float(x) - LEFT_PAD) / max(1e-6, self.ppf)

    @property
    def zoom_x(self):
        return self.ppf

    @zoom_x.setter
    def zoom_x(self, value):
        self.ppf = max(0.2, min(80.0, float(value)))
        self._clamp_view()

    def _clamp_view(self):
        clip = self.clip()
        duration = max(1, int(clip.duration)) if clip is not None else 1
        visible = max(1.0, (self.lane_width() - LEFT_PAD - RIGHT_PAD) / max(1e-6, self.ppf))
        if visible >= duration:
            self.view_start = 0.0
        else:
            self.view_start = _clamp(self.view_start, 0.0, duration - visible)

    def _refresh_views(self):
        self._clamp_view()
        if self.ruler is not None:
            self.ruler.update()
        for lane in self.lanes:
            lane.update()

    # --------------------------------------------------------------- refresh
    @crashlog.guard("keyframe editor")
    def refresh(self):
        clip = self.clip()
        rows = self.channels()
        signature = None if clip is None else (
            clip.id, tuple((row["id"], row.get("group"), bool(row.get("animated")))
                           for row in rows))
        if signature != self._signature:
            self._signature = signature
            self._rebuild(rows)
        else:
            self._update_values()
            self._sync_value_field()
            self._refresh_views()
        active = self.active_channel()
        if active and active != self._active_channel:
            self._active_channel = active
        self._update_actions()

    def _clear_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index in range(self.grid.rowCount()):
            self.grid.setRowStretch(index, 0)

    @crashlog.guard("keyframe editor")
    def _rebuild(self, rows):
        self._clear_grid()
        self.rows = list(rows)
        self.lanes = []
        self._row_widgets = []
        self._group_rows = {}
        self._group_buttons = {}
        header_property = QLabel("Property")
        header_property.setFixedHeight(RULER_H)
        header_property.setContentsMargins(tokens.SP2, 0, 0, 0)
        header_property.setFont(QFont(tokens.FONT_FAMILY, 8))
        header_property.setStyleSheet(f"color: {tokens.COLORS['muted']};")
        header_value = QLabel("Value")
        header_value.setFixedHeight(RULER_H)
        header_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_value.setContentsMargins(0, 0, tokens.SP2, 0)
        header_value.setFont(QFont(tokens.FONT_FAMILY, 8))
        header_value.setStyleSheet(f"color: {tokens.COLORS['muted']};")
        self.grid.addWidget(header_property, 0, 0)
        self.grid.addWidget(header_value, 0, 1)
        if self.ruler is not None:
            self.ruler.deleteLater()
        self.ruler = KeyframeRuler(self)
        self.grid.addWidget(self.ruler, 0, 2)

        if not self.rows:
            empty = QLabel("Select a clip with an animated property")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {tokens.COLORS['muted']};")
            self.grid.addWidget(empty, 1, 0, 1, 3)
            self._clamp_view()
            return

        groups = {}
        for row in self.rows:
            groups.setdefault(row.get("group") or "Animation", []).append(row)
        grid_row = 1
        for group, group_rows in groups.items():
            collapsed = group in self._collapsed
            button = QToolButton()
            button.setText(f"{group}  ({len(group_rows)})")
            button.setIcon(icon("chevron_right" if collapsed else "chevron_down"))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setAutoRaise(True)
            button.setFixedHeight(GROUP_H)
            button.setSizePolicy(button.sizePolicy().horizontalPolicy(),
                                 button.sizePolicy().verticalPolicy())
            button.setStyleSheet("text-align: left;")
            button.setToolTip(("Expand" if collapsed else "Collapse") + f" {group}")
            button.clicked.connect(lambda _checked=False, name=group, b=button:
                                   self._toggle_group(name, b))
            self.grid.addWidget(button, grid_row, 0, 1, 3)
            grid_row += 1
            self._group_buttons[group] = button
            self._group_rows[group] = []
            for row in group_rows:
                label = QLabel(f"  {row.get('label', row['id'])}")
                label.setContentsMargins(tokens.SP2, 0, 0, 0)
                label.setToolTip(row["id"])
                label.setFixedHeight(ROW_H)
                value = QLabel()
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                value.setContentsMargins(0, 0, tokens.SP2, 0)
                value.setFixedHeight(ROW_H)
                value.setFont(QFont(tokens.FONT_FAMILY, 9))
                lane = KeyLane(self, row, self.channel_colour(row["id"]))
                self.lanes.append(lane)
                self.grid.addWidget(label, grid_row, 0)
                self.grid.addWidget(value, grid_row, 1)
                self.grid.addWidget(lane, grid_row, 2)
                widgets = (label, value, lane)
                self._group_rows[group].append(widgets)
                self._row_widgets.append((row, label, value, lane))
                if collapsed:
                    for widget in widgets:
                        widget.setVisible(False)
                grid_row += 1
        self.grid.setRowStretch(grid_row, 1)
        self._update_values()
        self._sync_value_field()
        self._clamp_view()

    def _toggle_group(self, group, button):
        if group in self._collapsed:
            self._collapsed.discard(group)
        else:
            self._collapsed.add(group)
        collapsed = group in self._collapsed
        button.setIcon(icon("chevron_right" if collapsed else "chevron_down"))
        button.setToolTip(("Expand" if collapsed else "Collapse") + f" {group}")
        for widgets in self._group_rows.get(group, ()):
            for widget in widgets:
                widget.setVisible(not collapsed)

    def _update_values(self):
        clip = self.clip()
        if clip is None:
            return
        frame = _clamp(int(self.session.playhead) - int(clip.timeline_start), 0,
                       max(0, clip.duration - 1))
        for row, _label, value, _lane in self._row_widgets:
            number = CH.numeric_value_at(clip, row["id"], frame)
            value.setText(_format_value(number, row) if number is not None else "")

    def _selection_changed(self):
        self._sync_value_field()
        for lane in self.lanes:
            lane.update()
        self._update_actions()

    def _playhead_moved(self, _frame=0, _follow=True):
        self._update_values()
        self._sync_value_field()
        if self.ruler is not None:
            self.ruler.update()
        for lane in self.lanes:
            lane.update()

    def _update_actions(self):
        clip = self.clip()
        has_clip = clip is not None
        selected = bool(self.selected_entries())
        active = self.active_channel() is not None
        for button in (self.add_button, self.previous_button, self.next_button,
                       self.fit_button):
            button.setEnabled(has_clip and active)
        for button in (self.delete_button, self.copy_button, self.duplicate_button):
            button.setEnabled(has_clip and selected)
        self.paste_button.setEnabled(has_clip and active
                                     and self.session.has_key_clipboard())
        self.value_edit.setEnabled(has_clip and active)

    # ---------------------------------------------------------- value editing
    def _sync_value_field(self):
        if self._syncing_value:
            return
        self._syncing_value = True
        try:
            clip = self.clip()
            channel = self.active_channel()
            if clip is None or channel is None:
                self.value_edit.setEnabled(False)
                return
            meta = self.channel_meta(channel) or {}
            self.value_edit.setDecimals(max(0, min(4, int(meta.get("decimals", 2)))))
            self.value_edit.setRange(float(meta.get("minimum", -1e9)),
                                     float(meta.get("maximum", 1e9)))
            self.value_edit.setSingleStep(float(meta.get("step", 1.0)) or 1.0)
            unit = str(meta.get("unit") or "")
            self.value_edit.setSuffix(unit)
            selected = self.selected_entries()
            value = None
            if selected:
                cid, selected_channel, key_time = selected[0]
                value = CH.numeric_value_at(self.clip(), selected_channel, key_time)
                values = {round(float(CH.numeric_value_at(self.clip(), ch, t)), 6)
                          for _cid, ch, t in selected
                          if CH.numeric_value_at(self.clip(), ch, t) is not None}
                if len(values) > 1:
                    self.value_edit.setToolTip("Multiple selected values. Typing sets every "
                                               "selected keyframe to this value")
                else:
                    self.value_edit.setToolTip("Value of the selected keyframe(s)")
            else:
                frame = _clamp(int(self.session.playhead) - int(clip.timeline_start), 0,
                               max(0, clip.duration - 1))
                value = CH.numeric_value_at(clip, channel, frame)
                self.value_edit.setToolTip("No keyframe selected. Setting this value keys the "
                                           "active property at the playhead")
            if value is not None:
                self.value_edit.setValue(float(value))
            self.value_edit.setEnabled(True)
        finally:
            self._syncing_value = False

    def apply_value(self, _checked=False):
        if self._syncing_value:
            return False
        clip = self.clip()
        channel = self.active_channel()
        if clip is None or channel is None or clip.track.locked:
            return False
        value = float(self.value_edit.value())
        selected = self.selected_entries()
        before = self.session.timeline.snapshot()
        created = []
        if selected:
            for _cid, selected_channel, key_time in selected:
                meta = self.channel_meta(selected_channel) or {}
                number = _clamp(value, float(meta.get("minimum", -1e9)),
                                float(meta.get("maximum", 1e9)))
                CH.set_value(clip, selected_channel, number, key_time, auto_key=False)
            label = "Edit keyframe value" if len(selected) == 1 else "Edit keyframe values"
        else:
            meta = self.channel_meta(channel) or {}
            number = _clamp(value, float(meta.get("minimum", -1e9)),
                            float(meta.get("maximum", 1e9)))
            frame = _clamp(int(self.session.playhead) - int(clip.timeline_start), 0,
                           max(0, clip.duration - 1))
            CH.set_value(clip, channel, number, frame, auto_key=True)
            created = [(clip.id, channel, frame)]
            label = "Set keyframe value"
        committed = self.session.commit_keyframes(label, before)
        if committed:
            if created:
                self.session.set_key_selection(created)
            else:
                self.session.set_key_selection(selected)
            self.session.changed.emit()
        self._sync_value_field()
        return bool(committed)

    def edit_key_value(self, row, time):
        """Focus the value field for a key the user double-clicked."""
        self.session.set_key_selection({(self.clip().id, row["id"], int(time))})
        self._set_active_channel(row["id"])
        self.value_edit.setFocus()
        self.value_edit.selectAll()

    # ------------------------------------------------------------- navigation
    def add_at_playhead(self):
        clip = self.clip()
        channel = self.active_channel()
        if clip is None or channel is None:
            self.session.status.emit("Select a clip with an animated property")
            return 0
        frame = _clamp(int(self.session.playhead) - int(clip.timeline_start), 0,
                       max(0, clip.duration - 1))
        added = int(self.session.add_keyframe(clip.id, channel, frame))
        if added:
            self.session.status.emit(f"Keyframe added to {self.channel_meta(channel).get('label', channel)}")
            self.refresh()
        return added

    def add_key_at(self, row, frame):
        clip = self.clip()
        if clip is None or clip.track.locked:
            return False
        frame = _clamp(int(round(frame)), 0, max(0, clip.duration - 1))
        added = self.session.add_keyframe(clip.id, row["id"], frame)
        if added:
            self._set_active_channel(row["id"])
            self.refresh()
        return bool(added)

    def delete_selected(self):
        entries = self.selected_entries()
        if not entries:
            return 0
        removed = self.session.delete_keyframes(entries)
        if removed:
            self.session.clear_key_selection()
            self.session.status.emit("Keyframe deleted" if removed == 1
                                     else f"{removed} keyframes deleted")
            self.session.changed.emit()
        self.refresh()
        return removed

    def copy_selected(self):
        count = self.session.copy_keyframes(self.selected_entries())
        self.session.status.emit(f"Copied {count} keyframe(s)" if count
                                 else "Select keyframes to copy")
        self._update_actions()
        return count

    def paste_selected(self):
        clip = self.clip()
        channel = self.active_channel()
        if clip is None or channel is None:
            return 0
        count = self.session.paste_keyframes(clip.id, channel)
        if count:
            self.session.status.emit(f"{count} keyframe(s) pasted")
            self.session.changed.emit()
            self.refresh()
        return count

    def duplicate_selected(self):
        count = self.session.duplicate_keyframes(self.selected_entries())
        if count:
            self.session.status.emit(f"{count} keyframe(s) duplicated")
            self.session.changed.emit()
            self.refresh()
        return count

    def previous_key(self):
        return self._jump_key(-1)

    def next_key(self):
        return self._jump_key(1)

    def _jump_key(self, direction):
        clip = self.clip()
        channel = self.active_channel()
        if clip is None or channel is None:
            return False
        times = sorted(int(time) for time in CH.key_times(clip, channel))
        if not times:
            return False
        local = _clamp(int(self.session.playhead) - int(clip.timeline_start), 0,
                       max(0, clip.duration - 1))
        if direction < 0:
            choices = [time for time in times if time < local]
            target = choices[-1] if choices else times[-1]
        else:
            choices = [time for time in times if time > local]
            target = choices[0] if choices else times[0]
        self.session.set_playhead(int(clip.timeline_start) + target)
        self.session.set_key_selection({(clip.id, channel, target)})
        self._set_active_channel(channel)
        return True

    def fit(self, only_selected=False):
        clip = self.clip()
        if clip is None:
            return
        times = []
        if only_selected and self.selected_entries():
            for _cid, channel, time in self.selected_entries():
                times.append(int(time))
        else:
            for row in self.channels():
                times.extend(int(time) for time in CH.key_times(clip, row["id"]))
        if not times:
            self.view_start = 0.0
            self.ppf = max(0.2, min(80.0, (self.lane_width() - LEFT_PAD - RIGHT_PAD)
                                   / max(1, clip.duration)))
            self._refresh_views()
            return
        low, high = min(times), max(times)
        span = max(1, high - low)
        available = max(20.0, self.lane_width() - LEFT_PAD - RIGHT_PAD - 18)
        self.ppf = max(0.25, min(80.0, available / span))
        self.view_start = max(0.0, low - 2.0)
        self._refresh_views()

    def set_active(self, active):
        self.keyframe_mode_button.blockSignals(True)
        self.graph_mode_button.blockSignals(True)
        self.keyframe_mode_button.setChecked(bool(active))
        self.graph_mode_button.setChecked(not bool(active))
        self.keyframe_mode_button.blockSignals(False)
        self.graph_mode_button.blockSignals(False)

    def focus_entries(self, entries):
        clip = self.clip()
        if clip is None:
            return False
        entries = [(int(cid), str(channel), int(time)) for cid, channel, time
                   in (entries or ()) if int(cid) == int(clip.id)]
        self.session.set_key_selection(entries)
        if entries:
            self._set_active_channel(entries[0][1])
            self.fit(only_selected=True)
        else:
            self.fit()
        self.refresh()
        return bool(entries)

    # --------------------------------------------------------------- dragging
    def begin_key_drag(self, row, time, x, modifiers):
        clip = self.clip()
        if clip is None or clip.track.locked:
            self.session.status.emit("This clip is locked")
            return
        entry = (clip.id, row["id"], int(time))
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if ctrl:
            self.session.toggle_key_selection(*entry)
            self._drag = None
            return
        if shift:
            self.session.set_key_selection({entry}, additive=True)
        elif entry not in self.session.key_selection:
            self.session.set_key_selection({entry})
        self._set_active_channel(row["id"])
        entries = self.session.selected_key_entries()
        if not entries:
            return
        bases = self.session.key_bases(entries)
        if not bases:
            return
        # The snapshot is the single source for every mouse move.  Rebuilding from it
        # avoids the classic drag bug: looking up the key at its *new* time on the next
        # move misses it and some implementations then add a replacement key.
        self._drag = {
            "snapshot": self.session.timeline.snapshot(),
            "bases": bases,
            "anchor_time": int(time),
            "anchor_x": float(x),
            "delta": 0,
            "moved": False,
            "entries": entries,
            "clip_id": clip.id,
        }
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._refresh_views()

    def drag_to(self, x):
        drag = self._drag
        clip = self.clip()
        if drag is None or clip is None:
            return
        # The anchor moves with the grabbed key, not with the cursor's first pixel.
        delta = int(round(self.frame_at(x) - self.frame_at(drag["anchor_x"])))
        hook = self.key_shift_limits(drag["bases"])
        delta = _clamp(delta, hook[0], hook[1])
        if self.snap_button.isChecked():
            playhead_local = int(self.session.playhead) - int(clip.timeline_start)
            playhead_x = self.x_of(playhead_local)
            best = None
            for (clip_id, _channel), bucket in drag["bases"].items():
                target_clip = self.session.timeline.find_clip(clip_id)
                if target_clip is None:
                    continue
                for time in bucket["times"]:
                    candidate = int(time) + delta
                    gap = abs(self.x_of(candidate) - playhead_x)
                    if gap <= SNAP_PX and (best is None or gap < best[0]):
                        best = (gap, playhead_local - candidate)
            if best is not None:
                delta += best[1]
                delta = _clamp(delta, hook[0], hook[1])
        if delta == drag["delta"] and drag["moved"]:
            return
        base = drag["bases"]
        selection = set()
        for (clip_id, channel), bucket in base.items():
            target_clip = self.session.timeline.find_clip(clip_id)
            if target_clip is None or target_clip.track.locked:
                continue
            moved = {}
            for record in bucket["records"]:
                if int(record[0]) not in bucket["times"]:
                    continue
                new_time = _clamp(int(record[0]) + delta, 0,
                                  max(0, target_clip.duration - 1))
                moved[new_time] = (new_time, record[1], record[2], record[3], record[4])
                selection.add((clip_id, channel, new_time))
            if not moved:
                continue
            # Rebuild from the press snapshot, not from the live channel: reading the
            # live records preserved the intermediate position of every earlier mouse
            # move, which is exactly the "dragging leaves a trail of keys" bug.
            kept = [record for record in bucket["records"]
                    if int(record[0]) not in bucket["times"]]
            CH.replace_keys(target_clip, channel, kept + list(moved.values()))
        drag["delta"] = delta
        drag["moved"] = delta != 0
        if selection:
            self.session.set_key_selection(selection)
        self._refresh_views()
        self._notify_live()

    def key_shift_limits(self, bases):
        clip = self.clip()
        if clip is None:
            return 0, 0
        times = []
        for (clip_id, _channel), bucket in bases.items():
            if int(clip_id) == int(clip.id):
                times.extend(bucket["times"])
        if not times:
            return -10 ** 9, 10 ** 9
        return -min(times), max(0, clip.duration - 1) - max(times)

    def end_key_drag(self):
        drag = self._drag
        self._drag = None
        self.unsetCursor()
        if drag is None:
            return
        if drag.get("moved"):
            moved = list(self.session.selected_key_entries())
            label = "Move keyframe" if len(moved) <= 1 else f"Move {len(moved)} keyframes"
            self.session.commit_keyframes(label, drag["snapshot"])
        self._refresh_views()
        self._sync_value_field()

    def cancel_drag(self):
        drag = self._drag
        self._drag = None
        self.unsetCursor()
        if drag is not None:
            self.session.timeline.restore(drag["snapshot"])
            self.session.changed.emit()
        self._refresh_views()

    def _notify_live(self):
        now = time.monotonic()
        if now - self._last_live < 0.04:
            return
        self._last_live = now
        self.session.changed.emit()
        self.live_changed.emit()

    # ----------------------------------------------------------------- panning
    def begin_pan(self, x):
        self._pan = {"x": float(x), "start": self.view_start}
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def pan_to(self, x):
        if self._pan is None:
            return
        self.view_start = self._pan["start"] - (float(x) - self._pan["x"]) / max(1e-6, self.ppf)
        self._refresh_views()

    def end_pan(self):
        self._pan = None
        self.unsetCursor()

    def wheel_zoom(self, event):
        delta = event.angleDelta().y() / 120.0
        if abs(delta) < 1e-6:
            return
        modifiers = event.modifiers()
        # Same controls as the timeline and the graph editor: Ctrl+wheel zooms around
        # the pointer, Shift+wheel pans horizontally, a bare wheel does not zoom.
        if modifiers & Qt.KeyboardModifier.ShiftModifier and not modifiers & Qt.KeyboardModifier.ControlModifier:
            self.view_start -= delta * 40.0 / max(1e-6, self.ppf)
            self._refresh_views()
            return
        if not modifiers & Qt.KeyboardModifier.ControlModifier:
            return
        anchor = self.frame_at(event.position().x())
        factor = 1.15 ** delta
        self.ppf = max(0.2, min(80.0, self.ppf * factor))
        self.view_start = anchor - (event.position().x() - LEFT_PAD) / max(1e-6, self.ppf)
        self._refresh_views()

    def set_playhead_at_x(self, x):
        clip = self.clip()
        if clip is None:
            return
        frame = _clamp(int(round(self.frame_at(x))), 0, max(0, clip.duration - 1))
        self.session.set_playhead(int(clip.timeline_start) + frame)

    # ------------------------------------------------------------------ menus
    def show_context_menu(self, row, time, point):
        clip = self.clip()
        if clip is None:
            return
        menu = QMenu(self)
        if time is not None:
            entry = (clip.id, row["id"], int(time))
            if entry not in self.session.key_selection:
                self.session.set_key_selection({entry})
            menu.addAction("Delete Keyframe", self.delete_selected)
            menu.addAction("Copy", self.copy_selected)
            self._set_active_channel(row["id"])
        else:
            lane = next((lane for lane in self.lanes if lane.channel == row["id"]), None)
            local_x = lane.mapFromGlobal(point).x() if lane is not None else 0
            frame = int(round(self.frame_at(local_x)))
            menu.addAction("Add Keyframe Here", lambda: self.add_key_at(row, frame))
        paste = menu.addAction("Paste at Playhead", self.paste_selected)
        paste.setEnabled(self.session.has_key_clipboard())
        menu.addAction("Duplicate", self.duplicate_selected)
        menu.addSeparator()
        menu.addAction("Fit Selection", lambda: self.fit(only_selected=True))
        menu.addAction("Open in Graph Editor", lambda: self.graph_requested.emit())
        menu.exec(point)

    def handle_key(self, event):
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            return True
        if key == Qt.Key.Key_Escape:
            self.cancel_marquee()
            self.cancel_drag()
            return True
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_A:
                self.select_all()
                return True
            if key == Qt.Key.Key_C:
                self.copy_selected()
                return True
            if key == Qt.Key.Key_V:
                self.paste_selected()
                return True
            if key == Qt.Key.Key_D:
                self.duplicate_selected()
                return True
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.previous_key()
            return True
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.next_key()
            return True
        return False

    def select_all(self):
        self.session.set_key_selection(self.key_entries())
        self._update_actions()

    # --------------------------------------------------------------- marquee
    def begin_marquee(self, lane, pos, modifiers):
        """Start a selection box; a click is resolved only when the mouse is released."""
        if self.clip() is None:
            return
        origin = lane.mapTo(self.body, QPoint(int(round(pos.x())), int(round(pos.y()))))
        self._marquee = {
            "origin": QPointF(origin),
            "modifiers": modifiers,
            "base": set(self.session.key_selection),
            "moved": False,
        }
        self._marquee_rect = None
        self._set_active_channel(lane.channel)
        self._refresh_views()

    def _keys_in_marquee(self, rect):
        clip = self.clip()
        if clip is None:
            return set()
        chosen = set()
        for lane in self.lanes:
            if lane.isHidden():
                continue
            offset = lane.mapTo(self.body, QPoint(0, 0))
            for record in lane._records():
                # The marquee lives in body coordinates, so a lane's keys move by the
                # lane's own offset too (the lanes start after the property columns).
                point = QPointF(offset.x() + self.x_of(int(record[0])),
                                offset.y() + ROW_H / 2.0)
                if rect.contains(point):
                    chosen.add((clip.id, lane.channel, int(record[0])))
        return chosen

    def marquee_to(self, lane, pos):
        marquee = self._marquee
        if marquee is None:
            return
        point = lane.mapTo(self.body, QPoint(int(round(pos.x())), int(round(pos.y()))))
        if not marquee["moved"]:
            delta = QPointF(point) - marquee["origin"]
            if abs(delta.x()) < 3.0 and abs(delta.y()) < 3.0:
                return
            marquee["moved"] = True
        self._marquee_rect = QRectF(marquee["origin"], QPointF(point)).normalized()
        chosen = self._keys_in_marquee(self._marquee_rect)
        modifiers = marquee["modifiers"]
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            selection = marquee["base"] ^ chosen
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            selection = marquee["base"] | chosen
        else:
            selection = chosen
        self.session.set_key_selection(selection)
        self._refresh_views()

    def finish_marquee(self, lane, pos, modifiers):
        marquee = self._marquee
        self._marquee = None
        self._marquee_rect = None
        if marquee is None:
            return False
        if not marquee["moved"]:
            if not marquee["modifiers"] & (Qt.KeyboardModifier.ControlModifier
                                           | Qt.KeyboardModifier.ShiftModifier):
                self.session.clear_key_selection()
            self.set_playhead_at_x(pos.x())
            self._set_active_channel(lane.channel)
        self._refresh_views()
        return True

    def cancel_marquee(self):
        marquee = self._marquee
        self._marquee = None
        self._marquee_rect = None
        if marquee is not None:
            self.session.set_key_selection(marquee["base"])
            self._refresh_views()
            return True
        return False

    def keyPressEvent(self, event):
        if self.handle_key(event):
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._last_width = max(1, self.width())
        self._refresh_views()
