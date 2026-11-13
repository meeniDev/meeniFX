"""Mouse-driven Pen/mask tests, mask coordinate spaces and vector paint checks.

These tests send real ``QTest`` mouse and key events to the PreviewWidget and therefore
exercise the same code path a person's mouse does (widget hit testing, overlay modes,
cursor updates, session undo). They are automated: no human has performed a manual pass,
and nothing here claims otherwise.
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from meenifx.engine import EditorEngine
from meenifx.model import channels as CH
from meenifx.model import mask as MASK
from meenifx.model import path as PATHS
from meenifx.model import shape as SHAPE
from meenifx.model import transform as T
from meenifx.processing import masks as MASK_OPS
from meenifx.processing import shapes as SHAPE_OPS
from meenifx.ui.preview import PreviewWidget
from meenifx.ui.preview_transform import PreviewOverlay

NO_MOD = Qt.KeyboardModifier.NoModifier
LEFT = Qt.MouseButton.LeftButton


def pump(seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.003)


def preview(qapp, engine, size=(640, 360)):
    engine.source_width, engine.source_height = size
    engine.session.project_size = size
    view = PreviewWidget(quality_getter=lambda: 100)
    view.resize(*size)
    view.set_frame(np.zeros((size[1], size[0], 3), np.uint8), logical_size=size)
    view.show()
    pump()
    overlay = PreviewOverlay(view, engine, engine.session)
    view.overlay = overlay
    return view, overlay


def shape_clip(engine, box=(100, 100, 400, 200)):
    engine.source_width, engine.source_height = 640, 360
    engine.session.project_size = (640, 360)
    clip_id = engine.session.add_shape_clip("rectangle", box=box)
    clip = engine.timeline.find_clip(clip_id)
    engine.session.set_selection({clip_id})
    return clip


def widget_point(view, x, y):
    return view.overlay.to_widget(float(x), float(y)).toPoint()


def click(view, x, y, modifier=NO_MOD):
    """A real press+release at a project-space point."""
    QTest.mouseClick(view, LEFT, modifier, widget_point(view, x, y))
    pump(0.01)


def press(view, x, y, modifier=NO_MOD):
    QTest.mousePress(view, LEFT, modifier, widget_point(view, x, y))
    pump(0.01)


def move(view, x, y):
    QTest.mouseMove(view, widget_point(view, x, y))
    pump(0.01)


def release(view, x, y):
    QTest.mouseRelease(view, LEFT, NO_MOD, widget_point(view, x, y))
    pump(0.01)


def drag(view, start, end, modifier=NO_MOD):
    press(view, *start, modifier=modifier)
    move(view, *end)
    release(view, *end)


def project_points(engine, clip, mask_id, frame=0):
    mask = MASK.find(clip, mask_id)
    path = MASK.project_path(clip, mask, frame, engine.session.project_size)
    return [(point["x"], point["y"]) for point in PATHS.points(path)]


def test_qtest_clicks_drag_and_close_create_a_curved_closed_path(qapp):
    engine = EditorEngine(30)
    view, overlay = preview(qapp, engine)
    assert overlay.pen.set_mode("shape")
    overlay.mode = "pen"

    # Press-drag-release gives the first point a Bezier handle; clicks add corners.
    press(view, 120, 120)
    move(view, 160, 90)
    release(view, 160, 90)
    click(view, 300, 160)
    click(view, 120, 120)
    pump()

    paths = [clip for clip in engine.timeline.all_clips()
             if getattr(clip, "is_shape", False) and (clip.shape or {}).get("kind") == "path"]
    assert paths, "the mouse Pen did not create a custom path"
    points = PATHS.points(paths[0].shape["path"])
    assert paths[0].shape["path"]["closed"] is True
    assert len(points) == 2
    assert points[0]["out"] != [0.0, 0.0], "press-drag did not create a curve handle"
    assert overlay.mode == "select", "closing a path leaves the Pen tool"


def test_qtest_edits_a_mask_on_a_transformed_clip(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    T.set_channel(clip, "position_x", 40.0)
    T.set_channel(clip, "position_y", 20.0)
    T.set_channel(clip, "rotation", 15.0)
    T.set_channel(clip, "scale_x", 150.0)
    T.set_channel(clip, "scale_y", 150.0)
    mask_id = engine.session.add_clip_mask(clip.id, [(100, 100), (260, 100), (260, 200)])
    mask = MASK.find(clip, mask_id)
    assert mask["space"] == "layer"

    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    start = project_points(engine, clip, mask_id)[0]
    click(view, *start)
    assert overlay.mode == "pen"
    assert overlay.pen._editing == (clip.id, mask_id)
    assert len(MASK.masks(clip)) == 1

    drag(view, start, (start[0] + 30, start[1] + 20))
    moved = project_points(engine, clip, mask_id)[0]
    assert moved[0] == pytest.approx(start[0] + 30, abs=1.5)
    assert moved[1] == pytest.approx(start[1] + 20, abs=1.5)
    stored = PATHS.points(MASK.find(clip, mask_id)["path"])[0]
    # The mask is stored before the clip transform, so its stored point is not the
    # project point; both agree only through the transform matrix.
    assert (stored["x"], stored["y"]) != pytest.approx(moved)
    assert len(MASK.masks(clip)) == 1


def test_qtest_drags_a_bezier_handle(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [PATHS.point(120, 120, (0, 0), (40, 0), True),
                  PATHS.point(260, 120), PATHS.point(260, 220)], closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 120, 120)
    assert overlay.pen._editing == (clip.id, mask_id)
    click(view, 120, 120)

    drag(view, (160, 120), (170, 128))
    point = PATHS.points(MASK.find(clip, mask_id)["path"])[0]
    assert point["smooth"] is True
    assert point["out"][0] == pytest.approx(50.0, abs=1.5)
    assert point["out"][1] == pytest.approx(8.0, abs=1.5)


def test_qtest_t_key_converts_a_smooth_point_to_a_corner_and_undoes(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [PATHS.point(120, 120, (0, 0), (40, 0), True),
                  PATHS.point(260, 120), PATHS.point(260, 220)], closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 120, 120)
    click(view, 120, 120)
    assert 0 in overlay.pen.selected

    QTest.keyClick(view, Qt.Key.Key_T)
    pump(0.02)
    point = PATHS.points(MASK.find(clip, mask_id)["path"])[0]
    assert point["smooth"] is False
    assert point["in"] == [0.0, 0.0] and point["out"] == [0.0, 0.0]

    engine.session.undo()
    pump(0.02)
    restored_clip = engine.timeline.find_clip(clip.id)
    restored = PATHS.points(MASK.find(restored_clip, mask_id)["path"])[0]
    assert restored["smooth"] is True
    assert restored["out"][0] == pytest.approx(40.0, abs=1.5)


def test_qtest_moves_multiple_selected_points_together(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [(120, 120), (260, 120), (260, 220)], closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 120, 120)
    click(view, 120, 120)           # second click starts editing
    click(view, 260, 120)           # select point 1
    click(view, 260, 220, Qt.KeyboardModifier.ShiftModifier)   # add point 2
    assert overlay.pen.selected == {1, 2}

    drag(view, (260, 120), (280, 130))
    moved = project_points(engine, clip, mask_id)
    assert moved[1][0] == pytest.approx(280.0, abs=1.5)
    assert moved[2][0] == pytest.approx(280.0, abs=1.5)
    assert moved[2][1] == pytest.approx(230.0, abs=1.5)


def test_qtest_adds_a_point_on_a_segment_and_deletes_it(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [(120, 120), (300, 120), (300, 220)], closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 120, 120)
    click(view, 120, 120)
    before = len(PATHS.points(MASK.find(clip, mask_id)["path"]))

    sampled = PATHS.sampled(MASK.find(clip, mask_id)["path"], steps=20)
    first, second = sampled[8], sampled[9]
    midpoint = ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
    click(view, *midpoint)
    assert len(PATHS.points(MASK.find(clip, mask_id)["path"])) == before + 1

    QTest.keyClick(view, Qt.Key.Key_Delete)
    pump(0.02)
    assert len(PATHS.points(MASK.find(clip, mask_id)["path"])) == before


def test_qtest_path_keyframes_from_the_playhead_and_undo_redo(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [(120, 120), (300, 120), (300, 220)], closed=True)
    channel = CH.mask_path_channel(mask_id)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 120, 120)
    click(view, 120, 120)
    # Key the shape at frame 0 through the public channel API.
    engine.session.add_keyframe(clip.id, channel, 0)
    assert CH.key_times(clip, channel) == [0]
    # Masks are stored in layer space, so the stored path (not the project path) is what
    # the path channel interpolates.
    base_x = PATHS.points(MASK.find(clip, mask_id)["path"])[0]["x"]

    engine.session.set_playhead(clip.timeline_start + 12)
    pump(0.05)
    start = project_points(engine, clip, mask_id)[0]
    drag(view, start, (start[0] + 24, start[1] + 12))
    assert CH.key_times(clip, channel) == [0, 12]

    halfway = CH.value_at(clip, channel, 6)["points"][0]["x"]
    assert base_x < halfway < base_x + 24, "path keys interpolate between poses"

    engine.session.undo()
    restored = engine.timeline.find_clip(clip.id)
    assert CH.key_times(restored, channel) == [0]
    engine.session.redo()
    redone = engine.timeline.find_clip(clip.id)
    assert CH.key_times(redone, channel) == [0, 12]


def test_qtest_tool_switch_and_escape_leave_the_pen(qapp):
    engine = EditorEngine(30)
    view, overlay = preview(qapp, engine)
    overlay.pen.set_mode("shape")
    overlay.mode = "pen"
    press(view, 200, 200)

    QTest.keyClick(view, Qt.Key.Key_V)
    pump(0.02)
    assert overlay.mode == "select"
    assert not engine.timeline.all_clips(), "V must not commit the path"

    overlay.pen.set_mode("shape")
    overlay.mode = "pen"
    press(view, 210, 210)
    release(view, 210, 210)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    pump(0.02)
    assert overlay.mode == "select"
    assert not engine.timeline.all_clips()


def test_qtest_insert_anchor_preserves_the_curve(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [PATHS.point(120, 120, (0, 0), (80, -30), True),
                  PATHS.point(300, 140, (0, 0), (-40, 90), True),
                  PATHS.point(260, 280, (-30, -30), (0, 0), True)],
        closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 120, 120)
    click(view, 120, 120)
    stored_before = PATHS.sampled(MASK.find(clip, mask_id)["path"], steps=64)
    before_count = len(PATHS.points(MASK.find(clip, mask_id)["path"]))

    # Insert on the curve between the first two anchors, not on a straight chord.
    t = 0.35
    project_path = MASK.project_path(clip, MASK.find(clip, mask_id), 0,
                                    engine.session.project_size)
    p0, c0, c1, p1 = PATHS.segments(project_path)[0]
    mt = 1.0 - t
    point = (mt ** 3 * p0[0] + 3 * mt * mt * t * c0[0]
             + 3 * mt * t * t * c1[0] + t ** 3 * p1[0],
             mt ** 3 * p0[1] + 3 * mt * mt * t * c0[1]
             + 3 * mt * t * t * c1[1] + t ** 3 * p1[1])
    click(view, *point)
    after = MASK.find(clip, mask_id)["path"]
    assert len(PATHS.points(after)) == before_count + 1
    stored_after = PATHS.sampled(after, steps=64)
    # Sampling redistributes parameters but not geometry: every point on one sampled
    # curve must lie close to the other, otherwise the insertion cornered the segment.
    def nearest(point, samples):
        return min(abs(point[0] - other[0]) + abs(point[1] - other[1])
                   for other in samples)
    assert max(nearest(point, stored_after) for point in stored_before) < 2.5
    assert max(nearest(point, stored_before) for point in stored_after) < 2.5



def test_qtest_shift_constrains_new_points_and_anchor_moves(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [(120, 120), (300, 120), (300, 220)], closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 120, 120)
    click(view, 120, 120)
    click(view, 300, 120)
    click(view, 300, 220, Qt.KeyboardModifier.ShiftModifier)
    drag(view, (300, 120), (330, 150), modifier=Qt.KeyboardModifier.ShiftModifier)
    moved = project_points(engine, clip, mask_id)
    assert abs((moved[1][0] - 300) - (moved[1][1] - 120)) < 1.2
    assert abs((moved[2][0] - 300) - (moved[2][1] - 220)) < 1.2

    # A fresh shape path also honours Shift while placing a point.
    engine2 = EditorEngine(30)
    view2, overlay2 = preview(qapp, engine2)
    assert overlay2.pen.set_mode("shape")
    click(view2, 200, 200)
    click(view2, 350, 340, Qt.KeyboardModifier.ShiftModifier)
    points = PATHS.points(overlay2.pen.path)
    dx = points[1]["x"] - 200
    dy = points[1]["y"] - 200
    assert dx > 0 and dy > 0
    assert abs(dx - dy) < 0.7, "Shift did not snap the new point to 45 degrees"



def test_qtest_alt_breaks_paired_handles_and_ctrl_direct_selects(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [PATHS.point(140, 140, (-40, 0), (40, 0), True),
                  PATHS.point(320, 140), PATHS.point(320, 260)], closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"
    click(view, 140, 140)
    click(view, 140, 140)
    before_count = len(PATHS.points(MASK.find(clip, mask_id)["path"]))
    click(view, 560, 300, Qt.KeyboardModifier.ControlModifier)
    assert len(PATHS.points(overlay.pen.path)) == before_count, \
        "Ctrl-click must not insert another anchor"
    assert not overlay.pen.selected

    # Alt-dragging the outgoing handle breaks the pair; the incoming handle stays put.
    # QTest.mouseMove has no modifier argument, so hold Alt as a real key for the drag.
    QTest.keyPress(view, Qt.Key.Key_Alt)
    press(view, 180, 140, modifier=Qt.KeyboardModifier.AltModifier)
    position = widget_point(view, 200, 170)
    QApplication.sendEvent(view, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(position), QPointF(view.mapToGlobal(position)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.AltModifier))
    pump(0.01)
    release(view, 200, 170)
    QTest.keyRelease(view, Qt.Key.Key_Alt)
    pump(0.01)
    point = PATHS.points(MASK.find(clip, mask_id)["path"])[0]
    assert point["smooth"] is False
    assert point["out"][0] == pytest.approx(60.0, abs=2.0)
    assert point["out"][1] == pytest.approx(30.0, abs=2.0)
    assert point["in"] == pytest.approx([-40.0, 0.0], abs=0.5)



def test_qtest_pen_draft_undo_redo_is_one_step_per_gesture(qapp):
    engine = EditorEngine(30)
    view, overlay = preview(qapp, engine)
    assert overlay.pen.set_mode("shape")
    click(view, 100, 100)
    press(view, 240, 180)
    move(view, 300, 120)
    release(view, 300, 120)
    assert len(PATHS.points(overlay.pen.path)) == 2
    # Two complete gestures (a click and a click-drag) produced exactly two drafts.
    assert len(overlay.pen._history) == 2
    assert overlay.pen.undo()
    assert len(PATHS.points(overlay.pen.path)) == 1
    assert overlay.pen.redo()
    assert len(PATHS.points(overlay.pen.path)) == 2


def test_qtest_edits_a_transformed_custom_shape_path_with_undo(qapp):
    engine = EditorEngine(30)
    engine.source_width, engine.source_height = 640, 360
    engine.session.project_size = (640, 360)
    clip_id = engine.session.add_path_shape_clip(
        [PATHS.point(140, 100, (0, 0), (70, 20), True),
         PATHS.point(360, 120, (0, 0), (-40, 80), True),
         PATHS.point(320, 260, (-40, -20), (0, 0), True)], closed=True)
    clip = engine.timeline.find_clip(clip_id)
    T.set_channel(clip, "position_x", 35.0)
    T.set_channel(clip, "position_y", -18.0)
    T.set_channel(clip, "rotation", 12.0)
    T.set_channel(clip, "scale_x", 118.0)
    T.set_channel(clip, "scale_y", 118.0)
    engine.session.set_selection({clip_id})

    view, overlay = preview(qapp, engine)
    matrix = overlay.matrix(clip)
    project_before = PATHS.transform(PATHS.at(clip.shape["path"], 0), matrix)
    start = project_before["points"][0]
    click(view, start["x"], start["y"])
    assert overlay.mode == "pen"
    assert overlay.pen._editing_shape == clip_id

    drag(view, (start["x"], start["y"]), (start["x"] + 24, start["y"] + 14))
    project_after = PATHS.transform(
        PATHS.at(engine.timeline.find_clip(clip_id).shape["path"], 0), matrix)
    moved = project_after["points"][0]
    assert moved["x"] == pytest.approx(start["x"] + 24, abs=1.5)
    assert moved["y"] == pytest.approx(start["y"] + 14, abs=1.5)

    engine.session.undo()
    restored = PATHS.transform(
        PATHS.at(engine.timeline.find_clip(clip_id).shape["path"], 0), matrix)
    assert restored["points"][0]["x"] == pytest.approx(start["x"], abs=1.5)


def test_layer_masks_follow_transforms_and_legacy_masks_stay_fixed():
    engine = EditorEngine(30)
    engine.source_width, engine.source_height = 640, 360
    engine.session.project_size = (640, 360)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(clip.id, [(100, 100), (200, 100), (200, 200)])
    before = project_points(engine, clip, mask_id)
    T.set_channel(clip, "position_x", float(clip.transform["position_x"]) + 80.0)
    after = project_points(engine, clip, mask_id)
    assert after[0][0] == pytest.approx(before[0][0] + 80.0)
    assert after[0][1] == pytest.approx(before[0][1])

    # A project saved before spaces existed has no "space" key and must not move.
    legacy = {"id": 7, "name": "Legacy",
              "path": {"points": [(10, 10), (90, 10), (90, 90)], "closed": True}}
    clip.transform_meta["masks"] = [legacy]
    stored = MASK.masks(clip)[0]
    assert stored["space"] == "composition"
    legacy_point = MASK.at(clip, 0, (640, 360))[0]["path"]["points"][0]
    assert (legacy_point["x"], legacy_point["y"]) == pytest.approx((10.0, 10.0))


def test_layer_mask_follows_an_animated_transform_and_composition_does_not():
    engine = EditorEngine(30)
    engine.source_width, engine.source_height = 640, 360
    engine.session.project_size = (640, 360)
    clip = shape_clip(engine)
    T.set_channel(clip, "position_x", 0.0)
    T.set_channel(clip, "position_y", 0.0)
    mask_id = engine.session.add_clip_mask(clip.id, [(100, 100), (200, 100), (200, 200)])
    T.animate_channel(clip, "position_x", 0)
    T.set_channel(clip, "position_x", 120.0, 20, auto_key=True)

    at_start = np.asarray(project_points(engine, clip, mask_id, 0))
    at_end = np.asarray(project_points(engine, clip, mask_id, 20))
    assert at_end[0][0] == pytest.approx(at_start[0][0] + 120.0)

    assert engine.session.set_clip_mask_space(clip.id, mask_id, "composition")
    fixed_start = np.asarray(project_points(engine, clip, mask_id, 0))
    fixed_end = np.asarray(project_points(engine, clip, mask_id, 20))
    assert np.allclose(fixed_start, fixed_end), \
        "a composition-space mask must not follow the animated transform"


def test_mask_space_conversion_keeps_the_look_at_the_playhead():
    engine = EditorEngine(30)
    engine.source_width, engine.source_height = 640, 360
    engine.session.project_size = (640, 360)
    clip = shape_clip(engine)
    T.set_channel(clip, "rotation", 25.0)
    T.set_channel(clip, "scale_x", 160.0)
    T.set_channel(clip, "scale_y", 160.0)
    mask_id = engine.session.add_clip_mask(clip.id, [(120, 120), (240, 120), (240, 220)])
    before = np.asarray(project_points(engine, clip, mask_id))
    assert engine.session.set_clip_mask_space(clip.id, mask_id, "composition")
    assert MASK.find(clip, mask_id)["space"] == "composition"
    assert np.allclose(project_points(engine, clip, mask_id), before)
    assert engine.session.set_clip_mask_space(clip.id, mask_id, "layer")
    assert MASK.find(clip, mask_id)["space"] == "layer"
    assert np.allclose(project_points(engine, clip, mask_id), before)


def _masked_frame(offset):
    """A 64x64 white shape with a layer-space subtract mask, at a horizontal offset."""
    from meenifx.project.settings import ProjectSettings

    engine = EditorEngine(30)
    engine.apply_project_settings(ProjectSettings(name="Masks", width=64, height=64,
                                                  fps=30.0))
    clip_id = engine.session.add_shape_clip("rectangle", box=(0, 0, 32, 32))
    clip = engine.timeline.find_clip(clip_id)
    clip.shape.update({"fill_color": "#FFFFFF", "stroke_enabled": False})
    engine.session.set_selection({clip_id})
    T.set_channel(clip, "position_x", 0.0)
    T.set_channel(clip, "position_y", 0.0)
    engine.session.add_clip_mask(clip.id, [(24, 24), (40, 24), (40, 40), (24, 40)],
                                 mode="subtract")
    if offset:
        T.set_channel(engine.timeline.find_clip(clip_id), "position_x", float(offset))
    _raw, frame = engine.render_frame(0)
    return frame


def test_layer_masks_render_following_the_clip_transform():
    first = _masked_frame(0)
    assert first[37, 32].tolist() == [0, 0, 0], "the layer mask cuts the clip centre"
    assert first[20, 20].tolist() == [255, 255, 255]

    moved = _masked_frame(16)
    assert moved[37, 32][0] > 200, \
        "a layer-space mask moves with the clip instead of staying over the old spot"


def test_gradient_stop_colour_and_position_keyframes_interpolate_and_render():
    clip = shape_clip(EditorEngine(30))
    clip.shape.update({"kind": "rectangle", "gradient_enabled": True,
                       "stroke_enabled": False, "width": 240.0, "height": 160.0})
    clip.shape["gradient_stops"] = [{"position": 0, "color": "#FF0000"},
                                    {"position": 100, "color": "#0000FF"}]
    SHAPE.stop_set_value(clip, 0, "color", "#80FF0000", 0, True)
    SHAPE.stop_set_value(clip, 0, "color", "#FF00FF00", 20, True)
    blended = SHAPE.stop_value_at(clip, 0, "color", 10)
    parts = SHAPE._colour_parts(blended)
    assert 0 < parts[3] < 255, "alpha interpolates between the two keys"
    assert parts[0] > 0 and parts[1] > 0, "hue interpolates, not a held colour"

    SHAPE.stop_set_value(clip, 0, "position", 0.0, 0, True)
    SHAPE.stop_set_value(clip, 0, "position", 80.0, 20, True)
    assert SHAPE.stop_value_at(clip, 0, "position", 10) == pytest.approx(40.0)
    channel = SHAPE.stop_channel(0, "color")
    assert CH.key_times(clip, channel) == [0, 20]
    assert CH.value_at(clip, channel, 10) == blended

    first = SHAPE_OPS.render(SHAPE.resolve(clip, 0), 320, 240, 1.0)
    later = SHAPE_OPS.render(SHAPE.resolve(clip, 20), 320, 240, 1.0)
    assert not np.array_equal(first, later), "the gradient must animate with its keys"
    assert int(first[120, 160, 3]) < 255, "a half-transparent stop fades the fill"


def test_vector_strokes_use_real_joins_caps_dashes_and_alignment():
    def ink(**settings):
        clip = shape_clip(EditorEngine(30))
        clip.shape.update({"kind": "rectangle", "width": 200.0, "height": 140.0,
                           "fill_opacity": 0.0, "stroke_enabled": True,
                           "stroke_width": 16.0})
        clip.shape.update(settings)
        layer = SHAPE_OPS.render(SHAPE.resolve(clip, 0), 400, 300, 1.0)
        return int(layer[:, :, 3].sum())

    miter = ink(stroke_join="miter")
    bevel = ink(stroke_join="bevel")
    round_join = ink(stroke_join="round")
    assert len({miter, bevel, round_join}) == 3, "joins must be geometric, not identical"
    # A right-angled corner's miter ratio is below Qt's effective minimum limit, so the
    # limit is observable on an acute shape (a star's points) instead.
    assert ink(kind="star", stroke_join="miter", stroke_miter_limit=1.0) != \
        ink(kind="star", stroke_join="miter", stroke_miter_limit=10.0)
    assert ink(stroke_dash=14.0, stroke_gap=10.0) < miter, "dashes remove ink"
    center = ink(stroke_align="center")
    inside = ink(stroke_align="inside")
    outside = ink(stroke_align="outside")
    assert len({center, inside, outside}) == 3
    butt = ink(cap="butt", kind="line")
    assert butt != ink(cap="round", kind="line")
    assert ink(cap="square", kind="line") > butt, "square caps extend past a butt cap"

    # The same geometry at 2x render scale stays a stroke of the same project width:
    # pixel coverage grows with area, not with a fixed 1 px OpenCV line.
    clip = shape_clip(EditorEngine(30))
    clip.shape.update({"kind": "rectangle", "width": 200.0, "height": 140.0,
                       "fill_opacity": 0.0, "stroke_enabled": True, "stroke_width": 8.0})
    one = SHAPE_OPS.render(SHAPE.resolve(clip, 0), 400, 300, 1.0)
    two = SHAPE_OPS.render(SHAPE.resolve(clip, 0), 800, 600, 2.0)
    ratio = (two[:, :, 3] > 127).sum() / max(1, (one[:, :, 3] > 127).sum())
    assert ratio == pytest.approx(4.0, rel=0.25), ratio


def test_mask_space_and_gradient_keys_survive_project_serialization():
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(clip.id, [(120, 120), (260, 120), (260, 220)])
    engine.session.set_clip_mask_space(clip.id, mask_id, "composition")
    clip.shape.update({"gradient_enabled": True})
    clip.shape["gradient_stops"] = [{"position": 0, "color": "#FF0000"},
                                    {"position": 100, "color": "#0000FF"}]
    SHAPE.stop_set_value(clip, 0, "color", "#80FF0000", 0, True)
    SHAPE.stop_set_value(clip, 0, "color", "#FF00FF00", 20, True)

    sections = timeline_to_sections(engine.timeline, lambda path: path)
    restored = sections_to_timeline(sections, 30, lambda path: path)
    restored_clip = next(c for c in restored.all_clips() if c.id == clip.id)
    restored_mask = MASK.find(restored_clip, mask_id)
    assert restored_mask is not None
    assert restored_mask["space"] == "composition"
    assert SHAPE.stop_key_times(restored_clip, 0, "color") == [0, 20]
    assert SHAPE.stop_value_at(restored_clip, 0, "color", 10) == SHAPE.stop_value_at(
        clip, 0, "color", 10)
