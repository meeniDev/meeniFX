"""Regression tests for the reported editor bugs.

Each section pins one report:
  * Graph Editor opening / axis-label formatting (channel id vs channel metadata)
  * Fill Frame and Fit Frame using the real project resolution
  * Alt-drag stretching a linked video/audio pair
  * Dragging a linked clip to another track vertically
  * Effects that do not match a clip's media type
"""

import time

import numpy as np
import pytest

from conftest import synthetic_frame


def pump(qapp, seconds=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


def mouse(canvas, kind, point, modifiers=None):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    modifiers = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    position = QPointF(float(point[0]), float(point[1]))
    event = QMouseEvent(kind, position, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, modifiers)
    if kind == QMouseEvent.Type.MouseButtonPress:
        canvas.mousePressEvent(event)
    elif kind == QMouseEvent.Type.MouseMove:
        canvas.mouseMoveEvent(event)
    else:
        canvas.mouseReleaseEvent(event)


def drag(canvas, start, end, modifiers=None, steps=4):
    from PyQt6.QtGui import QMouseEvent

    mouse(canvas, QMouseEvent.Type.MouseButtonPress, start, modifiers)
    for index in range(1, steps + 1):
        blend = (start[0] + (end[0] - start[0]) * index / steps,
                 start[1] + (end[1] - start[1]) * index / steps)
        mouse(canvas, QMouseEvent.Type.MouseMove, blend, modifiers)
    mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end, modifiers)


@pytest.fixture()
def editor(qapp, tmp_path, monkeypatch):
    """A real MainWindow inside a real project, with a linked video/audio pair."""
    from meenifx.media.cache import MediaEntry
    from meenifx.project.settings import ProjectSettings
    from meenifx.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    window = MainWindow(qapp)
    window.resize(1500, 950)
    window.show()
    pump(qapp, 0.2)
    assert window.projects.create_project(
        ProjectSettings(name="BugFixes", width=1000, height=1000, fps=30.0), str(tmp_path))

    picture = synthetic_frame(640, 360)
    path = str(tmp_path / "synthetic.png")
    entry = MediaEntry("video", 400, 30)
    entry.frames = [picture.copy() for _ in range(400)]
    window.engine.media._entries[path] = entry

    timeline = window.engine.timeline
    timeline.tracks.clear()
    video_lane = timeline.add_track("video")
    upper = timeline.add_track("video")
    audio_lane = timeline.add_track("audio")
    audio_upper = timeline.add_track("audio")
    video = timeline.new_clip("picture", path, video_lane, 0, 120, source_out=400, source_fps=30)
    audio = timeline.new_clip("sound", path, audio_lane, 0, 120, source_out=400, source_fps=30,
                              stream="audio")
    timeline.assign_link([video.id, audio.id])
    window.engine.session.set_selection({video.id})
    window.timeline_panel.canvas.update()
    pump(qapp, 0.15)
    window._test = {"video": video.id, "audio": audio.id, "lane": video_lane.id,
                    "upper": upper.id, "audio_lane": audio_lane.id,
                    "audio_upper": audio_upper.id}
    yield window
    # An open project would make closeEvent ask about saving, which is modal.
    window.projects._set_clean()
    window.close()


def clip(window, key):
    return window.engine.timeline.find_clip(window._test[key])


def track(window, key):
    wanted = window._test[key]
    return next(t for t in window.engine.timeline.tracks if t.id == wanted)


def inside(clip, canvas, edge=None):
    """(x, y) inside a clip: below the fade grips and away from the trim edges."""
    rect = canvas._clip_rect(clip)
    if edge == "right":
        x = rect.right() - 3
    elif edge == "left":
        x = rect.left() + 3
    else:
        x = rect.center().x()
    y = min(rect.top() + 20.0, max(rect.top() + 6.0, rect.center().y()))
    return (float(x), float(y))


# --------------------------------------------------------------- graph editor
def test_graph_editor_opens_with_no_selection(editor, qapp):
    graph = editor.graph_editor
    editor.engine.session.set_selection(set())
    graph.refresh()
    pump(qapp, 0.05)
    assert graph.channels() == []
    assert graph.active_channel() is None
    assert graph.channel_meta(None) is None
    assert graph.canvas.grab().width() > 0, "an empty graph must still paint"


