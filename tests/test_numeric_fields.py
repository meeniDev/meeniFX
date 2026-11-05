"""ScrubField: one numeric behaviour for the whole app.

The widget is driven with real mouse and key events, not by calling its internals, so a
broken hit area, a lost modifier or a gesture that commits twice fails here.
"""

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication

from meenifx.ui.widgets import ScrubField, parse_number

LEFT = Qt.MouseButton.LeftButton
NONE = Qt.MouseButton.NoButton
PLAIN = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier


@pytest.mark.parametrize("text,expected", [
    ("50%", 50.0), ("50 %", 50.0), ("1,5", 1.5), ("1.5", 1.5), (" 12 px ", 12.0),
    ("24 fps", 24.0), ("3 f", 3.0), ("0,75s", 0.75), ("7", 7.0), ("", None), ("abc", None),
    ("-3 dB", -3.0), ("-0,5", -0.5), ("90\u00b0", 90.0), ("-24 px", -24.0),
])
def test_parse_number_accepts_french_units_and_signs(text, expected):
    assert parse_number(text) == expected


# --------------------------------------------------------------------- mouse helpers
def _send(widget, kind, x, modifiers=PLAIN, buttons=LEFT):
    """One mouse event over the value, at height/2."""
    y = widget.height() // 2
    event = QMouseEvent(kind, QPointF(x, y), QPointF(widget.mapToGlobal(QPoint(int(x), y))),
                        LEFT, buttons, modifiers)
    QApplication.sendEvent(widget, event)


def scrub(field, dx, modifiers=PLAIN):
    """Drag the value sideways the way a user does: press, some moves, release."""
    widget = field.value_widget
    start = widget.width() // 2
    _send(widget, QEvent.Type.MouseButtonPress, start, modifiers)
    for step in range(1, 5):
        _send(widget, QEvent.Type.MouseMove, start + round(dx * step / 4), modifiers)
    _send(widget, QEvent.Type.MouseButtonRelease, start + dx, modifiers, buttons=NONE)


def click(field, x=None, modifiers=PLAIN):
    widget = field.value_widget
    x = widget.width() // 2 if x is None else x
    _send(widget, QEvent.Type.MouseButtonPress, x, modifiers)
    _send(widget, QEvent.Type.MouseButtonRelease, x, modifiers, buttons=NONE)


def press_key(widget, key, modifiers=PLAIN, text=""):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text))


@pytest.fixture()
def field(qapp):
    widget = ScrubField("Position X", 0, -1000, 1000, default=0, step=1, unit="px",
                        decimals=0)
    widget.show()
    return widget


# --------------------------------------------------------------------------- scrubbing
def test_dragging_right_raises_and_dragging_left_lowers(field):
    scrub(field, 20)
    assert field.value() == 20, "right is up, one pixel at a time for a px field"
    scrub(field, -35)
    assert field.value() == -15, "left is down, and below zero is a normal value"


def test_a_whole_drag_is_one_gesture(field):
    started, edits, commits = [], [], []
    field.interaction_started.connect(lambda: started.append(1))
    field.edited.connect(edits.append)
    field.committed.connect(commits.append)
    scrub(field, 40)
    assert started == [1], "the caller takes its undo snapshot exactly once"
    assert len(edits) >= 2, "the preview follows the pointer"
    assert commits == [field.value()], "one release, one undo step"


def test_shift_drags_fine_and_ctrl_drags_coarse(field):
    scrub(field, 20, SHIFT)
    assert field.value() == 2, "Shift is a tenth of the speed"
    field.set_value(0)
    scrub(field, 20, CTRL)
    assert field.value() == 200, "Ctrl is ten times the speed"


def test_alt_lets_a_value_sit_between_the_steps(qapp):
    coarse = ScrubField("Blur", 52, 0, 900, default=0, step=5, decimals=0)
    coarse.show()
    scrub(coarse, 4)
    assert coarse.value() == 70, "a normal drag lands on the field's step grid"
    coarse.set_value(52)
    scrub(coarse, 4, ALT)
    assert coarse.value() == 72, "Alt turns the snapping off"


def test_the_cursor_shows_that_the_value_can_be_scrubbed(field):
    QApplication.sendEvent(field.value_widget, QEvent(QEvent.Type.Enter))
    assert field.value_widget.cursor().shape() == Qt.CursorShape.SizeHorCursor
    QApplication.sendEvent(field.value_widget, QEvent(QEvent.Type.Leave))
    assert field.value_widget.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_hover_arrows_step_the_value_one_step(field):
    widget = field.value_widget
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Enter))
    click(field, x=3)
    assert field.value() == -1, "the left arrow steps down"
    assert not field.box.isVisible(), "the arrow steps instead of opening the box"
    click(field, x=widget.width() - 3)
    assert field.value() == 0


