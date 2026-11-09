"""The small, non-modal notice that a newer build exists.

It deliberately is not a QMessageBox: a dialog would steal focus from the editor, and the
user may be in the middle of a playback, a recording or an export. The panel sits in the
corner of the window and follows the update from "available" to "downloading" to "ready",
with one primary action at each step. The ordinary window close is never blocked.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


def _size_text(done, total):
    try:
        done = int(done or 0)
        total = int(total or 0)
    except (TypeError, ValueError):
        return ""
    if done <= 0:
        return ""
    if total > 0:
        return f"{done / (1024 * 1024):.1f} of {total / (1024 * 1024):.1f} MB"
    return f"{done / (1024 * 1024):.1f} MB"


class UpdateNotice(QWidget):
    view_requested = pyqtSignal()
    download_requested = pyqtSignal()
    install_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    remind_requested = pyqtSignal()
    skip_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("updateNotice")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlags(Qt.WindowType.Widget)
        self.update_info = None
        self.state = "available"
        self._style()
        self.kicker = QLabel("Update available")
        self.kicker.setObjectName("updateNoticeKicker")
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setObjectName("updateNoticeTitle")
        self.detail = QLabel("Your projects and settings stay exactly where they are.")
        self.detail.setWordWrap(True)
        self.detail.setObjectName("updateNoticeDetail")
        self.primary = QPushButton("View Update")
        self.primary.setObjectName("updateNoticePrimary")
        self.primary.setDefault(True)
        self.primary.clicked.connect(self._primary_clicked)
        self.secondary = QPushButton("Remind Me Later")
        self.secondary.clicked.connect(self._secondary_clicked)
        self.tertiary = QPushButton("Skip This Version")
        self.tertiary.clicked.connect(self._skip)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self.primary)
        buttons.addWidget(self.secondary)
        buttons.addWidget(self.tertiary)
        close = QPushButton("\u00d7")
        close.setObjectName("updateNoticeClose")
        close.setFixedSize(22, 22)
        close.setToolTip("Dismiss until the next check")
        close.clicked.connect(self.hide)
        top = QHBoxLayout()
        top.addWidget(self.kicker)
        top.addStretch(1)
        top.addWidget(close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 12)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)
        self.hide()
        if parent is not None:
            parent.installEventFilter(self)

    def _style(self):
        from .. import tokens
        c = tokens.COLORS
        self.setStyleSheet(
            f"#updateNotice {{ background: {c['panel']}; border: 1px solid {c['accent']};"
            f" border-radius: {tokens.RADIUS_LG}px; }}"
            f"#updateNotice QLabel {{ background: transparent; color: {c['text']}; }}"
            f"#updateNoticeTitle {{ font-size: {tokens.FONT_TITLE}px; font-weight: 600; }}"
            f"#updateNoticeDetail {{ color: {c['muted']}; }}"
            f"#updateNoticeKicker {{ color: {c['accent']}; font-weight: 600;"
            f" font-size: {tokens.FONT_SMALL}px; text-transform: uppercase; }}"
            f"#updateNoticeClose {{ border: none; color: {c['muted']}; }}")

    # ------------------------------------------------------------------ content
    @staticmethod
    def _label(update):
        return "Alpha" if getattr(update.version, "prerelease", False) else "Stable"

    def show_update(self, update):
        """The first state: a newer build exists and can be downloaded."""
        self.update_info = update
        self.state = "available"
        self.kicker.setText("Update available")
        self.title.setText(f"meeniFX Studios {update.version_text} "
                           f"{self._label(update)} is available.")
        self.detail.setText("Download it in the background or view the release notes. "
                            "Nothing is installed without your confirmation.")
        self.primary.setText("View Update")
        self.secondary.setText("Remind Me Later")
        self.tertiary.setText("Skip This Version")
        self.secondary.setVisible(True)
        self.tertiary.setVisible(True)
        self._present()

    def show_downloading(self, update, done=0, total=0):
        """An installed build is fetching the verified installer."""
        if update is None:
            return
        self.update_info = update
        self.state = "downloading"
        self.kicker.setText("Downloading update")
        self.title.setText(f"meeniFX Studios {update.version_text} is downloading.")
        self.set_download_progress(done, total)
        self.primary.setText("View Update")
        self.secondary.setText("Cancel Download")
        self.secondary.setVisible(True)
        self.tertiary.setVisible(False)
        self._present()

    def set_download_progress(self, done, total):
        """Update the progress line without moving or re-raising the notice."""
        size = _size_text(done, total)
        self.detail.setText((f"{size} downloaded. " if size else "Starting the download. ")
                            + "You can keep editing; the installer runs only when you ask.")

    def show_ready(self, update):
        """The installer is downloaded and checksum-verified."""
        if update is None:
            return
        self.update_info = update
        self.state = "ready"
        self.kicker.setText("Update ready")
        self.title.setText(f"meeniFX Studios {update.version_text} is ready to install.")
        self.detail.setText("Restart meeniFX Studios to finish. Your projects and settings "
                            "stay exactly where they are.")
        self.primary.setText("Restart and Install")
        self.secondary.setText("Later")
        self.secondary.setVisible(True)
        self.tertiary.setVisible(False)
        self._present()

    def show_error(self, update, message):
        """The automatic download failed; offer the release page to try again."""
        self.update_info = update
        self.state = "error"
        self.kicker.setText("Update download failed")
        self.title.setText("The update could not be downloaded.")
        self.detail.setText(f"{message} You can try again from the Updates page.")
        self.primary.setText("View Update")
        self.secondary.setText("Later")
        self.secondary.setVisible(True)
        self.tertiary.setVisible(False)
        self._present()

    def _present(self):
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    # ------------------------------------------------------------------ actions
    def _primary_clicked(self):
        if self.state == "ready":
            self.install_requested.emit()
        else:
            self.view_requested.emit()

    def _secondary_clicked(self):
        if self.state == "downloading":
            self.cancel_requested.emit()
            self.show_update(self.update_info)
        elif self.state == "available":
            self._remind()
        else:
            self.hide()

    def _remind(self):
        self.hide()
        self.remind_requested.emit()

    def _skip(self):
        self.hide()
        self.skip_requested.emit()

    # ------------------------------------------------------------------ placement
    def reposition(self):
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(390, max(280, parent.width() - 36))
        self.setFixedWidth(width)
        self.adjustSize()
        margin = 18
        self.move(max(margin, parent.width() - self.width() - margin), margin + 38)

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.reposition()
        return super().eventFilter(watched, event)