def test_graph_axis_labels_come_from_channel_metadata(editor, qapp):
    from meenifx.model import channels as CH
    from meenifx.model import transform as T
    from meenifx.model.keyframes import AnimatedValue
    from meenifx.ui.graph_editor import _format_value

    picture = clip(editor, "video")
    T.animate_channel(picture, "position_x", 0)
    CH.set_value(picture, "transform.position_x", 0.0, 0, True)
    CH.set_value(picture, "transform.position_x", 160.0, 40, True)
    T.animate_channel(picture, "opacity", 0)
    CH.set_value(picture, "transform.opacity", 100.0, 0, True)
    CH.set_value(picture, "transform.opacity", 25.0, 40, True)
    picture.effects.append({"effect_id": "mosaic", "enabled": True,
                            "params": {"scale": AnimatedValue(base_value=12)}})

    graph = editor.graph_editor
    graph.refresh()
    pump(qapp, 0.05)

    position = graph.channel_meta("transform.position_x")
    opacity = graph.channel_meta("transform.opacity")
    assert position["unit"] == "px" and position["decimals"] == 2
    assert opacity["unit"] == "%" and opacity["decimals"] == 1
    assert _format_value(160.0, position) == "160.00px"
    assert _format_value(25.0, opacity) == "25.0%"
    assert _format_value(90.0, graph.channel_meta("transform.rotation")) == "90.00\u00b0"
    # A raw id is not metadata: the formatter falls back instead of raising.
    assert _format_value(12.0, "transform.position_x") == "12"
    assert _format_value(12.0, None) == "12"
    assert graph.channel_meta("transform.nope") is None

    # The exact crash path: painting the grid for every channel of the clip.
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QPainter, QPixmap

    graph.canvas.resize(640, 260)
    for channel in [row["id"] for row in graph.channels()]:
        graph.channel_tree.setCurrentItem(graph.channel_tree.findItems(
            graph.channel_meta(channel)["label"],  # type: ignore[index]
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MatchFlag.MatchExactly
            | __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MatchFlag.MatchRecursive, 0)[0])
        pixmap = QPixmap(640, 260)
        painter = QPainter(pixmap)
        try:
            low, high = graph.canvas.value_range()
            graph.canvas._paint_grid(painter, QRectF(0, 20, 640, 240), low, high)
        finally:
            painter.end()
    assert graph.canvas.grab().width() > 0


def test_graph_editor_survives_a_clip_with_keyframes(editor, qapp):
    from meenifx.model import channels as CH
    from meenifx.model import transform as T

    picture = clip(editor, "video")
    for name in ("scale_x", "rotation"):
        T.animate_channel(picture, name, 0)
        CH.set_value(picture, "transform." + name, 100.0, 0, True)
        CH.set_value(picture, "transform." + name, 40.0, 30, True)
    editor.graph_editor.refresh()
    pump(qapp, 0.05)
    assert len(editor.graph_editor.key_entries()) == 4
    assert editor.graph_editor.canvas.grab().width() > 0


# ------------------------------------------------------------------ fit / fill
def _compose(window, scale=1.0):
    editor = window.engine
    return editor.compose_frame(0, scale)


def _corner_colours(frame, inset=8):
    height, width = frame.shape[:2]
    return [frame[inset, inset], frame[inset, width - 1 - inset],
            frame[height - 1 - inset, inset], frame[height - 1 - inset, width - 1 - inset]]


