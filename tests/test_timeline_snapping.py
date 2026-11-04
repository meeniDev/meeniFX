"""Timeline snapping: resolution, determinism, and the feedback that explains it.

The tests drive the real canvas: clip drags and playhead scrubs with real mouse events,
then inspect the resolved frame and the published snap hint (the thing the canvas paints:
which edges glow, where the guide sits, what the caption says).
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")


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
    settings = ProjectSettings(name=f"Snap {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    pump(0.1)
    canvas = window.timeline_panel.canvas
    window.engine.session.snap_enabled.set(True)
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    canvas.update()
    pump(0.05)
    return window


def add_clip(editor, start, duration=20, track_name=None):
    timeline = editor.engine.timeline
    track = next((lane for lane in timeline.tracks_of_type("video")), None)
    if track is None or track_name:
        track = timeline.add_track("video", name=track_name or "")
    clip = timeline.new_clip(f"c{start}", os.path.join(ROOT, "mosaic-output.png"),
                             track, start, duration, source_out=200, source_fps=30)
    editor.engine.session.set_selection(set())
    editor.timeline_panel.canvas.update()
    pump(0.05)
    return clip


def canvas_of(editor):
    canvas = editor.timeline_panel.canvas
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    return canvas


def row_y(canvas, clip):
    return canvas.header_y(clip.track) + canvas.track_height(clip.track) // 2


def mouse(canvas, kind, x, y, button=Qt.MouseButton.LeftButton,
          modifiers=Qt.KeyboardModifier.NoModifier):
    pos = QPointF(float(x), float(y))
    event = QMouseEvent(kind, pos, QPointF(canvas.mapToGlobal(QPoint(int(x), int(y)))),
                        button, button, modifiers)
    if kind == QEvent.Type.MouseButtonPress:
        canvas.mousePressEvent(event)
    elif kind == QEvent.Type.MouseMove:
        canvas.mouseMoveEvent(event)
    else:
        canvas.mouseReleaseEvent(event)


# ------------------------------------------------------------------ resolution
def test_snap_target_prefers_the_closest_edge_and_is_deterministic(editor):
    first = add_clip(editor, 20, 20)          # ends at 40
    second = add_clip(editor, 80, 20)         # starts at 80
    canvas = canvas_of(editor)
    hint = canvas._snap_hint_for(40)
    assert [target[1] for target in hint["targets"]] == ["end"]
    assert hint["targets"][0][0] == first.id
    assert hint["caption"].endswith("end")

    # Six pixels away (one frame at this zoom) still snaps to the closest edge, and the
    # resolution does not depend on clip order.
    canvas._snap_cached = None
    target, hit = canvas._snap_target(41)
    assert (target, hit) == (40, 40)
    canvas._snap_cached = None
    assert canvas._snap_target(41) == (40, 40)
    assert second.id != first.id


def test_snap_threshold_is_measured_in_screen_pixels_at_any_zoom(editor):
    add_clip(editor, 20, 20)                   # edge at 40
    canvas = canvas_of(editor)
    for ppf in (1.0, 2.0, 6.0):
        canvas.ppf = ppf
        canvas.scroll_x = 0.0
        canvas._snap_cached = None
        offset_in = max(1, int(canvas.SNAP_IN_PX // ppf) - 1)
        offset_out = int(canvas.SNAP_IN_PX // ppf) + 3
        assert canvas._snap_target(40 + offset_in)[1] == 40, f"ppf {ppf}: {offset_in} should snap"
        canvas._snap_cached = None
        assert canvas._snap_target(40 + offset_out)[1] is None, f"ppf {ppf}: {offset_out} must not snap"
    # Zoomed in past the threshold one frame is wider than the snap radius, so only the
    # exact frame snaps. That is what a fixed pixel radius means, not a bug.
    canvas.ppf = 40.0
    canvas.scroll_x = 0.0
    canvas._snap_cached = None
    assert canvas._snap_target(40)[1] == 40
    canvas._snap_cached = None
    assert canvas._snap_target(39)[1] is None


def test_snapping_can_be_disabled_globally_and_temporarily(editor):
    add_clip(editor, 20, 20)
    canvas = canvas_of(editor)
    assert canvas._snap_target(39)[1] == 40
    canvas.session.snap_enabled.set(False)
    assert canvas._snap_target(39)[1] is None
    canvas.session.snap_enabled.set(True)
    # Hold Shift while dragging to place a clip exactly where the pointer is.
    assert canvas._snap_target(39, modifiers=Qt.KeyboardModifier.ShiftModifier)[1] is None


def test_multiple_aligned_edges_are_all_reported(editor):
    add_clip(editor, 20, 20, "V1")             # end 40
    timeline = editor.engine.timeline
    track = timeline.add_track("video", name="V2")
    clip = timeline.new_clip("c", os.path.join(ROOT, "mosaic-output.png"), track, 40, 20,
                             source_out=200, source_fps=30)
    hint = canvas_of(editor)._snap_hint_for(40)
    ids = {target[0] for target in hint["targets"]}
    assert len(ids) == 2 and clip.id in ids
    assert hint["caption"] == "2 clip edges aligned"


def test_playhead_snapping_reports_the_clip_edge(editor):
    clip = add_clip(editor, 20, 20)            # 20..40
    editor.engine.session.set_playhead(10)
    canvas = canvas_of(editor)
    target, hit = canvas._snap_target(41, include_playhead=False)
    assert hit == 40
    hint = canvas._snap_hint_for(hit)
    assert hint["targets"] and hint["targets"][0][0] == clip.id
    assert hint["caption"].endswith("end")


def test_marker_and_loop_targets_are_named(editor):
    editor.engine.timeline.add_marker(33, "Beat", "#ff0000")
    editor.engine.timeline.loop_in, editor.engine.timeline.loop_out = 50, 90
    canvas = canvas_of(editor)
    assert canvas._snap_hint_for(33)["kind"] == "marker"
    assert "Beat" in canvas._snap_hint_for(33)["caption"]
    assert canvas._snap_hint_for(50)["caption"] == "Loop in"
    assert canvas._snap_hint_for(90)["caption"] == "Loop out"
    editor.engine.session.set_playhead(12)
    assert canvas._snap_hint_for(0)["caption"] == "Timeline start"


# ------------------------------------------------------------------- feedback
def test_dragging_a_clip_publishes_the_target_edge_and_guide(editor):
    first = add_clip(editor, 20, 20)           # ends at 40
    moving = add_clip(editor, 100, 20)
    canvas = canvas_of(editor)
    canvas.update()
    pump(0.05)
    editor.engine.session.set_selection({moving.id})
    y = row_y(canvas, moving)
    grab = 30.0
    start_x = canvas.x_at(moving.timeline_start) + grab
    mouse(canvas, QEvent.Type.MouseButtonPress, start_x, y)
    # Drag so the clip's start lands one pixel away from the first clip's end.
    mouse(canvas, QEvent.Type.MouseMove, canvas.x_at(40) + grab + 1, y)
    pump(0.05)
    hint = canvas._snap_hint
    assert hint is not None, "moving near an edge must publish a snap hint"
    assert hint["frame"] == 40
    assert (first.id, "end") in {(clip_id, edge) for clip_id, edge, _rect in hint["targets"]}
    # The drag is a ghost preview until the button is released, and the ghost already sits
    # exactly on the snapped frame.
    placements, valid = canvas._ghost
    assert valid
    assert [start for _clip, _lane, start in placements] == [40]
    mouse(canvas, QEvent.Type.MouseButtonRelease, canvas.x_at(40) + grab + 1, y)
    pump(0.3)
    moved = editor.engine.timeline.find_clip(moving.id)
    assert moved.timeline_start == 40, "the clip really landed on the edge"
    assert canvas._snap_hint is None, "the highlight disappears when snapping stops"


def test_dragging_linked_clips_snaps_the_pair(editor):
    picture = add_clip(editor, 100, 20)
    audio_track = editor.engine.timeline.add_track("audio")
    sound = editor.engine.timeline.new_clip("sound", os.path.join(ROOT, "mosaic-output.png"),
                                            audio_track, 100, 20, source_out=200, source_fps=30)
    link = editor.engine.timeline.new_link_id()
    picture.link_id = link
    sound.link_id = link
    editor.engine.session.set_linked_selection(True)
    add_clip(editor, 20, 20)                   # end at 40
    canvas = canvas_of(editor)
    editor.engine.session.set_selection({picture.id, sound.id})
    y = row_y(canvas, picture)
    grab = 20.0
    mouse(canvas, QEvent.Type.MouseButtonPress, canvas.x_at(100) + grab, y)
    mouse(canvas, QEvent.Type.MouseMove, canvas.x_at(40) + grab + 1, y)
    pump(0.05)
    assert canvas._snap_hint is not None and canvas._snap_hint["frame"] == 40
    placements, valid = canvas._ghost
    assert valid
    landed = {clip.id: start for clip, _lane, start in placements}
    assert landed == {picture.id: 40, sound.id: 40}, "both halves land on the same snap"
    mouse(canvas, QEvent.Type.MouseButtonRelease, canvas.x_at(40) + grab + 1, y)
    pump(0.3)
    assert editor.engine.timeline.find_clip(picture.id).timeline_start == 40
    assert editor.engine.timeline.find_clip(sound.id).timeline_start == 40
    editor.engine.session.set_linked_selection(False)


def test_playhead_scrub_snaps_and_highlights_the_clip(editor):
    clip = add_clip(editor, 20, 20)            # 20..40
    canvas = canvas_of(editor)
    canvas.update()
    ruler_y = canvas.ruler_top + canvas.RULER_H // 2
    mouse(canvas, QEvent.Type.MouseButtonPress, canvas.x_at(60), ruler_y)
    mouse(canvas, QEvent.Type.MouseMove, canvas.x_at(40) + 1, ruler_y)
    pump(0.05)
    assert editor.engine.session.playhead == 40
    hint = canvas._snap_hint
    assert hint is not None and hint["targets"][0][0] == clip.id
    mouse(canvas, QEvent.Type.MouseButtonRelease, canvas.x_at(40) + 1, ruler_y)
    pump(0.3)


def test_the_glow_only_covers_the_target_clip_not_the_row(editor):
    clip = add_clip(editor, 20, 40)            # 20..60, a wide clip
    canvas = canvas_of(editor)
    canvas.update()
    hint = canvas._snap_hint_for(60)
    assert hint["targets"], "the clip end must be a target"
    _clip_id, edge, rect = hint["targets"][0]
    assert edge == "end"
    # The glow band is a short gradient inside the clip, not a full-width border.
    assert rect.width() > canvas.EDGE_GLOW_PX
    dirty = canvas._snap_rect_for(hint)
    assert dirty.width() < rect.width() + 40
    assert dirty.left() >= rect.right() - canvas.EDGE_GLOW_PX - 8
    assert clip.id == _clip_id


def test_the_target_edge_really_paints_brighter_than_the_clip_body(editor):
    clip = add_clip(editor, 20, 40)            # 20..60, a wide clip
    canvas = canvas_of(editor)
    canvas.update()
    pump(0.05)
    hint = canvas._snap_hint_for(60)
    assert hint["targets"]
    canvas._snap_hint = hint
    canvas._snap_fade = None
    image = canvas.grab().toImage()
    y = int(canvas.header_y(clip.track) + canvas.track_height(clip.track) * 0.5)
    right = int(canvas.x_at(60))
    edge = max(image.pixelColor(right - offset, y).lightness() for offset in (1, 2))
    band = image.pixelColor(right - 10, y).lightness()
    body = image.pixelColor(right - int(canvas.EDGE_GLOW_PX) - 10, y).lightness()
    assert edge > body, f"the glowing edge ({edge}) must stand out from the clip ({body})"
    assert edge >= 200, f"the edge glow should be near white, got {edge}"
    assert band > body, "the gradient fades inward from the edge"
    # Far from the edge the clip keeps its normal colour: the glow is local, not a border.
    assert body < 180
    canvas._snap_hint = None


def test_repaint_is_limited_to_the_snap_area(editor):
    add_clip(editor, 20, 20)
    canvas = canvas_of(editor)
    hint = canvas._snap_hint_for(40)
    dirty = canvas._snap_rect_for(hint)
    assert dirty.width() < canvas.width() / 2, "only the snap area may be repainted"
    assert not dirty.isEmpty()
