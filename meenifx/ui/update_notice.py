"""The small, non-modal notice that a newer build exists.

It deliberately is not a QMessageBox: a dialog would steal focus from the editor, and the
user may be in the middle of a playback, a recording or an export. The panel sits in the
corner of the window with three plain choices and fades away when dismissed.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class UpdateNotice(QWidget):
    view_requested = pyqtSignal()
    remind_requested = pyqtSignal()
    skip_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("updateNotice")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlags(Qt.WindowType.Widget)
        self.update_info = None
        self._style()
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setObjectName("updateNoticeTitle")
        self.detail = QLabel("Your projects and settings stay exactly where they are.")
        self.detail.setWordWrap(True)
        self.detail.setObjectName("updateNoticeDetail")
        view = QPushButton("View Update")
        view.setObjectName("updateNoticePrimary")
        view.setDefault(True)
        view.clicked.connect(self.view_requested.emit)
        remind = QPushButton("Remind Me Later")
        remind.clicked.connect(self._remind)
        skip = QPushButton("Skip This Version")
        skip.clicked.connect(self._skip)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(view)
        buttons.addWidget(remind)
        buttons.addWidget(skip)
        close = QPushButton("\u00d7")
        close.setObjectName("updateNoticeClose")
        close.setFixedSize(22, 22)
        close.setToolTip("Dismiss until the next check")
        close.clicked.connect(self.hide)
        top = QHBoxLayout()
        top.addWidget(QLabel("Update available"))
        top.itemAt(0).widget().setObjectName("updateNoticeKicker")
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
    def show_update(self, update):
        self.update_info = update
        label = "Alpha" if getattr(update.version, "prerelease", False) else "Stable"
        self.title.setText(f"meeniFX Studios {update.version_text} {label} is available.")
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

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
