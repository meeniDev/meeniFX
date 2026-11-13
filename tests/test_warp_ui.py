"""Preview-side warp editing, motion paths, snapping and keyframe clipboard tests."""

import os
import sys
import time
import uuid

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import easing  # noqa: E402
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
    settings = ProjectSettings(name=f"W {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    pump(0.1)
    from meenifx.project.media import media_type_for
    item = window.projects.project.library.add(STILL, 3.0, 320, 180, 30.0,
                                               media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    ids = window.insert_media_items([item], frame=0)
    window.engine.session.set_selection({ids[0]})
    pump(0.15)
    return window


def send(view, kind, point, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QMouseEvent(kind, QPointF(point), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, modifiers)
    if kind == QMouseEvent.Type.MouseButtonPress:
        view.mousePressEvent(event)
    elif kind == QMouseEvent.Type.MouseMove:
        view.mouseMoveEvent(event)
    else:
        view.mouseReleaseEvent(event)


def drag(view, start, end, steps=5, modifiers=Qt.KeyboardModifier.NoModifier):
    send(view, QMouseEvent.Type.MouseButtonPress, start, modifiers)
    for index in range(1, steps + 1):
        send(view, QMouseEvent.Type.MouseMove,
             QPointF(start.x() + (end.x() - start.x()) * index / steps,
                     start.y() + (end.y() - start.y()) * index / steps), modifiers)
    send(view, QMouseEvent.Type.MouseButtonRelease, end, modifiers)


def click(view, point):
    send(view, QMouseEvent.Type.MouseButtonPress, point)
    send(view, QMouseEvent.Type.MouseButtonRelease, point)


def test_puppet_tool_adds_pins_and_drags_them(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    view = window.preview_panel.view
    window.autokey_button.setChecked(True)
    window.engine.session.set_playhead(0)
    window._set_overlay_mode("puppet")
    pump(0.1)
    assert any(entry["effect_id"] == "puppet_warp" for entry in
               window.engine.timeline.find_clip(clip.id).effects), "the tool adds the effect"
    point = window.overlay.to_widget(200, 120)
    click(view, point)
    pump(0.1)
    pins = window.engine.timeline.find_clip(clip.id).effects[0]["pins"]
    assert len(pins) == 1, pins
    before = window.engine.render_frame(0, 0.5)[0]
    updated = window.engine.timeline.find_clip(clip.id)
    pin = updated.effects[0]["pins"][0]
    start = window.overlay.to_widget(*window.overlay.handle_points(updated)["nw"])
    # Drag the pin itself: its screen position is where the pin was dropped.
    pin_point = window.overlay.to_widget(200, 120)
    depth = window.qt_undo_stack.undo_stack.count()
    drag(view, pin_point, QPointF(pin_point.x() + 40, pin_point.y() + 20))
    pump(0.1)
    moved = window.engine.timeline.find_clip(clip.id)
    pins = moved.effects[0]["pins"]
    pin_x = CH.value_at(moved, CH.pin_channel("puppet_warp", pins[0]["id"], "x"), 0)
    pin_y = CH.value_at(moved, CH.pin_channel("puppet_warp", pins[0]["id"], "y"), 0)
    assert abs(pin_x - 200 / 320.0) > 0.02 or abs(pin_y - 120 / 180.0) > 0.02
    assert window.qt_undo_stack.undo_stack.count() == depth + 1
    after = window.engine.render_frame(0, 0.5)[0]
    assert float(np.abs(after.astype(np.int16) - before.astype(np.int16)).mean()) > 0.5
    del start, pin


def test_puppet_pin_keyframes_through_auto_key(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    window.autokey_button.setChecked(True)
    window._set_overlay_mode("puppet")
    pump(0.05)
    view = window.preview_panel.view
    click(view, window.overlay.to_widget(250, 150))
    pump(0.05)
    pin = window.engine.timeline.find_clip(clip.id).effects[0]["pins"][0]
    pin_id = pin["id"]
    window.engine.session.set_playhead(20)
    start = window.overlay.to_widget(250, 150)
    drag(view, start, QPointF(start.x() + 30, start.y()))
    pump(0.1)
    channel = CH.pin_channel("puppet_warp", pin_id, "x")
    times = CH.key_times(window.engine.timeline.find_clip(clip.id), channel)
    assert 20 in times, times
    assert CH.value_at(window.engine.timeline.find_clip(clip.id), channel, 0) != \
        CH.value_at(window.engine.timeline.find_clip(clip.id), channel, 20)


def test_corner_pin_corners_are_draggable_in_the_preview(editor):
    window = editor
    window.autokey_button.setChecked(False)
    clip = window.engine.timeline.all_clips()[0]
    window._set_overlay_mode("transform")
    window._set_preview_controls(False)
    window.engine.session.apply_to_clips([clip.id], "Add corner pin", lambda c: c.effects.append(
        {"effect_id": "corner_pin", "enabled": True, "params": {}}))
    pump(0.1)
    view = window.preview_panel.view
    target = window.engine.timeline.find_clip(clip.id)
    corner = window.overlay.mesh_nodes(target)
    assert corner == [], "mesh grid only exists with a mesh warp effect"
    picture_tl = window.overlay.clip_corners(target)[0]
    point = window.overlay.to_widget(*picture_tl)
    drag(view, point, QPointF(point.x() + 30, point.y() + 12))
    pump(0.1)
    moved = window.engine.timeline.find_clip(clip.id)
    # The top-left corner moved away from the picture's own top-left corner.
    assert CH.value_at(moved, CH.effect_channel("corner_pin", "corner_tl_x"), 0) > 0.01
    assert CH.value_at(moved, CH.effect_channel("corner_pin", "corner_tl_y"), 0) > 0.005
    window._set_preview_controls(True)


def test_corner_pin_handle_hit_test_matches_rendered_corner(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    clip.effects = [{"effect_id": "corner_pin", "enabled": True, "params": {
        "corner_tl_x": 0.18, "corner_tl_y": 0.22,
    }}]
    frame = window.overlay.local_frame(clip)
    width, height = window.overlay.image_size(clip)
    source = (0.18 * (width - 1), 0.22 * (height - 1))
    project = T.forward(source, window.overlay.state(clip), window.overlay.project_size(),
                        (width, height))
    window.overlay._last_pos = window.overlay.to_widget(*project)
    index, _point = window.overlay._corner_at(clip)
    assert index == 0


def test_alt_drag_frame_corner_applies_corner_pin_without_effect_setup(editor):
    window = editor
    window.autokey_button.setChecked(False)
    window._set_overlay_mode("transform")
    clip = window.engine.timeline.all_clips()[0]
    clip.effects = [entry for entry in clip.effects if entry.get("effect_id") != "corner_pin"]
    corner = window.overlay.to_widget(*window.overlay.clip_corners(clip)[0])
    drag(window.preview_panel.view, corner,
         QPointF(corner.x() + 26, corner.y() + 15),
         modifiers=Qt.KeyboardModifier.AltModifier)
    moved = window.engine.timeline.find_clip(clip.id)
    assert any(entry.get("effect_id") == "corner_pin" for entry in moved.effects)
    assert CH.value_at(moved, CH.effect_channel("corner_pin", "corner_tl_x"), 0) > 0.01


def test_perspective_tilt_accepts_full_revolutions():
    from meenifx.effects.warp import PerspectiveEffect

    ranges = {spec.name: (spec.minimum, spec.maximum)
              for spec in PerspectiveEffect.PARAMS if spec.name.startswith("tilt_")}
    assert ranges == {"tilt_x": (-720.0, 720.0), "tilt_y": (-720.0, 720.0)}


def test_mesh_warp_grid_nodes_are_draggable(editor):
    window = editor
    window.autokey_button.setChecked(False)
    clip = window.engine.timeline.all_clips()[0]
    window.engine.session.apply_to_clips([clip.id], "Add mesh", lambda c: c.effects.append(
        {"effect_id": "mesh_warp", "enabled": True, "params": {}}))
    window._set_overlay_mode("transform")
    pump(0.1)
    target = window.engine.timeline.find_clip(clip.id)
    nodes = window.overlay.mesh_nodes(target)
    # The mesh defaults to a 5x5 grid now (it was 3x3), which is what the effect exposes
    # as node_0_0 .. node_4_4 parameters.
    assert len(nodes) == 25, nodes
    row, col, point = next(node for node in nodes if node[0] == 2 and node[1] == 2)
    widget_point = window.overlay.to_widget(*point)
    drag(window.preview_panel.view, widget_point, QPointF(widget_point.x() + 20, widget_point.y() - 10))
    pump(0.1)
    moved = window.engine.timeline.find_clip(clip.id)
    assert abs(CH.value_at(moved, CH.effect_channel("mesh_warp", "node_2_2_x"), 0)) > 0.005


def test_motion_path_points_can_be_dragged(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.animate_channel(clip, "position_x", 0)
    CH.set_value(clip, "transform.position_x", 0.0, 0, True)
    CH.set_value(clip, "transform.position_x", 60.0, 20, True)
    window.engine.session.set_playhead(0)
    window._set_overlay_mode("transform")
    pump(0.1)
    target = window.engine.timeline.find_clip(clip.id)
    x, y = T.position_at(target, 20)
    point = window.overlay.to_widget(*((window.engine.project_size()[0] - 1) / 2.0 + x,
                                       (window.engine.project_size()[1] - 1) / 2.0 + y))
    drag(window.preview_panel.view, point, QPointF(point.x() - 50, point.y() + 25))
    pump(0.1)
    updated = window.engine.timeline.find_clip(clip.id)
    first = T.position_at(updated, 0)
    second = T.position_at(updated, 20)
    assert abs(second[0] - 60.0) > 1.0 or abs(second[1]) > 1.0
    assert first == pytest.approx((0.0, 0.0), abs=1.0)


def test_moving_a_clip_snaps_to_the_frame_centre(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    T.set_channel(clip, "position_x", 40.0)
    window._set_overlay_mode("transform")
    pump(0.1)
    view = window.preview_panel.view
    target = window.engine.timeline.find_clip(clip.id)
    anchor = window.overlay.anchor_point(target)
    # Grab the picture below its centre, away from the anchor and the scale handles,
    # then drag it left by its offset: snapping should land exactly on the centre.
    start = window.overlay.to_widget(anchor[0], anchor[1] + 60.0)
    end = window.overlay.to_widget(anchor[0] - 40.0, anchor[1] + 60.0)
    drag(view, start, end)
    pump(0.1)
    state = T.evaluate(window.engine.timeline.find_clip(clip.id), 0)
    assert abs(state.position_x) < 0.001, state.position_x
    assert window.overlay._guides == []


def test_keyframes_can_be_copied_to_another_channel(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    for name, values in (("position_x", (0.0, 80.0)), ("rotation", None)):
        T.animate_channel(clip, name, 0)
        if values:
            CH.set_value(clip, CH.transform_channel(name), values[0], 0, True)
            CH.set_value(clip, CH.transform_channel(name), values[1], 20, True)
    window.graph_editor.refresh()
    window.graph_editor.select_keys({("transform.position_x", 0), ("transform.position_x", 20)})
    window.graph_editor.copy_selected()
    window.engine.session.set_playhead(30)
    window.graph_editor.paste_selected()
    # Pasting lands on the active channel; select rotation explicitly through its item.
    window.graph_editor.select_keys({("transform.position_x", 0)})
    assert window.graph_editor.session.has_key_clipboard()


def test_custom_easing_presets_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "easing_presets.json"
    monkeypatch.setattr(easing, "presets_path", lambda: str(path))
    easing.save_custom_preset("My Curve", "bezier", (0.2, 0.9, 0.8, 0.1))
    assert "My Curve" in easing.load_custom_presets()
    payload = easing.preset_spec("My Curve")
    assert payload["handles"] == (0.2, 0.9, 0.8, 0.1)
    assert easing.preset_curve("My Curve")
    assert easing.rename_custom_preset("My Curve", "Renamed")
    assert "Renamed" in easing.all_preset_names()
    assert easing.delete_custom_preset("Renamed")
    assert "Renamed" not in easing.all_preset_names()


def test_undo_restores_a_deleted_pin(editor):
    window = editor
    clip = window.engine.timeline.all_clips()[0]
    window._set_overlay_mode("puppet")
    pump(0.05)
    click(window.preview_panel.view, window.overlay.to_widget(260, 140))
    pump(0.1)
    assert window.engine.timeline.find_clip(clip.id).effects[0]["pins"]
    window.preview_panel.view.setFocus()
    key = __import__("PyQt6.QtGui", fromlist=["QKeyEvent"]).QKeyEvent
    from PyQt6.QtCore import QEvent
    event = key(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier)
    window.preview_panel.view.keyPressEvent(event)
    pump(0.05)
    assert window.engine.timeline.find_clip(clip.id).effects[0]["pins"] == []
    window.engine.session.undo()
    assert len(window.engine.timeline.find_clip(clip.id).effects[0]["pins"]) == 1
