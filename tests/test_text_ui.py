"""Text generator in the real window: add a clip, edit words, key them, undo, save."""

import os
import sys
import time
import uuid

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import text as TXT  # noqa: E402
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
    settings = ProjectSettings(name=f"Txt {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.autokey_button.setChecked(False)
    pump(0.1)
    return window


def words_editor(window):
    for row in window.effects_panel._rows:
        if row.get("kind") == "text":
            return row["editor"]
    return None


def test_add_text_clip_builds_the_inspector_and_renders(editor):
    window = editor
    clip_id = window.add_text_clip()
    pump(0.2)
    assert clip_id is not None
    clip = window.engine.timeline.find_clip(clip_id)
    assert clip.is_text
    assert clip.track.type == "video"
    editor_box = words_editor(window)
    assert isinstance(editor_box, QPlainTextEdit), "the inspector must show the words box"
    assert editor_box.toPlainText() == "Text"
    frame = window.engine.render_frame(0, 1.0)[1]
    assert frame is not None


def test_editing_the_words_updates_the_preview_and_undo(editor):
    window = editor
    clip_id = window.add_text_clip()
    pump(0.2)
    clip = window.engine.timeline.find_clip(clip_id)
    box = words_editor(window)
    before = window.engine.render_frame(0, 1.0)[1]
    box.selectAll()
    box.insertPlainText("HELLO WORLD")
    pump(0.9)
    assert TXT.content_at(window.engine.timeline.find_clip(clip_id), 0) == "HELLO WORLD"
    window.engine.session.set_selection(set())
    window.engine.session.set_selection({clip.id})
    pump(0.1)
    after = window.engine.render_frame(0, 1.0)[1]
    assert float(np.abs(before.astype(np.int16) - after.astype(np.int16)).mean()) > 0.5
    window.engine.session.undo()
    pump(0.1)
    assert TXT.content_at(window.engine.timeline.find_clip(clip_id), 0) == "Text"


def test_auto_key_changes_the_words_without_cutting(editor):
    window = editor
    clip_id = window.add_text_clip()
    pump(0.2)
    window.autokey_button.setChecked(True)
    window.engine.session.set_playhead(0)
    box = words_editor(window)
    box.selectAll()
    box.insertPlainText("FIRST TITLE")
    pump(0.1)
    window.engine.session.set_playhead(45)
    box = words_editor(window)
    assert box.toPlainText() == "FIRST TITLE", "the inspector follows the playhead"
    box.selectAll()
    box.insertPlainText("SECOND TITLE")
    pump(0.15)
    clip = window.engine.timeline.find_clip(clip_id)
    assert TXT.content_keys(clip) == [0, 45], TXT.content_keys(clip)
    assert TXT.content_at(clip, 0) == "FIRST TITLE"
    assert TXT.content_at(clip, 50) == "SECOND TITLE"
    first = window.engine.render_frame(5, 1.0)[1]
    second = window.engine.render_frame(50, 1.0)[1]
    assert float(np.abs(first.astype(np.int16) - second.astype(np.int16)).mean()) > 1.0


def test_text_keyframes_show_up_in_the_timeline_rows(editor):
    window = editor
    clip_id = window.add_text_clip()
    pump(0.2)
    window.autokey_button.setChecked(True)
    box = words_editor(window)
    box.selectAll()
    box.insertPlainText("A")
    window.engine.session.set_playhead(20)
    box = words_editor(window)
    box.selectAll()
    box.insertPlainText("B")
    pump(0.15)
    clip = window.engine.timeline.find_clip(clip_id)
    rows = window.timeline_panel.canvas.animation_rows(clip)
    channels = [row["channel"] for row in rows]
    assert CH.text_content_channel() in channels
    # The timeline paints the text row like any other keyframe row.
    canvas = window.timeline_panel.canvas
    canvas._expanded_clips.add(clip.id)
    assert canvas.grab().width() > 0
    canvas._expanded_clips.discard(clip.id)
    assert CH.key_times(clip, CH.text_content_channel()) == [0, 20]
    # The menu action toggles: the playhead is on an existing key, so it removes it.
    window.toggle_text_key([clip.id])
    pump(0.1)
    assert CH.key_times(clip, CH.text_content_channel()) == [0]
    window.toggle_text_key([clip.id])
    pump(0.1)
    assert CH.key_times(clip, CH.text_content_channel()) == [0, 20]


def test_text_clip_survives_a_project_save_and_reload(editor):
    window = editor
    clip_id = window.add_text_clip()
    pump(0.2)
    window.autokey_button.setChecked(True)
    box = words_editor(window)
    box.selectAll()
    box.insertPlainText("SAVED TITLE")
    pump(0.1)
    clip = window.engine.timeline.find_clip(clip_id)
    clip.text["size"] = 64.0
    clip.text["font"] = "Georgia"
    assert window.projects.save()
    path = window.projects.project.path
    window.projects.open_path(path)
    pump(0.2)
    loaded = next(c for c in window.engine.timeline.all_clips() if c.is_text)
    assert TXT.content_at(loaded, 0) == "SAVED TITLE"
    assert loaded.text["font"] == "Georgia"
    assert loaded.text["size"] == pytest.approx(64.0)


def test_transform_handles_wrap_the_text_not_the_project(editor):
    window = editor
    clip_id = window.add_text_clip()
    pump(0.2)
    clip = window.engine.timeline.find_clip(clip_id)
    overlay = window.overlay
    corners = overlay.clip_corners(clip)
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    assert 60.0 < width < 300.0, "the box must be the words, not the 640px frame"
    assert 40.0 < height < 150.0
    assert abs((min(xs) + max(xs)) / 2.0 - 320.0) < 2.0
    assert 170.0 < (min(ys) + max(ys)) / 2.0 < 190.0
    assert overlay.widget_rect_for(clip).width() < overlay.widget.frame_rect().width() * 0.6
