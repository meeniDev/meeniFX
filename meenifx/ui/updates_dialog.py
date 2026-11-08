"""The Updates page: one place for the version, the channel and a safe update."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                             QVBoxLayout)

from .. import tokens
from ..tokens import COLORS
from ..updates import installer
from ..updates.model import RELEASES_PAGE
from ..updates.whats_new import notes_text
from ..version import VERSION, channel_label, display_version

CHANNEL_NOTE = ("Alpha follows prereleases as well as stable builds. Stable is reserved "
                "for later and only offers finished releases.")


def friendly_date(iso_text):
    try:
        parsed = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d %B %Y")
    except (TypeError, ValueError):
        return str(iso_text or "unknown")


def friendly_size(size):
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "unknown"
    return f"{size / (1024 * 1024):.1f} MB"


def install_update(window, path, update):
    """Hand a verified installer to Windows after a safe close; never force an exit.

    Returns True when the installer was started. Portable and source builds are told to
    use the release page instead: only an installed copy can be upgraded automatically.
    """
    try:
        installer.validate_installer(path)
    except installer.InstallerError as exc:
        QMessageBox.warning(window, "Update", str(exc))
        return False
    if installer.installation_kind() != "installed":
        QMessageBox.information(
            window, "Portable build",
            "Automatic installation is for the installed version of meeniFX Studios.\n\n"
            "This copy is portable or running from source. Download the new portable ZIP "
            "from the release page and replace this folder when you are ready - your "
            "projects and preferences are not stored inside it.")
        return False
    if getattr(window, "_export_dialog", None) is not None:
        window.toast_message("Finish the export before installing the update", kind="error")
        return False
    recorder = getattr(window, "recorder", None)
    if recorder is not None and getattr(recorder, "state", "idle") != "idle":
        window.toast_message("Stop the recording before installing the update", kind="error")
        return False
    if not window.projects.maybe_save():
        return False
    if window.isVisible():
        window.close()
    if window.isVisible():
        return False
    try:
        subprocess.Popen([str(path), *installer.installer_arguments()],
                         cwd=os.path.dirname(os.path.abspath(str(path))), close_fds=True)
    except OSError as exc:
        QMessageBox.warning(window, "Update", f"The installer could not be started: {exc}")
        return False
    from PyQt6.QtWidgets import QApplication
    QApplication.instance().quit()
    return True


class UpdatesDialog(QDialog):
    """Version, channel, check status and the install actions, in one calm page."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._update = manager.pending_update
        self._downloading = False
        self.setWindowTitle("Updates")
        self.setMinimumWidth(600)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['panel']}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(tokens.SP3)

        title = QLabel("Updates")
        title.setStyleSheet(f"font-size:{tokens.FONT_TITLE + 7}px;font-weight:600;")
        layout.addWidget(title)

        intro = QLabel("meeniFX Studios checks GitHub Releases for a newer Windows build. "
                       "Nothing is installed without your say-so.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{COLORS['muted']};")
        layout.addWidget(intro)

        status_group = QGroupBox("This installation")
        status_form = QFormLayout(status_group)
        status_form.addRow("Version", QLabel(display_version(VERSION)))
        self.channel_label = QLabel(channel_label(getattr(manager.settings, "channel", "alpha")))
        status_form.addRow("Channel", self.channel_label)
        self.installed_kind = QLabel({
            "installed": "Installed - updates can be installed automatically",
            "portable": "Portable - download the new ZIP manually",
            "source": "Running from source - updates are for packaged builds",
        }.get(installer.installation_kind(), "Unknown"))
        status_form.addRow("Build", self.installed_kind)
        self.last_check = QLabel("Never")
        status_form.addRow("Last check", self.last_check)
        layout.addWidget(status_group)

        self.available_box = QGroupBox("Available update")
        available_form = QFormLayout(self.available_box)
        self.available_version = QLabel("No newer build found yet")
        self.available_date = QLabel("-")
        self.available_size = QLabel("-")
        available_form.addRow("Version", self.available_version)
        available_form.addRow("Released", self.available_date)
        available_form.addRow("Download size", self.available_size)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(150)
        self.notes.setPlaceholderText("Release notes appear here when an update is available.")
        available_form.addRow("Release notes", self.notes)
        layout.addWidget(self.available_box)

        self.status = QLabel("Not checked yet.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{COLORS['muted']};")
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        top_row = QHBoxLayout()
        self.check_button = QPushButton("Check for Updates")
        self.check_button.clicked.connect(self.check_now)
        self.download_button = QPushButton("Download and Install")
        self.download_button.setDefault(True)
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button = QPushButton("Cancel Download")
        self.cancel_button.clicked.connect(self.cancel_download)
        self.cancel_button.setVisible(False)
        top_row.addWidget(self.check_button)
        top_row.addWidget(self.download_button)
        top_row.addWidget(self.cancel_button)
        top_row.addStretch(1)
        self.github_button = QPushButton("View on GitHub")
        self.github_button.clicked.connect(self.open_github)
        top_row.addWidget(self.github_button)

        action_row = QHBoxLayout()
        self.remind_button = QPushButton("Remind Me Later")
        self.remind_button.clicked.connect(self.remind_later)
        self.skip_button = QPushButton("Skip This Version")
        self.skip_button.clicked.connect(self.skip_version)
        action_row.addWidget(self.remind_button)
        action_row.addWidget(self.skip_button)
        action_row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setMinimumWidth(96)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        action_row.addWidget(buttons)
        layout.addLayout(action_row)

        hint = QLabel("Downloads go to a temporary folder and are only run after the "
                      "release's SHA-256 checksum matches. Checksums are not a publisher "
                      "signature; signing will be added when infrastructure is available.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{COLORS['muted']};font-size:{tokens.FONT_SMALL}px;")
        layout.addWidget(hint)

        manager.check_started.connect(self._on_check_started)
        manager.check_finished.connect(self._on_check_finished)
        manager.download_started.connect(self._on_download_started)
        manager.download_progress.connect(self._on_progress)
        manager.download_ready.connect(self._on_download_ready)
        manager.download_failed.connect(self._on_download_failed)
        self.refresh()

    # ------------------------------------------------------------------ state
    def refresh(self):
        settings = self.manager.settings
        self.channel_label.setText(channel_label(getattr(settings, "channel", "alpha")))
        last = getattr(settings, "last_check", "")
        self.last_check.setText(friendly_date(last) if last else "Never")
        update = self._update or self.manager.pending_update
        if update is not None:
            self._show_update(update)
        elif self.manager.last_result is not None:
            self._report(self.manager.last_result)
        self._sync_buttons()

    def _show_update(self, update):
        self._update = update
        self.available_box.setVisible(True)
        label = "Alpha" if getattr(update.version, "prerelease", False) else "Stable"
        self.available_version.setText(f"{update.version_text} ({label})")
        self.available_date.setText(friendly_date(update.published_at))
        self.available_size.setText(friendly_size(update.installer_size))
        self.notes.setPlainText(notes_text(update.version_text, update.notes))
        self.status.setText(f"meeniFX Studios {update.version_text} is ready to download.")
        self.status.setStyleSheet(f"color:{COLORS['text']};")

    def _report(self, result):
        if not result.ok:
            self.status.setText(f"{result.error.message} You can keep editing - updates "
                                "are optional.")
            self.status.setStyleSheet(f"color:{COLORS['warning']};")
            return
        if result.update is None:
            self.status.setText("You're on the latest build for this channel.")
            self.status.setStyleSheet(f"color:{COLORS['muted']};")
            return
        self._show_update(result.update)

    def _sync_buttons(self):
        busy = self._downloading
        self.check_button.setEnabled(not busy)
        self.download_button.setEnabled(self._update is not None and not busy
                                        and self._update.checksums is not None)
        if self._update is not None and self._update.checksums is None:
            self.download_button.setToolTip(
                "This release does not publish SHA256SUMS.txt, so it cannot be verified.")
        else:
            self.download_button.setToolTip("")
        self.cancel_button.setVisible(busy)
        self.remind_button.setEnabled(self._update is not None and not busy)
        self.skip_button.setEnabled(self._update is not None and not busy)
        self.github_button.setEnabled(True)

    # ------------------------------------------------------------------ actions
    def check_now(self):
        if self.manager.check(manual=True):
            self.status.setText("Checking GitHub Releases\u2026")
            self.status.setStyleSheet(f"color:{COLORS['muted']};")

    def start_download(self):
        update = self._update or self.manager.pending_update
        if update is None:
            return
        if not self.manager.download(update):
            self.status.setText("The download could not be started.")
            self.status.setStyleSheet(f"color:{COLORS['warning']};")

    def cancel_download(self):
        self.manager.cancel_download()
        self.status.setText("Cancelling the download\u2026")

    def remind_later(self):
        self.manager.remind_later()
        self.status.setText("We'll remind you about this update in 24 hours.")
        self.status.setStyleSheet(f"color:{COLORS['muted']};")
        self.accept()

    def skip_version(self):
        self.manager.skip()
        self.status.setText("This version will be skipped. Newer versions will still be "
                            "offered, and you can clear skipped versions in Preferences.")
        self.accept()

    def open_github(self):
        url = self._update.html_url if self._update is not None else RELEASES_PAGE
        QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------ signals
    def _on_check_started(self):
        self.check_button.setEnabled(False)
        self.status.setText("Checking GitHub Releases\u2026")
        self.status.setStyleSheet(f"color:{COLORS['muted']};")

    def _on_check_finished(self, result):
        self.check_button.setEnabled(True)
        self._report(result)
        self._sync_buttons()

    def _on_download_started(self):
        self._downloading = True
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("Downloading the installer\u2026")
        self.status.setStyleSheet(f"color:{COLORS['muted']};")
        self._sync_buttons()

    def _on_progress(self, done, total):
        if total <= 0:
            self.progress.setRange(0, 0)
            self.status.setText(f"Downloading the installer\u2026 {friendly_size(done)}")
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(min(100, int(done * 100 / total)))
        self.status.setText(f"Downloading the installer\u2026 {friendly_size(done)} of "
                            f"{friendly_size(total)}")

    def _on_download_ready(self, path, update):
        self._downloading = False
        self.progress.setVisible(False)
        self._sync_buttons()
        self.status.setText("Download verified. Preparing to install\u2026")
        window = self.parent()
        if window is None:
            return
        install_update(window, path, update)

    def _on_download_failed(self, kind, message):
        self._downloading = False
        self.progress.setVisible(False)
        self._sync_buttons()
        if kind == "cancelled":
            self.status.setText("Download cancelled. Nothing was installed.")
        else:
            self.status.setText(f"{message} You can try again or download from GitHub.")
        self.status.setStyleSheet(f"color:{COLORS['warning']};")

    def closeEvent(self, event):
        if self._downloading:
            self.manager.cancel_download()
        super().closeEvent(event)
