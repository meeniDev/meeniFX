"""Keyframe Editor multi-selection: marquee, shortcuts, and focused deletion."""

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication

from test_keyframe_ui import editor, pump  # noqa: F401

from meenifx.model import channels as CH
from meenifx.model.keyframes import AnimatedValue

ROW_H = 24


def _clip(window, clip_id=None):
    return window.engine.timeline.find_clip(clip_id or window._test_clip_id)


def _effect_channel(window):
    return CH.effect_channel(_clip(window).effects[0]["effect_id"], "scale")


def _set_keys(window, channel, pairs, clip_id=None):
    """Key one channel of any clip, exactly as the inspector's stopwatch would."""
    clip = _clip(window, clip_id)

    def action():
        target = window.engine.timeline.find_clip(clip.id)
        for frame, value in pairs:
            CH.ensure_key(target, channel, frame)
            CH.set_value(target, channel, float(value), frame, auto_key=False)

    window.engine.session.stack.run("Key test values", action)
    window.engine.session.changed.emit()
    window.keyframe_editor.refresh()
    window.graph_editor.refresh()


def _open(window, qapp):
    window.timeline_panel.canvas._toggle_keyframe_rows(_clip(window))
    pump(qapp, 0.2)
    window.keyframe_editor.fit()
    pump(qapp, 0.1)


def _focus(window, qapp, widget):
    """Give a widget real keyboard focus, as clicking it would in the running app."""
    window.activateWindow()
    qapp.processEvents()
    widget.setFocus()
    qapp.processEvents()


def _lane(panel, channel):
    return next(lane for lane in panel.lanes if lane.channel == channel)


def _mouse(widget, kind, point, modifiers=Qt.KeyboardModifier.NoModifier):
    buttons = (Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease
               else Qt.MouseButton.LeftButton)
    local = QPointF(point)
    global_pos = QPointF(widget.mapToGlobal(QPoint(int(local.x()), int(local.y()))))
    event = QMouseEvent(kind, local, global_pos, Qt.MouseButton.LeftButton,
                        buttons, modifiers)
    {
        QEvent.Type.MouseButtonPress: widget.mousePressEvent,
        QEvent.Type.MouseMove: widget.mouseMoveEvent,
        QEvent.Type.MouseButtonRelease: widget.mouseReleaseEvent,
    }[kind](event)


def _drag_points(panel, lane_a, lane_b):
    """Press/drag points (in lane A coordinates) that box the keys at frames 0..10.

    The press lands in empty space between the keys at frames 10 and 20, well beyond the
    hit radius of either diamond, so it starts a marquee instead of a key drag. Both
    endpoints sit just outside their lane's midline so the box spans every lane between
    them, including the keys on the lane the press started in.
    """
    body_a = lane_a.mapTo(panel.body, QPoint(0, 0))
    body_b = lane_b.mapTo(panel.body, QPoint(0, 0))
    start = QPointF(panel.x_of(15), ROW_H - 2.0)
    end = QPointF(max(0.5, panel.x_of(0) - 6.0),
                  body_b.y() - body_a.y() + 3.0)
    return start, end


def _marquee(panel, lane_a, lane_b, modifiers=Qt.KeyboardModifier.NoModifier):
    start, end = _drag_points(panel, lane_a, lane_b)
    _mouse(lane_a, QEvent.Type.MouseButtonPress, start, modifiers)
    _mouse(lane_a, QEvent.Type.MouseMove,
           QPointF((start.x() + end.x()) / 2.0, (start.y() + end.y()) / 2.0), modifiers)
    _mouse(lane_a, QEvent.Type.MouseMove, end, modifiers)
    _mouse(lane_a, QEvent.Type.MouseButtonRelease, end, modifiers)


def _edit_action(window, text):
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is None or menu.title() != "Edit":
            continue
        for action in menu.actions():
            if action.text() == text:
                return action
    raise AssertionError(f"the Edit menu has no {text!r} action")


def _times(window, channel, clip_id=None):
    stored = CH.stored(_clip(window, clip_id), channel)
    return [key.time for key in stored.keyframes] if isinstance(stored, AnimatedValue) else []


def _box_entries(clip_id, effect, position):
    """Keys inside the box: frames 0 and 10 on both lanes.

    Animating position_x also seeds position_y with a key on the same frame (the two
    axes are a single position), and that key sits between the two boxed lanes.
    """
    return {(clip_id, effect, 0), (clip_id, effect, 10),
            (clip_id, position, 0), (clip_id, position, 10),
            (clip_id, "transform.position_y", 0)}


def test_marquee_replaces_selection_across_lanes(editor, qapp):
    effect = _effect_channel(editor)
    position = "transform.position_x"
    _set_keys(editor, effect, [(0, 100), (10, 60), (20, 40)])
    _set_keys(editor, position, [(0, 0), (10, 20), (20, 40)])
    _open(editor, qapp)
    panel = editor.keyframe_editor
    clip_id = editor._test_clip_id
    editor.engine.session.set_playhead(0)
    editor.engine.session.set_key_selection({(clip_id, effect, 20)})
    pump(qapp, 0.05)

    _marquee(panel, _lane(panel, effect), _lane(panel, position))

    assert editor.engine.session.key_selection == _box_entries(clip_id, effect, position)
    assert editor.engine.session.playhead == 0, "drawing a marquee must not scrub"
    assert panel._marquee is None and panel._marquee_rect is None


