"""Project folders are virtual: hierarchy, moves, deletion and save/reopen."""

import os

import pytest

from meenifx.project.media import MediaLibrary
from meenifx.project import storage


def library_with_nested_folders():
    library = MediaLibrary()
    videos = library.add_bin("Videos")
    raw = library.add_bin("Raw", videos)
    graphics = library.add_bin("Graphics", videos)
    screenshots = library.add_bin("Screenshots", graphics)
    footage = library.add("C:/media/one.mp4", media_type="video", bin_id=raw)
    logo = library.add("C:/media/logo.png", media_type="image", bin_id=screenshots)
    return library, videos, raw, graphics, screenshots, footage, logo


def test_folders_nest_and_build_a_breadcrumb_chain():
    library, videos, raw, graphics, screenshots, _footage, _logo = \
        library_with_nested_folders()
    assert library.child_bins("") == [videos]
    assert library.child_bins(videos) == [raw, graphics]
    assert library.bin_chain(screenshots) == [videos, graphics, screenshots]
    assert library.bin_contains(videos, screenshots)
    assert not library.bin_contains(raw, screenshots)


def test_moving_media_between_folders_changes_no_path_or_timeline_reference():
    from conftest import build_engine

    library, _videos, raw, graphics, _screenshots, footage, _logo = \
        library_with_nested_folders()
    engine, clip = build_engine()
    engine.timeline.find_clip(clip.id).source_path = footage.path
    before = footage.path
    library.move_to_bin([footage.id], graphics)
    assert footage.bin == graphics
    assert footage.path == before
    assert engine.timeline.find_clip(clip.id).source_path == before
    # Multiple selection moves as one operation.
    library.move_to_bin([footage.id], raw)
    assert library.items[footage.id].bin == raw


def test_deleting_a_folder_can_move_its_contents_to_the_parent():
    library, videos, raw, _graphics, _screenshots, footage, _logo = \
        library_with_nested_folders()
    library.move_to_bin([footage.id], raw)
    library.remove_bin(raw, remove_items=False)
    assert raw not in library.bins
    assert footage.id in library.items
    assert library.items[footage.id].bin == videos


def test_deleting_a_folder_with_media_removes_references_not_files(tmp_path):
    library, videos, raw, _graphics, _screenshots, footage, _logo = \
        library_with_nested_folders()
    real = tmp_path / "one.mp4"
    real.write_bytes(b"not a real video, just a file that must survive")
    footage.path = str(real)
    library.reindex()
    library.remove_bin(videos, remove_items=True)
    assert real.exists(), "folder deletion must never touch the filesystem"
    assert footage.id not in library.items
    assert not library.bins


def test_nested_folders_survive_a_project_round_trip():
    library, videos, raw, graphics, screenshots, footage, logo = \
        library_with_nested_folders()
    rebuilt = MediaLibrary.from_dict(library.to_dict())
    assert rebuilt.bins == library.bins
    assert rebuilt.bin_chain(screenshots) == [videos, graphics, screenshots]
    assert rebuilt.items[footage.id].bin == raw
    assert rebuilt.items[logo.id].bin == screenshots
    assert rebuilt.items[footage.id].path == footage.path


def test_old_flat_bin_lists_still_load():
    library = MediaLibrary.from_dict({
        "items": [{"id": "m1", "type": "video", "name": "a.mp4", "path": "/a.mp4",
                   "bin": "b1"}],
        "bins": [{"id": "b1", "name": "Legacy"}],
    })
    assert library.bins == {"b1": "Legacy"}
    assert library.bin_parent("b1") == ""
    assert library.items["m1"].bin == "b1"


def test_folder_panel_uses_folder_terms_and_never_deletes_the_source(tmp_path, qapp,
                                                                    monkeypatch):
    """Delete File from Computer only ever delegates to the Recycle Bin helper."""
    from PyQt6.QtWidgets import QMessageBox

    from meenifx.project.settings import ProjectSettings
    from meenifx.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    moved = []
    monkeypatch.setattr(storage, "trash_path",
                        lambda path: (moved.append(path) or (True, "")))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes))
    window = MainWindow(qapp)
    window.resize(1200, 800)
    window.show()
    try:
        assert window.projects.create_project(
            ProjectSettings(name="Folders", width=640, height=360, fps=30.0), str(tmp_path))
        panel = window.project_panel
        assert panel.bin_button.text() == "New Folder"
        source = tmp_path / "take.mp4"
        source.write_bytes(b"pretend footage")
        item = window.projects.project.library.add(str(source), media_type="video")
        panel.rebuild(force=True)

        assert panel.delete_files([item]) is True
        assert moved == [str(source)]
        assert item.id not in window.projects.project.library.items
        assert source.exists(), "the panel must not unlink a file it did not move"
    finally:
        window.projects._set_clean()
        window.close()


def test_a_corrupt_parent_cycle_is_broken_on_load():
    library = MediaLibrary.from_dict({
        "items": [],
        "bins": [{"id": "b1", "name": "One", "parent": "b2"},
                 {"id": "b2", "name": "Two", "parent": "b1"}],
    })
    # Either folder may win the repair, but the chain must terminate.
    assert library.bin_chain("b1") in (["b1"], ["b2", "b1"])
    assert library.bin_chain("b2") in (["b2"], ["b1", "b2"])
