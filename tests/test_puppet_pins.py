"""Puppet pin editing: multi-selection, group drags, copy/paste, reset and disabling."""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
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
    from meenifx.project.media import media_type_for
    window.projects._set_clean()
    settings = ProjectSettings(name=f"P {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    item = window.projects.project.library.add(STILL, 3.0, 320, 180, 30.0,
                                               media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    ids = window.insert_media_items([item], frame=0)
    window.engine.session.set_selection({ids[0]})
    pump(0.15)
    window.autokey_button.setChecked(True)
    window.engine.session.set_playhead(0)
    window._set_overlay_mode("puppet")
    pump(0.1)
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


def click(view, point, modifiers=Qt.KeyboardModifier.NoModifier):
    send(view, QMouseEvent.Type.MouseButtonPress, point, modifiers)
    send(view, QMouseEvent.Type.MouseButtonRelease, point, modifiers)


def drag(view, start, end, modifiers=Qt.KeyboardModifier.NoModifier, steps=4):
    send(view, QMouseEvent.Type.MouseButtonPress, start, modifiers)
    for index in range(1, steps + 1):
        send(view, QMouseEvent.Type.MouseMove,
             QPointF(start.x() + (end.x() - start.x()) * index / steps,
                     start.y() + (end.y() - start.y()) * index / steps), modifiers)
    send(view, QMouseEvent.Type.MouseButtonRelease, end, modifiers)


def clip_of(window):
    return window.engine.timeline.all_clips()[0]


def pins_of(window):
    return CH.pins_of(window.engine.timeline.find_clip(clip_of(window).id), "puppet_warp")


def add_pin(window, x, y):
    point = window.overlay.to_widget(x, y)
    click(window.preview_panel.view, point)
    pump(0.05)
    return point


def pin_xy(window, pin_id, frame=0):
    clip = window.engine.timeline.find_clip(clip_of(window).id)
    return (CH.value_at(clip, CH.pin_channel("puppet_warp", pin_id, "x"), frame),
            CH.value_at(clip, CH.pin_channel("puppet_warp", pin_id, "y"), frame))


def test_ctrl_click_selects_several_pins(editor):
    window = editor
    view = window.preview_panel.view
    first = add_pin(window, 200, 120)
    second = add_pin(window, 400, 220)
    assert len(pins_of(window)) == 2
    click(view, first)
    pump(0.05)
    assert window.overlay._selected_pins == {pins_of(window)[0]["id"]}
    click(view, second, Qt.KeyboardModifier.ControlModifier)
    pump(0.05)
    assert window.overlay._selected_pins == {pin["id"] for pin in pins_of(window)}
    # Ctrl-click again removes it from the selection.
    click(view, second, Qt.KeyboardModifier.ControlModifier)
    pump(0.05)
    assert window.overlay._selected_pins == {pins_of(window)[0]["id"]}


def test_dragging_moves_every_selected_pin_by_the_same_offset(editor):
    window = editor
    view = window.preview_panel.view
    add_pin(window, 200, 120)
    add_pin(window, 400, 220)
    pins = pins_of(window)
    start_a = pin_xy(window, pins[0]["id"])
    start_b = pin_xy(window, pins[1]["id"])
    first_point = window.overlay.to_widget(200, 120)
    click(view, first_point)
    click(view, window.overlay.to_widget(400, 220), Qt.KeyboardModifier.ControlModifier)
    pump(0.05)
    assert len(window.overlay._selected_pins) == 2
    drag(view, first_point, QPointF(first_point.x() + 30, first_point.y() + 20))
    pump(0.1)
    moved_a = pin_xy(window, pins[0]["id"])
    moved_b = pin_xy(window, pins[1]["id"])
    dx_a, dy_a = moved_a[0] - start_a[0], moved_a[1] - start_a[1]
    dx_b, dy_b = moved_b[0] - start_b[0], moved_b[1] - start_b[1]
    assert dx_a == pytest.approx(dx_b, abs=1e-9), "both pins shift by the same amount in x"
    assert dy_a == pytest.approx(dy_b, abs=1e-9), "both pins shift by the same amount in y"
    assert abs(dx_a) > 1e-4 and abs(dy_a) > 1e-4, "the drag really moved them"


def test_auto_keyframe_reports_the_pin_keys_it_wrote(editor):
    window = editor
    messages = []
    window.engine.session.status.connect(messages.append)
    point = add_pin(window, 220, 140)
    drag(window.preview_panel.view, point, QPointF(point.x() + 25, point.y()))
    pump(0.05)
    assert any("puppet pin" in text.lower() and "keyframed" in text.lower()
               for text in messages), messages
    pin = pins_of(window)[0]
    keys = CH.key_records(window.engine.timeline.find_clip(clip_of(window).id),
                          CH.pin_channel("puppet_warp", pin["id"], "x"))
    assert keys and keys[0][0] == 0


def test_disabling_a_pin_takes_it_out_of_the_warp_without_unlocking_it(editor):
    window = editor
    point = add_pin(window, 220, 140)
    drag(window.preview_panel.view, point, QPointF(point.x() + 40, point.y() + 20))
    pump(0.05)
    before = window.engine.render_frame(0, 0.5)[0].copy()
    pin = pins_of(window)[0]
    moved_to = pin_xy(window, pin["id"])
    click(window.preview_panel.view,
          window.overlay.to_widget(moved_to[0] * 640, moved_to[1] * 360))
    pump(0.05)
    assert window.overlay._selected_pins
    assert window.overlay.toggle_pin_enabled()
    pump(0.05)
    pin = pins_of(window)[0]
    assert pin["enabled"] is False and not pin.get("locked")
    after = window.engine.render_frame(0, 0.5)[0]
    # With the only pin disabled the picture is the undeformed layer again.
    try:
        import numpy as np
        assert float(np.mean(np.abs(after.astype("float32") - before.astype("float32")))) > 0.5
    except ImportError:
        pass


def test_reset_pins_restores_the_rest_pose_and_clears_keys(editor):
    window = editor
    point = add_pin(window, 240, 150)
    drag(window.preview_panel.view, point, QPointF(point.x() + 50, point.y() + 10))
    pump(0.05)
    pin = pins_of(window)[0]
    moved_to = pin_xy(window, pin["id"])
    click(window.preview_panel.view,
          window.overlay.to_widget(moved_to[0] * 640, moved_to[1] * 360))
    pump(0.05)
    pin = pins_of(window)[0]
    assert CH.key_records(window.engine.timeline.find_clip(clip_of(window).id),
                          CH.pin_channel("puppet_warp", pin["id"], "x"))
    assert window.overlay.reset_selected_pins()
    pump(0.05)
    pin = pins_of(window)[0]
    assert float(pin["x"]) == pytest.approx(pin["src"][0])
    assert float(pin["y"]) == pytest.approx(pin["src"][1])
    clip = window.engine.timeline.find_clip(clip_of(window).id)
    assert CH.key_records(clip, CH.pin_channel("puppet_warp", pin["id"], "x")) == []


def test_copy_and_paste_pin_keys_onto_another_pin(editor):
    window = editor
    point = add_pin(window, 200, 120)
    drag(window.preview_panel.view, point, QPointF(point.x() + 40, point.y() + 30))
    add_pin(window, 420, 240)
    pump(0.05)
    first, second = pins_of(window)
    first_at = pin_xy(window, first["id"])
    second_at = pin_xy(window, second["id"])
    click(window.preview_panel.view, window.overlay.to_widget(
        int(first_at[0] * 640), int(first_at[1] * 360)))
    pump(0.05)
    assert window.overlay._selected_pins == {first["id"]}
    assert window.overlay.copy_pin_keys()
    click(window.preview_panel.view, window.overlay.to_widget(
        int(second_at[0] * 640), int(second_at[1] * 360)))
    pump(0.05)
    assert window.overlay._selected_pins == {second["id"]}
    window.engine.session.set_playhead(20)
    pump(0.05)
    assert window.overlay.paste_pin_keys()
    pump(0.05)
    clip = window.engine.timeline.find_clip(clip_of(window).id)
    pasted = CH.key_records(clip, CH.pin_channel("puppet_warp", second["id"], "x"))
    assert pasted, "the paste must create keys"
    assert min(key[0] for key in pasted) == 20, "the pasted keys start at the playhead"


def test_delete_removes_all_selected_pins_in_one_undo_step(editor):
    window = editor
    add_pin(window, 200, 120)
    add_pin(window, 420, 240)
    view = window.preview_panel.view
    click(view, window.overlay.to_widget(200, 120))
    click(view, window.overlay.to_widget(420, 240), Qt.KeyboardModifier.ShiftModifier)
    pump(0.05)
    assert len(window.overlay._selected_pins) == 2
    before = window.qt_undo_stack.undo_stack.count()
    assert window.overlay.delete_selection()
    pump(0.05)
    assert pins_of(window) == []
    assert window.qt_undo_stack.undo_stack.count() == before + 1
    window.engine.session.stack.undo()
    pump(0.05)
    assert len(pins_of(window)) == 2


def test_select_all_and_clear_selection(editor):
    window = editor
    add_pin(window, 200, 120)
    add_pin(window, 420, 240)
    assert window.overlay.select_all_pins()
    assert len(window.overlay.selected_pins()) == 2
    assert window.overlay.clear_pin_selection()
    assert window.overlay.selected_pins() == []
