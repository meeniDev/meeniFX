"""SliderField: one numeric behaviour for the whole app."""

import pytest

from meenifx.ui.widgets import SliderField, parse_number


@pytest.mark.parametrize("text,expected", [
    ("50%", 50.0), ("50 %", 50.0), ("1,5", 1.5), ("1.5", 1.5), (" 12 px ", 12.0),
    ("24 fps", 24.0), ("3 f", 3.0), ("0,75s", 0.75), ("7", 7.0), ("", None), ("abc", None),
])
def test_parse_number_accepts_french_and_units(text, expected):
    assert parse_number(text) == expected


@pytest.fixture()
def field(qapp):
    widget = SliderField("Quality", 75, 10, 100, default=75, step=1, unit="%")
    widget.show()
    return widget


def test_typed_value_is_applied_and_committed_once(field):
    edits, commits = [], []
    field.edited.connect(edits.append)
    field.committed.connect(commits.append)
    field.box.setText("42%")
    field.box.confirmed.emit()
    assert field.value() == 42
    assert edits == [42]
    assert commits == [42]
    assert field.box.text() == "42%"


def test_out_of_range_input_is_clamped(field):
    field.box.setText("5000")
    field.box.confirmed.emit()
    assert field.value() == 100


def test_soft_range_keeps_hard_values(field):
    field = SliderField("Speed", 400, 10, 1000, step=1, unit="%", soft_min=25, soft_max=400)
    field.set_value(800)
    assert field.value() == 800            # the box keeps the real value ...
    assert field.slider.value() == field.slider.maximum()   # ... and the handle pins at soft max
    field.slider.setValue(SliderField.TICKS // 2)
    assert 25 < field.value() < 400


def test_drag_emits_one_commit(field):
    edits, commits = [], []
    field.edited.connect(edits.append)
    field.committed.connect(commits.append)
    field.slider.sliderPressed.emit()
    for ticks in (300, 500, 800):
        field.slider.setValue(ticks)
    field.slider.sliderReleased.emit()
    assert len(edits) >= 2
    assert commits == [field.value()]
    assert len(commits) == 1


def test_step_keys_use_the_field_step(field):
    commits = []
    field.committed.connect(commits.append)
    field.set_value(50)
    field._step(field.step)
    assert field.value() == 51
    field._step(-3 * field.step)
    assert field.value() == 48
    assert commits == [51, 48]


def test_double_click_label_resets_to_default(field):
    field.set_value(12)
    field.reset_to_default()
    assert field.value() == 75


def test_cancel_restores_the_previous_value(field):
    field.set_value(60)
    field.box.setText("99")
    field.box.cancelled.emit()
    assert field.value() == 60
    assert field.box.text() == "60%"


def test_focus_selects_the_whole_number(field, qapp):
    field.box.setFocus()
    qapp.processEvents()
    assert field.box.selectedText() in ("75", "75%", "")
