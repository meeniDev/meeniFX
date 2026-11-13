"""Cleaning the recent list: missing entries, temp-folder projects and the no-recent switch.

The recent list is a plain JSON file, so these tests redirect ``storage.RECENTS_FILE`` at
the module attribute and give the temp-folder checks a marker folder of their own: pytest's
own ``tmp_path`` lives inside the real temp folder, which would make every test project
look like a leftover.
"""

import os
import sys

import pytest

from meenifx.project import storage

def _send2trash_installed():
    try:
        import send2trash  # noqa: F401
        return True
    except ImportError:
        return False


needs_trash = pytest.mark.skipif(
    sys.platform != "win32" and not _send2trash_installed(),
    reason="no Recycle Bin on this system")


@pytest.fixture()
def recents(tmp_path, monkeypatch):
    """A redirected recent list plus a marker "temp" folder that is not pytest's temp."""
    marker = tmp_path / "temp"
    work = tmp_path / "work"
    marker.mkdir()
    work.mkdir()
    # pytest's tmp_path is inside the real temp folder; the marker keeps these tests
    # independent of where it happens to be.
    monkeypatch.setattr(storage, "temp_root", lambda: str(marker))
    monkeypatch.setattr(storage, "RECENTS_FILE", str(tmp_path / "recents.json"))
    # conftest sets MEENIFX_NO_RECENT for the whole suite; the list tests need it off.
    monkeypatch.delenv("MEENIFX_NO_RECENT", raising=False)
    storage.enable_recents()
    yield {"marker": marker, "work": work, "file": str(tmp_path / "recents.json"),
           "tmp": tmp_path}
    storage.enable_recents()


def project_file(folder, name="Project"):
    """A stand-in project file: the list only cares whether the path exists."""
    folder = str(folder)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".mfxpf")
    with open(path, "wb") as handle:
        handle.write(b"x")
    return path


# ------------------------------------------------------------------ pruning
def test_prune_missing_drops_only_the_entries_whose_file_is_gone(recents):
    keep = project_file(recents["work"], "Keep")
    gone = project_file(recents["work"], "Gone")
    other_gone = project_file(recents["marker"], "Gone too")
    storage._write_json(recents["file"], [keep, gone, other_gone])
    os.remove(gone)
    os.remove(other_gone)

    assert storage.missing_recents(recents["file"]) == [gone, other_gone]
    assert storage.prune_missing(recents["file"]) == 2
    assert storage.load_recents(recents["file"]) == [keep]
    assert storage.prune_missing(recents["file"]) == 0, "a second run has nothing to do"


# --------------------------------------------------- temp-folder detection
def test_a_project_in_a_temp_subfolder_is_a_temp_project(recents):
    nested = recents["marker"] / "pytest-123" / "My project"
    path = project_file(nested)
    assert storage.is_in_temp(path) is True
    assert storage.temp_root() == str(recents["marker"])


def test_a_sibling_that_only_shares_the_prefix_is_not_in_temp(recents):
    lookalike = recents["tmp"] / "TempX" / "My project"
    path = project_file(lookalike)
    assert storage.is_in_temp(path) is False, "TempX is not inside Temp"


def test_temp_detection_is_case_insensitive_on_windows(recents, monkeypatch):
    path = project_file(recents["marker"] / "Sub" / "p")
    loudly = str(recents["marker"]).upper()
    monkeypatch.setattr(storage, "temp_root", lambda: loudly)
    assert storage.is_in_temp(path) is (sys.platform == "win32")


def test_the_temp_folder_itself_is_never_treated_as_a_temp_project(recents):
    assert storage.is_in_temp(str(recents["marker"])) is False
    assert storage.project_own_folder(str(recents["marker"] / "loose.mfxpf")) == ""


def test_a_project_outside_temp_is_not_a_temp_project(recents):
    path = project_file(recents["work"])
    assert storage.is_in_temp(path) is False
    assert storage.project_trash_items(path) == []


def test_remove_temp_recents_keeps_the_real_ones(recents):
    real = project_file(recents["work"], "Real")
    throwaway = project_file(recents["marker"] / "Run 1", "Throwaway")
    missing_temp = os.path.join(str(recents["marker"]), "Run 2", "Gone.mfxpf")
    storage._write_json(recents["file"], [real, throwaway, missing_temp])

    assert storage.remove_temp_recents(recents["file"]) == 2
    assert storage.load_recents(recents["file"]) == [real]


