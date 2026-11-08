"""Small shared widgets: the same button, header, group and field everywhere.

Deliberately thin: each one is a stock Qt widget with a ``role``/``kind`` property the
application stylesheet understands, plus the one behaviour the stock widget lacks. Colour
and shape live in ``stylesheet.py``; sizes come from ``tokens``. Accessible names are set
from the tooltip so screen readers and UI automation see the same label a user does.
Numeric fields are the existing ``ui.widgets.ScrubField`` / ``NumberEdit``; media and folder
cards are ``ui.cards.CardDelegate``.
"""

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .. import tokens
from ..icons import icon
from .illustrations import paint_illustration
from .themed import bind_theme


def repolish(widget):
    """Re-evaluate the stylesheet for one widget after a property change."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class Button(QPushButton):
    """Text button. ``kind``: "secondary" (default), "primary", "danger" or "ghost"."""

    def __init__(self, text="", kind="secondary", icon_name=None, tip=None, parent=None):
        super().__init__(text, parent)
        self.setProperty("kind", kind)
        self.setMinimumHeight(tokens.CONTROL_H)
        if icon_name:
            bind_theme(self, lambda b, n=icon_name: b.setIcon(icon(n)))
        if tip:
            self.setToolTip(tip)
        self.setAutoDefault(False)


class IconButton(QToolButton):
    """Flat icon-only button with a tooltip that doubles as its accessible name."""

    def __init__(self, icon_name, tip, checkable=False, size=None, parent=None):
        super().__init__(parent)
        self.setProperty("role", "icon")
        self.icon_name = icon_name
        self.setCheckable(checkable)
        side = size or tokens.CONTROL_H
        self.setFixedSize(side, side)
        self.setIconSize(QSize(tokens.ICON, tokens.ICON))
        self.setToolTip(tip)
        self.setAccessibleName(tip.split(" (")[0])
        bind_theme(self, lambda b: b.setIcon(icon(b.icon_name)))

    def set_icon_name(self, name):
        self.icon_name = name
        self.setIcon(icon(name))


class SearchField(QLineEdit):
    """Rounded search box with a leading magnifier and a clear button."""

    def __init__(self, placeholder="Search", parent=None):
        super().__init__(parent)
        self.setProperty("role", "search")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.setAccessibleName(placeholder)
        self._glass = self.addAction(icon("search", tokens.COLORS["muted"]),
                                     QLineEdit.ActionPosition.LeadingPosition)
        bind_theme(self, lambda f: f._glass.setIcon(icon("search", tokens.COLORS["muted"])))


class Dropdown(QComboBox):
    """Combo box that ignores the wheel until it has focus.

    Panels scroll; a wheel turn that lands on a drop-down while scrolling must not silently
    change a setting.
    """

    def __init__(self, items=(), parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(tokens.CONTROL_H)
        for item in items:
            label, data = item if isinstance(item, tuple) else (item, item)
            self.addItem(label, data)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class SectionHeader(QWidget):
    """Small heading with an optional right-hand action."""

    def __init__(self, title, action=None, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, tokens.SP1, 0, tokens.SP1)
        self.label = QLabel(title)
        self.label.setProperty("role", "section")
        row.addWidget(self.label)
        row.addStretch(1)
        if action is not None:
            row.addWidget(action)


class Separator(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("role", "separator")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedHeight(1)


def _group_header_style():
    return ("QToolButton { text-align: left; border: 0; background: transparent; "
            "font-weight: 600; padding: 0 4px; }"
            f"QToolButton:hover {{ background: {tokens.COLORS['accent_soft']}; }}")


class _GroupHeader(QToolButton):
    """Full-width, keyboard-focusable disclosure header."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setText(title)
        self.setCheckable(True)
        self.setChecked(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(tokens.SECTION_H)
        bind_theme(self, lambda b: (b.setStyleSheet(_group_header_style()), b.refresh()))
        self.toggled.connect(lambda _on: self.refresh())

    def refresh(self):
        self.setIcon(icon("chevron_down" if self.isChecked() else "chevron_right", tokens.COLORS["muted"]))


class CollapsibleGroup(QWidget):
    """A titled section that folds. ``body_layout`` is where the contents go."""

    toggled = pyqtSignal(bool)

    def __init__(self, title, expanded=True, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = _GroupHeader(title)
        self.header.setAccessibleName(title)
        outer.addWidget(self.header)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(tokens.SP1, tokens.SP1, tokens.SP1, tokens.SP2)
        self.body_layout.setSpacing(tokens.SP2)
        outer.addWidget(self.body)
        self.header.toggled.connect(self._on_toggled)
        self.set_expanded(expanded)

    def _on_toggled(self, on):
        self.body.setVisible(on)
        self.toggled.emit(on)

    def is_expanded(self):
        return self.header.isChecked()

    def set_expanded(self, on):
        self.header.setChecked(bool(on))
        self.body.setVisible(bool(on))


class _Dot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "idle"
        self.setFixedSize(10, 10)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.COLORS[StatusDot.COLORS.get(self.state, "muted")]))
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class StatusDot(QWidget):
    """Coloured dot with a text label. Colour is never the only signal: the label (or the
    tooltip) always says what the state is."""

    COLORS = {"ok": "success", "warn": "warning", "error": "red", "rec": "red", "idle": "muted"}

    def __init__(self, state="idle", text="", parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.SP1 + 2)
        self._dot = _Dot(self)
        self._label = QLabel(text)
        self._label.setProperty("quiet", True)
        row.addWidget(self._dot)
        row.addWidget(self._label)
        self.set_state(state, text)

    def set_state(self, state, text=None):
        self._dot.state = state
        if text is not None:
            self._label.setText(text)
            self._label.setVisible(bool(text))
            self.setToolTip(text)
            self.setAccessibleName(text)
        self._dot.update()


class _Art(QWidget):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(150, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event):
        painter = QPainter(self)
        paint_illustration(painter, self.rect().toRectF(), self.kind)


class EmptyState(QWidget):
    """Illustration, one friendly sentence, and an optional call to action."""

    def __init__(self, title, message="", art="folder", action=None, parent=None):
        super().__init__(parent)
        self.art = art
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.SP4, tokens.SP4, tokens.SP4, tokens.SP4)
        col.setSpacing(tokens.SP2)
        col.addStretch(1)
        self._art = _Art(art)
        col.addWidget(self._art, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = QLabel(title)
        self.title.setProperty("role", "heading")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        col.addWidget(self.title)
        self.message = QLabel(message)
        self.message.setProperty("quiet", True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setVisible(bool(message))
        col.addWidget(self.message)
        if action is not None:
            col.addWidget(action, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addStretch(1)
        # Never force a dock taller: the artwork and message give way when space is short.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        height = event.size().height()
        self._art.setVisible(height >= 230 and event.size().width() >= 190)
        self.message.setVisible(bool(self.message.text()) and height >= 120)

    def set_text(self, title, message=""):
        self.title.setText(title)
        self.message.setText(message)
        self.message.setVisible(bool(message) and self.height() >= 120)
