"""Manual Link/Unlink commands in the real window: menus, shortcuts and selection.

These tests use clips created directly in the timeline (the commands are metadata edits,
not media operations) and drive the canvas the way a right-click does.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QAction, QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu  # noqa: E402

from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402


def pump(seconds=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module")
def window(app):
    MainWindow._load_demo_project = lambda self: None
    editor = MainWindow(app)
    editor.resize(1400, 860)
    editor.show()
    pump(0.2)
    yield editor
    editor.projects._set_clean()
    editor.close()


@pytest.fixture()
def editor(window, tmp_path):
    window.projects._set_clean()
    settings = ProjectSettings(name=f"Links {uuid.uuid4().hex[:6]}", width=640, height=360,
                               fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    for track in window.engine.timeline.tracks:
        track.clips.clear()
    window.engine.session.set_selection(set())
    pump(0.1)
    return window


@pytest.fixture()
def menus(monkeypatch):
    """Capture menus instead of blocking inside QMenu.exec()."""
    captured = []

    def fake_exec(menu, *_args, **_kwargs):
        captured.append(menu)
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    return captured


def make_pair(window, video_start=0, audio_start=0):
    timeline = window.engine.timeline
    video_track = timeline.tracks_of_type("video")[0]
    audio_tracks = timeline.tracks_of_type("audio")
    audio_track = audio_tracks[0] if audio_tracks else timeline.add_track("audio")
    video = timeline.new_clip("cam.mp4", "cam.mp4", video_track, video_start, 60,
                              source_out=60, stream="video")
    audio = timeline.new_clip("mic.wav", "mic.wav", audio_track, audio_start, 60,
                              source_out=60, stream="audio")
    window.engine.session.set_selection({video.id, audio.id})
    pump(0.1)
    return video, audio


def action_named(menu, label):
    return next(action for action in menu.actions() if action.text() == label)


def right_click(canvas, clip):
    x = int(canvas.x_at(clip.timeline_start + max(1, clip.duration // 2)))
    # Stay away from the centre line (gain/opacity envelope) and from the edge grips.
    y = int(canvas.header_y(clip.track) + canvas.track_height(clip.track) * 0.3)
    event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y),
                        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
                        Qt.KeyboardModifier.NoModifier)
    canvas.mousePressEvent(event)
    pump(0.05)


def test_right_click_keeps_a_multi_selection_and_offers_link_and_unlink(editor, menus):
    window = editor
    video, audio = make_pair(window)
    canvas = window.timeline_panel.canvas
    assert window.engine.session.selected == {video.id, audio.id}

    menus.clear()
    right_click(canvas, audio)
    assert window.engine.session.selected == {video.id, audio.id}, \
        "right-clicking a selected clip must not collapse the selection"
    menu = menus[-1]
    link = action_named(menu, "Link Selected Clips")
    unlink = action_named(menu, "Unlink Selected Clips")
    assert link.isEnabled(), "two unlinked clips can be linked whatever their source"
    assert not unlink.isEnabled()

    link.trigger()
    pump(0.05)
    assert video.link_id == audio.link_id != 0
    assert (video.timeline_start, audio.timeline_start) == (0, 0)

    menus.clear()
    right_click(canvas, video)
    menu = menus[-1]
    assert not action_named(menu, "Link Selected Clips").isEnabled()
    assert action_named(menu, "Unlink Selected Clips").isEnabled()


def test_link_selected_clips_does_not_move_either_clip(editor, menus):
    window = editor
    video, audio = make_pair(window, video_start=0, audio_start=9)
    right_click(window.timeline_panel.canvas, audio)
    action_named(menus[-1], "Link Selected Clips").trigger()
    assert video.link_id == audio.link_id != 0
    assert (video.timeline_start, audio.timeline_start) == (0, 9), \
        "linking must preserve the current timeline positions"


def test_ctrl_l_and_ctrl_shift_l_are_wired_to_link_and_unlink(editor):
    window = editor
    video, audio = make_pair(window)
    window.timeline_panel.canvas.setFocus()
    actions = {}
    for action in window.findChildren(QAction):
        if action.text() in ("Link Selected Clips", "Unlink Selected Clips"):
            if not action.shortcut().isEmpty():
                actions[action.text()] = action
    assert actions["Link Selected Clips"].shortcut().toString() == "Ctrl+L"
    assert actions["Unlink Selected Clips"].shortcut().toString() == "Ctrl+Shift+L"

    actions["Link Selected Clips"].trigger()
    pump(0.05)
    assert video.link_id == audio.link_id != 0
    assert actions["Unlink Selected Clips"].isEnabled()

    actions["Unlink Selected Clips"].trigger()
    pump(0.05)
    assert video.link_id == audio.link_id == 0


def test_the_linked_selection_menu_option_separates_the_two_concepts(editor):
    window = editor
    make_pair(window)
    window._sync_linked_selection_action(True)
    action = window.linked_selection_action
    assert action.isChecked()
    assert action.shortcut().toString() == "Ctrl+Shift+K"
    assert "selection behaviour only" in action.toolTip()
    assert "Ctrl+L" in action.toolTip()
    assert "already-linked" in action.toolTip()
    # The old toolbar chain button is gone, but the behaviour is still reachable.
    assert not hasattr(window.timeline_panel, "linked")


def test_silence_dialog_scope_includes_linked_or_matching_clips(editor):
    from meenifx.ui.audio_tools import SilenceDialog

    window = editor
    video, audio = make_pair(window)
    window.engine.timeline.assign_link([video.id, audio.id])
    dialog = SilenceDialog(window, clip_id=audio.id)
    try:
        assert dialog.scope.currentData() == "linked"
        assert {member.id for member in dialog._members} == {video.id, audio.id}
        assert "Will cut:" in dialog.targets.text()
        dialog.scope.setCurrentIndex(dialog.scope.findData("audio"))
        assert [member.id for member in dialog._members] == [audio.id]
    finally:
        dialog.close()

    video2 = window.engine.timeline.new_clip(
        "same.mp4", "same.mp4", video.track, 120, 60, source_out=60, stream="video")
    audio2 = window.engine.timeline.new_clip(
        "same.mp4", "same.mp4", audio.track, 120, 60, source_out=60, stream="audio")
    dialog = SilenceDialog(window, clip_id=audio2.id)
    try:
        assert dialog.scope.currentData() == "matching", \
            "an unlinked but synchronised source is offered by default"
        assert {member.id for member in dialog._members} == {video2.id, audio2.id}
    finally:
        dialog.close()


def test_dead_silence_is_offered_only_where_there_is_audio(editor, menus):
    window = editor
    video, audio = make_pair(window)
    right_click(window.timeline_panel.canvas, audio)
    assert "Remove Dead Silence..." in [action.text() for action in menus[-1].actions()]

    still = window.engine.timeline.new_clip("still.png", "still.png",
                                            window.engine.timeline.tracks_of_type("video")[0],
                                            90, 30, stream="video")
    window.engine.session.set_selection({still.id})
    pump(0.05)
    right_click(window.timeline_panel.canvas, still)
    assert "Remove Dead Silence..." not in [action.text() for action in menus[-1].actions()]
    assert video.link_id == audio.link_id
