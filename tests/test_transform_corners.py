"""Free-transform corner handles: the opposite corner must stay pinned.

Every test here drives the real preview widget with mouse events and then measures the
fixed corner in composition space, so a wrong pivot, a cumulative update, a modifier jump
or a hidden aspect lock fails even when the resulting scale numbers look plausible.
"""

import os
import sys
import time
import uuid

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import transform as T  # noqa: E402
from meenifx.model.keyframes import AnimatedValue  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")
PROJECT = (640, 360)
TOL = 0.05                 # project pixels the pinned corner may drift
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
FIXED = {"nw": 2, "ne": 3, "se": 0, "sw": 1}          # TL, TR, BR, BL
# A drag that pulls each corner inward, in widget pixels.
INWARD = {"nw": (40, 60), "ne": (-40, 60), "se": (-40, -60), "sw": (40, -60)}


def pump(seconds=0.1):
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
    settings = ProjectSettings(name=f"FreeXf {uuid.uuid4().hex[:6]}", width=640, height=360,
                               fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    window.engine.session.set_playhead(0)
    pump(0.08)
    return window


# ----------------------------------------------------------------------- helpers
def insert(window, path, width, height):
    from meenifx.project.media import media_type_for
    project = window.projects.project
    item = project.library.add(path, 3.0, width, height, 30.0,
                               media_type=media_type_for(path))
    window.project_panel.rebuild(force=True)
    clips = window.insert_media_items([item], frame=0)
    assert clips, "the media was not inserted"
    clip = window.engine.timeline.find_clip(clips[0])
    window.engine.session.set_selection({clip.id})
    pump(0.12)
    return clip


def add_still(window):
    return insert(window, STILL, 320, 180)


def current(window, clip):
    return window.engine.timeline.find_clip(clip.id)


def corners_of(window, clip):
    live = current(window, clip)
    return T.content_corners(T.evaluate(live, 0), window.engine.project_size(),
                             window.overlay.image_size(live))


def send(window, kind, point, modifiers=NONE):
    view = window.preview_panel.view
    event = QMouseEvent(kind, QPointF(point), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, modifiers)
    if kind == QMouseEvent.Type.MouseButtonPress:
        view.mousePressEvent(event)
    elif kind == QMouseEvent.Type.MouseMove:
        view.mouseMoveEvent(event)
    else:
        view.mouseReleaseEvent(event)
    return event


def grab(window, clip, handle, modifiers=NONE):
    point = window.overlay.to_widget(*window.overlay.handle_points(current(window, clip))[handle])
    send(window, QMouseEvent.Type.MouseButtonPress, point, modifiers)
    return point


def slide(window, start, dx, dy, steps=6, modifiers=NONE):
    for index in range(1, steps + 1):
        send(window, QMouseEvent.Type.MouseMove,
             QPointF(start.x() + dx * index / steps, start.y() + dy * index / steps),
             modifiers)
    send(window, QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() + dx, start.y() + dy),
         modifiers)
    pump(0.08)


def assert_point(actual, expected, tolerance=TOL):
    assert abs(actual[0] - expected[0]) <= tolerance, f"x moved {actual[0] - expected[0]:+.4f}"
    assert abs(actual[1] - expected[1]) <= tolerance, f"y moved {actual[1] - expected[1]:+.4f}"


# ------------------------------------------------------------------- free resize
@pytest.mark.parametrize("handle", ["nw", "ne", "se", "sw"])
def test_every_corner_pins_the_opposite_corner_and_scales_freely(editor, handle):
    window = editor
    clip = add_still(window)
    before = corners_of(window, clip)[FIXED[handle]]
    start = grab(window, clip, handle)
    slide(window, start, *INWARD[handle])
    state = T.evaluate(current(window, clip), 0)
    assert_point(corners_of(window, clip)[FIXED[handle]], before)
    assert state.scale_x < 99.0 and state.scale_y < 99.0, "the corner drag did not shrink"
    assert abs(state.scale_x - state.scale_y) > 2.0, \
        "a plain corner drag must be non-uniform, not an aspect-locked resize"


