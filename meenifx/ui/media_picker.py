"""Reusable asset picker for effect parameters that consume media.

These controls used to be raw QLineEdits: a path was typed (or pasted) by hand and a
mistyped or moved file silently rendered nothing.  MediaPicker keeps the same stored
string, so projects and rendering are unchanged, but offers file browsing, existing
project media, a thumbnail/name, Replace, Clear and an explicit missing-file state.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImageReader, QPixmap
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QWidget)

from .. import tokens
from ..icons import icon
from .filedialogs import open_file


IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)"
LUT_FILTER = "Colour lookup tables (*.cube);;All files (*.*)"


class MediaPicker(QWidget):
    """A compact asset field: thumbnail, filename, Replace and Clear."""

    pathChanged = pyqtSignal(str)
    pathRejected = pyqtSignal(str)

    def __init__(self, path="", parent=None, *, media_kind="image",
                 project_provider=None, label="asset"):
        super().__init__(parent)
        self.media_kind = str(media_kind or "image")
        self.project_provider = project_provider
        self._path = ""
        self.last_error = ""
        self._label = label
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.SP1)

        self.thumb = QLabel(self)
        self.thumb.setFixedSize(42, 26)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            f"QLabel {{ background: {tokens.COLORS['panel_soft']};"
            f" border: 1px solid {tokens.COLORS['border']};"
            f" border-radius: {tokens.RADIUS}px; }}")
        self.name = QLabel(self)
        self.name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.choose = QToolButton(self)
        self.choose.setText("Choose…")
        self.choose.setToolTip(f"Choose a file or a project asset for this {label}")
        self.choose.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.choose.setProperty("role", "tool")
        self.clear = QToolButton(self)
        self.clear.setProperty("role", "icon")
        self.clear.setIcon(icon("close"))
        self.clear.setFixedSize(QSize(tokens.HIT, tokens.HIT))
        self.clear.setToolTip(f"Clear this {label}")

        layout.addWidget(self.thumb, 0)
        layout.addWidget(self.name, 1)
        layout.addWidget(self.choose, 0)
        layout.addWidget(self.clear, 0)

        self.choose.setMenu(self._menu())
        self.clear.clicked.connect(lambda: self.set_path("", emit=True))
        self.set_path(path)

    # ------------------------------------------------------------------ data
    def path(self):
        return self._path

    def set_path(self, path, emit=False):
        candidate = str(path or "").strip()
        if emit and candidate and not self._supported(candidate):
            filename = os.path.basename(candidate) or candidate
            extension = os.path.splitext(candidate)[1].lower() or "no extension"
            self.last_error = (f"Unsupported {self._label}: {filename} ({extension}) is not "
                               f"a readable {self.media_kind} file. The current asset was "
                               f"kept.")
            self.pathRejected.emit(self.last_error)
            return False
        self._path = candidate
        self.last_error = ""
        self._sync()
        if emit:
            self.pathChanged.emit(self._path)
        return True

    def _supported(self, path):
        """Reject a browse result before it can replace a working selection."""
        if not os.path.isfile(path):
            return False
        extension = os.path.splitext(path)[1].lower()
        if self.media_kind == "lut":
            return extension == ".cube"
        if extension not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"):
            return False
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        return reader.canRead()

    def _project(self):
        if not callable(self.project_provider):
            return None
        try:
            return self.project_provider()
        except Exception:
            return None

    def _project_media(self):
        project = self._project()
        library = getattr(project, "library", None)
        if library is None:
            return []
        result = []
        for item in library.items.values():
            if not getattr(item, "path", ""):
                continue
            if self.media_kind == "image" and not str(item.path).lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")):
                # Only decodable stills are useful as a sprite/texture/map.
                continue
            if self.media_kind == "lut" and not str(item.path).lower().endswith(".cube"):
                continue
            result.append(item)
        return result

    def _menu(self):
        menu = QMenu(self)
        browse = menu.addAction(icon("image"), "Browse files…")
        browse.triggered.connect(self._browse)
        media_menu = menu.addMenu(icon("film"), "Project media")
        items = self._project_media()
        if not items:
            empty = media_menu.addAction("No matching project media")
            empty.setEnabled(False)
        for item in items[:40]:
            action = media_menu.addAction(icon("image"), item.name)
            action.setToolTip(item.path)
            action.triggered.connect(
                lambda _checked=False, value=item.path: self.set_path(value, emit=True))
        menu.addSeparator()
        clear = menu.addAction(icon("close"), "Clear")
        clear.setEnabled(bool(self._path))
        clear.triggered.connect(lambda: self.set_path("", emit=True))
        return menu

    def _browse(self):
        filter_text = LUT_FILTER if self.media_kind == "lut" else IMAGE_FILTER
        path = open_file(self, f"Choose {self._label}", self._directory(), filter_text)
        if path:
            # A cancelled dialog leaves the current asset untouched.
            self.set_path(path, emit=True)

    def _directory(self):
        if self._path and os.path.isdir(os.path.dirname(self._path)):
            return os.path.dirname(self._path)
        project = self._project()
        path = getattr(project, "path", "")
        return os.path.dirname(path) if path else ""

    # ------------------------------------------------------------------ view
    def _sync(self):
        # Rebuild the menu so project media reflects additions while the card is open.
        self.choose.setMenu(self._menu())
        path = self._path
        if not path:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setToolTip("No asset selected")
            self.name.setText("No asset selected")
            self.name.setProperty("quiet", True)
            self.name.setToolTip("")
            self.clear.setEnabled(False)
            self.choose.setText("Choose…")
            return
        filename = os.path.basename(path) or path
        missing = not os.path.isfile(path)
        self.name.setText(f"{filename}  ·  missing" if missing else filename)
        self.name.setProperty("quiet", missing)
        self.name.setToolTip(
            f"{path}\nThe file is missing. Choose it again to relink without losing the "
            f"effect's other settings." if missing else path)
        self.name.setStyleSheet(
            f"color: {tokens.COLORS['warning']};" if missing else "")
        self.clear.setEnabled(True)
        self.choose.setText("Replace…")
        self.thumb.setPixmap(self._thumbnail(path, missing))

    def _thumbnail(self, path, missing):
        if missing:
            return icon("warning", tokens.COLORS["warning"], 18).pixmap(18, 18)
        if self.media_kind == "lut" or not str(path).lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")):
            return icon("lut" if self.media_kind == "lut" else "image",
                        tokens.COLORS["muted"], 18).pixmap(18, 18)
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return icon("warning", tokens.COLORS["warning"], 18).pixmap(18, 18)
        pixmap = QPixmap.fromImage(image)
        return pixmap.scaled(40, 24, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)

    def mouseDoubleClickEvent(self, event):
        self._browse()
        event.accept()
