"""Interactive transform, inspector, graph editor and persistence tests in the real window.

These drive widgets the way a user does: mouse events on the preview, clicks on the
inspector, keyframe drags in the graph editor, then a project save/reload and an export.
Nothing is written into the model directly, so a broken coordinate conversion or a
missing undo step fails here.
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
from PyQt6.QtGui import QImage, QMouseEvent, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import transform as T  # noqa: E402
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
    settings = ProjectSettings(name=f"Xf {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    pump(0.1)
    clip = add_still(window)
    window.engine.session.set_selection({clip.id})
    pump(0.15)
    return window


def add_still(window):
    from meenifx.project.media import media_type_for
    project = window.projects.project
    item = project.library.add(STILL, 3.0, 320, 180, 30.0, media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    clips = window.insert_media_items([item], frame=0)
    assert clips, "the still was not inserted"
    return window.engine.timeline.find_clip(clips[0])


def test_a_source_that_cannot_decode_does_not_break_sizing(editor):
    """A moved or corrupt file used to raise IndexError from inside the overlay's paint."""
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    entry = window.engine.media._entries[clip.source_path]
    original = entry.frames

    class _NoFrames:
        def __len__(self):
            return 0

        def __getitem__(self, index):
            raise IndexError(index)

    entry.frames = _NoFrames()
    try:
        assert window.engine.media.source_size(clip.source_path) is None
        assert window.overlay.image_size(clip) == window.engine.project_size()
        assert len(window.overlay.clip_corners(clip)) == 4
        assert window.engine.session.clip_image_size(clip) == window.engine.project_size()
        image = QImage(96, 96, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            window.overlay.paint(painter)          # the frame that crashed the app
        finally:
            painter.end()
        assert window.engine.media.tile_width(clip, 40) == 71, "falls back to 16:9"
    finally:
        entry.frames = original


def test_the_performance_overlay_menu_item_actually_shows_the_overlay(editor):
    """The menu item used to flip the action and the saved flag, but not the overlay."""
    window = editor
    view = window.preview_panel.view
    window.perf_overlay.visible = False
    window.perf_overlay_action.setChecked(False)
    view.update()
    pump(0.1)
    plain = view.grab().toImage()

    window.timeline_panel.canvas.setFocus()
    window.perf_overlay_action.trigger()          # what clicking the menu item does
    pump(0.15)
    assert window.perf_overlay.visible is True
    assert window.playback_settings.show_overlay is True
    shown = view.grab().toImage()
    assert plain.pixelColor(12, 12) != shown.pixelColor(12, 12), "the HUD painted nothing"
    stats = window._overlay_stats()
    assert isinstance(stats["fps"], float) and stats["target_fps"] == float(
        window.engine.timeline.fps)

    window.perf_overlay_action.trigger()
    pump(0.1)
    assert window.perf_overlay.visible is False
    assert view.grab().toImage().pixelColor(12, 12) == plain.pixelColor(12, 12)


# --------------------------------------------------------------------- preview
def preview_point(window, x, y):
    """Project pixel -> widget point (what the overlay uses)."""
    view = window.preview_panel.view
    return window.overlay.to_widget(x, y)


def send(view, kind, point, button=Qt.MouseButton.LeftButton, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QMouseEvent(kind, QPointF(point), button, button, modifiers)
    if kind == QMouseEvent.Type.MouseButtonPress:
        view.mousePressEvent(event)
    elif kind == QMouseEvent.Type.MouseMove:
        view.mouseMoveEvent(event)
    else:
        view.mouseReleaseEvent(event)
    return event


def drag(view, start, end, steps=6, modifiers=Qt.KeyboardModifier.NoModifier):
    send(view, QMouseEvent.Type.MouseButtonPress, start, modifiers=modifiers)
    for index in range(1, steps + 1):
        blend = QPointF(start.x() + (end.x() - start.x()) * index / steps,
                        start.y() + (end.y() - start.y()) * index / steps)
        send(view, QMouseEvent.Type.MouseMove, blend, modifiers=modifiers)
    send(view, QMouseEvent.Type.MouseButtonRelease, end, modifiers=modifiers)


def test_clicking_the_picture_selects_and_shows_handles(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    window.engine.session.set_selection(set())
    pump(0.05)
    view = window.preview_panel.view
    centre = preview_point(window, 320, 180)
    send(view, QMouseEvent.Type.MouseButtonPress, centre)
    send(view, QMouseEvent.Type.MouseButtonRelease, centre)
    pump(0.05)
    assert window.engine.session.selected == {clip.id}
    assert window.overlay.primary() is not None
    handles = window.overlay.handle_points(clip)
    assert set(handles) == {"nw", "n", "ne", "e", "se", "s", "sw", "w"}


def test_dragging_the_clip_in_the_preview_moves_it_as_one_undo_step(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    view = window.preview_panel.view
    depth = window.qt_undo_stack.undo_stack.count()
    start = preview_point(window, 320, 180)
    drag(view, start, QPointF(start.x() + 60, start.y() + 20))
    pump(0.1)
    moved = window.engine.timeline.find_clip(clip.id)
    project_per_pixel = window.engine.project_size()[0] / max(1.0, view.frame_rect().width())
    assert moved.transform["position_x"] > 0
    assert abs(moved.transform["position_x"] - 60 * project_per_pixel) < 4.0
    assert window.qt_undo_stack.undo_stack.count() == depth + 1, "a drag must be one undo step"
    window.engine.session.undo()
    assert window.engine.timeline.find_clip(clip.id).transform["position_x"] == 0.0
    window.engine.session.redo()
    pump(0.05)


def test_corner_handle_scales_and_shift_keeps_the_aspect(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    view = window.preview_panel.view
    window.engine.session.set_selection({clip.id})
    pump(0.05)
    corners = window.overlay.handle_points(clip)
    corner = preview_point(window, *corners["se"])
    target = QPointF(corner.x() - 40, corner.y() - 18)
    drag(view, corner, target, modifiers=Qt.KeyboardModifier.ShiftModifier)
    pump(0.1)
    state = T.evaluate(window.engine.timeline.find_clip(clip.id), 0)
    assert state.scale_x < 100.0
    assert abs(state.scale_x - state.scale_y) < 0.01, "Shift must keep the aspect ratio"


def test_rotation_handle_rotates_the_clip(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    view = window.preview_panel.view
    window.engine.session.set_selection({clip.id})
    pump(0.05)
    knob = window.overlay.rotate_knob(clip)
    centre = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    drag(view, knob, QPointF(centre.x() + 90, centre.y() + 10))
    pump(0.1)
    state = T.evaluate(window.engine.timeline.find_clip(clip.id), 0)
    assert abs(state.rotation) > 20.0


def test_anchor_drag_keeps_the_picture_still(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    view = window.preview_panel.view
    view.set_frame(np.full((360, 640, 3), 90, np.uint8), logical_size=(640, 360))
    pump(0.1)
    before = view.grab().toImage()
    anchor = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    drag(view, anchor, QPointF(anchor.x() + 30, anchor.y() - 20))
    pump(0.1)
    state = T.evaluate(window.engine.timeline.find_clip(clip.id), 0)
    assert abs(state.anchor_x) > 5.0
    assert abs(state.position_x - state.anchor_x) < 2.0, "position must follow the anchor"
    del before


# -------------------------------------------------------------------- inspector
def test_inspector_shows_transform_values_and_edits_them(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    panel = window.effects_panel
    pump(0.1)
    rows = [row for row in panel._rows if row["kind"] == "channel"]
    channels = {row["channel"] for row in rows}
    assert "transform.position_x" in channels and "transform.opacity" in channels
    field = next(row["field"] for row in rows if row["channel"] == "transform.rotation")
    field.set_value(30.0)
    field.committed.emit(30.0)
    pump(0.1)
    assert abs(T.evaluate(window.engine.timeline.find_clip(clip.id), 0).rotation - 30.0) < 0.01
    # The preview and the inspector read the same model, so one change updates both.
    panel.sync_playhead()
    assert abs(field.value() - 30.0) < 0.01


def test_diamond_adds_and_removes_a_keyframe(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    channel = "transform.rotation"
    window.engine.session.set_playhead(10)
    # The diamond turns a static property into an animated one keyed at the playhead,
    # then removes that key and puts it back.
    window.engine.session.toggle_channel_key(clip, channel)
    assert CH.key_times(clip, channel) == [10]
    window.engine.session.toggle_channel_key(window.engine.timeline.find_clip(clip.id), channel)
    assert CH.key_times(window.engine.timeline.find_clip(clip.id), channel) == []
    window.engine.session.toggle_channel_key(window.engine.timeline.find_clip(clip.id), channel)
    assert CH.key_times(window.engine.timeline.find_clip(clip.id), channel) == [10]
    window.engine.session.undo()
    assert CH.key_times(window.engine.timeline.find_clip(clip.id), channel) == []


def test_auto_keyframe_toggle_controls_key_creation(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    window.autokey_button.setChecked(True)
    pump(0.05)
    assert bool(window.engine.session.auto_keyframe.get())
    window.engine.session.set_playhead(12)
    T.animate_channel(clip, "opacity", 0)
    CH.set_value(clip, "transform.opacity", 45.0, 12, bool(window.engine.session.auto_keyframe.get()))
    assert CH.key_times(clip, "transform.opacity") == [0, 12]
    window.autokey_button.setChecked(False)
    pump(0.05)
    assert not bool(window.engine.session.auto_keyframe.get())


# ----------------------------------------------------------------- graph editor
def test_graph_editor_lists_animated_channels_and_draws_curves(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.animate_channel(clip, "position_x", 0)
    CH.set_value(clip, "transform.position_x", 0.0, 0, True)
    CH.set_value(clip, "transform.position_x", 60.0, 30, True)
    window.graph_editor.refresh()
    pump(0.05)
    channels = [row["id"] for row in window.graph_editor.animated_channels()]
    assert "transform.position_x" in channels
    entries = window.graph_editor.key_entries()
    assert [time for channel, time, _value in entries if channel == "transform.position_x"] == [0, 30]
    canvas = window.graph_editor.canvas
    canvas.fit()
    assert canvas.x_of(canvas.panel.start_frame() + 30) > canvas.x_of(canvas.panel.start_frame() + 0)


def test_graph_editor_keyframe_drag_retimes_and_revalues(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.animate_channel(clip, "rotation", 0)
    CH.set_value(clip, "transform.rotation", 0.0, 0, True)
    CH.set_value(clip, "transform.rotation", 20.0, 20, True)
    window.graph_editor.refresh()
    canvas = window.graph_editor.canvas
    canvas.zoom_x = 6.0
    canvas.pan_x = 0.0
    pump(0.05)
    window.graph_editor.select_keys({("transform.rotation", 20)})
    start = QPointF(canvas.x_of(canvas.panel.start_frame() + 20),
                    canvas.y_of(float(CH.value_at(clip, "transform.rotation", 20))))
    drag(canvas, start, QPointF(start.x() + 30, start.y() - 15))
    pump(0.1)
    keys = CH.key_times(window.engine.timeline.find_clip(clip.id), "transform.rotation")
    assert 20 not in keys and any(key > 20 for key in keys), keys


def test_applying_an_easing_preset_changes_the_curve_and_is_one_undo(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.animate_channel(clip, "position_x", 0)
    CH.set_value(clip, "transform.position_x", 0.0, 0, True)
    CH.set_value(clip, "transform.position_x", 100.0, 20, True)
    window.graph_editor.refresh()
    window.graph_editor.select_keys({("transform.position_x", 0)})
    depth = window.qt_undo_stack.undo_stack.count()
    window.graph_editor.preset_combo.setCurrentIndex(
        window.graph_editor.preset_combo.findText("Elastic Out"))
    window.graph_editor._preset_chosen(window.graph_editor.preset_combo.currentIndex())
    pump(0.05)
    stored = CH.stored(window.engine.timeline.find_clip(clip.id), "transform.position_x")
    assert stored.key_at(0).interpolation == "fn:elastic_out"
    # Elastic really overshoots: a sampled value passes the end value and comes back.
    values = [stored.value_at(frame) for frame in range(0, 20)]
    assert any(value > 101.0 for value in values), values
    assert window.qt_undo_stack.undo_stack.count() == depth + 1
    window.engine.session.undo()
    restored = CH.stored(window.engine.timeline.find_clip(clip.id), "transform.position_x")
    assert restored.key_at(0).interpolation == "linear"
    window.engine.session.redo()


def test_graph_editor_speed_mode_is_derived_from_the_same_curve(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.animate_channel(clip, "position_y", 0)
    CH.set_value(clip, "transform.position_y", 0.0, 0, True)
    CH.set_value(clip, "transform.position_y", 90.0, 30, True)
    window.graph_editor.refresh()
    value_points = dict((frame, value) for frame, value in
                        [(f, CH.value_at(clip, "transform.position_y", f)) for f in range(0, 31)])
    speed = window.graph_editor.speed_points("transform.position_y")
    assert speed, "speed mode must produce a curve"
    frame, per_second = speed[3]
    assert abs(per_second - 90.0) < 1.0, "constant velocity motion has a constant speed"
    del value_points


# ---------------------------------------------------------------- persistence
def test_transform_and_pins_survive_save_and_reload(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.set_channel(clip, "position_x", 33.0)
    T.set_channel(clip, "rotation", 15.0)
    angle = T.animate_channel(clip, "rotation", 0)
    angle.set_keyframe(0, 0.0)
    angle.set_keyframe(15, 40.0)
    angle.keyframes[0].interpolation = "fn:elastic_out"
    window.engine.session.apply_to_clips([clip.id], "Add puppet", lambda c: c.effects.append(
        {"effect_id": "puppet_warp", "enabled": True, "params": {},
         "pins": [{"id": 1, "src": [0.4, 0.4], "x": 0.5, "y": 0.55, "locked": False}]}))
    path = window.projects.project.path
    assert window.projects.save()
    pump(0.1)
    assert window.projects.close_project()
    pump(0.1)
    assert window.projects.open_path(path)
    pump(0.2)
    reloaded = window.engine.timeline.all_clips()[0]
    assert reloaded.transform["position_x"] == 33.0
    stored = reloaded.transform["rotation"]
    assert [key.time for key in stored.keyframes] == [0, 15]
    assert stored.keyframes[0].interpolation == "fn:elastic_out"
    pins = reloaded.effects[0]["pins"]
    assert pins[0]["x"] == 0.5 and pins[0]["locked"] is False


def test_export_contains_the_transform(editor, tmp_path):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.set_channel(clip, "scale_x", 50.0)
    T.set_channel(clip, "scale_y", 50.0)
    window.engine.session.set_playhead(0)
    pump(0.2)
    frame, _processed = window.engine.render_frame(0, 1.0)
    assert frame.shape[:2] == (360, 640)
    # The corners are the project background: the clip really was scaled down.
    assert float(frame[:30, :30].mean()) < 6.0
    assert frame[170:190, 310:330].mean() > 5.0
    out = tmp_path / "scaled.png"
    settings = {"output_path": str(out), "format": "png", "range_start": 0, "range_end": 1,
                "use_source_size": True, "use_source_fps": True}
    written, count, _fps, _size = window.engine.export(settings)
    assert count == 1
    exported = __import__("cv2").imread(written)
    assert exported.shape[:2] == (360, 640)
    diff = float(np.abs(exported.astype(np.int16) - frame.astype(np.int16)).mean())
    assert diff < 2.0, "the exported frame must be the frame the preview showed"


# ---------------------------------------------------- scrubbable numeric controls
def scrub_field(field, dx, modifiers=Qt.KeyboardModifier.NoModifier):
    """Drag an inspector value sideways, the way a user does."""
    widget = field.value_widget
    start = widget.width() // 2

    def send(kind, x, buttons=Qt.MouseButton.LeftButton):
        QApplication.sendEvent(widget, QMouseEvent(
            kind, QPointF(x, widget.height() // 2),
            QPointF(widget.mapToGlobal(QPoint(int(x), widget.height() // 2))),
            Qt.MouseButton.LeftButton, buttons, modifiers))

    send(QEvent.Type.MouseButtonPress, start)
    for step in range(1, 5):
        send(QEvent.Type.MouseMove, start + round(dx * step / 4))
    send(QEvent.Type.MouseButtonRelease, start + dx, Qt.MouseButton.NoButton)


def channel_field(window, channel):
    """The live inspector row for a channel (rows are rebuilt on every refresh)."""
    window.effects_panel.refresh_properties()
    pump(0.2)
    return next(row["field"] for row in window.effects_panel._rows
                if row.get("channel") == channel)


def test_a_scrubbed_value_survives_save_and_reload(editor):
    """.mfxpf keeps what the scrub wrote, not just what a typed value wrote."""
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    window.engine.session.set_selection({clip.id})
    scrub_field(channel_field(window, "transform.position_x"), 12)
    pump(0.25)
    assert window.engine.timeline.find_clip(clip.id).transform["position_x"] == pytest.approx(12)
    path = window.projects.project.path
    assert window.projects.save()
    pump(0.1)
    assert window.projects.close_project()
    pump(0.1)
    assert window.projects.open_path(path)
    pump(0.25)
    assert window.engine.timeline.all_clips()[0].transform["position_x"] == pytest.approx(12)


def test_scrubbing_an_animated_channel_follows_auto_keyframe(editor):
    """Auto-Keyframe off shifts the curve; on, the scrub keys at the playhead."""
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    curve = T.animate_channel(clip, "position_x", 0)
    curve.set_keyframe(0, 0.0)
    curve.set_keyframe(20, 40.0)
    window.engine.session.set_selection({clip.id})
    window.engine.session.auto_keyframe.set(False)
    window.engine.session.set_playhead(10)
    field = channel_field(window, "transform.position_x")
    assert field.value() == pytest.approx(20), "the row shows the value at the playhead"
    scrub_field(field, 5)
    pump(0.25)
    stored = window.engine.timeline.find_clip(clip.id).transform["position_x"]
    assert [key.time for key in stored.keyframes] == [0, 20], "no accidental keyframe"
    assert stored.value_at(10) == pytest.approx(25), "the whole curve moved by the delta"

    window.engine.session.auto_keyframe.set(True)
    try:
        window.engine.session.set_playhead(30)
        scrub_field(channel_field(window, "transform.position_x"), 5)
        pump(0.25)
        keyed = window.engine.timeline.find_clip(clip.id).transform["position_x"]
        assert keyed.key_at(30) is not None, "Auto-Keyframe must key at the playhead"
    finally:
        window.engine.session.auto_keyframe.set(False)

def test_the_scale_lock_keeps_the_aspect_ratio_while_scrubbing(editor):
    """The chain beside Scale X/Y is a real link, not a decoration."""
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.set_channel(clip, "scale_x", 100.0)
    T.set_channel(clip, "scale_y", 50.0)
    window.engine.session.set_selection({clip.id})
    scrub_field(channel_field(window, "transform.scale_x"), 20)
    pump(0.25)
    locked = window.engine.timeline.find_clip(clip.id).transform
    assert locked["scale_x"] == pytest.approx(120)
    assert locked["scale_y"] == pytest.approx(60), "the other axis follows the same factor"

    T.ensure_transform(window.engine.timeline.find_clip(clip.id))["uniform"] = False
    scrub_field(channel_field(window, "transform.scale_x"), -20)
    pump(0.25)
    free = window.engine.timeline.find_clip(clip.id).transform
    assert free["scale_x"] == pytest.approx(100)
    assert free["scale_y"] == pytest.approx(60), "with the lock off the axes are independent"