@pytest.mark.parametrize("handle", ["nw", "ne", "se", "sw"])
def test_ctrl_pins_the_corner_and_keeps_the_aspect(editor, handle):
    window = editor
    clip = add_still(window)
    before = corners_of(window, clip)[FIXED[handle]]
    start = grab(window, clip, handle, CTRL)
    slide(window, start, *INWARD[handle], modifiers=CTRL)
    state = T.evaluate(current(window, clip), 0)
    assert_point(corners_of(window, clip)[FIXED[handle]], before)
    assert state.scale_x < 100.0
    assert state.scale_x == pytest.approx(state.scale_y, rel=1e-6), \
        "Ctrl must hold the ratio the drag started with"


def test_ctrl_never_resets_the_size_and_releasing_it_does_not_jump(editor):
    window = editor
    clip = add_still(window)
    start = grab(window, clip, "se")
    first = QPointF(start.x() - 24, start.y() - 44)
    send(window, QMouseEvent.Type.MouseMove, first)
    pump(0.05)
    free = T.evaluate(current(window, clip), 0)
    assert free.scale_x < 100.0, "the drag moved nothing before the modifier change"

    # Press Ctrl without moving the pointer: rebasing must change nothing at all.
    send(window, QMouseEvent.Type.MouseMove, first, CTRL)
    pump(0.05)
    switched = T.evaluate(current(window, clip), 0)
    assert switched.scale_x == pytest.approx(free.scale_x, abs=1e-6)
    assert switched.scale_y == pytest.approx(free.scale_y, abs=1e-6)
    assert switched.scale_x != pytest.approx(100.0), "Ctrl must not reset the size"
    fixed = corners_of(window, clip)[0]

    # Keep dragging with Ctrl: the ratio captured at the press is preserved.
    held = QPointF(first.x() - 40, first.y() - 10)
    send(window, QMouseEvent.Type.MouseMove, held, CTRL)
    pump(0.05)
    locked = T.evaluate(current(window, clip), 0)
    assert locked.scale_x / locked.scale_y == pytest.approx(
        switched.scale_x / switched.scale_y, rel=1e-6)
    assert_point(corners_of(window, clip)[0], fixed)

    # Release Ctrl without moving: no jump either, and the axes are free again.
    send(window, QMouseEvent.Type.MouseMove, held, NONE)
    pump(0.05)
    released = T.evaluate(current(window, clip), 0)
    assert released.scale_x == pytest.approx(locked.scale_x, abs=1e-6)
    assert released.scale_y == pytest.approx(locked.scale_y, abs=1e-6)
    send(window, QMouseEvent.Type.MouseMove, QPointF(held.x() - 50, held.y() - 8), NONE)
    send(window, QMouseEvent.Type.MouseButtonRelease, QPointF(held.x() - 50, held.y() - 8))
    pump(0.08)
    final = T.evaluate(current(window, clip), 0)
    assert final.scale_x / final.scale_y != pytest.approx(locked.scale_x / locked.scale_y,
                                                          rel=1e-3)
    assert_point(corners_of(window, clip)[0], fixed)


def test_shift_resizes_from_the_centre_and_ctrl_shift_keeps_the_ratio(editor):
    window = editor
    clip = add_still(window)
    centre = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    start = grab(window, clip, "se", SHIFT)
    slide(window, start, -50, -30, modifiers=SHIFT)
    state = T.evaluate(current(window, clip), 0)
    # Both sides moved, so the pinned point is the middle, not the opposite corner.
    assert abs(state.scale_x - state.scale_y) > 1.0
    centre_after = window.overlay.to_widget(*window.overlay.anchor_point(current(window, clip)))
    assert abs(centre_after.x() - centre.x()) < 1.0, "a centre resize must not move the middle"
    assert abs(centre_after.y() - centre.y()) < 1.0

    clip = add_still(window)
    centre = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    start = grab(window, clip, "se", CTRL | SHIFT)
    slide(window, start, -50, -30, modifiers=CTRL | SHIFT)
    state = T.evaluate(current(window, clip), 0)
    assert state.scale_x == pytest.approx(state.scale_y, rel=1e-6)
    centre_after = window.overlay.to_widget(*window.overlay.anchor_point(current(window, clip)))
    assert abs(centre_after.x() - centre.x()) < 1.0
    assert abs(centre_after.y() - centre.y()) < 1.0, "Ctrl+Shift resizes around the middle"


