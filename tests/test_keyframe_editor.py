"""Dedicated Keyframe Editor: timing, values, group moves and Graph Editor sync."""

import time

import pytest
from PyQt6.QtCore import Qt

from test_keyframe_ui import editor, pump  # noqa: F401  (shared real-window fixture)

from meenifx import crashlog
from meenifx.model import channels as CH
from meenifx.model import presets as P
from meenifx.model.keyframes import AnimatedValue


def _clip(window):
    return window.engine.timeline.find_clip(window._test_clip_id)


def _effect_channel(window):
    return CH.effect_channel(_clip(window).effects[0]["effect_id"], "scale")


def _set_keys(window, channel, pairs):
    """Key a channel in the engine, exactly as the inspector's stopwatch would."""
    clip = _clip(window)

    def action():
        target = window.engine.timeline.find_clip(clip.id)
        for frame, value in pairs:
            CH.ensure_key(target, channel, frame)
            CH.set_value(target, channel, float(value), frame, auto_key=False)

    window.engine.session.stack.run("Key test values", action)
    window.engine.session.changed.emit()
    window.keyframe_editor.refresh()
    window.graph_editor.refresh()


def _times(window, channel):
    stored = CH.stored(_clip(window), channel)
    return [key.time for key in stored.keyframes] if isinstance(stored, AnimatedValue) else []


def _open(window):
    window.timeline_panel.canvas._toggle_keyframe_rows(_clip(window))
    pump(window.qt_app if hasattr(window, "qt_app") else None, 0.15)


def _row(panel, channel):
    return next(row for row in panel.rows if row["id"] == channel)


def test_editor_opens_with_readable_rows_groups_and_paints(editor, qapp):
    channel = _effect_channel(editor)
    _set_keys(editor, channel, [(0, 100), (20, 50)])
    _open(editor)
    panel = editor.keyframe_editor
    assert editor.docks["keyframe"].isVisible()
    ids = [row["id"] for row in panel.rows]
    assert "transform.position_x" in ids and channel in ids
    assert "Transform" in panel._group_buttons
    assert panel.lanes and panel.grab().width() > 0

    group_rows = panel._group_rows["Transform"]
    panel._toggle_group("Transform", panel._group_buttons["Transform"])
    assert all(widget.isHidden() for widgets in group_rows for widget in widgets)
    panel._toggle_group("Transform", panel._group_buttons["Transform"])
    assert all(not widget.isHidden() for widgets in group_rows for widget in widgets)

    modes = {button.text() for button in panel.mode_buttons}
    assert modes == {"Keyframe Editor", "Graph Editor"}


def test_add_and_value_edit_are_undoable(editor, qapp):
    channel = _effect_channel(editor)
    _set_keys(editor, channel, [(0, 100)])
    _open(editor)
    panel = editor.keyframe_editor
    panel._set_active_channel(channel)
    editor.engine.session.set_playhead(10)
    pump(qapp, 0.1)

    assert panel.add_at_playhead() == 1
    stored = CH.stored(_clip(editor), channel)
    assert stored.key_at(10) is not None
    assert (editor._test_clip_id, channel, 10) in editor.engine.session.key_selection

    panel.value_edit.setValue(23)
    assert panel.apply_value()
    pump(qapp, 0.1)
    assert CH.stored(_clip(editor), channel).key_at(10).value == pytest.approx(23)

    editor.engine.session.undo()
    pump(qapp, 0.1)
    assert CH.stored(_clip(editor), channel).key_at(10).value == pytest.approx(100)


