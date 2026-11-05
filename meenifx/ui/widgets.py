"""The one numeric editor: a compact scrubbable value with a typed-entry mode.

Every number in the app is edited through a ``ScrubField``, so a value behaves the same in
the Transform card, a text property or an effect parameter: it shows a compact number with
its unit, dragging it sideways scrubs it (right = up, left = down), a click types an exact
value, double-click does the same thing, and the arrow keys step it. Shift drags fine, Ctrl
drags coarse, and Alt turns the step snapping off so a value can land between steps.

A gesture emits ``edited`` continuously (the live preview follows it) and exactly one
``committed`` when it ends, which is what makes a whole scrub a single undo step;
``interaction_started`` fires before the first change so the caller can snapshot the model.
Typing is one gesture too: the value is applied on Enter (or when the box loses focus) and
Escape puts the previous value back, so a half-typed number never reaches the project.
"""

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                             QWidget)

from ..tokens import COLORS, CONTROL_H, RADIUS

# Unit spellings the parser accepts after a number ("50%", "24 fps", "12 px", "-3 dB").
_UNITS = ("%", "px", "fps", "frames", "px/f", "deg", "dB", "\u00b0", "s", "f", "\u00d7", "x")


def format_number(value, decimals=0):
    """Compact text for a value: 40, 1.5, 0.25 - never 40.0 for an integer field."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if decimals <= 0:
        return str(int(round(number)))
    text = f"{number:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def parse_number(text):
    """Parse user input: accepts "50%", "1,5", "12 px", "  7 ". Returns None if unusable."""
    if text is None:
        return None
    cleaned = str(text).strip().replace("\u00a0", " ").replace(",", ".")
    if not cleaned:
        return None
    for unit in sorted(_UNITS, key=len, reverse=True):
        if cleaned.lower().endswith(unit.lower()):
            cleaned = cleaned[: -len(unit)].strip()
            break
    cleaned = cleaned.replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


class NumberEdit(QLineEdit):
    """Number box: select all on focus, arrows step, Enter applies, Esc cancels."""

    step_up = pyqtSignal(int)
    step_down = pyqtSignal(int)
    confirmed = pyqtSignal()
    cancelled = pyqtSignal()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # Clicking the number selects it, so typing replaces it in one gesture.
        self.selectAll()

    def mouseDoubleClickEvent(self, event):
        super().mouseDoubleClickEvent(event)
        self.selectAll()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.confirmed.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            # The field decides the size of the step from the modifiers, so a step here
            # is only a direction.
            (self.step_up if key == Qt.Key.Key_Up else self.step_down).emit(1)
            event.accept()
            return
        super().keyPressEvent(event)


class _ScrubValue(QWidget):
    """The painted number: it is the handle. Drag to scrub, click to type, arrows to step.

    Kept separate from ``ScrubField`` because this widget owns all of the value maths -
    clamping, snapping, precision - and the field around it is only the label and the row.
    """

    edited = pyqtSignal(object)         # live, on every change of the quantized value
    committed = pyqtSignal(object)      # once per gesture, for a single undo step
    interaction_started = pyqtSignal()   # before the first change of a gesture

    DRAG_THRESHOLD = 3      # px of travel before a press becomes a scrub instead of a click
    ARROW_W = 10            # reserve beside the number for the hover arrows
    PAD = 4
    TIGHT_UNITS = ("%", "\u00b0")   # no space before these, like every other editor

    def __init__(self, value, minimum, maximum, *, default=None, step=1, unit="",
                 decimals=0, width=92, drag_step=None, parent=None):
        super().__init__(parent)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.step = float(step) if step else 0.0
        self.decimals = int(decimals)
        self.unit = (unit or "").strip()
        self.default = float(default) if default is not None else float(value)
        self.drag_step = float(drag_step) if drag_step else None
        self._value = self._clamp(value)
        self._hover = False
        self._dragging = False
        self._editing = False
        self._press = None
        self._drag_from = 0
        self._last_x = 0
        self._raw = 0.0
        self._arrow_hold = 0
        self._edit_before = None

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setFixedWidth(width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.edit = NumberEdit(self)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.edit.hide()
        self.edit.confirmed.connect(lambda: self._commit_text(keep_focus=True))
        self.edit.cancelled.connect(self._cancel_text)
        self.edit.editingFinished.connect(lambda: self._commit_text(keep_focus=False))
        self.edit.step_up.connect(lambda _direction: self.step_by(1))
        self.edit.step_down.connect(lambda _direction: self.step_by(-1))

    # ------------------------------------------------------------------ geometry
    def sizeHint(self):
        return QSize(self.width(), max(CONTROL_H - 6, QFontMetrics(self.font()).height() + 5))

    def minimumSizeHint(self):
        return self.sizeHint()

    # -------------------------------------------------------------------- value
    def _default_or(self, fallback):
        default = getattr(self, "default", None)
        return default if isinstance(default, float) else fallback

    def _clamp(self, value):
        """Clamp into range, treating anything unusable as the neutral value.

        A parameter can hold a value the widget cannot show: an older project with a
        string where a number belongs, or a NaN written by a bad curve. Raising here
        used to abort the whole application (the caller runs from a model-change slot),
        so an unusable value now reads as the parameter's neutral value instead.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = self._default_or(self.minimum)
        if number != number:                                   # NaN
            number = self._default_or(self.minimum)
        if number in (float("inf"), float("-inf")):
            number = self.maximum if number > 0 else self.minimum
        return max(self.minimum, min(self.maximum, number))

    def _increment(self):
        """Smallest change the display can show: 1, 0.1 or 0.01 depending on precision."""
        return 10.0 ** -self.decimals

    def _step_amount(self):
        return max(abs(self.step), self._increment())

    def _quantize(self, value, snap=True):
        value = self._clamp(value)
        if snap and self.step > 0:
            value = self._clamp(round(value / self.step) * self.step)
        return round(value, self.decimals) if self.decimals else int(round(value))

    @staticmethod
    def _multiplier(modifiers):
        """Shift = fine, Ctrl = coarse. Both together cancel out to normal speed."""
        factor = 1.0
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            factor *= 0.1
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            factor *= 10.0
        return factor

    @staticmethod
    def _snapping(modifiers):
        """Alt (or Shift for a fine drag) lets a value land between the field's steps."""
        return not bool(modifiers & (Qt.KeyboardModifier.ShiftModifier
                                     | Qt.KeyboardModifier.AltModifier))

    def value(self):
        return self._value

    def set_value(self, value, emit=False):
        """Update from the model/playhead without re-triggering an edit."""
        new = self._quantize(value, snap=False)
        if new == self._value:
            return
        self._value = new
        self.update()
        if emit:
            self.edited.emit(new)

    def step_by(self, steps=1.0, modifiers=None):
        """One arrow press / key step: a complete, undoable gesture on its own."""
        modifiers = QApplication.keyboardModifiers() if modifiers is None else modifiers
        amount = self._step_amount() * float(steps) * self._multiplier(modifiers)
        new = self._quantize(self._value + amount, snap=self._snapping(modifiers))
        if new == self._value:
            return
        self.interaction_started.emit()
        self._value = new
        self.update()
        self.edited.emit(new)
        self.committed.emit(new)

    # -------------------------------------------------------------------- text
    def number_text(self):
        return format_number(self._value, self.decimals)

    def unit_text(self):
        if not self.unit:
            return ""
        return self.unit if self.unit in self.TIGHT_UNITS else " " + self.unit

    # ------------------------------------------------------------------- paint
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 1, -1, -2)
        if self._hover or self._editing or self.hasFocus():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["panel_alt"] if self._dragging else COLORS["panel_soft"]))
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
        if self._editing:
            return
        metrics = QFontMetrics(self.font())
        unit = self.unit_text()
        number = self.number_text()
        unit_w = metrics.horizontalAdvance(unit) if unit else 0
        number_w = metrics.horizontalAdvance(number)
        right = rect.right() - self.PAD - self.ARROW_W - unit_w
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(QRect(right - number_w - 1, rect.top(), number_w + 2, rect.height()),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), number)
        if unit:
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRect(right + 1, rect.top(), unit_w + 1, rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), unit)
        if self._hover and not self._dragging:
            painter.setPen(QPen(QColor(COLORS["accent"] if self._arrow_hold else COLORS["muted"]),
                                1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            middle = rect.center().y() + 1
            self._chevron(painter, rect.left() + self.PAD + 2, middle, -1)
            self._chevron(painter, rect.right() - self.PAD - 2, middle, 1)

    @staticmethod
    def _chevron(painter, x, y, direction):
        """A small '>' (direction 1) or '<' (-1), drawn as two strokes."""
        arm = 3
        painter.drawLine(x + arm * direction, y - arm, x - arm * direction, y)
        painter.drawLine(x - arm * direction, y, x + arm * direction, y + arm)

    # ------------------------------------------------------------------- mouse
    def _arrow_at(self, point):
        if not self._hover:
            return 0
        if point.x() <= self.ARROW_W + self.PAD:
            return -1
        if point.x() >= self.width() - self.ARROW_W - self.PAD:
            return 1
        return 0

    def enterEvent(self, event):
        self._hover = True
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.unsetCursor()
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = event.position().toPoint()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._press = point
        self._drag_from = point.x()
        self._last_x = point.x()
        self._raw = float(self._value)
        self._dragging = False
        arrow = self._arrow_at(point)
        if arrow:
            self._arrow_hold = arrow
            self.step_by(arrow, event.modifiers())
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        if self._arrow_hold:
            event.accept()
            return
        point = event.position().toPoint()
        if not self._dragging:
            if abs(point.x() - self._press.x()) < self.DRAG_THRESHOLD:
                return
            self._dragging = True
            self._last_x = self._press.x()
            self._raw = float(self._value)
            self.interaction_started.emit()
        rate = self.drag_step if self.drag_step else self._step_amount()
        rate *= self._multiplier(event.modifiers())
        self._raw += (point.x() - self._last_x) * rate
        self._last_x = point.x()
        new = self._quantize(self._raw, snap=self._snapping(event.modifiers()))
        if new == self._value:
            event.accept()
            return
        self._value = new
        self.update()
        self.edited.emit(new)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        was_dragging = self._dragging
        was_arrow = bool(self._arrow_hold)
        self._dragging = False
        self._arrow_hold = 0
        self._press = None
        self.update()
        if was_dragging:
            self.committed.emit(self._value)
        elif not was_arrow:
            # A click that never moved is the "type a value" gesture; an arrow click has
            # already stepped the value and must not open the box under the pointer.
            self.begin_edit()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.begin_edit()
        event.accept()

    # ---------------------------------------------------------------- keyboard
    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2):
            self.begin_edit()
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self.step_by(-1, modifiers)
            event.accept()
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self.step_by(1, modifiers)
            event.accept()
            return
        if key == Qt.Key.Key_PageDown:
            self.step_by(-10, modifiers)
            event.accept()
            return
        if key == Qt.Key.Key_PageUp:
            self.step_by(10, modifiers)
            event.accept()
            return
        text = event.text()
        if text and (text[0].isdigit() or text[0] in "+-.,"):
            # Typing a digit goes straight into the box, so a value can be replaced
            # without aiming at it first.
            self.begin_edit()
            self.edit.insert(text)
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ typed entry
    def begin_edit(self):
        if self._editing:
            return
        self._editing = True
        self._edit_before = self._value
        self.edit.setText(self.number_text())
        self.edit.setGeometry(self.rect())
        self.edit.show()
        self.edit.raise_()
        self.edit.setFocus(Qt.FocusReason.MouseFocusReason)
        self.edit.selectAll()
        self.update()

    def _end_edit(self, keep_focus=False):
        self._editing = False
        self.edit.hide()
        if keep_focus:
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()

    def _commit_text(self, keep_focus=True):
        # Also reachable without the box on screen (a test or a script sets the text and
        # asks for it), so the empty case is the only thing to guard.
        parsed = parse_number(self.edit.text())
        if self._editing:
            self._end_edit(keep_focus=keep_focus)
        if parsed is None:
            return
        # A typed number lands exactly as written: it is the precise way to set a value,
        # so it is never pulled onto the scrubbing grid.
        new = self._quantize(parsed, snap=False)
        if new == self._value:
            return
        self.interaction_started.emit()
        self._value = new
        self.update()
        self.edited.emit(new)
        self.committed.emit(new)

    def _cancel_text(self):
        if self._editing:
            self._end_edit(keep_focus=True)
        if self._edit_before is not None:
            self._value = self._edit_before
        self.edit.setText(self.number_text())
        self.update()

    def reset_to_default(self):
        new = self._quantize(self.default, snap=False)
        if new == self._value:
            return
        self.interaction_started.emit()
        self._value = new
        self.update()
        self.edited.emit(new)
        self.committed.emit(new)


class ScrubField(QWidget):
    """One numeric value in a compact row: a label, the scrubbable value and its unit.

    This is the app's only numeric control. Everything the inspector needs to hang off a
    value (a stopwatch, the keyframe diamonds) is appended with ``add_trailing`` so those
    controls keep one aligned column down the card.
    """

    edited = pyqtSignal(object)         # live: every scrub move / valid typed change
    committed = pyqtSignal(object)      # once per user gesture, for a single undo step
    interaction_started = pyqtSignal()  # first move of a drag / start of a typed edit

    def __init__(self, label, value, minimum, maximum, *, default=None, step=1,
                 unit="", decimals=0, tooltip="", label_width=70, value_width=92,
                 drag_step=None, parent=None):
        super().__init__(parent)
        self.step = float(step) if step else 0.0
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.decimals = int(decimals)
        self.unit = unit or ""
        self.default = float(default) if default is not None else float(value)

        self._label_full = label
        self.label = QLabel(label, self)
        self.label.setFixedWidth(label_width)
        self.label.setToolTip(tooltip or f"{label} (double-click to reset)")
        self.label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.label.mouseDoubleClickEvent = lambda _event: self.reset_to_default()

        self.value_widget = _ScrubValue(value, minimum, maximum, default=self.default,
                                        step=step, unit=unit, decimals=decimals,
                                        width=value_width, drag_step=drag_step, parent=self)
        self.value_widget.setToolTip(tooltip or self._tip(label))
        self.value_widget.interaction_started.connect(self.interaction_started)
        self.value_widget.edited.connect(self.edited)
        self.value_widget.committed.connect(self.committed)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.label)
        row.addStretch(1)
        row.addWidget(self.value_widget)
        self._trailing = row
        self.setToolTip(tooltip or self._tip(label))
        self._elide_label()

    @staticmethod
    def _tip(label):
        return (f"{label}: drag sideways to change, click to type a value, "
                f"arrow keys to step. Shift = fine, Ctrl = coarse, Alt = no snapping")

    # ------------------------------------------------------------------ display
    @property
    def box(self):
        """The typed-entry line edit (hidden until the value is clicked)."""
        return self.value_widget.edit

    def value(self):
        return self.value_widget.value()

    def set_value(self, value, emit=False):
        self.value_widget.set_value(value, emit=emit)

    def set_value_silently(self, value):
        self.value_widget.set_value(value)

    def add_trailing(self, widget):
        """Append a fixed control (stopwatch, keyframe cluster) to the same column."""
        widget.setParent(self)
        self._trailing.addWidget(widget)
        return widget

    def reset_to_default(self):
        self.value_widget.reset_to_default()

    def set_tooltip(self, text):
        for widget in (self, self.label, self.value_widget, self.box):
            widget.setToolTip(text)

    def set_label_text(self, text):
        self._label_full = text
        self._elide_label()

    def _elide_label(self):
        """A fixed label column never grows: long names elide and keep a tooltip."""
        metrics = QFontMetrics(self.label.font())
        text = metrics.elidedText(self._label_full, Qt.TextElideMode.ElideRight,
                                  max(10, self.label.width() - 8))
        self.label.setText(text)
        self.label.setToolTip(self._label_full if text != self._label_full else self.label.toolTip())

    def showEvent(self, event):
        # The stylesheet font is only final once the widget is shown: eliding earlier
        # measured the wrong font and clipped the text without an ellipsis.
        super().showEvent(event)
        self._elide_label()