def test_side_handle_changes_one_axis_only(editor):
    window = editor
    clip = add_still(window)
    tl, _tr, _br, bl = corners_of(window, clip)
    start = grab(window, clip, "e")
    slide(window, start, 60, 0)
    state = T.evaluate(current(window, clip), 0)
    assert state.scale_x > 101.0
    assert state.scale_y == pytest.approx(100.0, abs=1e-9)
    after_tl, _a, _b, after_bl = corners_of(window, clip)
    assert_point(after_tl, tl)
    assert_point(after_bl, bl)


def test_a_resize_never_flips_or_collapses_the_picture(editor):
    window = editor
    clip = add_still(window)
    before = corners_of(window, clip)[0]
    start = grab(window, clip, "se")
    slide(window, start, -3000, -3000, steps=4)      # far past the opposite corner
    state = T.evaluate(current(window, clip), 0)
    assert state.scale_x > 0.0 and state.scale_y > 0.0, "the resize flipped the layer"
    assert abs(state.scale_x) / 100.0 * 640.0 >= 2.5
    # The pinned corner still cannot move, so the box reported is not a lie.
    assert_point(corners_of(window, clip)[0], before)


# ------------------------------------------------------- awkward existing states
@pytest.mark.parametrize("handle", ["se", "nw"])
def test_a_rotated_picture_pins_the_opposite_corner(editor, handle):
    window = editor
    clip = add_still(window)
    T.ensure_transform(clip)["rotation"] = 32.0
    window.engine.session.changed.emit()
    pump(0.1)
    before = corners_of(window, clip)[FIXED[handle]]
    start = grab(window, clip, handle)
    slide(window, start, *INWARD[handle])
    state = T.evaluate(current(window, clip), 0)
    assert abs(state.rotation - 32.0) < 1e-6, "resizing must not rotate the picture"
    assert abs(state.scale_x - state.scale_y) > 2.0
    assert_point(corners_of(window, clip)[FIXED[handle]], before)


def test_a_non_square_picture_pins_the_corner(editor, tmp_path):
    window = editor
    path = str(tmp_path / "portrait.png")
    assert cv2.imwrite(path, np.full((500, 200, 3), 110, np.uint8))
    clip = insert(window, path, 200, 500)
    before = corners_of(window, clip)[FIXED["se"]]
    start = grab(window, clip, "se")
    slide(window, start, -30, -60)
    state = T.evaluate(current(window, clip), 0)
    assert abs(state.scale_x - state.scale_y) > 2.0
    assert_point(corners_of(window, clip)[FIXED["se"]], before)


def test_a_non_uniformly_stretched_picture_keeps_its_own_ratio_under_ctrl(editor):
    window = editor
    clip = add_still(window)
    transform = T.ensure_transform(clip)
    transform["scale_x"], transform["scale_y"] = 150.0, 70.0
    window.engine.session.changed.emit()
    pump(0.1)
    before = corners_of(window, clip)[FIXED["se"]]
    start = grab(window, clip, "se", CTRL)
    slide(window, start, -30, -10, modifiers=CTRL)
    state = T.evaluate(current(window, clip), 0)
    assert state.scale_x / state.scale_y == pytest.approx(150.0 / 70.0, rel=1e-6)
    assert state.scale_x < 150.0, "Ctrl must keep the ratio, not restore the size"
    assert_point(corners_of(window, clip)[FIXED["se"]], before)