# ------------------------------------------------------------------ trashing
def test_only_a_clearly_own_folder_may_be_trashed(recents):
    own = recents["marker"] / "Own project"
    path = project_file(own)
    open(path + ".bak", "wb").close()
    os.makedirs(own / "Cache" / "preview")
    assert storage.project_own_folder(path) == str(own)
    assert storage.project_trash_items(path) == [str(own)]

    shared = recents["marker"] / "Shared"
    shared_path = project_file(shared, "Mine")
    (shared / "notes.txt").write_text("someone else's file")
    assert storage.project_own_folder(shared_path) == ""
    assert storage.project_trash_items(shared_path) == [shared_path]


@needs_trash
def test_trash_project_moves_the_projects_own_folder_to_the_bin(recents):
    folder = recents["marker"] / "Throwaway 42"
    path = project_file(folder)
    moved, messages = storage.trash_project(path)
    assert (moved, messages) == (1, [])
    assert not os.path.exists(str(folder)), "the project's own folder is gone"


@needs_trash
def test_trash_project_leaves_a_shared_folder_alone(recents):
    folder = recents["marker"] / "Shared"
    path = project_file(folder, "Mine")
    (folder / "keep.txt").write_text("not ours")
    moved, _messages = storage.trash_project(path)
    assert moved == 1
    assert os.path.isdir(str(folder)), "the folder stays"
    assert (folder / "keep.txt").exists()
    assert not os.path.exists(path), "only the project file went"


# --------------------------------------------------------------- no-recent
def test_the_environment_variable_keeps_a_run_out_of_the_list(recents, monkeypatch):
    path = project_file(recents["work"])
    monkeypatch.setenv("MEENIFX_NO_RECENT", "1")
    storage.add_recent(path, recents["file"])
    assert not os.path.exists(recents["file"]), "nothing was registered"


def test_disable_recents_blocks_the_list_and_the_session_marker(recents, monkeypatch):
    path = project_file(recents["work"])
    monkeypatch.setattr(storage, "SESSION_FILE", recents["file"])
    storage.disable_recents()
    try:
        storage.add_recent(path, recents["file"])
        # Exercise the application's default marker, not the explicit test-file override.
        storage.mark_session_open(path)
        assert not os.path.exists(recents["file"])
        assert storage.crashed_session_info(recents["file"]) is None
    finally:
        storage.enable_recents()


def test_enable_recents_restores_the_list(recents):
    path = project_file(recents["work"])
    storage.disable_recents()
    storage.add_recent(path, recents["file"])
    storage.enable_recents()
    storage.add_recent(path, recents["file"])
    assert storage.load_recents(recents["file"]) == [path]


def test_the_no_recent_flag_is_taken_out_of_the_arguments():
    from meenifx.app import parse_no_recent

    assert parse_no_recent(["--no-recent", "C:/a.mfxpf"]) == (["C:/a.mfxpf"], True)
    assert parse_no_recent(["C:/a.mfxpf"]) == (["C:/a.mfxpf"], False)
    assert parse_no_recent([]) == ([], False)


def test_startup_prune_is_silent_and_refreshes_the_window(recents):
    from meenifx.app import AppController

    keep = project_file(recents["work"], "Keep")
    storage._write_json(recents["file"], [keep, os.path.join(str(recents["work"]), "Gone.mfxpf")])
    reloads = []

    class FakeStart:
        def reload(self):
            reloads.append(True)

    controller = AppController.__new__(AppController)      # no window, just the action
    controller.start = FakeStart()
    controller._prune_missing_recents()

    assert storage.load_recents(recents["file"]) == [keep]
    assert reloads == [True]


def test_the_startup_option_defaults_to_on_and_round_trips(monkeypatch):
    from meenifx.ui import start_window as SW

    class FakeSettings:
        def __init__(self):
            self.values = {}

        def value(self, key, default=None):
            return self.values.get(key, default)

        def setValue(self, key, value):
            self.values[key] = value

    fake = FakeSettings()
    monkeypatch.setattr(SW, "app_settings", lambda: fake)
    assert SW.prune_missing_on_startup() is True
    SW.set_prune_missing_on_startup(False)
    assert SW.prune_missing_on_startup() is False
    SW.set_prune_missing_on_startup(True)
    assert SW.prune_missing_on_startup() is True