def test_drag_moves_one_selected_key_without_duplicating_it(editor, qapp):
    channel = _effect_channel(editor)
    _set_keys(editor, channel, [(0, 100), (20, 50)])
    _open(editor)
    panel = editor.keyframe_editor
    panel.fit()
    editor.engine.session.set_key_selection({(editor._test_clip_id, channel, 0)})
    pump(qapp, 0.1)
    row = _row(panel, channel)
    rows_object = id(panel.rows)
    before = editor.qt_undo_stack.undo_stack.count()

    panel.begin_key_drag(row, 0, panel.x_of(0), Qt.KeyboardModifier.NoModifier)
    panel.drag_to(panel.x_of(10))
    assert sorted(_times(editor, channel)) == [10, 20], \
        "during the drag the old key is gone and only one key exists at the new time"
    assert id(panel.rows) == rows_object, "dragging must not rebuild the editor"
    panel.end_key_drag()
    pump(qapp, 0.2)

    assert sorted(_times(editor, channel)) == [10, 20]
    assert len(_times(editor, channel)) == 2
    assert editor.qt_undo_stack.undo_stack.count() == before + 1, \
        "one drag is one undo step"
    assert (editor._test_clip_id, channel, 10) in editor.engine.session.key_selection

    editor.engine.session.undo()
    pump(qapp, 0.15)
    assert sorted(_times(editor, channel)) == [0, 20]


def test_multi_row_selection_moves_copy_paste_and_duplicate_together(editor, qapp):
    channel = _effect_channel(editor)
    position = "transform.position_x"
    _set_keys(editor, channel, [(0, 100), (10, 60), (20, 40)])
    _set_keys(editor, position, [(0, 0), (10, 20), (20, 40)])
    _open(editor)
    panel = editor.keyframe_editor
    panel.fit()
    clip_id = editor._test_clip_id
    selection = {(clip_id, channel, 0), (clip_id, channel, 10),
                 (clip_id, position, 0), (clip_id, position, 10)}
    editor.engine.session.set_key_selection(selection)
    pump(qapp, 0.1)

    panel.begin_key_drag(_row(panel, channel), 0, panel.x_of(0),
                         Qt.KeyboardModifier.NoModifier)
    panel.drag_to(panel.x_of(5))
    panel.end_key_drag()
    pump(qapp, 0.2)
    assert _times(editor, channel) == [5, 15, 20]
    assert _times(editor, position) == [5, 15, 20]

    assert panel.copy_selected() == 4
    editor.engine.session.set_playhead(40)
    assert panel.paste_selected() == 2, "the active channel receives its copied keys"
    assert 40 in _times(editor, channel) and 50 in _times(editor, channel)

    editor.engine.session.set_key_selection({(clip_id, channel, 40)})
    assert panel.duplicate_selected() >= 1
    assert 41 in _times(editor, channel)


def test_graph_and_keyframe_modes_share_selection_and_easing(editor, qapp):
    channel = _effect_channel(editor)
    _set_keys(editor, channel, [(0, 100), (20, 50)])
    _open(editor)
    panel = editor.keyframe_editor
    clip_id = editor._test_clip_id
    editor.engine.session.set_key_selection({(clip_id, channel, 0)})
    pump(qapp, 0.1)

    panel.graph_mode_button.click()
    pump(qapp, 0.2)
    assert editor.graph_editor.selection == {(channel, 0)}
    assert editor.docks["graph"].isVisible()
    assert editor.graph_editor.graph_mode_button.isChecked()
    assert not editor.keyframe_editor.keyframe_mode_button.isChecked()

    editor.graph_editor.apply_preset("Ease In")
    pump(qapp, 0.1)
    stored = CH.stored(_clip(editor), channel)
    assert stored.key_at(0).interpolation != "linear"

    editor.graph_editor.keyframe_mode_button.click()
    pump(qapp, 0.2)
    assert editor.docks["keyframe"].isVisible()
    assert editor.keyframe_editor.keyframe_mode_button.isChecked()
    assert editor.keyframe_editor.selected_entries() == [(clip_id, channel, 0)]


