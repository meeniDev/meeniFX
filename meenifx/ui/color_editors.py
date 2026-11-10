"""Interactive editors for the Curves and Color Wheels effects.

The Curves graph is the real control, not a decorated text field: points can be dragged, a
double-click adds one and a right-click removes one.  The dialog edits the same four curve
strings the renderer parses, so there is no separate curve model that can drift away from
the pixels.  Color Wheels uses one draggable trackball per lift/gamma/gain range.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QConicalGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

from .. import tokens
from ..effects.color_extra import (CURVE_CHANNELS, CURVE_LABELS, curve_lut, curve_text,
                                   parse_curve)


def _qcolour(colour):
    return QColor(colour) if isinstance(colour, QColor) else QColor(*colour)


class CurveGraph(QWidget):
    """A 0..255 curve editor with draggable control points."""

    channels = CURVE_CHANNELS
    labels = CURVE_LABELS

    def __init__(self, curves=None, channel="master", parent=None):
        super().__init__(parent)
        self.curves = {name: parse_curve((curves or {}).get(name, "0,0;255,255"))
                       for name in self.channels}
        self.channel = channel if channel in self.channels else "master"
        self.selected = -1
        self._drag = False
        self.setMinimumSize(300, 250)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_channel(self, channel):
        if channel in self.channels:
            self.channel = channel
            self.selected = -1
            self.update()

    def set_curves(self, curves):
        self.curves = {name: parse_curve((curves or {}).get(name, "0,0;255,255"))
                       for name in self.channels}
        self.update()

    def values(self):
        return {name: curve_text(points) for name, points in self.curves.items()}

    def curve_rect(self):
        return QRectF(22.0, 10.0, max(1.0, self.width() - 34.0),
                      max(1.0, self.height() - 32.0))

    def _to_screen(self, point):
        rect = self.curve_rect()
        return QPointF(rect.left() + point[0] / 255.0 * rect.width(),
                       rect.bottom() - point[1] / 255.0 * rect.height())

    def _from_screen(self, position):
        rect = self.curve_rect()
        return (max(0.0, min(255.0, (position.x() - rect.left()) / max(1.0, rect.width()) * 255.0)),
                max(0.0, min(255.0, (rect.bottom() - position.y()) / max(1.0, rect.height()) * 255.0)))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.curve_rect()
        painter.fillRect(self.rect(), QColor(tokens.COLORS["panel"]))
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
        for step in range(5):
            x = rect.left() + rect.width() * step / 4.0
            y = rect.top() + rect.height() * step / 4.0
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(QPen(QColor(tokens.COLORS["border_strong"]), 1))
        painter.drawRect(rect)
        for name in self.channels:
            if name == self.channel:
                continue
            painter.setPen(QPen(QColor(tokens.COLORS["muted"]), 1, Qt.PenStyle.DotLine))
            path = self._path(self.curves[name])
            painter.drawPath(path)
        painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 2.0))
        painter.drawPath(self._path(self.curves[self.channel]))
        painter.setBrush(QColor(tokens.COLORS["panel"]))
        painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 1.5))
        for index, point in enumerate(self.curves[self.channel]):
            centre = self._to_screen(point)
            radius = 5.0 if index == self.selected else 4.0
            painter.drawEllipse(centre, radius, radius)
        painter.end()

    def _path(self, points):
        lut = curve_lut(points)
        rect = self.curve_rect()
        path = QPainterPath()
        for x in range(256):
            position = QPointF(rect.left() + x / 255.0 * rect.width(),
                               rect.bottom() - float(lut[x]) / 255.0 * rect.height())
            path.moveTo(position) if x == 0 else path.lineTo(position)
        return path

    def _nearest(self, position):
        best, distance = -1, 14.0
        for index, point in enumerate(self.curves[self.channel]):
            delta = self._to_screen(point) - position
            length = math.hypot(delta.x(), delta.y())
            if length < distance:
                best, distance = index, length
        return best

    def mousePressEvent(self, event):
        position = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._nearest(position)
            if index >= 0:
                self.selected = index
                self._drag = True
                self.update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            index = self._nearest(position)
            points = self.curves[self.channel]
            if index not in (-1, 0, len(points) - 1):
                points.pop(index)
                self.selected = -1
                self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._drag or self.selected < 0:
            return
        points = self.curves[self.channel]
        x, y = self._from_screen(event.position())
        index = self.selected
        if index in (0, len(points) - 1):
            x = points[index][0]
        else:
            x = max(points[index - 1][0] + 1.0, min(points[index + 1][0] - 1.0, x))
        points[index] = (x, y)
        self.update()

    def mouseReleaseEvent(self, event):
        self._drag = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        x, y = self._from_screen(event.position())
        points = self.curves[self.channel]
        if all(abs(point[0] - x) > 3.0 for point in points):
            points.append((x, y))
            points.sort()
            self.selected = next(index for index, point in enumerate(points)
                                 if abs(point[0] - x) < 1e-6)
            self.update()
        event.accept()


class CurvesDialog(QDialog):
    def __init__(self, curves, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Curves")
        self.setMinimumSize(440, 440)
        self.graph = CurveGraph(curves)
        layout = QVBoxLayout(self)
        channels = QHBoxLayout()
        channels.addWidget(QLabel("Channel"))
        self.buttons = []
        for channel, label in zip(CURVE_CHANNELS, CURVE_LABELS):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(channel == "master")
            button.clicked.connect(lambda _=False, name=channel: self._choose(name))
            channels.addWidget(button)
            self.buttons.append(button)
        channels.addStretch(1)
        layout.addLayout(channels)
        layout.addWidget(self.graph, 1)
        hint = QLabel("Drag a point to shape the curve, double-click to add, right-click to remove.")
        hint.setProperty("quiet", True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        reset = buttons.addButton("Reset", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, channel):
        for name, button in zip(CURVE_CHANNELS, self.buttons):
            button.setChecked(name == channel)
        self.graph.set_channel(channel)

    def _reset(self):
        self.graph.set_curves({name: "0,0;255,255" for name in CURVE_CHANNELS})

    def values(self):
        return self.graph.values()


class ColorWheel(QWidget):
    """One draggable lift/gamma/gain trackball, in -100..100 coordinates."""

    def __init__(self, label="Lift", parent=None):
        super().__init__(parent)
        self.label = label
        self.x = 0.0
        self.y = 0.0
        self.setMinimumSize(132, 154)
        self.setMouseTracking(True)

    def set_value(self, x, y):
        self.x, self.y = float(x), float(y)
        self.update()

    def value(self):
        return self.x, self.y

    def wheel_centre(self):
        return QPointF(self.width() / 2.0, 70.0)

    def wheel_radius(self):
        return max(20.0, min(self.width(), 128.0) / 2.0 - 8.0)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        centre = self.wheel_centre()
        radius = self.wheel_radius()
        gradient = QConicalGradient(centre, 0.0)
        for step in range(13):
            hue = step * 30
            gradient.setColorAt(step / 12.0, QColor.fromHsv(hue, 210, 205))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
        painter.drawEllipse(centre, radius, radius)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 120), 1))
        painter.drawEllipse(centre, radius * 0.62, radius * 0.62)
        painter.drawLine(QPointF(centre.x() - radius, centre.y()),
                         QPointF(centre.x() + radius, centre.y()))
        painter.drawLine(QPointF(centre.x(), centre.y() - radius),
                         QPointF(centre.x(), centre.y() + radius))
        handle = QPointF(centre.x() + self.x / 100.0 * radius,
                         centre.y() - self.y / 100.0 * radius)
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(QPen(QColor(20, 20, 20), 1.3))
        painter.drawEllipse(handle, 5.0, 5.0)
        painter.setPen(QPen(QColor(tokens.COLORS["text"]), 1))
        painter.drawText(QRectF(0, 134, self.width(), 20),
                         Qt.AlignmentFlag.AlignCenter,
                         f"{self.label}  {self.x:+.0f}, {self.y:+.0f}")
        painter.end()

    def mousePressEvent(self, event):
        self._update_from(event.position())
        event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._update_from(event.position())
        super().mouseMoveEvent(event)

    def _update_from(self, position):
        centre = self.wheel_centre()
        radius = self.wheel_radius()
        x = (position.x() - centre.x()) / radius
        y = (centre.y() - position.y()) / radius
        length = math.hypot(x, y)
        if length > 1.0:
            x, y = x / length, y / length
        self.x, self.y = x * 100.0, y * 100.0
        self.update()


class ColorWheelsDialog(QDialog):
    def __init__(self, values, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Color Wheels")
        self.wheels = {}
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        for name in ("lift", "gamma", "gain"):
            wheel = ColorWheel(name.title())
            wheel.set_value((values or {}).get(f"{name}_x", 0.0),
                            (values or {}).get(f"{name}_y", 0.0))
            self.wheels[name] = wheel
            row.addWidget(wheel)
        layout.addLayout(row)
        hint = QLabel("Drag each trackball. Lift affects shadows, gamma midtones and gain highlights.")
        hint.setProperty("quiet", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        reset = buttons.addButton("Reset", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _reset(self):
        for wheel in self.wheels.values():
            wheel.set_value(0.0, 0.0)

    def values(self):
        result = {}
        for name, wheel in self.wheels.items():
            x, y = wheel.value()
            result[f"{name}_x"] = x
            result[f"{name}_y"] = y
        return result
