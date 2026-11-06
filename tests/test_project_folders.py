"""Project folders: real drop targets, navigation, moving media and save/reopen."""

import os
import time

import cv2
import numpy as np
import pytest
from PyQt6.QtCore import QMimeData, QPointF, QSettings, Qt
from PyQt6.QtGui import QDragLeaveEvent, QDropEvent
from PyQt6.QtWidgets import QInputDialog

from meenifx.project.media import norm_path

from conftest import synthetic_frame
from meenifx.project.settings import ProjectSettings
from meenifx.ui.project_panel import ROOT_BIN


def pump(qapp, seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


@pytest.fixture()
def panel_window(qapp, tmp_path, monkeypatch):
    from meenifx.ui import layout as dock_layout
    from meenifx.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    monkeypatch.setattr(dock_layout, "app_settings",
                        lambda: QSettings(str(tmp_path / "layout.ini"),
                                          QSettings.Format.IniFormat))
    window = MainWindow(qapp)
    window.resize(1400, 900)
    window.show()
    pump(qapp, 0.2)
    # Failures before Save would otherwise open the unsaved-changes prompt during
    # teardown and hide the real assertion in a headless run.
    window.closeEvent = lambda event: event.accept()
    window.projects.autosave_timer.stop()
    assert window.projects.create_project(
        ProjectSettings.from_preset("720p 30", "FolderTest"), str(tmp_path / "projects"))
    pump(qapp, 0.2)
    yield window
    window.close()


def _write_png(path, colour):
    cv2.imwrite(str(path), np.full((60, 90, 3), colour, np.uint8))


def _item(panel, kind, key):
    for index in range(panel.list.count()):
        item = panel.list.item(index)
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and data[0] == kind and data[1] == key:
            return item
    return None


def _drop(panel, mime, target_item):
    panel.list.scrollToItem(target_item)
    pos = panel.list.visualItemRect(target_item).center()
    event = QDropEvent(QPointF(pos), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    panel.list.dropEvent(event)


def test_moving_media_does_not_clear_or_rebuild_its_thumbnail(panel_window, qapp, tmp_path):
    window = panel_window
    panel = window.project_panel
    library = window.projects.project.library
    path = tmp_path / "thumb.png"
    _write_png(path, 80)
    panel.add_files([str(path)])
    item = library.by_path(str(path))
    assert item is not None
    key = norm_path(str(path))
    panel._thumbs[key] = synthetic_frame(16, 16)
    requested = []
    panel._request_thumb = lambda media: requested.append(media.id)
    folder = library.add_bin("Thumbs")
    panel._items_moved([item.id], folder)
    assert library.items[item.id].bin == folder
    assert key in panel._thumbs
    assert requested == []


def test_folders_are_real_drop_targets_and_survive_save_and_reopen(panel_window, qapp, tmp_path,
                                                                  monkeypatch):
    window = panel_window
    panel = window.project_panel
    library = window.projects.project.library

    root = tmp_path / "media"
    root.mkdir()
    first = root / "wide shot.png"
    second = root / "close up.png"
    long_name = root / ("a very long filename that must be elided cleanly in the grid.png")
    for path, colour in ((first, 40), (second, 90), (long_name, 140)):
        _write_png(path, colour)
    assert panel.add_files([str(first), str(second), str(long_name)]) == 3
    pump(qapp, 0.3)

    long_item = _item(panel, "item", library.by_path(str(long_name)).id)
    assert long_item is not None
    assert long_item.text().split("\n")[0] != long_name.name
    assert long_name.name in long_item.toolTip()

    folders = library.add_bin("Rushes")
    child = library.add_bin("Selects", folders)
    panel.rebuild(force=True)
    folder_item = _item(panel, "bin", folders)
    assert folder_item is not None
    assert not folder_item.icon().isNull()
    assert "Folder" in folder_item.toolTip()
    assert "Rushes" in folder_item.text()
    assert "item:hover" in panel.list.styleSheet()
    assert "item:selected" in panel.list.styleSheet()

    # Drag both selected assets onto the folder.  This is the same drop event Qt sends
    # to the list, including the target lookup and the drop highlight.
    first_item = _item(panel, "item", library.by_path(str(first)).id)
    second_item = _item(panel, "item", library.by_path(str(second)).id)
    panel.list.clearSelection()
    first_item.setSelected(True)
    second_item.setSelected(True)
    mime = panel.list.mimeData([first_item, second_item])

    class _DragMove:
        def __init__(self, mime, position):
            self._mime, self._position = mime, QPointF(position)

        def mimeData(self):
            return self._mime

        def position(self):
            return self._position

        def acceptProposedAction(self):
            pass

        def ignore(self):
            pass

    panel.list.dragMoveEvent(_DragMove(mime, folder_item_pos := panel.list.visualItemRect(folder_item).center()))
    assert folder_item.background().color().name() == "#2f5f8f"
    panel.list.dragLeaveEvent(QDragLeaveEvent())
    assert folder_item.background().style() == Qt.BrushStyle.NoBrush
    _drop(panel, mime, folder_item)
    pump(qapp, 0.1)
    assert library.by_path(str(first)).bin == folders
    assert library.by_path(str(second)).bin == folders

    # Double-click opens the folder; Back and the Project Root row both navigate.
    panel.list.clearSelection()
    open_item = _item(panel, "bin", folders)
    panel._double_clicked(open_item)
    assert panel._current_bin == folders
    assert panel.back_button.text() == "Back"
    assert panel.back_button.isVisible()
    child_item = _item(panel, "bin", child)
    assert child_item is not None
    select = _item(panel, "item", library.by_path(str(first)).id)
    panel.list.clearSelection()
    select.setSelected(True)
    _drop(panel, panel.list.mimeData([select]), child_item)
    pump(qapp, 0.1)
    assert library.by_path(str(first)).bin == child
    panel.toggle_view()
    assert "Selects" in _item(panel, "bin", child).text()
    assert "(1)" in _item(panel, "bin", child).text()
    panel.toggle_view()

    # Drop the other item on the root row to move it back out of the folder.
    other = _item(panel, "item", library.by_path(str(second)).id)
    root_item = _item(panel, "root", ROOT_BIN)
    assert root_item is not None
    panel.list.clearSelection()
    other.setSelected(True)
    _drop(panel, panel.list.mimeData([other]), root_item)
    pump(qapp, 0.1)
    assert library.by_path(str(second)).bin == ""

    panel.back_button.click()
    pump(qapp, 0.1)
    assert panel._current_bin == ""
    assert not panel.back_button.isVisible()

    # Rename still works and is virtual only.
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Renamed", True)))
    panel._rename_bin(folders)
    assert library.bins[folders] == "Renamed"

    # A timeline clip keeps its source reference while the library item moves.
    track = window.engine.timeline.add_track("video")
    clip = window.engine.timeline.new_clip("clip", str(first), track, 0, 30,
                                            source_out=30, source_fps=30)
    clip.effects.append({"effect_id": "face_tracker", "enabled": True,
                         "params": {"show_box": False, "show_numbers": True,
                                    "show_labels": True, "viz_preset": "custom"}})
    window.engine.session.set_selection({clip.id})
    assert window.projects.save()
    path = window.projects.project.path
    assert window.projects.close_project()
    assert window.projects.open_path(path)
    pump(qapp, 0.2)
    reopened = window.projects.project.library
    assert reopened.bins[folders] == "Renamed"
    assert reopened.by_path(str(first)).bin == child
    assert reopened.by_path(str(second)).bin == ""
    clips = list(window.engine.timeline.all_clips())
    assert any(c.source_path == str(first) for c in clips)
    loaded_clip = next(c for c in clips if c.source_path == str(first))
    face = next(e for e in loaded_clip.effects if e["effect_id"] == "face_tracker")
    assert face["params"]["show_box"] is False
    assert face["params"]["show_numbers"] is True
    assert face["params"]["show_labels"] is True
    assert os.path.exists(first) and os.path.exists(second)
