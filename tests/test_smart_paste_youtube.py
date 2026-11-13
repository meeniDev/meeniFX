"""Smart Paste + YouTube: the window, the modes, the range and the finished project media.

Metadata and the download are replaced with local stand-ins, so the tests drive the real
dialog, the real jobs, timeline insertion and saving without touching YouTube.
"""

import os
import shutil
import time

import pytest
from PyQt6.QtWidgets import QDialog, QDialogButtonBox

from link_fixtures import png_bytes
from meenifx.media import youtube
from meenifx.ui import smart_paste as SP
from meenifx.ui import youtube_import as YI
from test_keyframe_ui import editor  # noqa: F401  (shared offscreen window fixture)
from test_smart_paste import clipboard, process, start_project, wait_for
from test_youtube_media import fake_info
from youtube_fixtures import av_mp4, m4a_file

LINK = youtube.parse_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
LINK_URL = LINK.url


def fake_meta(link=None):
    return youtube.meta_from_info(link or LINK, fake_info(), thumbnail=png_bytes())


def dialog_with_meta(qapp, **kwargs):
    dialog = YI.YouTubeImportDialog(None, LINK, **kwargs)
    dialog.set_meta(fake_meta())
    return dialog


def quality_keys(dialog):
    return [dialog.quality_combo.itemData(i) for i in range(dialog.quality_combo.count())]


def quality_labels(dialog):
    return [dialog.quality_combo.itemText(i) for i in range(dialog.quality_combo.count())]


@pytest.fixture()
def window(editor):
    yield editor
    editor.projects._set_clean()
    editor.projects.autosave_timer.stop()


def accept_youtube(monkeypatch, *, mode="video_audio", start=None, end=None, add=True,
                   insert=True, inspect=None):
    """Stand in for the person using the window (a modal exec would block the test)."""
    def run(dialog):
        assert wait_for(lambda: dialog.meta is not None or dialog.error, 20,
                        "the video details never arrived"), dialog.error
        assert dialog.meta is not None, dialog.error
        assert dialog.select_mode(mode), f"{mode} is not offered for this video"
        if start is not None:
            dialog.set_range(start, end)
        dialog.permission.setChecked(True)
        dialog.add_box.setChecked(add)
        dialog.insert_box.setChecked(insert)
        dialog._validate()
        if inspect is not None:
            inspect(dialog)
        assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled(), \
            dialog.range_problem()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(YI.YouTubeImportDialog, "exec", run)


def fake_importer(tmp_path, *, sound=True, audio_only=False):
    """A local stand-in for import_media that still produces a real, decodable file."""
    seen = {}

    def import_media(link, meta, request, folder, *, progress=None, cancelled=None,
                     timeout=None):
        seen["request"] = request
        seen["folder"] = folder
        os.makedirs(folder, exist_ok=True)
        if audio_only:
            source = m4a_file(str(tmp_path / "source"))
            target = os.path.join(folder, "Imported tone.m4a")
        else:
            source = av_mp4(str(tmp_path / "source"), 4.0, audio=sound)
            target = os.path.join(folder, "Imported video.mp4")
        shutil.copyfile(source, target)
        if progress:
            progress("Finishing", 1.0, 1.0)
        return youtube.Imported(target, meta.title, 2.0 if audio_only else 4.0,
                                0 if audio_only else 320, 0 if audio_only else 240,
                                os.path.getsize(target))
    import_media.seen = seen
    return import_media


def use_metadata(monkeypatch, meta=None, error=""):
    def fetch(_link, **_kwargs):
        if error:
            raise youtube.YouTubeError(error)
        return meta or fake_meta()
    monkeypatch.setattr(SP.youtube, "fetch_metadata", fetch)


def media_files(project):
    folder = os.path.join(project.folder, "Media")
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


# ----------------------------------------------------------------- the window
def test_the_window_shows_the_video_and_only_the_qualities_it_has(qapp):
    dialog = dialog_with_meta(qapp)
    try:
        assert dialog.windowTitle() == "Import YouTube Video"
        assert dialog.title_label.text() == "Sunset Test"
        assert "Meeni Studio" in dialog.channel_label.text()
        assert "00:02:00.000" in dialog.duration_label.text()
        assert not dialog.thumbnail.pixmap().isNull(), "a thumbnail was offered and shown"
        assert quality_keys(dialog) == ["best", "1080", "720", "480", "360"]
        assert not any("1440" in label for label in quality_labels(dialog))
        modes = [dialog.mode_combo.itemText(i) for i in range(dialog.mode_combo.count())]
        assert modes == ["Video + Audio", "Video Only", "Audio Only"]
        assert dialog.advanced.isVisible() is False, "advanced settings start collapsed"
    finally:
        dialog.deleteLater()


