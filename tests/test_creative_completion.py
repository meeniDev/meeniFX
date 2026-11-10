"""Completion tests for mask editing, path keyframes, paint styles and motion.

These deliberately use the public session/model APIs plus the real PreviewWidget events.
They are regression tests, not replacements for a human GUI session.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication

from meenifx.engine import EditorEngine
from meenifx.media.cache import MediaEntry
from meenifx.model import channels as CH
from meenifx.model import mask as MASK
from meenifx.model import motion as MOTION
from meenifx.model import path as PATHS
from meenifx.model import shape as SHAPE
from meenifx.model import transform as T
from meenifx.processing import shapes as SHAPE_OPS
from meenifx.ui.preview import PreviewWidget
from meenifx.ui.preview_transform import PreviewOverlay
from meenifx.ui.timeline_settings import TimelineSettings
from meenifx.ui.timeline_widget import TimelinePanel
from meenifx.model import text as TXT


def pump(seconds=0.08):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.003)


def mouse_event(kind, x, y, button=Qt.MouseButton.LeftButton,
                buttons=Qt.MouseButton.LeftButton, modifiers=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(kind, QPointF(float(x), float(y)),
                       QPointF(float(x), float(y)), button, buttons, modifiers)


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


def test_editing_existing_mask_adds_drag_delete_and_undo(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(
        clip.id, [(100, 100), (300, 100), (300, 200)], closed=True)
    view, overlay = preview(qapp, engine)
    overlay.mode = "select"

    # Selecting the outline enters the Pen in edit mode instead of adding a mask.
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, 100, 100))
    pump(0.02)
    assert overlay.mode == "pen"
    assert overlay.pen._editing == (clip.id, mask_id)
    assert len(MASK.masks(clip)) == 1

    # Drag the first point. The committed mask must change, not gain another mask.
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, 100, 100))
    overlay.mouse_move(mouse_event(QEvent.Type.MouseMove, 125, 125))
    overlay.mouse_release(mouse_event(QEvent.Type.MouseButtonRelease, 125, 125))
    # Masks are layer space now, so the public project-space path is what the preview
    # moved; the stored path stays in the clip's own coordinates.
    path = MASK.project_path(clip, MASK.find(clip, mask_id), 0,
                             engine.session.project_size)
    assert abs(PATHS.points(path)[0]["x"] - 125.0) < 1e-6
    assert len(MASK.masks(clip)) == 1

    # Click on an existing segment inserts a point.
    sampled = PATHS.sampled(path, steps=20)
    a, b = sampled[8], sampled[9]
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    before = len(PATHS.points(MASK.find(clip, mask_id)["path"]))
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, *mid))
    overlay.mouse_release(mouse_event(QEvent.Type.MouseButtonRelease, *mid))
    assert len(PATHS.points(MASK.find(clip, mask_id)["path"])) == before + 1

    # Delete selected point and convert the next one to a smooth point.
    overlay.pen.delete_selected()
    assert len(PATHS.points(MASK.find(clip, mask_id)["path"])) == before
    overlay.pen.selected = {0}
    overlay.pen.toggle_smooth()
    assert PATHS.points(MASK.find(clip, mask_id)["path"])[0]["smooth"] is True

    engine.session.undo()
    restored = engine.timeline.find_clip(clip.id)
    assert PATHS.points(MASK.find(restored, mask_id)["path"])[0]["smooth"] is False


def test_mask_visibility_rename_duplicate_and_outline(qapp):
    engine = EditorEngine(30)
    clip = shape_clip(engine)
    first = engine.session.add_clip_mask(clip.id, [(10, 10), (80, 10), (80, 80)])
    engine.session.update_clip_mask(clip.id, first, {"visible": False,
                                                     "show_outline": False,
                                                     "name": "Cutout"})
    mask = MASK.find(clip, first)
    assert mask["name"] == "Cutout" and not mask["visible"] and not mask["show_outline"]
    assert MASK.at(clip, 0) == []
    second = engine.session.duplicate_clip_mask(clip.id, first)
    assert second and second != first
    assert len(MASK.masks(clip)) == 2
    assert MASK.find(clip, second)["name"].endswith("copy")
    engine.session.undo()
    assert len(MASK.masks(engine.timeline.find_clip(clip.id))) == 1


def test_path_keys_interpolate_copy_move_and_hold_on_topology_mismatch():
    clip = SimpleNamespace(id=1, duration=60, timeline_start=0, transform_meta={},
                           effects=[], is_shape=False, is_text=False)
    mask = MASK.add(clip, [(0, 0), (100, 0), (100, 100)])
    channel = CH.mask_path_channel(mask["id"])
    assert CH.ensure_key(clip, channel, 0)
    edited = PATHS.normalise({"points": [(0, 20), (100, 20), (100, 100)],
                              "closed": True})
    MASK.set_path(MASK.find(clip, mask["id"]), edited, frame=10)
    assert CH.key_times(clip, channel) == [0, 10]
    halfway = CH.value_at(clip, channel, 5)
    assert abs(halfway["points"][0]["y"] - 10.0) < 1e-6

    # A different vertex count must hold the previous pose, never fold indices.
    MASK.set_path(MASK.find(clip, mask["id"]),
                  {"points": [(0, 30), (100, 30), (100, 100), (0, 100)],
                   "closed": True}, frame=20)
    held = CH.value_at(clip, channel, 15)
    assert len(held["points"]) == 3

    assert CH.set_key_time(clip, channel, 20, 25) == 25
    records = CH.key_records(clip, channel)
    assert [record[0] for record in records] == [0, 10, 25]


def test_gradient_and_dashed_stroke_render_at_multiple_resolutions():
    settings = SHAPE.blank("path")
    settings["path"] = PATHS.normalise({"points": [(40, 40), (280, 40), (160, 220)],
                                        "closed": True})
    settings["gradient_enabled"] = True
    settings["gradient_type"] = "linear"
    settings["gradient_stops"] = [{"position": 0, "color": "#FF0000"},
                                  {"position": 50, "color": "#00FF00"},
                                  {"position": 100, "color": "#0000FF"}]
    settings["fill_opacity"] = 100
    settings["stroke_enabled"] = True
    settings["stroke_width"] = 8
    settings["stroke_dash"] = 12
    settings["stroke_gap"] = 6
    settings["stroke_join"] = "round"
    settings["cap"] = "round"
    source = SimpleNamespace(id=2, duration=30, timeline_start=0, shape=settings,
                             transform_meta={}, effects=[], is_shape=True, is_text=False)
    spec = SHAPE.resolve(source, 0)
    assert spec["gradient_enabled"] and spec["gradient_stops"][1]["color"] == "#00FF00"
    for scale in (1.0, 2.0):
        layer = SHAPE_OPS.render(spec, int(320 * scale), int(240 * scale), scale)
        assert int(layer[:, :, 3].max()) > 0
        assert layer.shape[0] == round(240 * scale)


def test_motion_offset_preserves_authored_position_and_replace_is_explicit():
    clip = SimpleNamespace(id=1, duration=60, timeline_start=0, transform_meta={},
                           effects=[], is_shape=False, is_text=False,
                           transform=T.default_transform())
    T.set_channel(clip, "position_x", 40.0)
    T.set_channel(clip, "position_y", 10.0)
    MOTION.set_path(clip, [(0, 0), (100, 0)], enabled=True)
    assert MOTION.ensure(clip)["mode"] == "offset"
    x0, y0 = T.position_at(clip, 0)
    assert (x0, y0) == pytest.approx((40.0, 10.0))
    x30, y30 = T.position_at(clip, 30)
    assert x30 > 40.0 and y30 == pytest.approx(10.0)
    MOTION.ensure(clip)["mode"] = "replace"
    x30, _y30 = T.position_at(clip, 30)
    assert x30 == pytest.approx(50.0)


def test_motion_offset_combines_with_animated_position():
    clip = SimpleNamespace(id=1, duration=60, timeline_start=0, transform_meta={},
                           effects=[], is_shape=False, is_text=False,
                           transform=T.default_transform())
    T.ensure_transform(clip)
    T.set_channel(clip, "position_x", 0.0, 0, auto_key=True)
    T.set_channel(clip, "position_x", 100.0, 40, auto_key=True)
    MOTION.set_path(clip, [(0, 0), (40, 0)], enabled=True)
    x0, _ = T.position_at(clip, 0)
    x20, _ = T.position_at(clip, 20)
    assert x0 == pytest.approx(0.0)
    assert x20 > 50.0, "the authored 50 px position must remain underneath the path"


def test_timeline_snapping_matrix_fps_zoom_and_clip_types(qapp):
    for fps in (24, 25, 29.97, 30, 60):
        engine = EditorEngine(fps)
        engine.source_width, engine.source_height = 640, 360
        engine.session.project_size = (640, 360)
        panel = TimelinePanel(engine, None)
        canvas = panel.canvas
        canvas.ppf = 6.0
        canvas.scroll_x = 0.0
        engine.session.snap_enabled.set(True)
        timeline = engine.timeline
        video_track = timeline.add_track("video")
        video = timeline.new_clip("video", "", video_track, 10, 20,
                                  source_out=20, source_fps=fps)
        audio_track = timeline.add_track("audio")
        audio = timeline.new_clip("audio", "", audio_track, 40, 20,
                                  source_out=20, source_fps=fps)
        timeline.assign_link([video.id, audio.id])
        text = timeline.new_clip("text", "", timeline.add_track("video"), 70, 20,
                                 text=TXT.blank())
        shape = timeline.new_clip("shape", "", timeline.add_track("video"), 100, 20,
                                  shape=SHAPE.blank("rectangle"))
        nested_source = timeline.new_clip("nested source", "",
                                          timeline.add_track("video"), 130, 20,
                                          shape=SHAPE.blank("ellipse"))
        nested_id = engine.session.precompose("Nested", [nested_source.id])
        assert nested_id
        nested = timeline.find_clip(nested_id)
        clips = [video, audio, text, shape, nested]
        edges = sorted({int(value) for clip in clips
                        for value in (clip.timeline_start, clip.end)})
        for ppf in (1.0, 6.0, 20.0):
            canvas.ppf = ppf
            canvas.scroll_x = 0.0
            for edge in edges:
                canvas._snap_cached = None
                assert canvas._snap_target(edge)[1] == edge, (fps, ppf, edge)
        canvas.ppf = 6.0
        engine.session.set_playhead(video.end)
        canvas._snap_cached = None
        assert canvas._snap_target(video.end)[1] == video.end
        panel.deleteLater()


def test_preview_snapping_respects_target_flags(qapp):
    engine = EditorEngine(30)
    view, overlay = preview(qapp, engine)
    clip = shape_clip(engine)
    overlay.snap_enabled = True
    state = T.evaluate(clip, 0)
    overlay.snap_edges = False
    overlay.snap_center = False
    overlay.snap_objects = False
    dx, dy = overlay._snap_delta(clip, state, 2.0, 2.0, False)
    assert (dx, dy) == pytest.approx((2.0, 2.0))
    overlay.snap_edges = True
    corners = T.content_corners(state, overlay.project_size(), overlay.image_size(clip))
    left = min(corner[0] for corner in corners)
    start_dx = -left + 2.0
    dx, _dy = overlay._snap_delta(clip, state, start_dx, 0.0, False)
    assert dx == pytest.approx(-left)


def test_snapping_preferences_round_trip_and_flags(monkeypatch, tmp_path):
    from PyQt6.QtCore import QSettings
    from meenifx.ui import timeline_settings

    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(timeline_settings, "app_settings", lambda: store)
    settings = TimelineSettings({"snap_timeline": False, "snap_preview": True,
                                 "timeline_snap_px": 14, "preview_snap_px": 5,
                                 "show_snap_guides": False, "snap_edges": False,
                                 "snap_center": True, "snap_objects": False})
    settings.save()
    loaded = TimelineSettings.load()
    assert loaded.as_dict() == settings.as_dict()
    assert loaded.timeline_snap_px == 14 and loaded.preview_snap_px == 5
    assert not loaded.snap_timeline and not loaded.snap_objects


def test_nested_precomposition_keeps_mask_alpha_and_serializes():
    from meenifx.project.settings import ProjectSettings
    from meenifx.project.serialize import (compositions_to_sections,
                                           sections_to_compositions)

    engine = EditorEngine(30)
    engine.apply_project_settings(ProjectSettings(name="Nested", width=64, height=64,
                                                  fps=30.0))
    lower = np.zeros((64, 64, 3), np.uint8)
    lower[:] = (20, 40, 200)
    entry = MediaEntry("video", 30, 30)
    entry.frames = [lower.copy() for _ in range(30)]
    engine.media._entries["lower.png"] = entry
    shape_id = engine.session.add_shape_clip("rectangle", box=(0, 0, 64, 64))
    shape = engine.timeline.find_clip(shape_id)
    shape.shape["fill_color"] = "#FFFFFF"
    shape.shape["stroke_enabled"] = False
    engine.session.add_clip_mask(shape.id, [(16, 16), (48, 16), (48, 48), (16, 48)],
                                 mode="subtract")
    inner_id = engine.session.precompose("Inner", [shape.id])
    assert inner_id
    inner = engine.timeline.find_clip(inner_id)
    outer_id = engine.session.precompose("Outer", [inner_id])
    assert outer_id
    outer = engine.timeline.find_clip(outer_id)
    lower_track = engine.timeline.add_track("video")
    engine.timeline.tracks.remove(lower_track)
    engine.timeline.tracks.append(lower_track)
    engine.timeline.new_clip("Lower", "lower.png", lower_track, 0, 60,
                             source_out=60, source_fps=30)
    _raw, frame = engine.render_frame(0)
    assert frame[32, 32].tolist() == [20, 40, 200]
    assert frame[4, 4].tolist() == [255, 255, 255]
    sections = compositions_to_sections(engine.compositions, lambda path: path)
    restored = sections_to_compositions(sections, lambda path: path)
    assert restored.get(inner.composition_id) is not None
    assert outer.composition_id in restored


def test_pen_gui_click_drag_close_v_and_escape(qapp):
    engine = EditorEngine(30)
    engine.session.project_size = (640, 360)
    view, overlay = preview(qapp, engine)
    overlay.pen.set_mode("shape")
    overlay.mode = "pen"
    # A drag gives the first point a curve handle; the next click closes the path.
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, 120, 120))
    overlay.mouse_move(mouse_event(QEvent.Type.MouseMove, 160, 90))
    overlay.mouse_release(mouse_event(QEvent.Type.MouseButtonRelease, 160, 90))
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, 300, 160))
    overlay.mouse_release(mouse_event(QEvent.Type.MouseButtonRelease, 300, 160))
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, 120, 120))
    overlay.mouse_release(mouse_event(QEvent.Type.MouseButtonRelease, 120, 120))
    pump(0.05)
    paths = [clip for clip in engine.timeline.all_clips() if getattr(clip, "is_shape", False)
             and (clip.shape or {}).get("kind") == "path"]
    assert paths, "the GUI Pen did not create a custom path shape"
    assert paths[0].shape["path"]["points"][0]["out"] != [0.0, 0.0]

    # V leaves an open Pen operation and returns to Selection without adding a clip.
    count = len(engine.timeline.all_clips())
    overlay.pen.set_mode("shape")
    overlay.mode = "pen"
    overlay.pen.press(mouse_event(QEvent.Type.MouseButtonPress, 200, 200))
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.NoModifier)
    view.keyPressEvent(event)
    assert overlay.mode == "select"
    assert len(engine.timeline.all_clips()) == count


def test_pen_gui_edits_mask_points_instead_of_creating_a_second_mask(qapp):
    engine = EditorEngine(30)
    view, overlay = preview(qapp, engine)
    clip = shape_clip(engine)
    mask_id = engine.session.add_clip_mask(clip.id, [(100, 100), (300, 100), (300, 200)])
    overlay.mode = "select"
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, 100, 100))
    assert overlay.pen._editing == (clip.id, mask_id)
    overlay.mouse_press(mouse_event(QEvent.Type.MouseButtonPress, 100, 100))
    overlay.mouse_move(mouse_event(QEvent.Type.MouseMove, 130, 140))
    overlay.mouse_release(mouse_event(QEvent.Type.MouseButtonRelease, 130, 140))
    assert len(MASK.masks(clip)) == 1
    moved = MASK.project_path(clip, MASK.find(clip, mask_id), 0,
                              engine.session.project_size)
    assert PATHS.points(moved)[0]["x"] == pytest.approx(130.0)
