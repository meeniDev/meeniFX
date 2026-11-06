"""Smart Paste: clipboard pictures and files become project media, text stays text."""

import os
import time

import numpy as np
import pytest
from PyQt6.QtCore import QMimeData, QPointF, QUrl
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QLineEdit

from meenifx.media.reader import read_image
from meenifx.project.settings import ProjectSettings
from meenifx.ui import smart_paste as SP
from test_keyframe_ui import editor  # noqa: F401  (shared offscreen window fixture)


def half_transparent(width=64, height=48):
    """A picture whose left half is fully transparent and whose right half is solid red."""
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(width // 2, 0, width // 2, height, QColor(220, 40, 40, 255))
    painter.end()
    return image


@pytest.fixture()
def window(editor):
    """An editor whose close never asks "save changes?" (that dialog would block the run)."""
    yield editor
    editor.projects._set_clean()
    editor.projects.autosave_timer.stop()


def process(seconds=0.02):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.004)


def clipboard():
    return QApplication.clipboard()


def start_project(window, tmp_path):
    settings = ProjectSettings.from_preset("720p 30", "Paste Test")
    assert window.projects.create_project(settings, str(tmp_path / "projects"))
    process(0.2)
    return window.projects.project


def wait_for(predicate, seconds=6.0, message=""):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        process(0.02)
        if predicate():
            return True
    assert predicate(), message
    return True


# ------------------------------------------------------------------ stills
def test_pasted_picture_becomes_a_project_file_and_a_clip(window, tmp_path):
    editor = window
    project = start_project(editor, tmp_path)
    clipboard().clear()
    clipboard().setImage(half_transparent())

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips()), "the paste never created a clip"

    clips = editor.engine.timeline.all_clips()
    assert len(clips) == 1
    clip = clips[0]
    assert clip.kind == "image" and clip.duration == 150, "5 s at 30 fps by default"
    assert os.path.isfile(clip.source_path), "the clipboard pixels must live in a real file"
    expected_folder = os.path.join(project.folder, "Media")
    assert os.path.dirname(clip.source_path) == expected_folder
    assert clip.source_path.lower().endswith(".png")

    # The library references the same file, so a later save writes it into .mfxpf.
    item = project.library.by_path(clip.source_path)
    assert item is not None and item.type == "image"
    assert (item.width, item.height) == (64, 48)

    picture = read_image(clip.source_path)
    assert picture.shape[2] == 4, "the PNG kept its alpha channel"
    assert int(picture[:, :, 3].min()) == 0 and int(picture[:, :, 3].max()) == 255

    # One paste is one undo step and removes the clip again.
    before = len(editor.engine.timeline.all_clips())
    editor.engine.session.undo()
    assert len(editor.engine.timeline.all_clips()) == before - 1


def test_pasted_picture_survives_save_and_reopen(window, tmp_path):
    editor = window
    project = start_project(editor, tmp_path)
    clipboard().clear()
    clipboard().setImage(half_transparent(32, 32))
    editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips())

    assert editor.projects.save()
    path = project.path
    assert path and os.path.isfile(path)
    from meenifx.project.container import read_container

    data = read_container(path)
    items = data["media"]["items"]
    assert len(items) == 1 and items[0]["type"] == "image"
    assert items[0]["rel_path"].startswith("Media/")
    stored = os.path.join(project.folder, items[0]["rel_path"].replace("/", os.sep))
    assert os.path.isfile(stored), "the picture is stored next to the project"
    assert len(data["clips"]["id"]) == 1


def test_paste_preferences_set_duration_placement_and_selection(window, tmp_path):
    editor = window
    project = start_project(editor, tmp_path)
    timeline = editor.engine.timeline
    track = timeline.tracks_of_type("video")[0]
    existing = timeline.new_clip("existing", "", track, 0, 40, source_out=40)
    editor.engine.session.set_selection({existing.id})

    SP.PasteSettings(2.0, "after", select=False, fit=True).save()
    clipboard().clear()
    clipboard().setImage(half_transparent())
    editor.smart_paste.paste()
    assert wait_for(lambda: len(timeline.all_clips()) == 2)
    pasted = next(c for c in timeline.all_clips() if c.id != existing.id)
    assert pasted.duration == 60, "2 s at 30 fps"
    assert pasted.timeline_start == existing.end, "placed after the selection, not at the playhead"
    assert pasted.id not in editor.engine.session.selected, "the preference asked for no selection"
    assert pasted.transform["scale_x"] == 100.0
    assert project is not None


