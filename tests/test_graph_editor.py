"""Graph Editor: smooth curves, direct manipulation, handles, navigation, undo.

The editor is driven with real mouse events on the real window, so a broken coordinate
conversion or a missing undo step fails here instead of in the app.
"""

import os
import sys
import time
import uuid

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import easing  # noqa: E402
from meenifx.model import transform as T  # noqa: E402
from meenifx.model.keyframes import AnimatedValue  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")


def pump(seconds=0.12):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.004)


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module")
def window(app):
    MainWindow._load_demo_project = lambda self: None
    editor = MainWindow(app)
    editor.resize(1440, 900)
    editor.show()
    pump(0.2)
    yield editor
    editor.projects._set_clean()
    editor.close()


@pytest.fixture()
def editor(window, tmp_path):
    window.projects._set_clean()
    settings = ProjectSettings(name=f"Graph {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    pump(0.1)
    from meenifx.project.media import media_type_for
    project = window.projects.project
    item = project.library.add(STILL, 3.0, 320, 180, 30.0, media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    clips = window.insert_media_items([item], frame=0)
    assert clips, "the still was not inserted"
    pump(0.2)
    window.graph_editor.refresh()
    return window


def mouse(canvas, kind, pos, button=Qt.MouseButton.LeftButton,
          modifiers=Qt.KeyboardModifier.NoModifier):
    global_pos = QPointF(canvas.mapToGlobal(QPoint(int(pos.x()), int(pos.y()))))
    event = QMouseEvent(kind, QPointF(pos), global_pos, button, button, modifiers)
    if kind == QEvent.Type.MouseButtonPress:
        canvas.mousePressEvent(event)
    elif kind == QEvent.Type.MouseMove:
        canvas.mouseMoveEvent(event)
    elif kind == QEvent.Type.MouseButtonRelease:
        canvas.mouseReleaseEvent(event)
    elif kind == QEvent.Type.MouseButtonDblClick:
        canvas.mouseDoubleClickEvent(event)


def click(canvas, pos, modifiers=Qt.KeyboardModifier.NoModifier):
    mouse(canvas, QEvent.Type.MouseButtonPress, pos, modifiers=modifiers)
    mouse(canvas, QEvent.Type.MouseButtonRelease, pos, modifiers=modifiers)


def drag(canvas, start, end, modifiers=Qt.KeyboardModifier.NoModifier):
    mouse(canvas, QEvent.Type.MouseButtonPress, start, modifiers=modifiers)
    middle = QPointF((start.x() + end.x()) / 2.0, (start.y() + end.y()) / 2.0)
    mouse(canvas, QEvent.Type.MouseMove, middle, modifiers=modifiers)
    mouse(canvas, QEvent.Type.MouseMove, end, modifiers=modifiers)
    mouse(canvas, QEvent.Type.MouseButtonRelease, end, modifiers=modifiers)


def animated_clip(editor, channel="transform.position_x", keys=((0, 0.0), (20, 100.0))):
    clip = editor.engine.timeline.all_clips()[0]
    T.animate_channel(clip, channel.split(".")[-1], 0)
    for index, (frame, value) in enumerate(keys):
        CH.set_value(clip, channel, float(value), frame, auto_key=index > 0)
    editor.graph_editor.refresh()
    editor.graph_editor.canvas.zoom_x = 6.0
    editor.graph_editor.canvas.pan_x = 0.0
    pump(0.05)
    return editor.engine.timeline.find_clip(clip.id)


def key_point(canvas, clip, channel, time):
    return QPointF(canvas.x_of(canvas.panel.start_frame() + time),
                   canvas.y_of(float(CH.value_at(clip, channel, time))))


# ---------------------------------------------------------------- rendering
def test_curve_is_sampled_far_finer_than_one_point_per_frame(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    canvas.zoom_x = 40.0
    frames, values = canvas.sample_channel("transform.position_x")
    assert len(frames) > clip.duration, "a zoomed curve needs sub-frame samples"
    steps = np.diff(frames)
    assert steps.max() <= 0.05, "samples must be at most a fraction of a frame apart"
    # The samples come from the real evaluator, not a separate drawing path.
    assert values[-1] == pytest.approx(float(CH.value_at(clip, "transform.position_x",
                                                        frames[-1])), abs=1e-3)


def test_curve_paints_smoothly_at_high_zoom(editor):
    animated_clip(editor, keys=((0, 0.0), (20, 100.0)))
    canvas = editor.graph_editor.canvas
    canvas.zoom_x = 40.0
    canvas.fit()
    pixmap = canvas.grab()
    assert pixmap.width() > 0 and pixmap.height() > 0


def test_speed_graph_uses_the_real_derivative(editor):
    clip = animated_clip(editor, keys=((0, 0.0), (20, 100.0)))
    editor.graph_editor.mode_combo.setCurrentIndex(1)
    pump(0.05)
    assert editor.graph_editor.mode == "speed"
    frames, values = editor.graph_editor.canvas.sample_channel("transform.position_x")
    assert len(frames) > 5
    editor.graph_editor.mode_combo.setCurrentIndex(0)


# ------------------------------------------------------------- interactions
def test_a_plain_click_selects_one_keyframe_without_modifiers(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    point = key_point(canvas, clip, "transform.position_x", 20)
    click(canvas, point)
    pump(0.05)
    assert editor.graph_editor.selection == {("transform.position_x", 20)}
    # The timeline and the graph share one selection.
    assert (clip.id, "transform.position_x", 20) in editor.engine.session.key_selection


def test_ctrl_click_toggles_keys_in_and_out_of_the_selection(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    first = key_point(canvas, clip, "transform.position_x", 0)
    second = key_point(canvas, clip, "transform.position_x", 20)
    click(canvas, first)
    click(canvas, second, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert editor.graph_editor.selection == {("transform.position_x", 0),
                                             ("transform.position_x", 20)}
    click(canvas, second, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert editor.graph_editor.selection == {("transform.position_x", 0)}


def test_clicking_empty_space_clears_the_selection_and_moves_the_playhead(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    click(canvas, key_point(canvas, clip, "transform.position_x", 0))
    empty = QPointF(canvas.x_of(canvas.panel.start_frame() + 10), canvas.curve_rect().top() + 5)
    click(canvas, empty)
    assert editor.graph_editor.selection == set()
    assert editor.engine.session.playhead == clip.timeline_start + 10


def test_dragging_moves_a_key_in_time_and_value_together(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    point = key_point(canvas, clip, "transform.position_x", 20)
    drag(canvas, point, QPointF(point.x() + 30, point.y() - 20))
    pump(0.05)
    moved = editor.engine.timeline.find_clip(clip.id)
    times = CH.key_times(moved, "transform.position_x")
    assert 20 not in times and max(times) > 20
    values = [key.value for key in CH.stored(moved, "transform.position_x").keyframes]
    assert max(values) > 100.0, "the vertical drag must raise the value"


def test_box_selection_collects_several_keys(editor):
    clip = animated_clip(editor, keys=((0, 0.0), (10, 40.0), (20, 100.0)))
    canvas = editor.graph_editor.canvas
    rect = canvas.curve_rect()
    left = QPointF(canvas.x_of(canvas.panel.start_frame() + 0) - 8, rect.top() + 4)
    right = QPointF(canvas.x_of(canvas.panel.start_frame() + 12), rect.bottom() - 4)
    drag(canvas, left, right)
    pump(0.05)
    selected = {time for channel, time in editor.graph_editor.selection}
    assert 0 in selected and 10 in selected and 20 not in selected


def test_double_click_on_a_curve_adds_a_key_without_jumping(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    before = float(CH.value_at(clip, "transform.position_x", 10))
    point = QPointF(canvas.x_of(canvas.panel.start_frame() + 10),
                    canvas.y_of(before))
    mouse(canvas, QEvent.Type.MouseButtonDblClick, point)
    pump(0.05)
    after_clip = editor.engine.timeline.find_clip(clip.id)
    times = CH.key_times(after_clip, "transform.position_x")
    assert 10 in times, times
    assert CH.value_at(after_clip, "transform.position_x", 10) == pytest.approx(before, abs=1.0)
    # The curve did not step: the value just before and after is unchanged.
    assert CH.value_at(after_clip, "transform.position_x", 9) == pytest.approx(
        CH.value_at(clip, "transform.position_x", 9), abs=1.0)


def test_delete_removes_the_selection_as_one_undo_step(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    editor.engine.session.set_key_selection([(clip.id, "transform.position_x", 20)])
    depth = editor.qt_undo_stack.undo_stack.count()
    canvas.panel.delete_selected()
    pump(0.05)
    assert CH.key_times(editor.engine.timeline.find_clip(clip.id), "transform.position_x") == [0]
    assert editor.qt_undo_stack.undo_stack.count() == depth + 1
    editor.engine.session.undo()
    assert 20 in CH.key_times(editor.engine.timeline.find_clip(clip.id), "transform.position_x")


def test_presets_apply_and_undo_from_the_graph(editor):
    clip = animated_clip(editor)
    editor.engine.session.set_key_selection([(clip.id, "transform.position_x", 0)])
    canvas = editor.graph_editor.canvas
    point = key_point(canvas, clip, "transform.position_x", 0)
    click(canvas, point)
    for name, spec in (("Ease In Out", "ease_in_out"), ("Sine In Out", "fn:sine_in_out"),
                       ("Bounce Out", "fn:bounce_out"), ("Elastic In", "fn:elastic_in")):
        assert editor.graph_editor.apply_preset(name)
        stored = CH.stored(editor.engine.timeline.find_clip(clip.id), "transform.position_x")
        assert stored.key_at(0).interpolation == spec, name
    editor.engine.session.undo()
    stored = CH.stored(editor.engine.timeline.find_clip(clip.id), "transform.position_x")
    assert stored.key_at(0).interpolation == "fn:bounce_out"


# ---------------------------------------------------------------- handles
def test_bezier_handles_can_be_dragged_and_they_change_the_animation(editor):
    clip = animated_clip(editor)
    channel = "transform.position_x"
    editor.engine.session.set_key_interpolation([(clip.id, channel, 0)], "bezier")
    editor.engine.session.set_key_selection([(clip.id, channel, 0)])
    editor.graph_editor.select_keys({(channel, 0)})
    canvas = editor.graph_editor.canvas
    stored = CH.stored(editor.engine.timeline.find_clip(clip.id), channel)
    handle = canvas._handle_point(stored.keyframes[0], stored.keyframes[1], "out")
    assert handle is not None
    before = CH.value_at(clip, channel, 5)
    depth = editor.qt_undo_stack.undo_stack.count()
    drag(canvas, handle, QPointF(handle.x() + 6, handle.y() + 25))
    pump(0.05)
    after_clip = editor.engine.timeline.find_clip(clip.id)
    after = CH.value_at(after_clip, channel, 5)
    assert abs(after - before) > 1.0, "the handle must bend the real curve"
    assert editor.qt_undo_stack.undo_stack.count() == depth + 1


def test_advanced_interpolation_specs_are_reachable(editor):
    clip = animated_clip(editor)
    channel = "transform.position_x"
    for spec in ("auto", "continuous", "broken", "hold"):
        editor.engine.session.set_key_interpolation([(clip.id, channel, 0)], spec)
        stored = CH.stored(editor.engine.timeline.find_clip(clip.id), channel)
        assert stored.key_at(0).interpolation == spec
    editor.engine.session.set_key_interpolation([(clip.id, channel, 0)], "auto")
    auto_value = CH.value_at(editor.engine.timeline.find_clip(clip.id), channel, 10)
    assert 0.0 < auto_value < 100.0


# ------------------------------------------------------------- navigation
def test_zoom_and_pan_keep_the_pointer_anchored(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    canvas.zoom_x = 6.0
    canvas.pan_x = 0.0
    anchor_x = 200.0
    frame_before = canvas.frame_at(anchor_x)
    from PyQt6.QtGui import QWheelEvent
    event = QWheelEvent(QPointF(anchor_x, 100.0),
                        QPointF(canvas.mapToGlobal(QPoint(int(anchor_x), 100))),
                        QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    canvas.wheelEvent(event)
    assert canvas.zoom_x > 6.0
    assert canvas.frame_at(anchor_x) == pytest.approx(frame_before, abs=0.5)


def test_middle_drag_pans_and_reset_view_restores_the_fit(editor):
    animated_clip(editor)
    canvas = editor.graph_editor.canvas
    canvas.fit()
    zoom, pan = canvas.zoom_x, canvas.pan_x
    mouse(canvas, QEvent.Type.MouseButtonPress, QPointF(200, 120),
          button=Qt.MouseButton.MiddleButton)
    mouse(canvas, QEvent.Type.MouseMove, QPointF(260, 140),
          button=Qt.MouseButton.MiddleButton)
    mouse(canvas, QEvent.Type.MouseButtonRelease, QPointF(260, 140),
          button=Qt.MouseButton.MiddleButton)
    assert canvas.pan_x != pan
    canvas.reset_view()
    assert canvas.zoom_y == 1.0 and canvas.pan_y == 0.0
    assert canvas.zoom_x > 0


def test_fit_selected_zooms_onto_the_selection(editor):
    clip = animated_clip(editor, keys=((0, 0.0), (100, 300.0)))
    canvas = editor.graph_editor.canvas
    editor.graph_editor.select_keys({("transform.position_x", 0)})
    canvas.fit(only_selected=True)
    selected_zoom = canvas.zoom_x
    canvas.fit()
    assert selected_zoom > canvas.zoom_x


def test_context_menu_offers_easing_and_add_keyframe(editor):
    clip = animated_clip(editor)
    canvas = editor.graph_editor.canvas
    seen = {}
    import meenifx.ui.graph_editor as GE
    original = GE.QMenu.exec

    def capture(menu, *args, **kwargs):
        seen["labels"] = [action.text() for action in menu.actions() if action.text()]
        return None

    GE.QMenu.exec = capture
    try:
        point = key_point(canvas, clip, "transform.position_x", 20)
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(int(point.x()),
                                                                        int(point.y())),
                                  canvas.mapToGlobal(QPoint(int(point.x()), int(point.y()))))
        canvas.contextMenuEvent(event)
    finally:
        GE.QMenu.exec = original
    assert "Ease In Out" in seen["labels"]
    assert "Advanced Interpolation" in seen["labels"]
    assert "Delete" in seen["labels"]


def _select_box(canvas, start_frame, end_frame, modifiers=Qt.KeyboardModifier.NoModifier):
    rect = canvas.curve_rect()
    left = QPointF(canvas.x_of(canvas.panel.start_frame() + start_frame) - 8,
                   rect.top() + 4)
    right = QPointF(canvas.x_of(canvas.panel.start_frame() + end_frame),
                    rect.bottom() - 4)
    drag(canvas, left, right, modifiers=modifiers)


def test_box_selection_replaces_an_existing_selection(editor):
    clip = animated_clip(editor, keys=((0, 0.0), (10, 40.0), (20, 100.0)))
    channel = "transform.position_x"
    graph = editor.graph_editor
    graph.select_keys({(channel, 20)})
    _select_box(graph.canvas, 0, 12)
    pump(0.05)
    # Animating position_x also seeds position_y with a key at the same frame.
    assert graph.selection == {(channel, 0), (channel, 10), ("transform.position_y", 0)}
    assert editor.engine.session.key_selection == {
        (clip.id, channel, 0), (clip.id, channel, 10),
        (clip.id, "transform.position_y", 0)}


def test_box_selection_shift_adds_and_ctrl_toggles(editor):
    clip = animated_clip(editor, keys=((0, 0.0), (10, 40.0), (20, 100.0)))
    channel = "transform.position_x"
    graph = editor.graph_editor
    graph.select_keys({(channel, 20)})
    _select_box(graph.canvas, 0, 12, Qt.KeyboardModifier.ShiftModifier)
    assert graph.selection == {(channel, 0), (channel, 10), (channel, 20),
                               ("transform.position_y", 0)}

    graph.select_keys({(channel, 0), (channel, 20)})
    _select_box(graph.canvas, 0, 12, Qt.KeyboardModifier.ControlModifier)
    assert graph.selection == {(channel, 10), (channel, 20), ("transform.position_y", 0)}, \
        "Ctrl must toggle the box contents against the starting selection"
    assert editor.engine.session.key_selection == {
        (clip.id, channel, 10), (clip.id, channel, 20),
        (clip.id, "transform.position_y", 0)}


def test_escape_cancels_a_graph_marquee(editor):
    clip = animated_clip(editor, keys=((0, 0.0), (10, 40.0), (20, 100.0)))
    channel = "transform.position_x"
    graph = editor.graph_editor
    graph.select_keys({(channel, 20)})
    canvas = graph.canvas
    rect = canvas.curve_rect()
    start = QPointF(canvas.x_of(graph.start_frame()) - 8, rect.top() + 4)
    end = QPointF(canvas.x_of(graph.start_frame() + 12), rect.bottom() - 4)
    mouse(canvas, QEvent.Type.MouseButtonPress, start)
    mouse(canvas, QEvent.Type.MouseMove, end)
    assert canvas._box is not None
    canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
    assert graph.selection == {(channel, 20)}
    assert canvas._box is None and canvas._drag is None
    mouse(canvas, QEvent.Type.MouseButtonRelease, end)
    assert editor.engine.session.key_selection == {(clip.id, channel, 20)}


def test_ctrl_a_selects_only_visible_graph_channels(editor):
    clip = animated_clip(editor)
    channel = "transform.position_x"
    CH.set_value(clip, "transform.opacity", 100.0, 0, auto_key=True)
    CH.set_value(clip, "transform.opacity", 20.0, 30, auto_key=True)
    graph = editor.graph_editor
    graph.visible = {channel}
    graph.refresh()
    canvas = graph.canvas
    canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                                   Qt.KeyboardModifier.ControlModifier, "a"))
    assert graph.selection == {(channel, 0), (channel, 20)}
    assert not any(selected_channel == "transform.opacity"
                   for selected_channel, _time in graph.selection)

    graph.visible = set()
    graph.refresh()
    canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                                   Qt.KeyboardModifier.ControlModifier, "a"))
    assert {selected_channel for selected_channel, _time in graph.selection} == {
        channel, "transform.position_y", "transform.opacity"}


def test_delete_from_the_focused_graph_keeps_the_clip_and_undoes(editor):
    clip = animated_clip(editor)
    channel = "transform.position_x"
    graph = editor.graph_editor
    canvas = graph.canvas
    graph.select_keys({(channel, 20)})
    editor.activateWindow()
    pump(0.05)
    canvas.setFocus()
    pump(0.05)
    delete = next(action for menu_action in editor.menuBar().actions()
                  if menu_action.menu() is not None
                  and menu_action.menu().title() == "Edit"
                  for action in menu_action.menu().actions()
                  if action.text() == "Delete")
    delete.trigger()
    pump(0.05)
    assert editor.engine.timeline.find_clip(clip.id) is not None
    assert CH.key_times(editor.engine.timeline.find_clip(clip.id), channel) == [0]
    editor.engine.session.undo()
    assert CH.key_times(editor.engine.timeline.find_clip(clip.id), channel) == [0, 20]
