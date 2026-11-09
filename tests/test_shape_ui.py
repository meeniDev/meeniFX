"""The shape tool in the real window: toolbar, live drag, modifiers, Escape, inspector.

Everything here drives the same entry points a user does - the toolbar action, mouse
events on the preview overlay, the Type combo - so a missing mode, a wrong modifier or a
card that never rebuilds fails here.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import shape as SHAPE  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

NONE = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier


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
    pump(0.25)
    yield editor
    editor.projects._set_clean()
    editor.close()


@pytest.fixture()
def editor(window, tmp_path):
    window.projects._set_clean()
    settings = ProjectSettings(name=f"ShapeUI {uuid.uuid4().hex[:6]}", width=640,
                               height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    window.engine.session.set_playhead(0)
    window.effects_panel._built_key = None
    pump(0.15)
    return window


def event(kind, pos, modifiers=NONE, button=Qt.MouseButton.LeftButton):
    point = pos if isinstance(pos, QPointF) else QPointF(pos[0], pos[1])
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, point, point, button, buttons, modifiers)


def drag(overlay, start, end, modifiers=NONE):
    overlay.mouse_press(event(QEvent.Type.MouseButtonPress, start, modifiers))
    overlay.mouse_move(event(QEvent.Type.MouseMove, end, modifiers))
    overlay.mouse_release(event(QEvent.Type.MouseButtonRelease, end, modifiers))
    pump(0.08)


def draw(window, kind, start, end, modifiers=NONE):
    overlay = window.overlay
    window._start_shape_tool(kind)
    pump(0.05)
    drag(overlay, overlay.to_widget(*start), overlay.to_widget(*end), modifiers)
    return window.engine.timeline.all_clips()


def test_the_toolbar_arms_the_shape_tool(editor):
    window = editor
    window._start_shape_tool("star")
    pump(0.05)
    assert window.overlay.mode == "shape"
    assert window.overlay.shape_kind == "star"
    assert window.shape_button.isChecked()
    window._set_overlay_mode("select")
    assert not window.shape_button.isChecked()


def test_the_add_menu_command_creates_a_centred_shape(editor):
    window = editor
    clip_id = window.add_shape("ellipse")
    pump(0.1)
    clip = window.engine.timeline.find_clip(clip_id)
    assert clip is not None and clip.is_shape
    assert abs(clip.transform["position_x"]) < 0.01
    assert abs(clip.transform["position_y"]) < 0.01


def test_dragging_in_the_preview_creates_a_selected_shape(editor):
    window = editor
    clips = draw(window, "rectangle", (80, 60), (280, 220))
    assert len(clips) == 1
    clip = clips[0]
    assert clip.is_shape and clip.id in window.engine.session.selected
    assert abs(clip.shape["width"] - 200) < 1.5
    assert abs(clip.shape["height"] - 160) < 1.5
    # The tool hands the new clip to the transform handles.
    assert window.overlay.mode == "transform"
    assert window.overlay.primary() is not None


def test_shift_draws_a_square_and_a_circle(editor):
    window = editor
    window.engine.session.set_selection(set())
    clips = draw(window, "ellipse", (80, 60), (280, 140), SHIFT)
    clip = clips[0]
    assert abs(clip.shape["width"] - clip.shape["height"]) < 1.5
    assert clip.shape["kind"] == "ellipse"


def test_alt_draws_from_the_centre(editor):
    window = editor
    clips = draw(window, "rectangle", (320, 180), (420, 260), ALT)
    clip = clips[0]
    assert abs(clip.shape["width"] - 200) < 2.0
    assert abs(clip.shape["height"] - 160) < 2.0
    assert abs(clip.transform["position_x"]) < 1.0
    assert abs(clip.transform["position_y"]) < 1.0


def test_escape_cancels_a_draft_without_creating_a_clip(editor):
    window = editor
    overlay = window.overlay
    before = len(window.engine.timeline.all_clips())
    window._start_shape_tool("polygon")
    overlay.mouse_press(event(QEvent.Type.MouseButtonPress, overlay.to_widget(100, 80)))
    overlay.mouse_move(event(QEvent.Type.MouseMove, overlay.to_widget(300, 220)))
    view = window.preview_panel.view
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, NONE))
    overlay.mouse_release(event(QEvent.Type.MouseButtonRelease,
                                overlay.to_widget(300, 220)))
    pump(0.08)
    assert len(window.engine.timeline.all_clips()) == before
    assert overlay.mode == "select"
    assert overlay._shape_draft is None


def test_the_inspector_shows_the_shape_card_and_switches_type(editor):
    window = editor
    clips = draw(window, "rectangle", (80, 60), (280, 220))
    clip = clips[0]
    pump(0.15)
    rows = [row for row in window.effects_panel._rows if row.get("kind") == "channel"]
    channels = {row.get("channel") for row in rows}
    for name in ("width", "height", "fill_opacity", "stroke_width", "stroke_opacity"):
        assert SHAPE.shape_channel(name) in channels
    # Corner radius only exists on the shape that has corners.
    assert SHAPE.shape_channel("radius") not in channels
    window.effects_panel._set_shape_kind(clip, "star")
    pump(0.15)
    channels = {row.get("channel") for row in window.effects_panel._rows
                if row.get("kind") == "channel"}
    assert SHAPE.shape_channel("points") in channels
    assert SHAPE.shape_channel("inner_ratio") in channels
    assert window.engine.timeline.find_clip(clip.id).shape["kind"] == "star"


def test_corner_handles_resize_the_shape_and_pin_the_opposite_corner(editor):
    window = editor
    clips = draw(window, "rectangle", (120, 90), (360, 270))
    clip = clips[0]
    overlay = window.overlay
    overlay.mode = "transform"
    pump(0.05)
    before = overlay.clip_corners(clip)
    nw_before = before[0]
    se = overlay.to_widget(*before[2])
    target = QPointF(se.x() - 60, se.y() - 40)
    overlay.mouse_press(event(QEvent.Type.MouseButtonPress, (se.x(), se.y())))
    overlay.mouse_move(event(QEvent.Type.MouseMove, (target.x(), target.y())))
    overlay.mouse_release(event(QEvent.Type.MouseButtonRelease, (target.x(), target.y())))
    pump(0.1)
    after = overlay.clip_corners(clip)
    assert abs(after[0][0] - nw_before[0]) < 0.6
    assert abs(after[0][1] - nw_before[1]) < 0.6
    assert abs(after[2][0] - before[2][0]) > 20, "the dragged corner did not move"


def test_transform_handles_wrap_the_shape_not_the_project(editor):
    window = editor
    clip_id = window.add_shape("rectangle")
    pump(0.12)
    clip = window.engine.timeline.find_clip(clip_id)
    overlay = window.overlay
    corners = overlay.clip_corners(clip)
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    assert 315.0 < max(xs) - min(xs) < 330.0, "the box must be the shape, not the frame"
    assert 175.0 < max(ys) - min(ys) < 195.0
    assert overlay.widget_rect_for(clip).width() < overlay.widget.frame_rect().width() * 0.7

    # Empty canvas around a small shape is not part of the object for hit-testing.
    window.engine.session.set_selection(set())
    overlay.mouse_press(event(QEvent.Type.MouseButtonPress, overlay.to_widget(60, 60)))
    overlay.mouse_release(event(QEvent.Type.MouseButtonRelease, overlay.to_widget(60, 60)))
    pump(0.05)
    assert clip_id not in window.engine.session.selected
    overlay.mouse_press(event(QEvent.Type.MouseButtonPress, overlay.to_widget(320, 180)))
    overlay.mouse_release(event(QEvent.Type.MouseButtonRelease, overlay.to_widget(320, 180)))
    pump(0.05)
    assert clip_id in window.engine.session.selected


def test_shape_box_follows_position_scale_rotation_and_anchor(editor):
    window = editor
    clip_id = window.add_shape("rectangle")
    pump(0.12)
    clip = window.engine.timeline.find_clip(clip_id)
    overlay = window.overlay
    centre = ((640 - 1) / 2.0, (360 - 1) / 2.0)

    def box():
        points = overlay.clip_corners(clip)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys),
                (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)

    clip.transform["position_x"] = 80.0
    clip.transform["position_y"] = -40.0
    clip.transform["scale_x"] = 50.0
    clip.transform["scale_y"] = 50.0
    moved = box()
    assert abs(moved[4] - (centre[0] + 80.0)) < 0.01
    assert abs(moved[5] - (centre[1] - 40.0)) < 0.01
    assert abs(moved[2] - 161.0) < 0.1 and abs(moved[3] - 91.0) < 0.1

    clip.transform["rotation"] = 90.0
    rotated = box()
    assert abs(rotated[2] - 91.0) < 0.1
    assert abs(rotated[3] - 161.0) < 0.1
    assert abs(rotated[4] - moved[4]) < 0.01 and abs(rotated[5] - moved[5]) < 0.01

    clip.transform["rotation"] = 0.0
    clip.transform["anchor_x"] = 40.0
    anchored = box()
    assert abs(anchored[4] - (moved[4] - 20.0)) < 0.01, "anchor must remain the pivot"


def test_backspace_does_not_eat_the_shape_tool(editor):
    """Escape leaves the tool; a plain click then selects instead of drawing."""
    window = editor
    overlay = window.overlay
    window._start_shape_tool("line")
    overlay.mouse_press(event(QEvent.Type.MouseButtonPress, overlay.to_widget(80, 80)))
    overlay.mouse_move(event(QEvent.Type.MouseMove, overlay.to_widget(200, 160)))
    overlay.mouse_release(event(QEvent.Type.MouseButtonRelease, overlay.to_widget(200, 160)))
    pump(0.08)
    clip = window.engine.timeline.all_clips()[0]
    assert clip.shape["kind"] == "line"
    assert abs(float(clip.transform["rotation"])) > 10.0