def test_pasted_file_from_explorer_uses_the_real_file(window, tmp_path):
    editor = window
    start_project(editor, tmp_path)
    import cv2

    source = tmp_path / "from explorer.png"
    cv2.imwrite(str(source), np.full((20, 30, 3), 90, np.uint8))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    clipboard().clear()
    clipboard().setMimeData(mime)
    editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips())
    clip = editor.engine.timeline.all_clips()[0]
    assert os.path.normcase(os.path.abspath(clip.source_path)) ==         os.path.normcase(os.path.abspath(str(source))), "a copied file is linked, not copied again"


# ------------------------------------------------------------------- rules
def test_text_field_keeps_its_own_paste(window, tmp_path):
    editor = window
    start_project(editor, tmp_path)
    field = QLineEdit()
    field.show()
    field.setFocus()
    process(0.05)
    clipboard().clear()
    clipboard().setText("typed words")
    editor.smart_paste.paste()
    assert field.text() == "typed words"
    assert editor.engine.timeline.all_clips() == [], "a text paste never touches the timeline"
    field.close()


def test_media_panel_focus_imports_without_a_clip(window, tmp_path):
    editor = window
    project = start_project(editor, tmp_path)
    panel = editor.project_panel
    panel.show()
    editor.activateWindow()
    process(0.05)
    panel.list.setFocus()
    process(0.05)
    clipboard().clear()
    clipboard().setImage(half_transparent())
    editor.smart_paste.paste()
    assert wait_for(lambda: len(project.library) == 1, 4.0)
    assert editor.engine.timeline.all_clips() == [], "the Media panel only imports"
    panel.hide()


def test_internal_clip_paste_still_works_until_the_clipboard_changes(window, tmp_path):
    editor = window
    start_project(editor, tmp_path)
    timeline = editor.engine.timeline
    track = timeline.tracks_of_type("video")[0]
    clip = timeline.new_clip("copy me", "", track, 0, 30, source_out=30)
    editor.engine.session.set_selection({clip.id})
    editor.engine.session.copy_selected()
    clipboard().clear()
    clipboard().setText("unrelated text")
    editor.smart_paste.paste()
    assert len(timeline.all_clips()) == 2, "an in-app copy pastes clips, not clipboard text"

    clipboard().setImage(half_transparent())          # a new clipboard picture is newer
    editor.smart_paste.paste()
    assert wait_for(lambda: any(c.kind == "image" for c in timeline.all_clips()),
                    message="a fresh clipboard picture wins over the older internal copy")


def test_a_still_url_is_recognised_but_a_web_page_is_not():
    assert SP.image_url("https://example.com/pics/cat.png") == "https://example.com/pics/cat.png"
    assert SP.image_url("https://example.com/page?id=3") == ""
    assert SP.image_url("not a link") == ""
    assert SP.image_url("ftp://example.com/cat.png") == ""


def test_paste_settings_round_trip(tmp_path):
    from PyQt6.QtCore import QSettings

    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    SP.PasteSettings(7.5, "after", select=False, fit=True).save(store)
    loaded = SP.PasteSettings.load(store)
    assert (loaded.seconds, loaded.placement, loaded.select, loaded.fit) == (7.5, "after", False, True)
    SP.PasteSettings("bad", "everywhere", True, False).save(store)
    loaded = SP.PasteSettings.load(store)
    assert (loaded.seconds, loaded.placement) == (5.0, "playhead")


def test_clipboard_gif_bytes_are_kept(window, tmp_path):
    editor = window
    start_project(editor, tmp_path)
    mime = QMimeData()
    mime.setData("image/gif", b"GIF89a" + b"\x00" * 16)
    clipboard().clear()
    clipboard().setMimeData(mime)
    payload = editor.smart_paste.payload()
    assert payload and payload["kind"] == "gif" and payload["data"].startswith(b"GIF89a")
    assert SP.gif_bytes(None) is None
