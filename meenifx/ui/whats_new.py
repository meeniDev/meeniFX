"""The What's New dialog shown on the first launch after an update."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QVBoxLayout, QWidget)

from .. import tokens
from ..tokens import COLORS
from ..updates import whats_new

CHANGELOG_URL = "https://github.com/meeniDev/meeniFX/blob/main/CHANGELOG.md"


class WhatsNewDialog(QDialog):
    """Readable release notes, once per installed version."""

    def __init__(self, version_text, notes, parent=None, *, date_text="",
                 release_url=None, acknowledge=None):
        super().__init__(parent)
        self.version_text = version_text
        self._acknowledge = acknowledge
        self._release_url = release_url or whats_new.notes_url(version_text)
        self.setWindowTitle("What's New in meeniFX Studios")
        self.setMinimumSize(560, 520)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setStyleSheet(f"QDialog {{ background: {COLORS['panel']}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(tokens.SP2)

        kicker = QLabel("WHAT'S NEW")
        kicker.setStyleSheet(f"color:{COLORS['accent']};font-size:{tokens.FONT_SMALL}px;"
                             "font-weight:600;letter-spacing:1px;")
        layout.addWidget(kicker)

        title = QLabel("meeniFX Studios")
        title.setStyleSheet(f"font-size:{tokens.FONT_TITLE + 9}px;font-weight:600;")
        layout.addWidget(title)

        subtitle = QLabel(f"Version {version_text}" + (f"  \u00b7  {date_text}" if date_text else ""))
        subtitle.setStyleSheet(f"color:{COLORS['muted']};")
        layout.addWidget(subtitle)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(8)
        sections = whats_new.parse_notes(whats_new.notes_text(version_text, notes))
        if not sections:
            sections = [("Highlights", ["This build includes behind-the-scenes improvements."])]
        for heading, items in sections:
            label = QLabel(heading)
            label.setStyleSheet(f"font-size:{tokens.FONT_TITLE}px;font-weight:600;"
                                f"color:{COLORS['accent']};")
            body_layout.addWidget(label)
            for item in items:
                bullet = QLabel(f"\u2022  {item}")
                bullet.setWordWrap(True)
                bullet.setTextFormat(Qt.TextFormat.PlainText)
                bullet.setStyleSheet(f"color:{COLORS['text']};")
                body_layout.addWidget(bullet)
            body_layout.addSpacing(6)
        note = QLabel("You can reopen these notes any time from Help > What's New.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{COLORS['muted']};font-size:{tokens.FONT_SMALL}px;")
        body_layout.addSpacing(4)
        body_layout.addWidget(note)
        body_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        layout.addWidget(scroll, 1)

        row = QHBoxLayout()
        changelog = QPushButton("View Full Changelog")
        changelog.clicked.connect(self._open_changelog)
        row.addWidget(changelog)
        row.addStretch(1)
        started = QPushButton("Get Started")
        started.setDefault(True)
        started.setMinimumWidth(120)
        started.clicked.connect(self.accept)
        row.addWidget(started)
        layout.addLayout(row)

    def _open_changelog(self):
        QDesktopServices.openUrl(QUrl(self._release_url or CHANGELOG_URL))

    def accept(self):
        if callable(self._acknowledge):
            self._acknowledge(self.version_text)
        super().accept()