def test_marquee_shift_adds_and_ctrl_toggles(editor, qapp):
    effect = _effect_channel(editor)
    position = "transform.position_x"
    _set_keys(editor, effect, [(0, 100), (10, 60), (20, 40)])
    _set_keys(editor, position, [(0, 0), (10, 20), (20, 40)])
    _open(editor, qapp)
    panel = editor.keyframe_editor
    clip_id = editor._test_clip_id
    lane_a, lane_b = _lane(panel, effect), _lane(panel, position)

    editor.engine.session.set_key_selection({(clip_id, effect, 20)})
    _marquee(panel, lane_a, lane_b, Qt.KeyboardModifier.ShiftModifier)
    assert editor.engine.session.key_selection == (
        _box_entries(clip_id, effect, position) | {(clip_id, effect, 20)})

    base = {(clip_id, effect, 0), (clip_id, position, 20)}
    editor.engine.session.set_key_selection(base)
    _marquee(panel, lane_a, lane_b, Qt.KeyboardModifier.ControlModifier)
    assert editor.engine.session.key_selection == (
        _box_entries(clip_id, effect, position) ^ base), \
        "Ctrl must toggle the keys inside the box against the starting selection"


def test_escape_cancels_a_marquee_and_restores_the_previous_selection(editor, qapp):
    effect = _effect_channel(editor)
    position = "transform.position_x"
    _set_keys(editor, effect, [(0, 100), (10, 60), (20, 40)])
    _set_keys(editor, position, [(0, 0), (10, 20), (20, 40)])
    _open(editor, qapp)
    panel = editor.keyframe_editor
    clip_id = editor._test_clip_id
    lane_a, lane_b = _lane(panel, effect), _lane(panel, position)
    base = {(clip_id, effect, 20)}
    editor.engine.session.set_key_selection(base)

    start, end = _drag_points(panel, lane_a, lane_b)
    _mouse(lane_a, QEvent.Type.MouseButtonPress, start)
    _mouse(lane_a, QEvent.Type.MouseMove, end)
    assert editor.engine.session.key_selection != base

    lane_a.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
    assert editor.engine.session.key_selection == base
    assert panel._marquee is None and panel._marquee_rect is None
    _mouse(lane_a, QEvent.Type.MouseButtonRelease, end)
    assert editor.engine.session.key_selection == base


def test_ctrl_a_and_delete_route_to_the_focused_keyframe_editor(editor, qapp):
    effect = _effect_channel(editor)
    position = "transform.position_x"
    _set_keys(editor, effect, [(0, 100), (10, 60)])
    _set_keys(editor, position, [(0, 0), (10, 20)])
    _open(editor, qapp)
    panel = editor.keyframe_editor
    clip_id = editor._test_clip_id
    editor.engine.session.clear_key_selection()
    lane = _lane(panel, effect)
    _focus(editor, qapp, lane)
    assert QApplication.focusWidget() is lane

    _edit_action(editor, "Select All").trigger()
    assert editor.engine.session.key_selection == _box_entries(clip_id, effect, position)

    raw = _clip(editor)
    _edit_action(editor, "Delete").trigger()
    pump(qapp, 0.1)
    assert editor.engine.timeline.find_clip(clip_id) is not None, \
        "a focused keyframe editor must not delete the clip"
    assert _times(editor, effect) == [] and _times(editor, position) == []

    editor.engine.session.undo()
    pump(qapp, 0.1)
    assert _times(editor, effect) == [0, 10]
    assert _times(editor, position) == [0, 10]
    assert editor.engine.timeline.find_clip(raw.id) is not None


def test_ctrl_a_keeps_text_selection_inside_the_value_field(editor, qapp):
    effect = _effect_channel(editor)
    _set_keys(editor, effect, [(0, 100), (10, 60)])
    _open(editor, qapp)
    panel = editor.keyframe_editor
    before = set(editor.engine.session.key_selection)
    line = panel.value_edit.lineEdit()
    line.setText("42")
    _focus(editor, qapp, line)
    line.selectAll()
    qapp.processEvents()

    line.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                                 Qt.KeyboardModifier.ControlModifier, "a"))
    assert line.selectedText().startswith("42")
    assert editor.engine.session.key_selection == before

    _edit_action(editor, "Select All").trigger()
    assert editor.engine.session.key_selection == before

    _edit_action(editor, "Delete").trigger()
    pump(qapp, 0.1)
    assert editor.engine.timeline.find_clip(editor._test_clip_id) is not None
    assert _times(editor, effect) == [0, 10], "typing in the value field keeps its keys"


def test_text_shape_and_effect_keyframes_delete_together_and_undo(editor, qapp):
    engine = editor.engine
    text_id = engine.session.add_text_clip("Animated title", start=0, duration=60)
    text_size = CH.text_channel("size")
    _set_keys(editor, text_size, [(0, 40), (20, 90)], clip_id=text_id)
    shape_id = engine.session.add_shape_clip("star", box=(80, 40, 120, 100))
    shape_width = CH.shape_channel("width")
    _set_keys(editor, shape_width, [(0, 100), (20, 180)], clip_id=shape_id)
    effect = _effect_channel(editor)
    _set_keys(editor, effect, [(0, 100), (20, 50)])

    entries = [(text_id, text_size, 0), (text_id, text_size, 20),
               (shape_id, shape_width, 0), (shape_id, shape_width, 20),
               (editor._test_clip_id, effect, 0), (editor._test_clip_id, effect, 20)]
    assert engine.session.delete_keyframes(entries) == len(entries)
    assert CH.key_times(engine.timeline.find_clip(text_id), text_size) == []
    assert CH.key_times(engine.timeline.find_clip(shape_id), shape_width) == []
    assert CH.key_times(_clip(editor), effect) == []
    engine.session.undo()
    assert CH.key_times(engine.timeline.find_clip(text_id), text_size) == [0, 20]
    assert CH.key_times(engine.timeline.find_clip(shape_id), shape_width) == [0, 20]
    assert CH.key_times(_clip(editor), effect) == [0, 20]
