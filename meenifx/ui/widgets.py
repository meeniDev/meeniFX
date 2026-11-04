"""Shared numeric editor: label + slider + typed number box (+ optional accent unit).

Every numeric control in the app is a SliderField, so typing behaves the same
everywhere: click the number to select it, Enter applies, Esc cancels, Up/Down step
(Shift x10, Ctrl x0.1), the label resets to the default on double-click, and both
"," and "." are accepted as decimal separators. A drag emits `edited` continuously
and exactly one `committed` on release, which is what makes a drag one undo step.
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QSlider, QSizePolicy, QWidget

# Unit spellings the parser accepts after a number ("50%", "24 fps", "12 px").
_UNITS = ("%", "px", "fps", "s", "f", "frames", "px/f", "×", "x")


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
        if cleaned.lower().endswith(unit.strip().lower()):
            cleaned = cleaned[: -len(unit.strip())].strip()
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
        modifiers = event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.confirmed.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            factor = 10 if modifiers & Qt.KeyboardModifier.ShiftModifier else (
                0.1 if modifiers & Qt.KeyboardModifier.ControlModifier else 1)
            (self.step_up if key == Qt.Key.Key_Up else self.step_down).emit(0 if factor == 0 else 1)
            event.accept()
            return
        super().keyPressEvent(event)


class SliderField(QWidget):
    """One numeric value, edited with a slider, a typed box or the keyboard."""

    edited = pyqtSignal(object)       # live: every slider move / valid typed change
    committed = pyqtSignal(object)    # once per user gesture, for a single undo step
    interaction_started = pyqtSignal()  # first move of a drag / focus into the box

    TICKS = 1000

    def __init__(self, label, value, minimum, maximum, *, default=None, step=1,
                 unit="", decimals=0, soft_min=None, soft_max=None, tooltip="",
                 label_width=82, box_width=64, parent=None):
        super().__init__(parent)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.step = float(step) if step else 1.0
        self.decimals = int(decimals)
        self.unit = unit or ""
        self.default = float(default) if default is not None else float(value)
        self._value = self._clamp(value)
        self._soft_min = self._clamp(minimum if soft_min is None else soft_min)
        self._soft_max = self._clamp(maximum if soft_max is None else soft_max)
        if self._soft_max <= self._soft_min:
            self._soft_min, self._soft_max = self.minimum, self.maximum
        self._editing_box = False
        self._drag_snapshot = None

        self.label = QLabel(label, self)
        self.label.setFixedWidth(label_width)
        self.label.setToolTip(tooltip or f"{label} (double-click to reset)")
        self.label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.label.mouseDoubleClickEvent = lambda _event: self.reset_to_default()
        self._label_full = label
        self._elide_label()

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, self.TICKS)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(20)
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.slider.setToolTip(tooltip or label)
        self.slider.setValue(self._to_ticks(self._value))

        self.box = NumberEdit(self)
        self.box.setFixedWidth(box_width)
        self.box.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.box.setToolTip(tooltip or f"{label}: click to type, Up/Down to step, Enter to apply")
        self.box.setText(self._display(self._value))

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.label)
        row.addWidget(self.slider, 1)
        row.addWidget(self.box)
        self._trailing = row

        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderPressed.connect(self._begin_drag)
        self.slider.sliderReleased.connect(self._end_drag)
        self.slider.sliderMoved.connect(lambda _position: self._raise_if_needed())
        self.box.editingFinished.connect(self._commit_text)
        self.box.confirmed.connect(self._commit_text)
        self.box.cancelled.connect(self._cancel_text)
        self.box.step_up.connect(lambda _n: self._step(self.step))
        self.box.step_down.connect(lambda _n: self._step(-self.step))

    # ------------------------------------------------------------------ display
    def _display(self, value):
        return format_number(value, self.decimals) + self.unit

    def _clamp(self, value):
        """Clamp into range, treating anything unusable as the default.

        A parameter can hold a value the widget cannot show: an older project with a
        string where a number belongs, or a NaN written by a bad curve. Raising here
        used to abort the whole application (the caller runs from a model-change slot),
        so an unusable value now reads as the parameter's neutral value.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = self.default if isinstance(getattr(self, "default", None), float) else self.minimum
        if number != number:                       # NaN
            number = self.default if isinstance(getattr(self, "default", None), float) else self.minimum
        if number in (float("inf"), float("-inf")):
            number = self.maximum if number > 0 else self.minimum
        return max(self.minimum, min(self.maximum, number))

    def _to_ticks(self, value):
        span = max(1e-9, self._soft_max - self._soft_min)
        ratio = (self._clamp(value) - self._soft_min) / span
        return int(round(max(0.0, min(1.0, ratio)) * self.TICKS))

    def _quantize(self, value):
        value = self._clamp(value)
        if self.step > 0:
            value = round(value / self.step) * self.step
            value = self._clamp(value)
        return round(value, max(0, self.decimals)) if self.decimals else int(round(value))

    def value(self):
        return self._value

    def set_value(self, value, emit=False):
        """Update from the model/playhead without re-triggering an edit."""
        new = self._quantize(value)
        if new == self._value:
            return
        self._value = new
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_ticks(new))
        self.slider.blockSignals(False)
        if not self.box.hasFocus():
            self.box.setText(self._display(new))
        if emit:
            self.edited.emit(new)

    def add_trailing(self, widget):
        """Append a fixed control (stopwatch, keyframe cluster) to the same column."""
        widget.setParent(self)
        self._trailing.addWidget(widget)
        return widget

    # ----------------------------------------------------------------- slider
    def _on_slider(self, ticks):
        if self._drag_snapshot is None:
            self._begin_drag()
        span = self._soft_max - self._soft_min
        value = self._quantize(self._soft_min + span * ticks / self.TICKS)
        if value == self._value:
            return
        self._value = value
        self.box.blockSignals(True)
        self.box.setText(self._display(value))
        self.box.blockSignals(False)
        self.edited.emit(value)

    def _begin_drag(self):
        if self._drag_snapshot is None:
            self._drag_snapshot = self._value
            self.interaction_started.emit()

    def _end_drag(self):
        snapshot = self._drag_snapshot
        self._drag_snapshot = None
        if snapshot is not None and snapshot != self._value:
            self.committed.emit(self._value)

    def _raise_if_needed(self):
        if not self.isVisible():
            return

    # ------------------------------------------------------------- typed box
    def _commit_text(self):
        parsed = parse_number(self.box.text())
        if parsed is None:
            self.box.setText(self._display(self._value))
            return
        new = self._quantize(parsed)
        changed = new != self._value
        if changed:
            self.interaction_started.emit()
            self._value = new
            self.slider.blockSignals(True)
            self.slider.setValue(self._to_ticks(new))
            self.slider.blockSignals(False)
            self.edited.emit(new)
        self.box.setText(self._display(self._value))
        if changed:
            self.committed.emit(self._value)

    def _cancel_text(self):
        self.box.setText(self._display(self._value))
        self.box.clearFocus()

    def _step(self, delta):
        new = self._quantize(self._value + delta)
        if new == self._value:
            self.box.setText(self._display(self._value))
            return
        self.interaction_started.emit()
        self._value = new
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_ticks(new))
        self.slider.blockSignals(False)
        self.box.setText(self._display(new))
        self.edited.emit(new)
        self.committed.emit(new)

    def reset_to_default(self):
        if self._quantize(self.default) == self._value:
            return
        self.interaction_started.emit()
        self.set_value(self.default, emit=True)
        self.committed.emit(self._value)

    def set_tooltip(self, text):
        for widget in (self, self.slider, self.box):
            widget.setToolTip(text)
        self.label.setToolTip(text)

    def _elide_label(self):
        """A fixed label column never grows: long names elide and keep a tooltip."""
        metrics = QFontMetrics(self.label.font())
        text = metrics.elidedText(self._label_full, Qt.TextElideMode.ElideRight,
                                  max(10, self.label.width() - 8))
        self.label.setText(text)
        if text != self._label_full:
            slider = getattr(self, "slider", None)
            self.label.setToolTip(f"{self._label_full} - {slider.toolTip()}" if slider else self._label_full)

    def set_label_text(self, text):
        self._label_full = text
        self._elide_label()

    def showEvent(self, event):
        # The stylesheet font is only final once the widget is shown: eliding earlier
        # measured the wrong font and clipped the text without an ellipsis.
        super().showEvent(event)
        self._elide_label()
