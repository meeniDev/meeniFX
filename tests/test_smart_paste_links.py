"""Smart Paste links: consent dialog, download progress, project media, timeline, save.

The HTTP server is local (tests/link_fixtures.py), and only its loopback address is allowed
through the private-address guard - the guard itself is covered in test_link_import.py.
"""

import os

import pytest
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel

from link_fixtures import media_server  # noqa: F401  (pytest fixture)
from meenifx.media import link_import as L
from meenifx.ui import smart_paste as SP
from test_keyframe_ui import editor  # noqa: F401  (shared offscreen window fixture)
from test_smart_paste import clipboard, process, start_project, wait_for


@pytest.fixture(autouse=True)
def loopback_links(monkeypatch):
    monkeypatch.setenv(L.ALLOW_LOCAL_ENV, "1")


@pytest.fixture()
def window(editor):
    """An editor whose close never asks about unsaved changes."""
    yield editor
    editor.projects._set_clean()
    editor.projects.autosave_timer.stop()


def accept_import(monkeypatch, add=True, insert=True, preserve=True):
    """Stand in for the person clicking Import (a modal dialog would block the test)."""
    def run(dialog):
        dialog.add_box.setChecked(add)
        dialog.insert_box.setChecked(insert)
        dialog.preserve_box.setChecked(preserve)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SP.ImportLinkDialog, "exec", run)


def media_names(project):
    folder = os.path.join(project.folder, "Media")
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


# ------------------------------------------------------------------- the dialog
def test_import_dialog_shows_the_link_and_defaults(media_server, qapp):
    info = L.probe(media_server.url("/anim.gif"))
    dialog = SP.ImportLinkDialog(None, info, insert_default=True)
    try:
        assert dialog.windowTitle() == "Import Media from Link"
        text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert info.host in text and "Animated GIF" in text and "anim.gif" in text
        assert "Nothing is downloaded" in text
        assert dialog.add_box.isChecked() and dialog.insert_box.isChecked()
        assert dialog.preserve_box.isChecked()
        assert not dialog.preserve_box.isEnabled(), "an animated GIF is always kept as it is"
        accept_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert accept_button.text() == "Import"
        assert dialog.options() == {"add": True, "insert": True, "preserve": True}
        dialog.add_box.setChecked(False)
        dialog.insert_box.setChecked(False)
        assert not accept_button.isEnabled(), "Import needs a destination"
    finally:
        dialog.deleteLater()


def test_from_the_media_panel_the_default_is_import_only(window):
    panel = window.project_panel
    panel.show()
    window.activateWindow()
    process(0.05)
    panel.list.setFocus()
    process(0.05)
    try:
        assert window.smart_paste._media_panel_active()
        info = L.LinkInfo("https://cdn.discordapp.com/attachments/1/2/cat.png",
                          "cdn.discordapp.com", "cat.png", ".png", "image")
        dialog = SP.ImportLinkDialog(window, info,
                                     insert_default=not window.smart_paste._media_panel_active())
        try:
            assert dialog.add_box.isChecked()
            assert not dialog.insert_box.isChecked()
        finally:
            dialog.deleteLater()
    finally:
        panel.hide()


# ------------------------------------------------------------- the whole import
def test_pasted_png_link_becomes_project_media_and_a_clip(window, tmp_path, media_server,
                                                          monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    accept_import(monkeypatch)
    clipboard().setText(media_server.url("/cat.png"))

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 12,
                    "the pasted link never produced a clip")

    clip = editor.engine.timeline.all_clips()[0]
    assert clip.kind == "image" and os.path.isfile(clip.source_path)
    assert os.path.dirname(clip.source_path) == os.path.join(project.folder, "Media")
    assert project.library.by_path(clip.source_path) is not None
    assert media_names(project) == ["cat.png"]

    # One paste is one undo step.
    editor.engine.session.undo()
    assert editor.engine.timeline.all_clips() == []


def test_pasted_gif_link_stays_animated(window, tmp_path, media_server, monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    accept_import(monkeypatch)
    clipboard().setText(media_server.url("/anim.gif"))

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 12,
                    "the pasted GIF never produced a clip")

    clip = editor.engine.timeline.all_clips()[0]
    assert clip.source_path.lower().endswith(".gif"), "the original GIF file must be kept"
    entry = editor.engine.media.entry(clip.source_path)
    assert entry.kind == "video" and entry.frame_count > 1, "the GIF must not flatten"