def test_import_waits_for_the_permission_confirmation(qapp):
    dialog = dialog_with_meta(qapp)
    try:
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert not dialog.permission.isChecked()
        assert not ok.isEnabled()
        assert "permission" in dialog.range_problem().lower()
        dialog.permission.setChecked(True)
        assert ok.isEnabled()
        dialog.permission.setChecked(False)
        assert not ok.isEnabled(), "permission cannot be skipped"
    finally:
        dialog.deleteLater()


def test_audio_only_offers_audio_qualities_not_resolutions(qapp):
    dialog = dialog_with_meta(qapp)
    try:
        assert dialog.select_mode("audio_only")
        assert quality_keys(dialog) == ["best", "audio-48"]
        assert all("kbps" in label for label in quality_labels(dialog))
        assert dialog.select_mode("video_only")
        assert quality_keys(dialog) == ["best", "1080", "720", "480", "360"]
    finally:
        dialog.deleteLater()


def test_invalid_ranges_disable_import_and_say_why(qapp):
    dialog = dialog_with_meta(qapp)
    try:
        dialog.permission.setChecked(True)
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        dialog.start_edit.setText("-5")
        dialog._edits_changed()
        assert "negative" in dialog.range_problem().lower() and not ok.isEnabled()
        assert "after the start" in dialog.set_range(20, 10)
        assert "past the end" in dialog.set_range(0, 500)
        assert dialog.set_range(12.345, 30.5) == ""
        assert dialog.start_edit.text() == "00:00:12.345", "precise time entry is kept"
        assert ok.isEnabled()
        dialog.reset_range()
        assert dialog.start_edit.text() == "00:00:00.000"
        assert dialog.end_edit.text() == "00:02:00.000"
        assert ok.isEnabled()
    finally:
        dialog.deleteLater()


def test_a_failed_probe_explains_itself_and_offers_a_local_file(qapp, monkeypatch, tmp_path):
    dialog = YI.YouTubeImportDialog(None, LINK)
    try:
        dialog.set_error("This video is private. Ask the owner for access.")
        assert dialog.meta is None
        assert "private" in dialog.status.text().lower()
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
        assert not dialog.mode_combo.isEnabled()
        local = tmp_path / "mine.mp4"
        local.write_bytes(b"not really media")
        monkeypatch.setattr(YI.FD, "open_file",
                            lambda *args, **kwargs: str(local))
        dialog._choose_local()
        assert dialog.local_path == str(local)
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------- the import
def test_a_pasted_watch_link_imports_picture_and_sound(window, tmp_path, monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    use_metadata(monkeypatch)
    importer = fake_importer(tmp_path)
    monkeypatch.setattr(SP.youtube, "import_media", importer)
    accept_youtube(monkeypatch, start=1.0, end=3.0)
    clipboard().setText(LINK_URL)

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 25,
                    "the YouTube import never produced a clip")

    request = importer.seen["request"]
    assert (request.mode, request.start, request.end) == ("video_audio", 1.0, 3.0)
    clip = editor.engine.timeline.all_clips()[0]
    assert clip.kind == "video" and os.path.isfile(clip.source_path)
    assert os.path.dirname(os.path.abspath(clip.source_path)) == \
        os.path.abspath(os.path.join(project.folder, "Media"))
    assert project.library.by_path(clip.source_path) is not None

    editor.engine.session.undo()
    assert editor.engine.timeline.all_clips() == [], "one import is one undo step"


def test_video_only_mode_imports_a_soundless_clip(window, tmp_path, monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    use_metadata(monkeypatch)
    importer = fake_importer(tmp_path, sound=False)
    monkeypatch.setattr(SP.youtube, "import_media", importer)
    accept_youtube(monkeypatch, mode="video_only")
    clipboard().setText(LINK_URL)

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 25)
    assert importer.seen["request"].mode == "video_only"
    clip = editor.engine.timeline.all_clips()[0]
    assert not editor._has_audio(clip.source_path), "Video Only carries no sound"


def test_audio_only_import_is_ready_for_waveforms_and_captions(window, tmp_path, monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    use_metadata(monkeypatch)
    importer = fake_importer(tmp_path, audio_only=True)
    monkeypatch.setattr(SP.youtube, "import_media", importer)
    accept_youtube(monkeypatch, mode="audio_only")
    clipboard().setText(LINK_URL)

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 25)
    assert importer.seen["request"].mode == "audio_only"
    clip = editor.engine.timeline.all_clips()[0]
    assert clip.source_path.lower().endswith(".m4a")
    assert clip.carries_audio

    from meenifx.ui import audio_tools

    assert [c.id for c in audio_tools.audio_clips(editor, {clip.id})] == [clip.id], \
        "Auto Captions sees the imported sound as a normal audio clip"
    assert wait_for(lambda: editor.waveforms.library.cached(clip.source_path) is not None, 25,
                    "the imported audio must have a waveform")