@pytest.mark.parametrize("project_size", [(1000, 1000), (1280, 720), (720, 1280), (1000, 1350)])
def test_fill_frame_covers_the_project_and_fit_frame_letterboxes(editor, qapp, project_size):
    from meenifx.model import transform as T
    from meenifx.project.settings import ProjectSettings

    width, height = project_size
    editor.engine.apply_project_settings(ProjectSettings(name="x", width=width, height=height, fps=30.0))
    assert editor.engine.session.project_size == (width, height)

    picture = clip(editor, "video")
    timeline = editor.engine.timeline
    timeline.tracks.clear()
    lane = timeline.add_track("video")
    lane.clips.clear()
    picture = timeline.new_clip("picture", picture.source_path, lane, 0, 120,
                                source_out=400, source_fps=30)
    editor.engine.session.set_selection({picture.id})
    pump(qapp, 0.05)

    editor.engine.session.fill_frame([picture.id], lambda c: (640, 360))
    filled = clip_by_id(editor, picture.id)
    assert filled.transform["scale_x"] == pytest.approx(filled.transform["scale_y"])
    _x, _y, fitted_w, fitted_h = T.fit_rect((width, height), (640, 360))
    expected = max(width / fitted_w, height / fitted_h) * 100.0
    assert filled.transform["scale_x"] == pytest.approx(expected, rel=1e-3)
    frame = _compose(editor, 1.0)
    background = np.array(editor.engine.background_bgr, dtype=np.float32)
    for corner in _corner_colours(frame):
        assert np.abs(corner - background).max() > 4, "Fill Frame must cover the corners"

    depth = editor.qt_undo_stack.undo_stack.count()
    editor.engine.session.undo()
    assert clip_by_id(editor, picture.id).transform["scale_x"] == pytest.approx(100.0)
    editor.engine.session.redo()
    assert clip_by_id(editor, picture.id).transform["scale_x"] == pytest.approx(expected, rel=1e-3)
    assert editor.qt_undo_stack.undo_stack.count() >= depth

    editor.engine.session.fit_frame([picture.id])
    fitted = clip_by_id(editor, picture.id)
    assert fitted.transform["scale_y"] == pytest.approx(100.0)
    frame = _compose(editor, 1.0)
    top = frame[2, width // 2]
    same_aspect = abs((640.0 / 360.0) - (width / float(height))) <= 0.02
    if same_aspect:
        assert np.abs(top - background).max() > 4, "same aspect: Fit fills the frame exactly"
    else:
        assert np.abs(top - background).max() <= 4, "Fit Frame keeps the picture inside the frame"


def clip_by_id(window, clip_id):
    return window.engine.timeline.find_clip(clip_id)


def test_fill_frame_targets_text_clips_at_the_project_size(editor, qapp):
    from meenifx.model import text as TXT

    text_id = editor.engine.session.add_text_clip("Hello", start=0, duration=30)
    editor.engine.session.fill_frame([text_id], lambda c: (0, 0))
    text = clip_by_id(editor, text_id)
    assert text.is_text and TXT.content_at(text, 0) == "Hello"
    assert text.transform["scale_x"] == pytest.approx(100.0)


# ------------------------------------------------------------- linked editing
def test_alt_drag_stretches_the_linked_audio_in_the_same_undo_step(editor, qapp):
    canvas = editor.timeline_panel.canvas
    picture = clip(editor, "video")
    depth = editor.qt_undo_stack.undo_stack.count()
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QMouseEvent

    start = (canvas.x_at(picture.end) - 3, canvas.header_y(picture.track) + canvas.track_height(picture.track) / 2)
    end = (canvas.x_at(200), start[1])
    drag(canvas, start, end, Qt.KeyboardModifier.AltModifier)
    pump(qapp, 0.1)

    video, audio = clip(editor, "video"), clip(editor, "audio")
    assert video.duration == pytest.approx(200, abs=2), video.duration
    assert audio.duration == video.duration, "the linked audio must stretch by the same amount"
    assert audio.timeline_start == video.timeline_start
    assert audio.speed == pytest.approx(video.speed)
    assert editor.engine.timeline.link_is_synced(video)
    assert editor.qt_undo_stack.undo_stack.count() == depth + 1, "one undo command"

    editor.engine.session.undo()
    assert clip(editor, "video").duration == 120
    assert clip(editor, "audio").duration == 120


def test_stretch_limit_is_the_nearest_neighbour_on_either_lane(editor, qapp):
    from meenifx.model import timeline as TL

    timeline = editor.engine.timeline
    builder = timeline.find_clip(editor._test["video"])
    audio_lane = clip(editor, "audio").track
    audio = clip(editor, "audio")
    blocker = timeline.new_clip("later", audio.source_path, audio_lane, 160, 20,
                                source_out=400, source_fps=30, stream="audio")
    speed = timeline.apply_stretch(timeline.snapshot(), builder.id, "right", 400,
                                   also=[audio.id])
    assert speed is not None and speed > 0
    assert builder.end <= blocker.timeline_start
    assert audio.end == builder.end
    assert isinstance(TL.SYNC_TOLERANCE, float)


def test_vertical_drag_changes_only_the_dragged_clip_track(editor, qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QMouseEvent

    canvas = editor.timeline_panel.canvas
    picture, audio = clip(editor, "video"), clip(editor, "audio")
    audio_track = audio.track
    upper = track(editor, "upper")
    start = (canvas.x_at(picture.timeline_start + 40),
             canvas.header_y(picture.track) + canvas.track_height(picture.track) / 2)
    target_y = canvas.header_y(upper) + canvas.track_height(upper) / 2
    drag(canvas, start, (start[0] + 20, target_y))
    pump(qapp, 0.1)

    assert clip(editor, "video").track is upper, "the dragged video clip changes lane"
    assert clip(editor, "audio").track is audio_track, "the linked audio keeps its lane"
    # Time still follows the pointer for both members; only the lane is independent.
    assert clip(editor, "audio").timeline_start == clip(editor, "video").timeline_start
    assert clip(editor, "video").track.type == "video"
    editor.engine.session.undo()
    assert clip(editor, "video").track is not upper


def test_horizontal_drag_keeps_the_pair_in_sync(editor, qapp):
    canvas = editor.timeline_panel.canvas
    picture = clip(editor, "video")
    start = (canvas.x_at(picture.timeline_start + 40),
             canvas.header_y(picture.track) + canvas.track_height(picture.track) / 2)
    end = (canvas.x_at(picture.timeline_start + 40 + 30), start[1])
    drag(canvas, start, end)
    pump(qapp, 0.1)

    video, audio = clip(editor, "video"), clip(editor, "audio")
    assert video.timeline_start == pytest.approx(30, abs=2)
    assert audio.timeline_start == video.timeline_start
    assert video.track is not None and video.track.type == "video"
    assert audio.track is not None and audio.track.type == "audio"
    assert editor.engine.timeline.link_is_synced(video)


def test_turning_linked_selection_off_leaves_the_partner_behind(editor, qapp):
    editor.engine.session.set_linked_selection(False)
    picture = clip(editor, "video")
    # Linked Selection off: clicking one member no longer selects (or drags) the other.
    editor.engine.session.set_selection({picture.id})
    assert editor.engine.session.selected == {picture.id}
    canvas = editor.timeline_panel.canvas
    start = (canvas.x_at(picture.timeline_start + 40),
             canvas.header_y(picture.track) + canvas.track_height(picture.track) / 2)
    drag(canvas, start, (start[0] + 25, start[1]))
    pump(qapp, 0.1)
    assert clip(editor, "video").timeline_start > 0
    assert clip(editor, "audio").timeline_start == 0, "Linked Selection off must not move it"


def test_unlinked_clips_do_not_follow(editor, qapp):
    picture = clip(editor, "video")
    editor.engine.session.set_selection({picture.id, clip(editor, "audio").id})
    assert editor.engine.session.unlink_selected()
    assert clip(editor, "video").link_id == 0 and clip(editor, "audio").link_id == 0
    editor.engine.session.set_selection({picture.id})
    canvas = editor.timeline_panel.canvas
    start = (canvas.x_at(picture.timeline_start + 40),
             canvas.header_y(picture.track) + canvas.track_height(picture.track) / 2)
    drag(canvas, start, (start[0] + 25, start[1]))
    pump(qapp, 0.1)
    assert clip(editor, "audio").timeline_start == 0


def test_vertical_drag_of_the_audio_keeps_the_video_lane(editor, qapp):
    canvas = editor.timeline_panel.canvas
    picture, audio = clip(editor, "video"), clip(editor, "audio")
    video_track = picture.track
    target = track(editor, "audio_upper")
    grab = inside(audio, canvas)
    drop = (grab[0] + 16, canvas.header_y(target) + 12)
    drag(canvas, grab, drop)
    pump(qapp, 0.1)

    assert clip(editor, "audio").track is target, "the dragged audio clip re-lanes"
    assert clip(editor, "video").track is video_track, "the linked video keeps its lane"
    assert clip(editor, "audio").timeline_start == clip(editor, "video").timeline_start
    editor.engine.session.undo()
    assert clip(editor, "audio").track is not target


def test_stretch_leaves_a_partner_on_a_locked_track_alone(editor, qapp):
    from PyQt6.QtCore import Qt

    canvas = editor.timeline_panel.canvas
    picture = clip(editor, "video")
    editor.engine.session.set_track_attr(clip(editor, "audio").track.id, locked=True)
    pump(qapp, 0.1)
    start = inside(clip(editor, "video"), canvas, "right")
    drag(canvas, start, (canvas.x_at(picture.end + 40), start[1]),
         Qt.KeyboardModifier.AltModifier)
    pump(qapp, 0.1)
    assert clip(editor, "video").duration > 120, "the dragged clip still stretches"
    assert clip(editor, "audio").duration == 120, "a locked lane must not be edited"


# ------------------------------------------------------------------- effects
def test_video_effects_are_refused_on_audio_clips(editor, qapp):
    messages = []
    editor.engine.session.status.connect(messages.append)
    panel = editor.effects_panel
    track = clip(editor, "audio").track
    source = clip(editor, "audio").source_path
    # An audio-only clip that is not part of the linked pair.
    voice = editor.engine.timeline.new_clip("voice", source, track, 200, 40,
                                            source_out=400, source_fps=30, stream="audio")
    editor.engine.session.set_selection({voice.id})
    pump(qapp, 0.1)
    panel.refresh_effect_list()

    assert not editor.engine.clip_accepts_effect(voice, "mosaic")
    assert editor.engine.clip_accepts_effect(clip(editor, "video"), "mosaic")
    # And the audio effects go the other way: an audio clip takes them, a picture clip does not.
    assert editor.engine.clip_accepts_effect(voice, "audio_eq")
    assert not editor.engine.clip_accepts_effect(clip(editor, "video"), "audio_eq")
    # Every effect declares the media it can process: picture effects video, the audio
    # effects audio. Nothing may declare nothing (that would make it unusable).
    assert all(effect.media_types() and set(effect.media_types()) <= {"video", "audio"}
               for effect in editor.engine.effects)
    assert any(set(effect.media_types()) == {"audio"} for effect in editor.engine.effects)

    from PyQt6.QtCore import Qt
    labels = {}
    root = panel.effect_tree
    for index in range(root.topLevelItemCount()):
        top = root.topLevelItem(index)
        for child in range(top.childCount()):
            item = top.child(child)
            labels[item.data(0, Qt.ItemDataRole.UserRole)] = item
    mosaic = labels["mosaic"]
    assert "video only" in mosaic.text(0)
    assert not (mosaic.flags() & Qt.ItemFlag.ItemIsEnabled), "it must not be addable"

    panel.add_effect("mosaic")
    pump(qapp, 0.05)
    assert voice.effects == [], "the engine must reject the effect"
    assert any("video-only" in message for message in messages), messages

    canvas = editor.timeline_panel.canvas
    canvas._add_effect_to_clip(voice, "mosaic")
    pump(qapp, 0.05)
    assert clip_by_id(editor, voice.id).effects == []


def test_a_visual_effect_on_a_linked_pair_lands_on_the_video_member(editor, qapp):
    panel = editor.effects_panel
    editor.engine.session.set_selection({editor._test["audio"]})
    assert editor.engine.session.selected == {editor._test["video"], editor._test["audio"]}
    pump(qapp, 0.1)
    panel.add_effect("mosaic")
    pump(qapp, 0.1)
    assert [e["effect_id"] for e in clip(editor, "video").effects] == ["mosaic"]
    assert clip(editor, "audio").effects == [], "the audio member must stay untouched"


def test_incompatible_effects_are_disabled_not_executed(editor, qapp):
    import numpy as np

    audio = clip(editor, "audio")
    audio.effects.append({"effect_id": "mosaic", "enabled": True, "params": {"scale": 8}})
    disabled = editor.engine.sanitize_effects()
    assert (audio.id, "mosaic") in disabled
    entry = audio.effects[0]
    assert entry["enabled"] is False and entry["incompatible"] is True

    layer = np.full((16, 16, 4), 200, dtype=np.uint8)
    after = editor.engine.apply_effects_layer(layer, audio.effects, 0, 1.0, "audio")
    assert np.array_equal(after, layer), "an incompatible effect must never run"


def test_old_projects_with_incompatible_effects_still_open(editor, qapp, tmp_path):
    from meenifx.engine import EditorEngine
    from meenifx.project import io as project_io

    audio = clip(editor, "audio")
    audio.effects.append({"effect_id": "mosaic", "enabled": True, "params": {"scale": 8}})
    project = editor.projects.project
    data = project_io.collect(editor.engine, project)

    fresh = EditorEngine(30)
    loaded, _missing = project_io.apply(fresh, data, None)
    assert loaded is not None
    lanes = fresh.timeline.tracks_of_type("audio")
    assert lanes and lanes[0].clips
    restored = lanes[0].clips[0]
    entry = next(e for e in restored.effects if e["effect_id"] == "mosaic")
    assert entry["enabled"] is False and entry["incompatible"] is True
    assert any(key == "mosaic" for _cid, key in fresh.last_incompatible)
