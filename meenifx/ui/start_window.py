"""Start window: recent projects as cards, New / Open, search and sort."""
import os
import shutil
import time

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal, QSettings
from PyQt6.QtGui import QPixmap, QImage, QColor, QPainter, QIcon
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
                             QToolButton, QVBoxLayout, QWidget, QAbstractItemView)

from ..icons import icon
from ..project import container, storage
from ..project.settings import PROJECT_EXTENSION, PROJECT_FILTER, fps_text
from .branding import app_icon, wordmark
from .filedialogs import choose_folder, open_file
from .layout import app_settings
from .timeline_settings import TimelineSettings
from .toast import Toast
from .themed import muted_text
from .. import tokens

THUMB_SIZE = QSize(224, 126)
SHOW_ON_STARTUP_KEY = "startup/showStartWindow"
PRUNE_MISSING_KEY = "startup/pruneMissingProjects"


def show_on_startup():
    value = app_settings().value(SHOW_ON_STARTUP_KEY, True)
    return value in (True, "true", "1", 1)


def set_show_on_startup(on):
    app_settings().setValue(SHOW_ON_STARTUP_KEY, bool(on))


def prune_missing_on_startup():
    """Preferences switch: drop entries whose file is gone when the window opens."""
    value = app_settings().value(PRUNE_MISSING_KEY, True)
    return value in (True, "true", "1", 1)


def set_prune_missing_on_startup(on):
    app_settings().setValue(PRUNE_MISSING_KEY, bool(on))


def _placeholder(missing):
    image = QImage(THUMB_SIZE, QImage.Format.Format_RGB32)
    image.fill(QColor(32, 34, 40))
    return QPixmap.fromImage(image)


def _relative_time(timestamp):
    delta = max(0, time.time() - timestamp)
    if delta < 60:
        return "Just now"
    for limit, unit, size in ((3600, "minute", 60), (86400, "hour", 3600), (86400 * 30, "day", 86400)):
        if delta < limit:
            n = int(delta // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def _count_label(items, noun):
    """The Clean up entry with its count, singular or plural: 1 missing project, 12 of them."""
    return f"Remove {len(items)} {noun}" + ("" if len(items) == 1 else "s")


def _paint_temp_badge(painter):
    """Small "TEMP" label in the thumbnail corner: a project living in the temp folder."""
    painter.save()
    font = painter.font()
    font.setPixelSize(8)
    font.setBold(True)
    painter.setFont(font)
    text = "TEMP"
    width = painter.fontMetrics().horizontalAdvance(text) + 8
    rect = QRectF(THUMB_SIZE.width() - width - 4, 4, width, 12)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 150))
    painter.drawRoundedRect(rect, 3, 3)
    painter.setPen(QColor(tokens.COLORS["coral_text"]))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