def test_a_custom_anchor_does_not_become_the_fixed_corner(editor):
    window = editor
    clip = add_still(window)
    transform = T.ensure_transform(clip)
    transform["anchor_x"], transform["anchor_y"] = 120.0, -80.0
    window.engine.session.changed.emit()
    pump(0.1)
    before = corners_of(window, clip)[0]
    anchor = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    start = grab(window, clip, "se")
    slide(window, start, -40, -50)
    assert_point(corners_of(window, clip)[0], before)
    anchor_after = window.overlay.to_widget(*window.overlay.anchor_point(current(window, clip)))
    assert abs(anchor_after.x() - anchor.x()) > 2.0, "the anchor must travel with the picture"


@pytest.mark.parametrize("zoom", [0.5, 1.0, 2.0])
def test_the_fixed_corner_holds_at_every_preview_zoom(editor, zoom):
    window = editor
    clip = add_still(window)
    view = window.preview_panel.view
    view.set_zoom(zoom)
    pump(0.12)
    before = corners_of(window, clip)[0]
    before_widget = window.overlay.to_widget(*before)
    start = grab(window, clip, "se")
    slide(window, start, -20, -30)
    after = corners_of(window, clip)[0]
    assert_point(after, before)
    after_widget = window.overlay.to_widget(*after)
    assert abs(after_widget.x() - before_widget.x()) <= 0.5
    assert abs(after_widget.y() - before_widget.y()) <= 0.5


def test_a_text_layer_resizes_from_its_corner(editor):
    window = editor
    clip_id = window.add_text_clip()
    assert clip_id is not None
    window.engine.session.set_selection({clip_id})
    pump(0.15)
    clip = window.engine.timeline.find_clip(clip_id)
    assert clip.is_text
    before = corners_of(window, clip)[0]
    start = grab(window, clip, "se")
    slide(window, start, -30, -30)
    state = T.evaluate(current(window, clip), 0)
    assert state.scale_x < 99.0 and state.scale_y < 99.0
    assert abs(state.scale_x - state.scale_y) > 0.5
    assert_point(corners_of(window, clip)[0], before)


# ------------------------------------------------------------- undo and keyframes
def test_one_drag_is_one_undo_step_and_auto_keyframe_writes_keys(editor):
    window = editor
    clip = add_still(window)
    window.autokey_button.setChecked(True)
    window.engine.session.set_playhead(0)
    pump(0.08)
    depth = window.qt_undo_stack.undo_stack.count()
    start = grab(window, clip, "se")
    slide(window, start, -34, -46)
    assert window.qt_undo_stack.undo_stack.count() == depth + 1, \
        "one drag must be exactly one undo step"
    keyed = current(window, clip)
    assert isinstance(keyed.transform["scale_x"], AnimatedValue)
    assert isinstance(keyed.transform["scale_y"], AnimatedValue)
    assert keyed.transform["scale_x"].value_at(0) < 100.0
    window.engine.session.undo()
    pump(0.08)
    restored = current(window, clip)
    assert restored.transform["scale_x"] == 100.0
    assert restored.transform["position_x"] == 0.0


def test_dragging_inside_moves_without_resizing(editor):
    window = editor
    clip = add_still(window)
    start = window.overlay.to_widget(*window.overlay.handle_points(clip)["nw"])
    inside = QPointF(start.x() + 120, start.y() + 90)
    send(window, QMouseEvent.Type.MouseButtonPress, inside)
    send(window, QMouseEvent.Type.MouseMove, QPointF(inside.x() + 40, inside.y() + 20))
    send(window, QMouseEvent.Type.MouseButtonRelease, QPointF(inside.x() + 40, inside.y() + 20))
    pump(0.08)
    state = T.evaluate(current(window, clip), 0)
    assert state.scale_x == pytest.approx(100.0, abs=1e-9)
    assert state.scale_y == pytest.approx(100.0, abs=1e-9)
    assert abs(state.position_x) > 1.0
