"""The update UI: the non-modal notice, the Updates page and What's New."""

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from meenifx.ui.update_notice import UpdateNotice
from meenifx.ui.updates_dialog import (UpdatesDialog, friendly_date, friendly_size,
                                      no_update_message)
from meenifx.ui.whats_new import WhatsNewDialog
from meenifx.updates import model
from meenifx.updates.service import CheckResult, UpdateError
from meenifx.updates.settings import UpdateSettings
from meenifx.version import VERSION


def build_update(version="0.1.0-alpha.2", *, prerelease=True, notes="### Added\n- A calmer notice"):
    tag = f"v{version}"
    assets = [{"name": model.installer_name(version),
               "browser_download_url": f"https://github.com/meeniDev/meeniFX/releases/download/{tag}/{model.installer_name(version)}",
               "size": 12 * 1024 * 1024},
              {"name": model.CHECKSUMS_NAME,
               "browser_download_url": f"https://github.com/meeniDev/meeniFX/releases/download/{tag}/SHA256SUMS.txt",
               "size": 200}]
    release = model.parse_release({"tag_name": tag, "draft": False, "prerelease": prerelease,
                                   "body": notes, "published_at": "2026-11-08T10:00:00Z",
                                   "html_url": f"https://github.com/meeniDev/meeniFX/releases/tag/{tag}",
                                   "assets": assets})
    return model.release_update(release)


class StubManager(QObject):
    check_started = pyqtSignal()
    check_finished = pyqtSignal(object)
    download_started = pyqtSignal()
    download_progress = pyqtSignal(int, int)
    download_ready = pyqtSignal(str, object)
    download_failed = pyqtSignal(str, str)
    update_available = pyqtSignal(object)

    def __init__(self, update=None):
        super().__init__()
        self.settings = UpdateSettings()
        self.settings.last_check = "2026-11-08T10:00:00+00:00"
        self.pending_update = update
        self.last_result = None
        self.ready_path = None
        self.ready_update = None
        self.downloading = False
        self.calls = []

    def check(self, manual=False):
        self.calls.append(("check", manual))
        return True

    def download(self, update=None):
        self.calls.append(("download", update.version_text))
        return True

    def cancel_download(self):
        self.calls.append(("cancel", None))

    def remind_later(self):
        self.calls.append(("remind", None))
        self.settings.remind_later()

    def skip(self):
        self.calls.append(("skip", None))
        self.settings.skip(self.pending_update.version_text)


def test_the_updates_page_names_the_current_build_and_channel(qapp):
    dialog = UpdatesDialog(StubManager(), None)
    try:
        labels = [label.text() for label in dialog.findChildren(type(dialog.status))]
        assert any(VERSION in text for text in labels)
        assert dialog.channel_label.text() == "Alpha"
        assert dialog.windowTitle() == "Updates"
        assert not dialog.available_box.isHidden()
    finally:
        dialog.close()


def test_an_available_update_fills_in_the_version_date_size_and_notes(qapp):
    update = build_update()
    dialog = UpdatesDialog(StubManager(update), None)
    try:
        assert "0.1.0-alpha.2" in dialog.available_version.text()
        assert "08 November 2026" in dialog.available_date.text()
        assert dialog.available_size.text() == "12.0 MB"
        assert "calmer notice" in dialog.notes.toPlainText()
        assert dialog.download_button.isEnabled()
        assert dialog.status.text().startswith("meeniFX Studios 0.1.0-alpha.2")
    finally:
        dialog.close()


def test_checking_and_downloading_go_through_the_manager(qapp):
    manager = StubManager(build_update())
    dialog = UpdatesDialog(manager, None)
    try:
        dialog.check_now()
        assert ("check", True) in manager.calls
        dialog.start_download()
        assert ("download", "0.1.0-alpha.2") in manager.calls
        dialog._on_download_started()
        dialog._on_progress(50, 100)
        assert dialog.progress.value() == 50
        assert not dialog.check_button.isEnabled()
        dialog._on_download_failed("checksum", "The checksum did not match.")
        assert "checksum did not match" in dialog.status.text()
        assert dialog.check_button.isEnabled()
    finally:
        dialog.close()


