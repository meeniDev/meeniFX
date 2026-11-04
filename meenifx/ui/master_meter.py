"""Slim vertical master meter with a volume slider beside it."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QSlider, QToolButton, QVBoxLayout, QWidget

from .. import tokens
from ..icons import icon


class LevelBar(QWidget):
    """Vertical level bar fed by the audio engine's real rms/peak signal."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rms = 0.0
        self._peak = 0.0
        self.setFixedWidth(tokens.SP2)
        self.setMinimumHeight(60)

    def reset(self):
        self._rms = self._peak = 0.0
        self.update()

    def set_level(self, rms, peak):
        self._rms = min(1.0, max(0.0, float(rms) * 1.8))
        self._peak = min(1.0, max(0.0, float(peak)))
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        rect = self.rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(tokens.COLORS["bg"]))
        p.drawRoundedRect(rect, 2, 2)
        if self._peak > 0.004:
            gradient = QLinearGradient(0, rect.bottom(), 0, rect.top())
            gradient.setColorAt(0.0, QColor(tokens.COLORS["success"]))
            gradient.setColorAt(0.75, QColor(tokens.COLORS["success"]))
            gradient.setColorAt(1.0, QColor(tokens.COLORS["warning"]))
            height = max(1, int(rect.height() * self._peak))
            p.setBrush(gradient)
            p.drawRect(0, rect.height() - height, rect.width(), height)
        p.end()


class MasterMeter(QWidget):
    volume_changed = pyqtSignal(float)      # 0.0 .. 1.2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(tokens.SP4 * 2 + tokens.SP2)
        self.bar = LevelBar()
        self.slider = QSlider(Qt.Orientation.Vertical)
        self.slider.setRange(0, 120)
        self.slider.setValue(100)
        self.slider.setToolTip("Master volume: 100%")
        self.slider.valueChanged.connect(self._on_slider)
        self.mute = QToolButton()
        self.mute.setProperty("role", "icon")
        self.mute.setCheckable(True)
        self.mute.setIcon(icon("volume"))
        self.mute.setFixedSize(tokens.HIT + 2, tokens.HIT + 2)
        self.mute.setToolTip("Mute the master output")
        self.mute.toggled.connect(self._on_mute)
        self._muted_from = 100
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(tokens.SP1)
        body.addWidget(self.bar)
        body.addWidget(self.slider)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.SP1)
        layout.addLayout(body, 1)
        layout.addWidget(self.mute, 0, Qt.AlignmentFlag.AlignHCenter)
        self.setToolTip("Master level and volume")

    def set_volume_percent(self, value):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(value)))
        self.slider.blockSignals(False)
        self._on_slider(int(round(value)), emit=False)

    def set_unavailable(self, message):
        self.slider.setEnabled(False)
        self.mute.setEnabled(False)
        self.bar.reset()
        self.setToolTip(f"Audio unavailable: {message}")
        self.bar.setToolTip(self.toolTip())

    def _on_slider(self, value, emit=True):
        self.slider.setToolTip(f"Master volume: {value}%")
        self.mute.blockSignals(True)
        self.mute.setChecked(value == 0)
        self.mute.setIcon(icon("mute" if value == 0 else "volume"))
        self.mute.blockSignals(False)
        if value:
            self._muted_from = value
        if emit:
            self.volume_changed.emit(value / 100.0)

    def _on_mute(self, muted):
        self.slider.setValue(0 if muted else self._muted_from or 100)
