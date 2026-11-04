"""Floating recording bar and the 3-2-1 countdown. Both are hidden from screen capture."""

import ctypes
import sys
from collections import deque

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QToolButton, QWidget

from .. import tokens
from ..icons import icon
from .recorder_panel import LevelBar

C = tokens.COLORS
WDA_EXCLUDEFROMCAPTURE = 0x11


def exclude_from_capture(widget):
    """Keep this window out of screen recordings (Windows 10 2004+). Returns True when applied."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False


def format_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60:02d}:{seconds % 60:02d}"


class LevelHistory(QWidget):
    """Scrolling level history, one lane per source being recorded.

    The bars are sampled on a fixed clock rather than on every level signal, so the trace
    scrolls at a constant speed no matter how often a device reports, and a lane that is not
    recording does not exist at all (a microphone-only take shows one full-height lane).
    """

    COLUMNS = 150          # about 4 seconds at the 40 ms sampling interval
    INTERVAL_MS = 40
    MIC_COLOR = "#6fd6a8"
    SYSTEM_COLOR = "#7aa7ff"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(150)
        self.setToolTip("Live input levels")
        self._lanes = []            # [(key, label, color, deque)]
        self._levels = {}           # key -> latest 0..1 level
        self._clock = QTimer(self)
        self._clock.setInterval(self.INTERVAL_MS)
        self._clock.timeout.connect(self._sample)

    # ------------------------------------------------------------------ setup
    def set_sources(self, kinds):
        """kinds: the audio sources being recorded, e.g. ["mic"] or ["mic", "system"]."""
        wanted = [kind for kind in ("mic", "system") if kind in set(kinds or ())]
        if [lane[0] for lane in self._lanes] == wanted:
            return
        colors = {"mic": self.MIC_COLOR, "system": self.SYSTEM_COLOR}
        labels = {"mic": "Mic", "system": "System"}
        self._lanes = [(kind, labels[kind], colors[kind], deque(maxlen=self.COLUMNS))
                       for kind in wanted]
        self._levels = {kind: 0.0 for kind in wanted}
        self.setVisible(bool(wanted))
        if wanted:
            self._clock.start()
        else:
            self._clock.stop()
        self.updateGeometry()
        self.update()

    def push(self, kind, level):
        """Latest level for one source; the trace samples it on its own clock."""
        if kind in self._levels:
            self._levels[kind] = max(0.0, min(1.0, float(level)))

    def reset(self):
        for lane in self._lanes:
            lane[3].clear()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self._lanes:
            self._clock.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._clock.stop()

    def _sample(self):
        for kind, _label, _color, history in self._lanes:
            # Slow release, so one quiet sample cannot make the trace flicker.
            level = self._levels.get(kind, 0.0)
            peak = history[-1] if history else 0.0
            history.append(max(level, peak * 0.72))
        self.update()

    # ------------------------------------------------------------------ paint
    def sizeHint(self):
        return self.minimumSizeHint()

    def minimumSizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(150, 14 * max(1, len(self._lanes)) + 6 * max(0, len(self._lanes) - 1))

    def paintEvent(self, _event):
        if not self._lanes:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(Qt.PenStyle.NoPen)
        gap = 6
        count = len(self._lanes)
        lane_h = max(8, (self.height() - gap * (count - 1)) // count)
        p.setBrush(QColor(C["panel_soft"]))
        p.drawRoundedRect(0, 0, self.width(), lane_h, 3, 3)
        for index, (kind, _label, color, history) in enumerate(self._lanes):
            top = index * (lane_h + gap)
            p.setBrush(QColor(C["panel_soft"]))
            p.drawRoundedRect(0, top, self.width(), lane_h, 3, 3)
            if not history:
                continue
            p.setBrush(QColor(color))
            count_cols = len(history)
            start = max(0, self.width() - count_cols)
            for offset, value in enumerate(history):
                x = start + offset
                if x >= self.width():
                    break
                height = int(round(lane_h * max(0.0, min(1.0, value))))
                if height > 0:
                    p.drawRect(x, top + lane_h - height, 1, height)
        p.end()


class FloatingBar(QWidget):
    pause_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setObjectName("recorderBar")
        self.setStyleSheet(
            f"#recorderBar{{background:{C['panel']};border:1px solid {C['border']};border-radius:8px;}}"
            f"QLabel{{color:{C['text']};}}")
        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP3, tokens.SP2, tokens.SP2, tokens.SP2)
        row.setSpacing(tokens.SP2)
        self.dot = QLabel("\u25cf")
        self.dot.setStyleSheet(f"color:{C['red']};font-size:14px;")
        self.time = QLabel("00:00")
        font = QFont(tokens.FONT_FAMILY)
        font.setPixelSize(15)
        font.setBold(True)
        self.time.setFont(font)
        self.time.setMinimumWidth(52)
        # What is being captured right now: "Screen - Mic - System".
        self.sources = QLabel("")
        self.sources.setStyleSheet(f"color:{C['muted']};font-size:{tokens.FONT_SMALL}px;")
        self.meter = LevelBar()
        self.meter.setFixedWidth(64)
        self.meter.setToolTip("Microphone level")
        self.system_meter = LevelBar()
        self.system_meter.setFixedWidth(64)
        self.system_meter.setToolTip("System audio level")
        self.system_meter.hide()
        self.history = LevelHistory()
        self.history.hide()
        self.pause = self._button("pause", "Pause / resume")
        self.stop = self._button("stop", "Stop")
        self.stop.setStyleSheet(f"QToolButton{{background:{C['red']};border-radius:4px;}}")
        self.stop.setIcon(icon("stop", C["on_accent"]))
        self.pause.clicked.connect(lambda _=False: self.pause_clicked.emit())
        self.stop.clicked.connect(lambda _=False: self.stop_clicked.emit())
        for w in (self.dot, self.time, self.sources, self.history, self.meter, self.system_meter,
                  self.pause, self.stop):
            row.addWidget(w)
        self._blink = QTimer(self)
        self._blink.setInterval(600)
        self._blink.timeout.connect(self._toggle_dot)
        self._paused = False
        self._drag = None

    def _button(self, name, tip):
        b = QToolButton()
        b.setProperty("role", "icon")
        b.setIcon(icon(name))
        b.setToolTip(tip)
        b.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
        return b

    def _toggle_dot(self):
        self.dot.setVisible(not self.dot.isVisible())

    def present(self):
        self.history.reset()
        self.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - self.width() // 2, screen.top() + 16)
        self.show()
        exclude_from_capture(self)
        self._blink.start()

    def dismiss(self):
        self._blink.stop()
        self.history.reset()
        self.hide()

    def set_elapsed(self, seconds):
        self.time.setText(format_time(seconds))

    def set_sources(self, kinds):
        """Show exactly the sources being recorded, including their live levels.

        A microphone-only take says so and keeps one full-height history lane, so the audio
        recording is never invisible just because no video is being captured.
        """
        names = {"webcam": "Webcam", "screen": "Screen", "mic": "Mic", "system": "System"}
        kinds = [kind for kind in ("screen", "webcam", "mic", "system") if kind in set(kinds or ())]
        self.sources.setText("  -  ".join(names[k] for k in kinds) if kinds else "")
        audio = [kind for kind in ("mic", "system") if kind in kinds]
        self.meter.setVisible("mic" in audio)
        self.system_meter.setVisible("system" in audio)
        self.history.set_sources(audio)
        self.adjustSize()

    def push_level(self, kind, level):
        """Route one live level to its meter and to the history trace."""
        if kind == "mic":
            self.meter.set_level(level)
        elif kind == "system":
            self.system_meter.set_level(level)
        self.history.push(kind, level)

    def set_paused(self, paused):
        self._paused = paused
        self.pause.setIcon(icon("record", C["red"]) if paused else icon("pause"))
        self.pause.setToolTip("Resume" if paused else "Pause")
        if paused:
            self._blink.stop()
            self.dot.setVisible(True)
            self.dot.setStyleSheet(f"color:{C['muted']};font-size:14px;")
        else:
            self.dot.setStyleSheet(f"color:{C['red']};font-size:14px;")
            self._blink.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _event):
        self._drag = None


class Countdown(QWidget):
    """Big 3-2-1 in the middle of the primary screen; `finished` fires after the last number."""

    finished = pyqtSignal()
    cancelled = pyqtSignal()

    def __init__(self, seconds=3):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(180, 180)
        self.total = seconds
        self.left = seconds
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def begin(self):
        self.left = self.total
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.center() - QPoint(self.width() // 2, self.height() // 2))
        self.show()
        exclude_from_capture(self)
        self._timer.start()
        self.update()

    def _tick(self):
        self.left -= 1
        if self.left <= 0:
            self._timer.stop()
            self.hide()
            self.finished.emit()
        else:
            self.update()

    def cancel(self):
        self._timer.stop()
        self.hide()
        self.cancelled.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(11, 13, 16, 215))
        p.drawEllipse(self.rect().adjusted(8, 8, -8, -8))
        p.setPen(QColor(C["red"]))
        font = QFont(tokens.FONT_FAMILY)
        font.setPixelSize(88)
        font.setBold(True)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(max(1, self.left)))