def test_a_verified_download_turns_the_button_into_restart_and_install(qapp, monkeypatch,
                                                                        tmp_path):
    update = build_update()
    manager = StubManager(update)
    path = tmp_path / "setup.exe"
    path.write_bytes(b"installer")
    manager.ready_path = str(path)
    manager.ready_update = update
    calls = []
    monkeypatch.setattr("meenifx.ui.updates_dialog.install_update",
                        lambda parent, installer_path, info: calls.append(
                            (installer_path, info.version_text)))
    dialog = UpdatesDialog(manager, None)
    try:
        assert dialog.download_button.text() == "Restart and Install"
        assert "verified" in dialog.status.text().lower()
        dialog.start_download()
        assert calls == [(str(path), update.version_text)]
    finally:
        dialog.close()


def test_a_failed_check_explains_itself_without_closing_the_editor(qapp):
    manager = StubManager()
    dialog = UpdatesDialog(manager, None)
    result = CheckResult(None, None, False, UpdateError("offline", "Could not reach GitHub."))
    try:
        dialog._on_check_finished(result)
        assert "Could not reach GitHub." in dialog.status.text()
    finally:
        dialog.close()


def test_the_notice_moves_from_available_to_downloading_to_ready(qapp):
    notice = UpdateNotice(None)
    seen = []
    notice.view_requested.connect(lambda: seen.append("view"))
    notice.install_requested.connect(lambda: seen.append("install"))
    notice.cancel_requested.connect(lambda: seen.append("cancel"))
    notice.remind_requested.connect(lambda: seen.append("remind"))
    notice.skip_requested.connect(lambda: seen.append("skip"))
    notice.show_update(build_update())
    try:
        assert "0.1.0-alpha.2" in notice.title.text()
        assert "Alpha" in notice.title.text()
        assert notice.primary.text() == "View Update"
        assert notice.secondary.text() == "Remind Me Later"
        notice.show_downloading(build_update(), 12 * 1024 * 1024 // 2, 12 * 1024 * 1024)
        assert "6.0 of 12.0 MB" in notice.detail.text()
        assert notice.secondary.text() == "Cancel Download"
        notice.show_ready(build_update())
        assert "ready to install" in notice.title.text()
        assert notice.primary.text() == "Restart and Install"
        notice.primary.click()
        assert seen == ["install"]
    finally:
        notice.close()


def test_whats_new_groups_the_notes_and_acknowledges_the_version(qapp):
    acknowledged = []
    dialog = WhatsNewDialog("0.1.0-alpha.2",
                            "### Added\n- A calmer notice\n\n### Fixed\n- A noisy toast",
                            None, date_text="8 November 2026",
                            acknowledge=acknowledged.append)
    try:
        labels = [label.text() for label in dialog.findChildren(
            __import__("PyQt6.QtWidgets", fromlist=["QLabel"]).QLabel)]
        assert "Added" in labels and "Fixed" in labels
        assert any("calmer notice" in text for text in labels)
        dialog.accept()
        assert acknowledged == ["0.1.0-alpha.2"]
    finally:
        dialog.close()


class DictStore(dict):
    def value(self, key, default=None):
        return dict.get(self, key, default)

    def setValue(self, key, value):
        self[key] = value


class FakeService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def check(self, current=..., channel="alpha", force=False):
        self.calls.append((current, channel, force))
        return self.result


def test_the_manager_saves_the_state_and_respects_skip_unless_manual(qapp):
    from meenifx.updates.manager import UpdateManager

    update = build_update()
    result = CheckResult(update, None, False)
    store = DictStore()
    settings = UpdateSettings()
    settings.skip(update.version_text)
    manager = UpdateManager(store=store, service=FakeService(result), settings=settings)
    seen = []
    manager.update_available.connect(seen.append)

    manager._on_checked(result, manual=False)
    assert seen == [], "a skipped version must not notify automatically"
    assert store.value("updates/lastCheck")
    assert manager.pending_update is update

    manager._on_checked(result, manual=True)
    assert seen == [update], "a manual check must override the skip"
    assert manager.last_result is result


def test_an_installed_build_downloads_an_available_update_automatically(qapp, monkeypatch, tmp_path):
    from meenifx.updates import installer as installer_module
    from meenifx.updates.manager import UpdateManager

    update = build_update()
    result = CheckResult(update, None, False)
    manager = UpdateManager(store=DictStore(), service=FakeService(result),
                            settings=UpdateSettings(auto_download=True))
    monkeypatch.setattr(installer_module, "can_auto_install", lambda: True)
    downloads = []
    manager.download = lambda update=None, opener=None: downloads.append(
        update.version_text) or True
    manager._on_checked(result, manual=False)
    assert downloads == [update.version_text]


def test_auto_download_can_be_disabled_or_respects_a_skipped_version(qapp, monkeypatch):
    from meenifx.updates import installer as installer_module
    from meenifx.updates.manager import UpdateManager

    update = build_update()
    result = CheckResult(update, None, False)
    monkeypatch.setattr(installer_module, "can_auto_install", lambda: True)
    for settings, manual in ((UpdateSettings(auto_download=False), False),
                             (UpdateSettings(auto_download=True), False)):
        if settings.auto_download:
            settings.skip(update.version_text)
        manager = UpdateManager(store=DictStore(), service=FakeService(result), settings=settings)
        downloads = []
        manager.download = lambda update=None, opener=None: downloads.append(update) or True
        manager._on_checked(result, manual=manual)
        assert downloads == []


def test_a_completed_download_is_remembered_as_ready(qapp, tmp_path):
    from meenifx.updates.manager import UpdateManager

    update = build_update()
    path = tmp_path / "setup.exe"
    path.write_bytes(b"installer")
    manager = UpdateManager(store=DictStore(), service=FakeService(CheckResult(update)),
                            settings=UpdateSettings())
    ready = []
    manager.download_ready.connect(lambda path, info: ready.append((path, info.version_text)))
    manager._on_downloaded(str(path), update)
    assert manager.ready_update is update
    assert manager.ready_path == str(path)
    assert ready == [(str(path), update.version_text)]
    assert manager.download(update) is True
    assert ready[-1] == (str(path), update.version_text)


def test_the_manager_refuses_a_release_without_checksums(qapp):
    from meenifx.updates.manager import UpdateManager

    update = build_update()
    update = type(update)(release=update.release, installer=update.installer, portable=None,
                          checksums=None)
    manager = UpdateManager(store=DictStore(), service=FakeService(CheckResult(update)),
                            settings=UpdateSettings())
    failures = []
    manager.download_failed.connect(lambda kind, message: failures.append((kind, message)))
    assert manager.download(update) is False
    assert failures and failures[0][0] == "checksum"


def test_the_main_window_exposes_the_updates_page_and_help_entries(qapp):
    from PyQt6.QtWidgets import QMenu

    from meenifx.ui.main_window import MainWindow

    window = MainWindow(qapp)
    try:
        assert window.update_manager is not None
        assert window.update_notice is not None
        assert callable(window.show_updates) and callable(window.show_whats_new)
        help_menu = next(menu for menu in window.menuBar().findChildren(QMenu)
                         if menu.title() == "Help")
        labels = [action.text() for action in help_menu.actions()]
        assert "Check for Updates..." in labels
        assert "What's New..." in labels
        assert "About meeniFX Studios" in labels
        window._on_update_available(build_update())
        assert "0.1.0-alpha.2" in window.update_notice.title.text()
        window._on_update_ready("setup.exe", build_update())
        assert window.update_notice.state == "ready"
        assert window.update_notice.primary.text() == "Restart and Install"
        window.update_notice.hide()
    finally:
        window.close()
        window.deleteLater()


def test_preferences_carries_the_updates_section(qapp):
    from meenifx.ui.start_window import PreferencesDialog

    dialog = PreferencesDialog(None)
    try:
        assert dialog.update_auto.text() == "Check for updates automatically"
        assert dialog.update_auto_download.text() == "Download updates automatically"
        assert dialog.update_channel.currentData() in ("alpha", "stable")
        assert dialog.update_channel.count() == 2
        assert dialog.update_skipped.text()
        assert dialog.update_last_check.text()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_no_update_message_explains_an_alpha_build_on_stable():
    text, warning = no_update_message("stable", "0.1.0-alpha.3")
    assert warning and "Alpha" in text and "Stable" in text
    assert "Preferences > Updates" in text
    text, warning = no_update_message("alpha", "0.1.0-alpha.3")
    assert not warning and "latest build" in text
    text, warning = no_update_message("stable", "0.1.0")
    assert not warning and "latest build" in text


def test_dates_and_sizes_are_readable():
    assert friendly_date("2026-11-08T10:00:00Z") == "08 November 2026"
    assert friendly_date("") == "unknown"
    assert friendly_size(12 * 1024 * 1024) == "12.0 MB"
    assert friendly_size(0) == "unknown"