def test_baked_bounce_shows_y_movement_and_opacity_in_both_editors(editor, qapp):
    clip = _clip(editor)
    instance = P.add(clip, "bounce", "in", program="bounce", args={"px": 1.0},
                     knobs={"unit": "whole"}, duration=30, name="Bounce")
    before = [P.evaluate(clip, frame, project_size=(640, 360)).pose for frame in range(31)]
    written = P.bake(clip, instance, project_size=(640, 360))
    assert "transform.position_y" in written and "transform.opacity" in written
    _open(editor)
    panel = editor.keyframe_editor
    panel.refresh()
    editor.graph_editor.refresh()
    ids = [row["id"] for row in panel.rows]
    assert "transform.position_y" in ids and "transform.opacity" in ids
    assert "transform.position_y" in [row["id"] for row in editor.graph_editor.channels()]
    show = _row(panel, "transform.position_y")
    assert show["animated"] and len(CH.key_records(_clip(editor), show["id"])) > 1
    for frame, pose in enumerate(before):
        assert CH.value_at(_clip(editor), "transform.position_y", frame) == \
            pytest.approx(pose.dy, abs=0.01)
        assert CH.value_at(_clip(editor), "transform.opacity", frame) == \
            pytest.approx(pose.opacity * 100.0, abs=0.01)
    assert CH.value_at(_clip(editor), "transform.position_y", 0) < \
        CH.value_at(_clip(editor), "transform.position_y", 30) - 20.0


def test_pan_zoom_fit_and_previous_next_navigation(editor, qapp):
    channel = _effect_channel(editor)
    _set_keys(editor, channel, [(0, 100), (15, 75), (30, 50)])
    _open(editor)
    panel = editor.keyframe_editor
    panel._set_active_channel(channel)
    panel.fit()
    original_zoom = panel.ppf
    panel.wheel_zoom(type("Wheel", (), {
        "angleDelta": lambda self: type("Delta", (), {"y": lambda self: 120})(),
        "modifiers": lambda self: Qt.KeyboardModifier.ControlModifier,
        "position": lambda self: type("Pos", (), {"x": lambda self: 120})(),
    })())
    assert panel.ppf > original_zoom
    panel.begin_pan(100)
    panel.pan_to(160)
    panel.end_pan()
    panel.fit()
    assert panel.x_of(0) >= 0

    editor.engine.session.set_playhead(10)
    assert panel.next_key()
    assert editor.engine.session.playhead == 15
    assert panel.next_key()
    assert editor.engine.session.playhead == 30
    assert panel.previous_key()
    assert editor.engine.session.playhead == 15


def test_dragging_a_keyframe_rebuilds_values_from_the_drag_snapshot(editor, qapp):
    """Regression: a second mouse move must not add a new key or jump the selection."""
    channel = _effect_channel(editor)
    _set_keys(editor, channel, [(0, 100), (20, 50)])
    _open(editor)
    panel = editor.keyframe_editor
    panel.fit()
    editor.engine.session.set_key_selection({(editor._test_clip_id, channel, 0)})
    pump(qapp, 0.05)
    row = _row(panel, channel)
    panel.begin_key_drag(row, 0, panel.x_of(0), Qt.KeyboardModifier.NoModifier)
    for target in (3, 6, 9, 12):
        panel.drag_to(panel.x_of(target))
    assert _times(editor, channel) == [12, 20]
    panel.end_key_drag()
    assert _times(editor, channel) == [12, 20]


def test_painting_every_editor_surface_raises_nothing(editor, qapp, monkeypatch):
    """Regression: paint handlers are wrapped by ``crashlog.guard``, so a NameError inside
    ``paintEvent`` is logged and swallowed instead of failing a test. Paint every surface
    explicitly and assert nothing was recorded - this is how the missing ``QPointF`` import
    in the ruler and the lane diamonds reached a release."""
    channel = _effect_channel(editor)
    _set_keys(editor, channel, [(0, 100), (20, 50)])
    _open(editor)
    panel = editor.keyframe_editor
    panel.fit()
    pump(qapp, 0.05)

    problems = []
    monkeypatch.setattr(crashlog, "record", lambda *args, **kwargs: problems.append(args))
    crashlog.reset()

    assert panel.ruler.grab().width() > 0
    for lane in panel.lanes:
        assert lane.grab().width() > 0
        lane._hover = lane._records()[0][0] if lane._records() else None
        assert lane.grab().width() > 0
    lane._hover = None

    assert not problems, problems