# -------------------------------------------------------------- start window
@pytest.fixture()
def window(recents, qapp):
    from meenifx.ui.start_window import StartWindow

    start = StartWindow()
    start.resize(900, 560)
    qapp.processEvents()
    yield start
    start.close()
    start.deleteLater()
    qapp.processEvents()


def test_the_clean_up_menu_counts_what_it_can_remove(window, recents):
    window._infos = []
    window._fill_clean_menu()
    assert [(a.text(), a.isEnabled()) for a in window.clean_menu.actions()] == [
        ("Remove missing projects", False), ("Remove temp-folder projects", False)]

    real = project_file(recents["work"], "Real")
    throwaway = project_file(recents["marker"] / "Run 3", "Throwaway")
    storage._write_json(recents["file"], [real, throwaway])
    window.reload()
    window._fill_clean_menu()
    assert [(a.text(), a.isEnabled()) for a in window.clean_menu.actions()] == [
        ("Remove missing projects", False), ("Remove 1 temp-folder project", True)]

    storage._write_json(recents["file"], [real, throwaway, os.path.join(str(recents["work"]), "Gone.mfxpf")])
    window.reload()
    window._fill_clean_menu()
    assert [a.text() for a in window.clean_menu.actions()] == [
        "Remove 1 missing project", "Remove 1 temp-folder project"]


def test_cleaning_missing_projects_from_the_menu_reports_the_count(window, recents):
    keep = project_file(recents["work"], "Keep")
    storage._write_json(recents["file"], [keep] + [
        os.path.join(str(recents["work"]), f"Gone{i}.mfxpf") for i in range(2)])
    window.reload()
    assert window.grid.count() == 3

    window._clean_missing()
    assert storage.load_recents(recents["file"]) == [keep]
    assert window.grid.count() == 1
    assert window.toast.message() == "Removed 2"


def test_cleaning_temp_projects_only_edits_the_list(window, recents, monkeypatch):
    from meenifx.ui import start_window as SW

    keep = project_file(recents["work"], "Keep")
    throwaway = project_file(recents["marker"] / "Run 4", "Throwaway")
    storage._write_json(recents["file"], [keep, throwaway])
    window.reload()
    monkeypatch.setattr(SW.TempCleanupDialog, "exec", lambda self: 1)
    monkeypatch.setattr(SW.TempCleanupDialog, "trash_folders", lambda self: False)

    window._clean_temp()
    assert storage.load_recents(recents["file"]) == [keep]
    assert os.path.isfile(throwaway), "without the option the files stay"
    assert window.toast.message() == "Removed 1"


@needs_trash
def test_cleaning_temp_projects_can_move_them_to_the_bin(window, recents, monkeypatch):
    from meenifx.ui import start_window as SW

    folder = recents["marker"] / "Run 5"
    throwaway = project_file(folder, "Throwaway")
    storage._write_json(recents["file"], [throwaway])
    window.reload()
    monkeypatch.setattr(SW.TempCleanupDialog, "exec", lambda self: 1)
    monkeypatch.setattr(SW.TempCleanupDialog, "trash_folders", lambda self: True)

    window._clean_temp()
    assert storage.load_recents(recents["file"]) == []
    assert not os.path.exists(str(folder))
    assert "Recycle Bin" in window.toast.message()


def test_the_temp_badge_is_drawn_for_temp_projects_only(window, recents, monkeypatch):
    from meenifx.ui import start_window as SW

    real = project_file(recents["work"], "Real")
    throwaway = project_file(recents["marker"] / "Run 6", "Throwaway")
    storage._write_json(recents["file"], [real, throwaway])
    window.reload()

    painted = []
    original = SW._paint_temp_badge
    monkeypatch.setattr(SW, "_paint_temp_badge", lambda painter: painted.append(True) or original(painter))
    window.refresh_view()
    assert len(painted) == 1, "one badge, for the project inside the temp folder"
