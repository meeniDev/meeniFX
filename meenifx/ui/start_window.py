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
        side.addWidget(self.new_button)
        side.addWidget(self.open_button)
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


class PreferencesDialog(QDialog):
    def __init__(self, parent=None, section=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences" if section != "previews"
                            else "Preferences \u2013 Effect Previews")
        self._section = section
        layout = QVBoxLayout(self)
        self.startup = QCheckBox("Show start window on startup")
        self.startup.setChecked(show_on_startup())
        layout.addWidget(self.startup)
        self.prune_missing = QCheckBox("Remove missing projects from the list automatically at startup")
        self.prune_missing.setChecked(prune_missing_on_startup())
        self.prune_missing.setToolTip("Silently drops recent entries whose file is gone when the "
                                      "window opens, so a deleted or moved project cannot linger")
        layout.addWidget(self.prune_missing)
        from ..recording.settings import RecorderSettings
        self._recorder_settings = RecorderSettings.load()
        row = QHBoxLayout()
        row.addWidget(QLabel("Recordings folder"))
        self.rec_folder = QLineEdit(self._recorder_settings.folder)
        self.rec_folder.setPlaceholderText("Project's Recordings folder")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_recordings)
        row.addWidget(self.rec_folder, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self.rec_float = QCheckBox("Show floating recording controls over other applications")
        self.rec_float.setChecked(bool(self._recorder_settings.floating_bar))
        self.rec_float.setToolTip("Off by default: the main window shows the recording time and "
                                 "the stop button, so switching applications stays unobstructed")
        layout.addWidget(self.rec_float)
        engine = getattr(parent, "engine", None)
        if engine is not None:
            from .widgets import ScrubField
            self.fps_cap = ScrubField("Playback FPS cap", engine.playback_fps.get(), 12, 60, default=30, step=1,
                                       label_width=110, tooltip="Upper limit for preview playback refresh")
            self.fps_cap.edited.connect(lambda value: engine.playback_fps.set(int(round(value))))
            layout.addWidget(self.fps_cap)
        self._build_playback_section(layout)
        self._build_paste_section(layout)
        self._build_caption_section(layout)
        self._build_preview_section(layout)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------ playback & performance
    def _build_playback_section(self, layout):
        """GPU, preview quality, cache budgets/location, proxies, hardware codecs.

        Every value is stored in the app's INI file through ``PlaybackSettings``; the
        backend line is read from the live detector, so it can only report what is really
        active (including an honest "measured slower on the GPU, using CPU").
        """
        from PyQt6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QSpinBox)
        from ..preview.settings import PlaybackSettings
        self.playback_settings = PlaybackSettings.load()
        settings = self.playback_settings

        group = QGroupBox("Playback && Performance", self)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.gpu_mode = QComboBox()
        for label, value in (("Auto (measure and choose)", "auto"), ("On (force OpenCL)", "on"),
                             ("Off (CPU only)", "off")):
            self.gpu_mode.addItem(label, value)
        self.gpu_mode.setCurrentIndex(max(0, self.gpu_mode.findData(settings.gpu_mode)))
        form.addRow("GPU acceleration", self.gpu_mode)

        self.quality_mode = QComboBox()
        for label in ("Auto", "Full", "1/2", "1/4", "1/8"):
            self.quality_mode.addItem(label, label)
        self.quality_mode.setCurrentIndex(max(0, self.quality_mode.findData(settings.quality_mode)))
        form.addRow("Preview quality", self.quality_mode)

        self.ram_cache = QSpinBox()
        self.ram_cache.setRange(64, 16384); self.ram_cache.setSingleStep(128)
        self.ram_cache.setSuffix(" MB"); self.ram_cache.setValue(settings.ram_cache_mb)
        form.addRow("RAM cache limit", self.ram_cache)

        self.disk_cache = QSpinBox()
        self.disk_cache.setRange(0, 102400); self.disk_cache.setSingleStep(512)
        self.disk_cache.setSuffix(" MB"); self.disk_cache.setValue(settings.disk_cache_mb)
        form.addRow("Disk cache limit", self.disk_cache)

        from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton
        folder_row = QHBoxLayout()
        self.cache_folder = QLineEdit(settings.cache_folder)
        self.cache_folder.setPlaceholderText("Project's Cache folder (default)")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_cache_folder)
        folder_row.addWidget(self.cache_folder, 1)
        folder_row.addWidget(browse)
        form.addRow("Cache location", folder_row)

        self.background_render = QCheckBox("Render preview frames in the background")
        self.background_render.setChecked(settings.background_render)
        form.addRow("", self.background_render)

        self.auto_proxy = QCheckBox("Generate proxy media for heavy sources")
        self.auto_proxy.setChecked(settings.auto_proxy)
        form.addRow("", self.auto_proxy)

        self.proxy_width = QComboBox()
        for width in (640, 960, 1280, 1920):
            self.proxy_width.addItem(f"{width} px wide", width)
        self.proxy_width.setCurrentIndex(max(0, self.proxy_width.findData(settings.proxy_width)))
        form.addRow("Proxy quality", self.proxy_width)

        self.gain_headroom = QSpinBox()
        self.gain_headroom.setRange(0, 24)
        self.gain_headroom.setSuffix(" dB")
        self.gain_headroom.setValue(settings.gain_headroom_db)
        self.gain_headroom.setToolTip("Maximum boost the audio gain control (and its timeline "
                                      "envelope) may apply above 0 dB")
        form.addRow("Gain headroom", self.gain_headroom)

        self.hardware_decode = QComboBox()
        self.hardware_encode = QComboBox()
        for combo in (self.hardware_decode, self.hardware_encode):
            for label, value in (("Auto", "auto"), ("On", "on"), ("Off", "off")):
                combo.addItem(label, value)
        self.hardware_decode.setCurrentIndex(max(0, self.hardware_decode.findData(settings.hardware_decode)))
        self.hardware_encode.setCurrentIndex(max(0, self.hardware_encode.findData(settings.hardware_encode)))
        form.addRow("Hardware decoding", self.hardware_decode)
        form.addRow("Hardware encoding", self.hardware_encode)

        self.backend_label = QLabel(self._backend_text())
        self.backend_label.setWordWrap(True)
        self.backend_label.setObjectName("backendInfo")
        form.addRow("Active backend", self.backend_label)
        self.gpu_mode.currentIndexChanged.connect(lambda _i: self._refresh_backend_text())

        self.cache_notifications = QCheckBox("Show a message when background caching finishes")
        self.cache_notifications.setChecked(settings.cache_notifications)
        self.cache_notifications.setToolTip("Off by default: the timeline cache indicator shows "
                                            "the same thing without interrupting the edit")
        form.addRow("", self.cache_notifications)

        clear = QPushButton("Clear preview cache now")
        clear.clicked.connect(self._clear_preview_cache)
        form.addRow("", clear)
        layout.addWidget(group)

    def _backend_text(self):
        from ..media import reader as reader_module
        from ..preview.backend import gpu
        from ..media import hardware
        acceleration = reader_module.decode_acceleration()
        decode = {0: "software decode", 2: "D3D11 decode"}.get(acceleration, "hardware decode")
        encode = "NVENC available" if hardware._CACHE.get("nvenc") else "encode: verified on first export"
        return f"{gpu.summary()} | {decode} | {encode}"

    def _refresh_backend_text(self):
        """Switch the live backend immediately so the reported state matches the choice."""
        from ..preview.backend import gpu
        gpu.set_mode(self.gpu_mode.currentData() or "auto")
        self.backend_label.setText(self._backend_text())

    def _browse_cache_folder(self):
        folder = choose_folder(self, "Preview cache folder", self.cache_folder.text() or "")
        if folder:
            self.cache_folder.setText(folder)

    def _clear_preview_cache(self):
        parent = self.parent()
        if hasattr(parent, "clear_preview_cache"):
            parent.clear_preview_cache()
            self.backend_label.setText(self._backend_text())

    def _browse_recordings(self):
        folder = choose_folder(self, "Recordings folder", self.rec_folder.text())
        if folder:
            self.rec_folder.setText(folder)

    # ------------------------------------------------------------ smart paste
    def _build_paste_section(self, layout):
        """Smart Paste: what Ctrl+V does with a clipboard picture or file.

        These values live in the app's INI file (``PasteSettings``): they describe how a
        paste behaves, not what the project contains, so they never travel in ``.mfxpf``.
        """
        from PyQt6.QtWidgets import QDoubleSpinBox, QFormLayout, QGroupBox
        from .smart_paste import PasteSettings
        self.paste_settings = PasteSettings.load()
        settings = self.paste_settings

        group = QGroupBox("Smart Paste", self)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.paste_seconds = QDoubleSpinBox()
        self.paste_seconds.setRange(0.2, 120.0)
        self.paste_seconds.setSingleStep(0.5)
        self.paste_seconds.setDecimals(1)
        self.paste_seconds.setSuffix(" s")
        self.paste_seconds.setValue(settings.seconds)
        self.paste_seconds.setToolTip("Clip length for an image pasted from the clipboard")
        form.addRow("Default image duration", self.paste_seconds)

        self.paste_placement = QComboBox()
        self.paste_placement.addItem("At the playhead", "playhead")
        self.paste_placement.addItem("After the selected clip", "after")
        self.paste_placement.setCurrentIndex(max(0, self.paste_placement.findData(settings.placement)))
        form.addRow("Paste placement", self.paste_placement)

        self.paste_select = QCheckBox("Select the pasted clip")
        self.paste_select.setChecked(settings.select)
        form.addRow("", self.paste_select)

        self.paste_fit = QCheckBox("Fit pasted images to the frame")
        self.paste_fit.setChecked(settings.fit)
        self.paste_fit.setToolTip("Reset the pasted picture's transform to the centred, 100 % fit")
        form.addRow("", self.paste_fit)

        note = QLabel("Ctrl+V in a text field still pastes text, and in the Media panel it only "
                      "adds the file without touching the timeline.")
        note.setWordWrap(True)
        muted_text(note)
        form.addRow("", note)
        layout.addWidget(group)

    def _apply_paste_settings(self):
        from .smart_paste import PasteSettings
        PasteSettings(float(self.paste_seconds.value()),
                      str(self.paste_placement.currentData() or "playhead"),
                      self.paste_select.isChecked(),
                      self.paste_fit.isChecked()).save()

    # ----------------------------------------------------------- caption models
    def _build_caption_section(self, layout):
        """Speech models downloaded for Auto Captions: they can be deleted here."""
        from PyQt6.QtWidgets import QGroupBox, QFormLayout
        from ..captions import whisper
        self._caption_whisper = whisper

        group = QGroupBox("Captions \u2013 Speech Models", self)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        backend = ("faster-whisper is installed; transcription runs locally and offline."
                   if whisper.backend_available() else
                   "faster-whisper is not installed - Auto Captions cannot transcribe. "
                   "Install it with:  pip install faster-whisper")
        self.caption_backend = QLabel(backend)
        self.caption_backend.setWordWrap(True)
        form.addRow("Engine", self.caption_backend)
        folder = QLabel(whisper.MODEL_DIR)
        folder.setWordWrap(True)
        muted_text(folder)
        form.addRow("Models folder", folder)
        self._caption_models_host = QWidget()
        self._caption_models_layout = QVBoxLayout(self._caption_models_host)
        self._caption_models_layout.setContentsMargins(0, 0, 0, 0)
        self._caption_models_layout.setSpacing(4)
        form.addRow("Downloaded", self._caption_models_host)
        layout.addWidget(group)
        self._refresh_caption_models()

    def _refresh_caption_models(self):
        whisper = self._caption_whisper
        layout = self._caption_models_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        models = whisper.installed_models()
        if not models:
            empty = QLabel("No model downloaded yet")
            muted_text(empty)
            layout.addWidget(empty)
            return
        for name, size in models:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            label = QLabel(f"{name}  ({size / (1024 * 1024):.0f} MB)")
            line.addWidget(label, 1)
            delete = QPushButton("Delete")
            delete.clicked.connect(lambda _checked=False, model=name: self._delete_caption_model(model))
            line.addWidget(delete)
            layout.addWidget(row)

    def _delete_caption_model(self, name):
        answer = QMessageBox.question(self, "Delete speech model",
                                      f"Delete the downloaded {name} model?\n\n"
                                      "Auto Captions will ask to download it again next time.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._caption_whisper.delete_model(name)
        self._refresh_caption_models()

    # ------------------------------------------------------ effect preview images
    def _build_preview_section(self, layout):
        """Appearance > Effect Previews: the reference image and how cards draw it.

        These values live in the app's INI file (``PreviewSettings``), never in a project:
        changing the reference picture must not touch media, exports or ``.mfxpf`` files.
        """
        from PyQt6.QtWidgets import QGroupBox, QFormLayout
        from .. import thumbnails as THUMBS
        self.preview_settings = THUMBS.PreviewSettings.load()

        group = QGroupBox("Appearance \u2013 Effect Previews", self)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.preview_image = QComboBox()
        self.preview_image.addItem("Default (figure_cafe.png)", "default")
        self.preview_image.addItem("Custom image", "custom")
        self.preview_image.setCurrentIndex(
            max(0, self.preview_image.findData(self.preview_settings.image_mode)))
        form.addRow("Preview image", self.preview_image)

        row = QHBoxLayout()
        self.preview_path = QLineEdit(self.preview_settings.custom_path)
        self.preview_path.setReadOnly(True)
        self.preview_path.setPlaceholderText(str(THUMBS.default_reference()
                                                 or "figure_cafe.png not found"))
        choose = QPushButton("Choose Image\u2026")
        choose.clicked.connect(self._choose_preview_image)
        reset = QPushButton("Reset to Default")
        reset.clicked.connect(self._reset_preview_image)
        row.addWidget(self.preview_path, 1)
        row.addWidget(choose)
        row.addWidget(reset)
        form.addRow("", row)

        self.preview_thumb = QLabel()
        self.preview_thumb.setFixedSize(168, 112)
        self.preview_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow("Preview", self.preview_thumb)

        self.preview_background = QComboBox()
        for label, value in (("Checkerboard", "checker"), ("Dark", "dark"),
                             ("Light", "light")):
            self.preview_background.addItem(label, value)
        self.preview_background.setCurrentIndex(
            max(0, self.preview_background.findData(self.preview_settings.background)))
        form.addRow("Preview background", self.preview_background)

        self.preview_quality = QComboBox()
        for label, value in (("Low", "low"), ("Medium", "medium"), ("High", "high")):
            self.preview_quality.addItem(label, value)
        self.preview_quality.setCurrentIndex(
            max(0, self.preview_quality.findData(self.preview_settings.quality)))
        form.addRow("Thumbnail quality", self.preview_quality)

        self.preview_image.currentIndexChanged.connect(self._refresh_preview_thumb)
        self.preview_background.currentIndexChanged.connect(self._refresh_preview_thumb)
        layout.addWidget(group)
        self._refresh_preview_thumb()

    def _preview_form_settings(self):
        """The values currently shown in the section (not yet saved)."""
        from .. import thumbnails as THUMBS
        return THUMBS.PreviewSettings(
            image_mode=str(self.preview_image.currentData() or "default"),
            custom_path=self.preview_path.text().strip(),
            background=str(self.preview_background.currentData() or "checker"),
            quality=str(self.preview_quality.currentData() or "medium"))

    def _refresh_preview_thumb(self, *_args):
        from PyQt6.QtGui import QPixmap
        from .. import thumbnails as THUMBS
        settings = self._preview_form_settings()
        ratio = self.preview_thumb.devicePixelRatioF() or 1.0
        size = self.preview_thumb.size()
        image = THUMBS.reference_preview(int(size.width() * ratio),
                                        int(size.height() * ratio), settings)
        if image is None:
            self.preview_thumb.setText("Reference image missing")
            self.preview_thumb.setPixmap(QPixmap())
            return
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        self.preview_thumb.setPixmap(pixmap)

    def _choose_preview_image(self):
        path = open_file(self, "Choose preview image", "",
                         "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;All files (*)")
        if not path:
            return
        from .. import thumbnails as THUMBS
        if not THUMBS._readable(path):
            QMessageBox.warning(self, "Preview image",
                                "That image cannot be read by this build of meeniFX.\n"
                                "The default reference picture will be used instead.")
            return
        self.preview_path.setText(path)
        self.preview_image.setCurrentIndex(max(0, self.preview_image.findData("custom")))
        self._refresh_preview_thumb()

    def _reset_preview_image(self):
        self.preview_path.clear()
        self.preview_image.setCurrentIndex(max(0, self.preview_image.findData("default")))
        self._refresh_preview_thumb()

    def _apply_preview_settings(self):
        """Persist the preview values and ask the window to rebuild its thumbnails."""
        self.preview_settings = self._preview_form_settings()
        self.preview_settings.save()
        parent = self.parent()
        reload_thumbs = getattr(parent, "_reload_effect_previews", None)
        if callable(reload_thumbs):
            reload_thumbs()

    def accept(self):
        set_show_on_startup(self.startup.isChecked())
        set_prune_missing_on_startup(self.prune_missing.isChecked())
        self._recorder_settings.folder = self.rec_folder.text().strip()
        self._recorder_settings.floating_bar = self.rec_float.isChecked()
        self._recorder_settings.save()
        self._apply_playback_settings()
        self._apply_paste_settings()
        self._apply_preview_settings()
        super().accept()

    def _apply_playback_settings(self):
        """Persist the Playback & Performance values and push them into the live app."""
        from ..preview.settings import PlaybackSettings
        settings = PlaybackSettings({"gpu_mode": self.gpu_mode.currentData(),
                                     "quality_mode": self.quality_mode.currentData(),
                                     "ram_cache_mb": self.ram_cache.value(),
                                     "disk_cache_mb": self.disk_cache.value(),
                                     "cache_folder": self.cache_folder.text().strip(),
                                     "background_render": self.background_render.isChecked(),
                                     "auto_proxy": self.auto_proxy.isChecked(),
                                     "proxy_width": self.proxy_width.currentData(),
                                     "hardware_decode": self.hardware_decode.currentData(),
                                     "hardware_encode": self.hardware_encode.currentData(),
                                     "show_overlay": self.playback_settings.show_overlay,
                                     "prefetch_seconds": self.playback_settings.prefetch_seconds,
                                     "gain_headroom_db": self.gain_headroom.value(),
                                     "waveform_mode": self.playback_settings.waveform_mode,
                                     "cache_notifications": self.cache_notifications.isChecked()})
        settings.save()
        self.playback_settings = settings
        parent = self.parent()
        apply = getattr(parent, "_apply_playback_settings", None)
        if callable(apply):
            apply(settings)