def test_a_video_link_is_inserted_at_the_playhead(window, tmp_path, media_server, monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    editor.engine.session.set_playhead(20)
    accept_import(monkeypatch)
    clipboard().setText(media_server.url("/clip.mp4"))

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 15,
                    "the pasted video never produced a clip")
    clip = editor.engine.timeline.all_clips()[0]
    assert clip.source_path.lower().endswith(".mp4")
    assert clip.timeline_start >= 20, "the import goes to the playhead"


def test_an_expired_link_is_explained_and_imports_nothing(window, tmp_path, media_server,
                                                          monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    messages = []
    monkeypatch.setattr(editor, "toast_message", lambda text, **kw: messages.append(text))

    def no_dialog(_dialog):
        raise AssertionError("an expired link must not open the import dialog")

    monkeypatch.setattr(SP.ImportLinkDialog, "exec", no_dialog)
    clipboard().setText(media_server.url("/expired.png?ex=65a1b2c3&is=abc&hm=def"))

    assert editor.smart_paste.paste()
    assert wait_for(lambda: any("expired" in text.lower() for text in messages), 12,
                    "the expired link produced no message")
    assert "expired" in " ".join(messages).lower()
    assert editor.engine.timeline.all_clips() == []
    assert media_names(project) == []


def test_cancelling_the_download_leaves_no_file_or_clip(window, tmp_path, media_server,
                                                        monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    accept_import(monkeypatch)
    clipboard().setText(media_server.url("/slow.gif"))
    url = clipboard().text()

    assert editor.smart_paste.paste()
    assert wait_for(lambda: url in editor.smart_paste._progress, 12, "no progress dialog")
    editor.smart_paste._progress[url]._cancel()
    assert wait_for(lambda: url not in editor.smart_paste._progress, 20, "cancel never finished")

    assert editor.engine.timeline.all_clips() == []
    assert media_names(project) == [], "a cancelled download must not leave a partial file"


def test_a_web_page_link_is_explained_without_touching_the_network(window, tmp_path, media_server,
                                                                  monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    messages = []
    # An in-app clip copy legitimately outranks clipboard text; this test is about the link.
    from meenifx.model.session import TimelineSession

    monkeypatch.setattr(TimelineSession, "_clipboard", None, raising=False)
    monkeypatch.setattr(editor, "toast_message", lambda text, **kw: messages.append(text))
    clipboard().setText(media_server.url("/page"))

    assert editor.smart_paste.paste()
    assert messages and "web page" in messages[0].lower()
    assert media_server.requests == []


def test_imported_link_media_survives_save_and_reopen(window, tmp_path, media_server, monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    accept_import(monkeypatch)
    clipboard().setText(media_server.url("/cat.png"))

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 12)
    assert editor.projects.save()
    assert project.path and os.path.isfile(project.path)

    from meenifx.project.container import read_container

    data = read_container(project.path)
    items = data["media"]["items"]
    assert len(items) == 1 and items[0]["type"] == "image"
    assert items[0]["rel_path"].startswith("Media/")
    stored = os.path.join(project.folder, items[0]["rel_path"].replace("/", os.sep))
    assert os.path.isfile(stored)
    assert len(data["clips"]["id"]) == 1


def test_a_running_download_is_owned_by_smart_paste(window, tmp_path, media_server, monkeypatch):
    """The crash fix: the job must have a real owner while its thread is running.

    A QRunnable with autoDelete off is owned by Python, so the old code - which kept only the
    URL string - collected it mid-download, and the process died with no traceback at all.
    """
    editor = window
    start_project(editor, tmp_path)
    accept_import(monkeypatch)
    clipboard().setText(media_server.url("/slow.gif"))
    url = clipboard().text()

    assert editor.smart_paste.paste()
    assert wait_for(lambda: url in editor.smart_paste._links, 12, "the download never started")
    job = editor.smart_paste._links[url]
    assert isinstance(job, SP._DownloadJob)
    assert not job.autoDelete(), "the thread pool must not delete the runnable under itself"
    editor.smart_paste._progress[url]._cancel()
    assert wait_for(lambda: url not in editor.smart_paste._links, 20, "the job was never released")
