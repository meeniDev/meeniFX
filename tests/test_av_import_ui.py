"""Project media -> timeline, end to end inside the real MainWindow.

The drag source and the drop target are exercised the way Qt delivers them:
`_MediaList.mimeData` / `start_media_drag` on one side, `QDragEnterEvent` / `QDragMoveEvent`
/ `QDropEvent` sent to the timeline canvas on the other. Nothing is inserted by hand, so a
broken MIME payload, a wrong coordinate conversion or a missing acceptDrops shows up here.
"""
import os
import sys
import tempfile
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.constants import MEDIA_MIME  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import av_fixture  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_ONLY = os.path.join(ROOT, "mosaic-output.mp4")
SOUND = os.path.join(ROOT, "demo_audio", "music-bed.wav")
STILL = os.path.join(ROOT, "mosaic-output.png")


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication(sys.argv)
    yield application


@pytest.fixture()
def av_file(tmp_path_factory):
    folder = tmp_path_factory.mktemp("av")
    return av_fixture.write_av_avi(str(folder / "clip-with-sound.avi"), seconds=2.0)


def pump(seconds=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def add_to_library(window, path):
    panel = window.project_panel
    if panel.project.library.by_path(path) is None:
        assert panel.add_files([path]) == 1, f"{path} was not added"
    pump(0.05)
    return panel.project.library.by_path(path)


def media_item_widget(panel, media):
    for index in range(panel.list.count()):
        item = panel.list.item(index)
        kind, key, _path = item.data(Qt.ItemDataRole.UserRole)
        if kind == "item" and key == media.id:
            return item
    raise AssertionError("media item is not in the Project panel")


def drag_mime(panel, item):
    """The payload the Project panel's own drag source produces for that item."""
    assert panel.list._kind(item) == "item"
    return panel.list.mimeData([item])


def drop_on_canvas(canvas, mime, frame, track=None, expect_valid=True):
    """Deliver a real drag sequence to the canvas, exactly like the platform would."""
    if track is None:
        track = canvas.timeline.tracks_of_type("video")[0]
    x = int(canvas.x_at(frame))
    y = int(canvas.header_y(track) + canvas.track_height(track) / 2)
    pos = QPointF(x, y)
    for factory in (QDragEnterEvent, QDragMoveEvent):
        event = factory(QPoint(x, y), Qt.DropAction.CopyAction, mime,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(canvas, event)
        pump(0.02)
    assert canvas._media_drop is not None, "the canvas did not show a placement preview"
    assert canvas._media_drop["valid"] is expect_valid, canvas._media_drop
    preview = canvas._media_drop
    drop = QDropEvent(pos, Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(canvas, drop)
    pump(0.3)
    return preview


@pytest.fixture(scope="module")
def window(app):
    """One long-lived window; every test opens its own project in it."""
    MainWindow._load_demo_project = lambda self: None
    editor = MainWindow(app)
    editor.resize(1400, 860)
    editor.show()
    pump(0.15)
    yield editor
    editor.projects._set_clean()
    editor.close()


@pytest.fixture()
def editor(window, tmp_path):
    """A fresh, empty project with the default tracks."""
    window.projects._set_clean()
    settings = ProjectSettings(name=f"AV {uuid.uuid4().hex[:8]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path)), "project could not be created"
    window.engine.session.set_selection(set())
    window.project_panel.rebuild(force=True)
    pump(0.1)
    assert window.engine.timeline.all_clips() == []
    return window


def test_importing_only_touches_the_library(editor):
    window = editor
    media = add_to_library(window, VIDEO_ONLY)
    assert media is not None and media.type == "video"
    assert window.engine.timeline.all_clips() == [], "importing must not create timeline clips"


def test_dragging_a_video_with_audio_creates_a_linked_pair(editor, av_file):
    window = editor
    media = add_to_library(window, av_file)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    mime = drag_mime(panel, media_item_widget(panel, media))
    assert mime.hasFormat(MEDIA_MIME)
    assert media.id in bytes(mime.data(MEDIA_MIME)).decode().splitlines()

    preview = drop_on_canvas(canvas, mime, 45)
    assert len(preview["placements"]) == 2, "the preview must show both clips"
    landed = preview["frame"]      # the frame under the pointer, after pixel rounding
    assert preview["placements"][0]["start"] == preview["placements"][1]["start"] == landed

    clips = window.engine.timeline.all_clips()
    assert len(clips) == 2, [(c.track.type, c.timeline_start) for c in clips]
    video = next(c for c in clips if c.track.type == "video")
    audio = next(c for c in clips if c.track.type == "audio")
    assert video.timeline_start == audio.timeline_start == landed
    assert video.link_id == audio.link_id != 0
    assert video.source_path == audio.source_path == av_file
    assert (video.stream, audio.stream) == ("video", "audio")
    assert window.engine.timeline.link_is_synced(video)
    assert window.engine.session.selected == {video.id, audio.id}


def test_one_undo_removes_both_and_redo_restores_the_link(editor, av_file):
    window = editor
    media = add_to_library(window, av_file)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    drop_on_canvas(canvas, drag_mime(panel, media_item_widget(panel, media)), 0)
    session = window.engine.session
    assert len(session.timeline.all_clips()) == 2
    session.undo()
    assert session.timeline.all_clips() == []
    session.redo()
    clips = session.timeline.all_clips()
    assert len(clips) == 2 and clips[0].link_id == clips[1].link_id != 0
    assert {c.timeline_start for c in clips} == {0}


def test_video_only_file_gets_a_single_clip(editor):
    window = editor
    media = add_to_library(window, VIDEO_ONLY)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    preview = drop_on_canvas(canvas, drag_mime(panel, media_item_widget(panel, media)), 10)
    clips = window.engine.timeline.all_clips()
    assert len(clips) == 1 and clips[0].track.type == "video" and clips[0].link_id == 0
    assert clips[0].timeline_start == preview["frame"]


def test_audio_only_file_lands_on_an_audio_track(editor):
    window = editor
    media = add_to_library(window, SOUND)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    drop_on_canvas(canvas, drag_mime(panel, media_item_widget(panel, media)), 0,
                   track=canvas.timeline.tracks_of_type("audio")[0])
    clips = window.engine.timeline.all_clips()
    assert len(clips) == 1 and clips[0].track.type == "audio" and clips[0].link_id == 0


def test_image_lands_on_a_video_track_for_three_seconds(editor):
    window = editor
    media = add_to_library(window, STILL)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    drop_on_canvas(canvas, drag_mime(panel, media_item_widget(panel, media)), 0)
    clip = window.engine.timeline.all_clips()[0]
    assert clip.track.type == "video" and clip.kind == "image"
    assert clip.duration == window.engine.timeline.fps * 3


def test_drop_is_refused_on_a_locked_lane_and_nothing_is_inserted(editor, av_file):
    window = editor
    media = add_to_library(window, av_file)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    audio_lane = canvas.timeline.tracks_of_type("audio")[0]
    window.engine.session.set_track_attr(audio_lane.id, locked=True)
    mime = drag_mime(panel, media_item_widget(panel, media))
    preview = drop_on_canvas(canvas, mime, 0, expect_valid=False)
    assert "locked" in preview["reason"].lower(), preview
    assert window.engine.timeline.all_clips() == [], "a refused drop must not leave half a pair"


def test_dropping_into_occupied_space_advances_both_clips(editor, av_file):
    window = editor
    media = add_to_library(window, av_file)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    mime = drag_mime(panel, media_item_widget(panel, media))
    drop_on_canvas(canvas, mime, 0)
    first = sorted(window.engine.timeline.all_clips(), key=lambda c: c.track.type)
    drop_on_canvas(canvas, mime, 0)
    clips = window.engine.timeline.all_clips()
    assert len(clips) == 4
    second = [c for c in clips if c.id not in {c.id for c in first}]
    video = next(c for c in second if c.track.type == "video")
    audio = next(c for c in second if c.track.type == "audio")
    assert video.timeline_start == audio.timeline_start
    assert video.timeline_start >= first[0].end, "the second drop lands after the first"


def test_drag_source_starts_a_drag_with_media_ids(editor, monkeypatch):
    """The panel's mouse handling must call QDrag.exec with the media MIME payload."""
    window = editor
    media = add_to_library(window, VIDEO_ONLY)
    panel = window.project_panel
    item = media_item_widget(panel, media)
    panel.list.clearSelection()
    item.setSelected(True)

    captured = {}

    class FakeDrag:
        def __init__(self, source=None):
            self.source = source

        def setMimeData(self, mime):
            captured["mime"] = mime

        def setPixmap(self, pixmap):
            captured["pixmap"] = pixmap

        def setHotSpot(self, point):
            captured["hotspot"] = point

        def exec(self, action):
            captured["action"] = action
            return Qt.DropAction.CopyAction

    monkeypatch.setattr("meenifx.ui.project_panel.QDrag", FakeDrag)
    assert panel.list.start_media_drag(item) == Qt.DropAction.CopyAction
    mime = captured["mime"]
    assert isinstance(mime, QMimeData) and mime.hasFormat(MEDIA_MIME)
    assert bytes(mime.data(MEDIA_MIME)).decode().splitlines() == [media.id]
    assert captured["pixmap"].width() > 0


def test_mouse_drag_threshold_starts_the_drag(editor, monkeypatch):
    """Below QApplication.startDragDistance nothing happens; past it the drag begins."""
    from PyQt6.QtGui import QMouseEvent

    window = editor
    media = add_to_library(window, VIDEO_ONLY)
    panel = window.project_panel
    viewport = panel.list.viewport()
    item = media_item_widget(panel, media)
    panel.list.scrollToItem(item)
    pump(0.05)
    centre = panel.list.visualItemRect(item).center()
    started = []
    monkeypatch.setattr(panel.list, "start_media_drag", lambda target: started.append(target))
    panel.list._drag_press = None

    def move_to(point, buttons):
        event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(point),
                            viewport.mapToGlobal(QPointF(point)),
                            Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(viewport, event)

    press = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(centre),
                        viewport.mapToGlobal(QPointF(centre)), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, press)
    move_to(centre + QPoint(2, 1), Qt.MouseButton.LeftButton)
    assert not started, "a movement below the drag threshold must not start a drag"
    move_to(centre + QPoint(30, 12), Qt.MouseButton.LeftButton)
    assert started and started[0] is item, "moving past the threshold must start the media drag"
    panel.list._drag_press = None


def test_insert_at_playhead_and_double_click_still_work(editor, av_file):
    window = editor
    media = add_to_library(window, av_file)
    panel = window.project_panel
    window.engine.session.set_playhead(70)
    panel.insert_at_playhead([media])
    clips = window.engine.timeline.all_clips()
    assert len(clips) == 2 and {c.timeline_start for c in clips} == {70}
    assert clips[0].link_id == clips[1].link_id != 0

    item = media_item_widget(panel, media)
    panel._double_clicked(item)
    assert len(window.engine.timeline.all_clips()) == 4


def test_dropping_on_the_preview_inserts_the_pair_at_the_playhead(editor):
    window = editor
    av = add_to_library(window, av_fixture.write_av_avi(
        os.path.join(tempfile.mkdtemp(prefix="meenifx-preview-"), "preview-drop.avi"), seconds=1.0))
    window.engine.session.set_playhead(45)
    pump(0.05)
    mime = drag_mime(window.project_panel, media_item_widget(window.project_panel, av))

    target = window.preview_panel
    enter = QDragEnterEvent(QPoint(20, 20), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, enter)
    assert enter.isAccepted(), "the preview must accept project media"
    drop = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, drop)
    pump(0.3)

    clips = window.engine.timeline.all_clips()
    assert len(clips) == 2, clips
    assert {clip.stream for clip in clips} == {"video", "audio"}
    assert all(clip.timeline_start == 45 for clip in clips), "the drop must use the playhead"
    assert len({clip.link_id for clip in clips}) == 1 and clips[0].link_id


def test_project_save_and_reload_keeps_the_pair_linked(editor, av_file, app, tmp_path):
    window = editor
    media = add_to_library(window, av_file)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    drop_on_canvas(canvas, drag_mime(panel, media_item_widget(panel, media)), 30)
    before = {c.id: (c.stream, c.link_id, c.timeline_start, c.source_path)
              for c in window.engine.timeline.all_clips()}
    assert window.projects.save()
    path = window.projects.project.path
    original = window.engine.timeline.all_clips()

    second = MainWindow(app)
    second.resize(1200, 760)
    second.show()
    second.projects.open_path(path)
    pump(0.4)
    after = {c.id: (c.stream, c.link_id, c.timeline_start, c.source_path)
             for c in second.engine.timeline.all_clips()}
    assert after == before, (before, after)
    clips = second.engine.timeline.all_clips()
    assert len({c.link_id for c in clips}) == 1
    assert second.engine.timeline.link_is_synced(clips[0])
    assert len(original) == 2
    second.projects._set_clean()
    second.deleteLater()
    pump(0.1)


def test_linked_selection_toggle_switches_group_selection(editor, av_file):
    window = editor
    media = add_to_library(window, av_file)
    panel, canvas = window.project_panel, window.timeline_panel.canvas
    drop_on_canvas(canvas, drag_mime(panel, media_item_widget(panel, media)), 0)
    session = window.engine.session
    video = next(c for c in session.timeline.all_clips() if c.track.type == "video")
    audio = next(c for c in session.timeline.all_clips() if c.track.type == "audio")

    session.set_selection({video.id})
    assert session.selected == {video.id, audio.id}
    assert window.linked_selection_action.isChecked()

    window.linked_selection_action.setChecked(False)
    assert session.linked_selection_enabled is False
    session.set_selection({video.id})
    assert session.selected == {video.id}
    assert video.link_id != 0, "turning the toggle off never unlinks"
