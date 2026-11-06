"""Small non-blocking toast for the few messages that actually matter.

The window has no status bar: routine feedback (imports, marker edits, tool
changes) belongs to the controls that already show that state. A toast is only
for something the user must not miss - a failed import, a finished export, a
missing audio device - so it fades away on its own and never takes focus.
"""

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QWidget


class Toast(QWidget):
    """Bottom-centre overlay: appears, waits, fades out, hides."""

    LIFETIME_MS = 2400
    FADE_MS = 280
    EDGE_MARGIN = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.label = QLabel("", self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setMaximumWidth(560)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 7, 14, 8)
        row.addWidget(self.label)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(self.FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._after_fade)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fade_out)
        self._kind = "info"
        self._style()
        self.hide()

    def _style(self):
        from .. import tokens
        c = tokens.COLORS
        edge = c["coral_text"] if self._kind == "error" else c["border_strong"]
        self.setStyleSheet(
            f"#toast {{ background: {c['panel']}; border: 1px solid {edge}; border-radius: {tokens.RADIUS}px; }}"
            f"#toast QLabel {{ color: {c['text']}; background: transparent; }}")

    # ------------------------------------------------------------------ content
    def message(self):
        return self.label.text()

    def show_message(self, text, kind="info", duration_ms=None):
        """Show `text` for a moment; a second message simply replaces the first."""
        text = " ".join(str(text or "").split())
        if not text:
            return
        self._fade.stop()
        self._timer.stop()
        self._kind = "error" if kind == "error" else "info"
        self._style()
        self.label.setText(text)
        self.adjustSize()
        self.reposition()
        self._opacity.setOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start(int(duration_ms or self.LIFETIME_MS))

    def fade_out(self):
        if not self.isVisible():
            return
        self._timer.stop()
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _after_fade(self):
        if self._opacity.opacity() <= 0.01:
            self.hide()

    def reposition(self):
        """Keep the panel bottom-centre of its window, clear of the window edges."""
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(self.sizeHint().width(), max(200, parent.width() - 2 * self.EDGE_MARGIN))
        self.setFixedWidth(width)
        self.adjustSize()
        x = max(self.EDGE_MARGIN, (parent.width() - self.width()) // 2)
        y = max(self.EDGE_MARGIN, parent.height() - self.height() - self.EDGE_MARGIN)
        self.move(x, y)