def test_the_media_panel_default_is_import_only(window, tmp_path, monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    panel = editor.project_panel
    panel.show()
    editor.activateWindow()
    process(0.05)
    panel.list.setFocus()
    process(0.05)
    use_metadata(monkeypatch)
    monkeypatch.setattr(SP.youtube, "import_media",
                        lambda *args, **kwargs: pytest.fail("nothing should download here"))
    seen = {}

    def inspect(dialog):
        seen["insert"] = dialog.insert_box.isChecked()
        seen["add"] = dialog.add_box.isChecked()

    def run(dialog):
        assert wait_for(lambda: dialog.meta is not None, 20)
        inspect(dialog)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(YI.YouTubeImportDialog, "exec", run)
    clipboard().setText(LINK_URL)
    try:
        assert editor.smart_paste.paste()
        assert seen == {"insert": False, "add": True}
        assert editor.engine.timeline.all_clips() == []
    finally:
        panel.hide()


def test_cancelling_an_import_leaves_no_media(window, tmp_path, monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    use_metadata(monkeypatch)

    def slow_import(link, meta, request, folder, *, progress=None, cancelled=None,
                    timeout=None):
        if progress:
            progress("Downloading", 0.1, 1.0)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                raise youtube.YouTubeCancelled()
            time.sleep(0.01)
        raise AssertionError("the test never cancelled the import")

    monkeypatch.setattr(SP.youtube, "import_media", slow_import)
    accept_youtube(monkeypatch)
    clipboard().setText(LINK_URL)

    assert editor.smart_paste.paste()
    assert wait_for(lambda: LINK_URL in editor.smart_paste._youtube_progress, 20,
                    "no progress window appeared")
    editor.smart_paste._youtube_progress[LINK_URL]._cancel()
    assert wait_for(lambda: LINK_URL not in editor.smart_paste._youtube_jobs, 25,
                    "cancel never finished")
    assert editor.engine.timeline.all_clips() == []
    assert media_files(project) == []


def test_a_private_video_is_explained_without_downloading(window, tmp_path, monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    use_metadata(monkeypatch, error="This video is private. Ask the owner for access.")
    monkeypatch.setattr(SP.youtube, "import_media",
                        lambda *args, **kwargs: pytest.fail("nothing should download here"))
    seen = {}

    def run(dialog):
        assert wait_for(lambda: dialog.error, 20, "the error state never appeared")
        seen["error"] = dialog.error
        seen["import_enabled"] = dialog.buttons.button(
            QDialogButtonBox.StandardButton.Ok).isEnabled()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(YI.YouTubeImportDialog, "exec", run)
    clipboard().setText(LINK_URL)
    assert editor.smart_paste.paste()
    assert "private" in seen["error"].lower()
    assert seen["import_enabled"] is False
    assert editor.engine.timeline.all_clips() == []
    assert media_files(project) == []


def test_an_invalid_youtube_link_is_explained_without_opening_the_window(window, tmp_path,
                                                                        monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    messages = []
    monkeypatch.setattr(editor, "toast_message", lambda text, **kw: messages.append(text))
    monkeypatch.setattr(YI.YouTubeImportDialog, "exec",
                        lambda dialog: pytest.fail("no window for a broken link"))
    clipboard().setText("https://www.youtube.com/watch?v=abc")

    assert editor.smart_paste.paste()
    assert messages and "invalid video ID" in messages[0]
    assert editor.engine.timeline.all_clips() == []


def test_other_video_platforms_are_still_explained(window, tmp_path, monkeypatch):
    editor = window
    start_project(editor, tmp_path)
    messages = []
    from meenifx.model.session import TimelineSession

    monkeypatch.setattr(TimelineSession, "_clipboard", None, raising=False)
    monkeypatch.setattr(editor, "toast_message", lambda text, **kw: messages.append(text))
    clipboard().setText("https://vimeo.com/123456789")

    assert editor.smart_paste.paste()
    assert messages and "Vimeo" in messages[0]
    assert editor.engine.timeline.all_clips() == []


def test_youtube_media_survives_save_and_reopen(window, tmp_path, monkeypatch):
    editor = window
    project = start_project(editor, tmp_path)
    use_metadata(monkeypatch)
    monkeypatch.setattr(SP.youtube, "import_media", fake_importer(tmp_path))
    accept_youtube(monkeypatch)
    clipboard().setText(LINK_URL)

    assert editor.smart_paste.paste()
    assert wait_for(lambda: editor.engine.timeline.all_clips(), 25)
    assert editor.projects.save()
    assert project.path and os.path.isfile(project.path)

    from meenifx.project.container import read_container

    data = read_container(project.path)
    items = data["media"]["items"]
    assert len(items) == 1 and items[0]["type"] == "video"
    assert items[0]["rel_path"].startswith("Media/")
    stored = os.path.join(project.folder, items[0]["rel_path"].replace("/", os.sep))
    assert os.path.isfile(stored), "the imported video is stored next to the project"
    # Video + Audio becomes the picture clip and its linked sound clip.
    assert len(data["clips"]["id"]) == 2