class ProjectInfo:
    """What a card shows; read from the container's meta entry only (never the timeline)."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.exists = os.path.isfile(path)
        # Tests and automation leave projects behind in the system temp folder; the card
        # says so, and the Clean up menu can drop them without touching real work.
        self.in_temp = storage.is_in_temp(path)
        self.modified = 0.0
        self.detail = ""
        self.thumb = None
        self.broken = False
        if not self.exists:
            return
        try:
            self.modified = os.path.getmtime(path)
            meta, thumb = container.read_summary(path)
            self.detail = f"{meta['width']}x{meta['height']}  {fps_text(meta['fps'])} fps"
            self.modified = float(meta.get("modified", self.modified))
            if thumb:
                pixmap = QPixmap()
                if pixmap.loadFromData(thumb):
                    self.thumb = pixmap
        except Exception:
            self.broken = True
            self.detail = "Unreadable project"


class StartWindow(QWidget):
    open_requested = pyqtSignal(str)
    new_requested = pyqtSignal()
    browse_requested = pyqtSignal()
    sample_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("meeniFX Studios")
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.resize(1040, 640)
        self.setMinimumSize(720, 460)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = QWidget(self)
        left.setObjectName("startSidebar")
        left.setFixedWidth(240)
        side = QVBoxLayout(left)
        side.setContentsMargins(20, 24, 20, 20)
        side.setSpacing(10)
        logo = wordmark(190)
        header = QLabel(self)
        if logo:
            header.setPixmap(logo)
        else:
            header.setText("meeniFX Studios")
        side.addWidget(header)
        side.addSpacing(18)
        self.new_button = QPushButton("New Project")
        self.new_button.setProperty("primary", True)
        self.new_button.setMinimumHeight(38)
        self.open_button = QPushButton("Open Project")
        self.open_button.setMinimumHeight(34)
        self.sample_button = QPushButton("Open Sample Project")
        self.sample_button.setMinimumHeight(34)
        self.sample_button.setToolTip("Open a short example with text, shapes, "
                                      "animation and a transition.")
        side.addWidget(self.new_button)
        side.addWidget(self.open_button)
        side.addWidget(self.sample_button)
        side.addStretch(1)
        self.startup_check = QCheckBox("Show on startup")
        self.startup_check.setChecked(show_on_startup())
        self.startup_check.toggled.connect(set_show_on_startup)
        side.addWidget(self.startup_check)
        root.addWidget(left)

        main = QVBoxLayout()
        main.setContentsMargins(20, 20, 20, 16)
        bar = QHBoxLayout()
        title = QLabel("Recent projects")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        bar.addWidget(title)
        bar.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(200)
        self.clean_button = QToolButton()
        self.clean_button.setIcon(icon("clean"))
        self.clean_button.setToolTip("Clean up the recent list")
        self.clean_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.clean_button.setFixedSize(28, 28)
        self.clean_menu = QMenu(self.clean_button)
        self.clean_button.setMenu(self.clean_menu)
        self.clean_menu.aboutToShow.connect(self._fill_clean_menu)
        self.sort = QComboBox()
        self.sort.addItems(["Last modified", "Name"])
        bar.addWidget(self.search)
        bar.addWidget(self.clean_button)
        bar.addWidget(self.sort)
        main.addLayout(bar)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setIconSize(THUMB_SIZE)
        self.grid.setGridSize(QSize(THUMB_SIZE.width() + 28, THUMB_SIZE.height() + 78))
        self.grid.setSpacing(6)
        self.grid.setWordWrap(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.setFrameShape(QListWidget.Shape.NoFrame)
        main.addWidget(self.grid, 1)
        self.empty = QLabel("No recent projects")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        muted_text(self.empty)
        main.addWidget(self.empty)
        root.addLayout(main, 1)

        self.new_button.clicked.connect(self.new_requested)
        self.open_button.clicked.connect(self.browse_requested)
        self.sample_button.clicked.connect(self.sample_requested.emit)
        self.search.textChanged.connect(self.refresh_view)
        self.sort.currentIndexChanged.connect(self.refresh_view)
        self.grid.itemActivated.connect(self._activate)
        self.grid.itemClicked.connect(self._activate)
        self.grid.customContextMenuRequested.connect(self._menu)
        self.toast = Toast(self)
        self._infos = []
        self.reload()

    # ---- data ----------------------------------------------------------
    def reload(self):
        self._infos = [ProjectInfo(p) for p in storage.load_recents()]
        self.refresh_view()

    def refresh_view(self, *_):
        needle = self.search.text().strip().lower()
        infos = [i for i in self._infos if needle in i.name.lower()]
        if self.sort.currentIndex() == 1:
            infos.sort(key=lambda i: i.name.lower())
        else:
            infos.sort(key=lambda i: i.modified, reverse=True)
        self.grid.clear()
        for info in infos:
            self.grid.addItem(self._make_item(info))
        self.grid.setVisible(bool(infos))
        self.empty.setVisible(not infos)
        self.empty.setText("No matching projects" if self._infos else "No recent projects")

    def _make_item(self, info):
        dim = not info.exists or info.broken
        second = "File not found" if not info.exists else info.detail
        text = f"{info.name}\n{second}\n{_relative_time(info.modified) if info.exists else info.path}"
        item = QListWidgetItem(text)
        pixmap = (info.thumb or _placeholder(dim)).scaled(THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation)
        canvas = QPixmap(THUMB_SIZE)
        canvas.fill(QColor(24, 26, 31))
        painter = QPainter(canvas)
        if dim:
            painter.setOpacity(0.35)
        painter.drawPixmap((THUMB_SIZE.width() - pixmap.width()) // 2, (THUMB_SIZE.height() - pixmap.height()) // 2, pixmap)
        painter.setOpacity(1.0)
        if info.in_temp:
            _paint_temp_badge(painter)
        painter.end()
        item.setIcon(QIcon(canvas))
        item.setData(Qt.ItemDataRole.UserRole, info.path)
        item.setToolTip(info.path)
        if dim:
            item.setForeground(QColor(120, 124, 132))
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        return item

    def _info_for(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        return next((i for i in self._infos if i.path == path), None)

    def toast_message(self, text, kind="info", duration_ms=None):
        self.toast.show_message(text, kind=kind, duration_ms=duration_ms)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.reposition()

    # ---- clean up -------------------------------------------------------
    def _fill_clean_menu(self):
        """Rebuild the menu each time it opens, so the counts are always current."""
        missing = [i for i in self._infos if not i.exists]
        temps = [i for i in self._infos if i.in_temp]
        self.clean_menu.clear()
        missing_action = self.clean_menu.addAction(
            _count_label(missing, "missing project") if missing else "Remove missing projects")
        missing_action.setEnabled(bool(missing))
        missing_action.setToolTip("Drop every entry whose project file is gone (the list only)")
        missing_action.triggered.connect(self._clean_missing)
        temp_action = self.clean_menu.addAction(
            _count_label(temps, "temp-folder project") if temps else "Remove temp-folder projects")
        temp_action.setEnabled(bool(temps))
        temp_action.setToolTip("Drop entries inside the system temp folder, where tests and "
                               "automation leave throwaway projects")
        temp_action.triggered.connect(self._clean_temp)

    def _clean_missing(self):
        removed = storage.prune_missing()
        if removed:
            self.reload()
        self.toast_message(f"Removed {removed}" if removed else "Nothing to remove",
                           duration_ms=1800)

    def _clean_temp(self):
        paths = [i.path for i in self._infos if i.in_temp]
        if not paths:
            return
        dialog = TempCleanupDialog(len(paths), self)
        if not dialog.exec():
            return
        removed = storage.remove_temp_recents()
        trashed, failures = 0, []
        if dialog.trash_folders():
            for path in paths:
                moved, messages = storage.trash_project(path)
                trashed += moved
                failures.extend(messages)
        self.reload()
        text = f"Removed {removed}"
        if trashed:
            text += f", moved {trashed} project{'s' if trashed != 1 else ''} to the Recycle Bin"
        if failures:
            text += f" ({len(failures)} could not be moved: {failures[0]})"
        self.toast_message(text, kind="error" if failures else "info",
                           duration_ms=4200 if failures else 2400)

    # ---- actions -------------------------------------------------------
    def _activate(self, item):
        info = self._info_for(item)
        if info is None:
            return
        if not info.exists:
            self._locate(info)
        else:
            self.open_requested.emit(info.path)

    def _menu(self, pos):
        item = self.grid.itemAt(pos)
        info = self._info_for(item) if item else None
        if info is None:
            return
        menu = QMenu(self)
        if info.exists:
            menu.addAction("Open", lambda: self.open_requested.emit(info.path))
            menu.addAction("Show in folder", lambda: show_in_folder(info.path))
            menu.addAction("Rename...", lambda: self._rename(info))
            menu.addAction("Duplicate", lambda: self._duplicate(info))
        else:
            menu.addAction("Locate...", lambda: self._locate(info))
        menu.addAction("Remove from list", lambda: self._remove(info))
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _remove(self, info):
        storage.remove_recent(info.path)
        self.reload()

    def _locate(self, info):
        path = open_file(self, f"Locate {info.name}", os.path.dirname(info.path), PROJECT_FILTER)
        if path and storage.is_project_file(path):
            storage.replace_recent(info.path, path)
            self.reload()

    def _rename(self, info):
        name, ok = QInputDialog.getText(self, "Rename project", "Name:", text=info.name)
        name = storage.safe_name(name) if ok else ""
        if not name or name == info.name:
            return
        target = storage.ensure_extension(os.path.join(os.path.dirname(info.path), name))
        if os.path.exists(target):
            QMessageBox.warning(self, "Rename project", "A project with that name already exists.")
            return
        try:
            os.replace(info.path, target)
            for extra in (storage.backup_path(info.path), storage.autosave_path(info.path)):
                if os.path.exists(extra):
                    os.replace(extra, storage.backup_path(target) if extra.endswith(".bak") else storage.autosave_path(target))
        except OSError as exc:
            QMessageBox.warning(self, "Rename project", str(exc))
            return
        storage.replace_recent(info.path, target)
        self.reload()

    def _duplicate(self, info):
        folder = os.path.dirname(info.path)
        n = 1
        while True:
            suffix = " copy" if n == 1 else f" copy {n}"
            target = os.path.join(folder, info.name + suffix + PROJECT_EXTENSION)
            if not os.path.exists(target):
                break
            n += 1
        try:
            shutil.copy2(info.path, target)
        except OSError as exc:
            QMessageBox.warning(self, "Duplicate project", str(exc))
            return
        storage.add_recent(target)
        self.reload()

    # ---- drag and drop --------------------------------------------------
    def dragEnterEvent(self, event):
        if any(storage.is_project_file(u.toLocalFile()) for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if storage.is_project_file(path):
                self.open_requested.emit(path)
                event.acceptProposedAction()
                return


class TempCleanupDialog(QDialog):
    """Confirm dropping the temp-folder projects, optionally moving their folders to the Bin."""

    def __init__(self, count, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Remove temp-folder projects")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        title = QLabel(f"{count} project{'s' if count != 1 else ''} in the system temp folder")
        title.setStyleSheet("font-weight: 600;")
        layout.addWidget(title)
        note = QLabel("Tests and automation create their projects there. Removing them only edits "
                      "this list; the files stay where they are unless you ask for the Recycle Bin.")
        note.setWordWrap(True)
        muted_text(note)
        layout.addWidget(note)
        self.trash = QCheckBox("Also move the project folders to the Recycle Bin")
        self.trash.setChecked(False)
        self.trash.setToolTip("Only a folder that holds nothing but its project is moved; "
                              "a folder with anything else in it is left alone")
        layout.addWidget(self.trash)
        buttons = QDialogButtonBox()
        remove = buttons.addButton("Remove", QDialogButtonBox.ButtonRole.AcceptRole)
        remove.setProperty("primary", True)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def trash_folders(self):
        return self.trash.isChecked()


def show_in_folder(path):
    import subprocess
    import sys
    path = os.path.abspath(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


# Category-based settings live in their own module; this keeps the alias importable here.
from .preferences_dialog import PreferencesDialog  # noqa: E402