def test_a_slow_drag_never_repeats_a_value(qapp):
    """Sub-step pointer movement is accumulated, not rounded away into duplicate edits."""
    gated = ScrubField("Frames", 0, 0, 4000, default=0, step=100, decimals=0)
    gated.show()
    edits = []
    gated.edited.connect(edits.append)
    scrub(gated, 60)
    assert edits == sorted(set(edits)), f"duplicate or backwards edits: {edits}"


# ------------------------------------------------------------------------- typing
def test_a_click_that_does_not_move_opens_the_typed_box(field):
    click(field)
    assert field.box.isVisible()
    assert field.box.text() == "0", "the box opens on the current value"
    field.box.setFocus()
    assert field.box.selectedText() in ("0", "")
    field.box.setText("42")
    field.box.confirmed.emit()
    assert field.value() == 42
    assert not field.box.isVisible()


def test_typing_negative_and_decimal_values(qapp):
    precise = ScrubField("Rotation", 0, -3600, 3600, default=0, step=0.5, unit="\u00b0",
                         decimals=2)
    precise.show()
    click(precise)
    precise.box.setText("-2.5")
    precise.box.confirmed.emit()
    assert precise.value() == -2.5
    assert precise.value_widget.number_text() == "-2.5"


def test_a_typed_value_lands_exactly_and_commits_once(qapp):
    coarse = ScrubField("Opacity", 50, 0, 100, default=100, step=5, unit="%", decimals=0)
    coarse.show()
    edits, commits = [], []
    coarse.edited.connect(edits.append)
    coarse.committed.connect(commits.append)
    click(coarse)
    coarse.box.setText("42")
    coarse.box.confirmed.emit()
    assert coarse.value() == 42, "typing is the precise way in, so it is not snapped"
    assert edits == [42] and commits == [42]


def test_out_of_range_input_is_clamped(field):
    field.box.setText("5000")
    field.box.confirmed.emit()
    assert field.value() == 1000


def test_unparsable_input_keeps_the_current_value(field):
    field.set_value(60)
    field.box.setText("abc")
    field.box.confirmed.emit()
    assert field.value() == 60


def test_escape_restores_the_previous_value(field):
    field.set_value(60)
    click(field)
    field.box.setText("99")
    field.box.cancelled.emit()
    assert field.value() == 60
    assert field.box.text() == "60"
    assert not field.box.isVisible()


def test_typing_a_digit_on_the_focused_value_starts_the_box(field):
    widget = field.value_widget
    widget.setFocus()
    press_key(widget, Qt.Key.Key_7, text="7")
    assert field.box.isVisible()
    assert field.box.text() == "7", "the digit replaces the old value"


# ----------------------------------------------------------------------- keyboard
def test_arrow_keys_step_by_the_field_step(field):
    widget = field.value_widget
    widget.setFocus()
    press_key(widget, Qt.Key.Key_Right)
    press_key(widget, Qt.Key.Key_Up)
    assert field.value() == 2
    press_key(widget, Qt.Key.Key_Left)
    press_key(widget, Qt.Key.Key_Down)
    assert field.value() == 0
    press_key(widget, Qt.Key.Key_PageUp)
    assert field.value() == 10, "Page Up is a ten-step jump"
    press_key(widget, Qt.Key.Key_PageDown)
    assert field.value() == 0


def test_fine_keyboard_steps_need_a_finer_precision(qapp):
    precise = ScrubField("Scale", 100, 0, 1000, default=100, step=1, unit="%", decimals=2)
    precise.show()
    precise.value_widget.setFocus()
    press_key(precise.value_widget, Qt.Key.Key_Right, SHIFT)
    assert precise.value() == pytest.approx(100.1)


def test_double_click_on_the_label_resets_to_the_default(field):
    field.set_value(12)
    field.reset_to_default()
    assert field.value() == 0


# -------------------------------------------------------------------------- display
def test_the_value_shows_its_unit_without_a_slider(field):
    assert field.value_widget.number_text() == "0"
    assert field.value_widget.unit_text() == " px"
    assert not hasattr(field, "slider"), "the long slider is gone"


def test_a_model_value_is_shown_without_being_snapped_or_emitted(field):
    edits = []
    field.edited.connect(edits.append)
    field.set_value(37.4)
    assert field.value() == 37, "an integer field shows whole pixels"
    assert edits == [], "following the model must never look like a user edit"
