"""Project panel: the project's media as a list or grid, with bins, thumbnails and drag and drop."""
import hashlib
import os

import cv2
from PyQt6.QtCore import QMimeData, QObject, QPoint, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QIcon, QImage, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QPushButton, QToolButton, QVBoxLayout, QWidget)

from ..constants import AUDIO_EXT, IMAGE_EXT, MEDIA_MIME, VIDEO_EXT
from ..project import io as project_io
from ..project.media import media_type_for, norm_path
from .filedialogs import open_file, open_files

ROOT_BIN = "<root>"
GRID_ICON = QSize(96, 54)
LIST_ICON = QSize(48, 28)
ALL_EXT = tuple(VIDEO_EXT) + tuple(IMAGE_EXT) + tuple(AUDIO_EXT)
THUMB_W = 192


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}:{rest:02d}" if minutes < 60 else f"{minutes // 60}:{minutes % 60:02d}:{rest:02d}"


def thumb_cache_file(cache_dir, path):
    try:
        stat = os.stat(path)
        key = f"{os.path.abspath(path)}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        key = os.path.abspath(path)
    return os.path.join(cache_dir, hashlib.md5(key.encode("utf-8")).hexdigest() + ".jpg")


def probe_media(path, cache_dir=None):
    """(info dict, QImage or None) for a video or image; runs on a worker thread."""
    kind = media_type_for(path)
    info, frame = {}, None
    if kind == "audio" or not os.path.isfile(path):
        return info, None
    cached = thumb_cache_file(cache_dir, path) if cache_dir else None
    if kind == "image":
        frame = cv2.imread(path, cv2.IMREAD_COLOR)
        if frame is None:
            try:
                import imageio.v3 as iio
                frame = cv2.cvtColor(iio.imread(path)[..., :3], cv2.COLOR_RGB2BGR)
            except Exception:
                frame = None
        if frame is not None:
            info = {"width": frame.shape[1], "height": frame.shape[0], "fps": 0.0, "duration": 0.0}
    else:
        cap = cv2.VideoCapture(path)
        try:
            if cap.isOpened():
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                info = {"width": width, "height": height, "fps": fps, "duration": count / fps if fps > 0 else 0.0}
                if not (cached and os.path.isfile(cached)):
                    if count > 20:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, int(count * 0.1))
                    ok, frame = cap.read()
                    if not ok:
                        frame = None
        finally:
            cap.release()
    if cached and os.path.isfile(cached):
        image = QImage(cached)
        return info, (image if not image.isNull() else None)
    if frame is None:
        return info, None
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (THUMB_W, max(1, int(h * THUMB_W / w))), interpolation=cv2.INTER_AREA)
    if cached:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            cv2.imwrite(cached, small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        except OSError:
            pass
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    return info, QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888).copy()


class _ProbeSignals(QObject):
    done = pyqtSignal(str, object, object)


class _ProbeJob(QRunnable):
    def __init__(self, item_id, path, cache_dir, signals):
        super().__init__()
        self.item_id, self.path, self.cache_dir, self.signals = item_id, path, cache_dir, signals

    def run(self):
        try:
            info, image = probe_media(self.path, self.cache_dir)
        except Exception:
            info, image = {}, None
        try:
            self.signals.done.emit(self.item_id, info, image)
        except RuntimeError:
            pass


class _MediaList(QListWidget):
    files_dropped = pyqtSignal(list, str)    # paths, target bin id ("" = current bin)
    items_moved = pyqtSignal(list, str)      # media ids, destination bin id ("" = project root)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Qt's built-in drag never produced a QDrag the timeline could recognise, so the drag
        # is started by hand below with the media MIME payload. Drops are still accepted for
        # moving items between bins.
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self._drag_press = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setMovement(QListWidget.Movement.Static)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setFrameShape(QListWidget.Shape.NoFrame)

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction

    # ---- explicit drag source ------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_press = event.position().toPoint()
        else:
            self._drag_press = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            moved = (event.position().toPoint() - self._drag_press).manhattanLength()
            if moved >= QApplication.startDragDistance():
                item = self.itemAt(self._drag_press)
                self._drag_press = None
                if item is not None and self._kind(item) == "item":
                    self.start_media_drag(item)
                    return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_press = None
        super().mouseReleaseEvent(event)

    @staticmethod
    def _kind(item):
        data = item.data(Qt.ItemDataRole.UserRole)
        return data[0] if data else ""

    def drag_items(self, item):
        """Every selected media item, or just the one under the pointer."""
        chosen = [i for i in self.selectedItems() if self._kind(i) == "item"]
        return chosen if item in chosen else [item]

    def start_media_drag(self, item):
        """Begin a real drag carrying media ids; returns the action Qt reported."""
        items = self.drag_items(item)
        mime = self.mimeData(items)
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = self._drag_pixmap(items)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        try:
            return drag.exec(Qt.DropAction.CopyAction)
        finally:
            self.viewport().update()

    def _drag_pixmap(self, items):
        """Semi-transparent thumbnail (and a count) so the drag reads as a clip being carried."""
        size = self.iconSize()
        pixmap = items[0].icon().pixmap(size)
        if pixmap.isNull() or pixmap.width() == 0:
            pixmap = QPixmap(size)
            pixmap.fill(QColor("#20242d"))
        canvas = QPixmap(pixmap.size())
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setOpacity(0.72)
        painter.drawPixmap(0, 0, pixmap)
        if len(items) > 1:
            painter.setOpacity(1.0)
            painter.setPen(QColor("#ffffff"))
            font = painter.font()
            font.setPixelSize(max(10, size.height() // 3))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, f"+{len(items) - 1}")
        painter.end()
        return canvas

    def mimeData(self, items):
        data = QMimeData()
        ids, urls = [], []
        for item in items:
            kind, key, path = item.data(Qt.ItemDataRole.UserRole)
            if kind == "item":
                ids.append(key)
                if path and os.path.isfile(path):
                    urls.append(QUrl.fromLocalFile(path))
        data.setData(MEDIA_MIME, "\n".join(ids).encode("utf-8"))
        data.setUrls(urls)
        return data

    def _accepts(self, mime):
        return mime.hasFormat(MEDIA_MIME) or mime.hasUrls()

    def dragEnterEvent(self, event):
        event.acceptProposedAction() if self._accepts(event.mimeData()) else event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction() if self._accepts(event.mimeData()) else event.ignore()

    def dropEvent(self, event):
        target = self.itemAt(event.position().toPoint())
        bin_id = ""
        if target is not None:
            kind, key, _path = target.data(Qt.ItemDataRole.UserRole)
            if kind == "bin":
                bin_id = key
            elif kind == "up":
                bin_id = ROOT_BIN
        mime = event.mimeData()
        if mime.hasFormat(MEDIA_MIME):
            ids = bytes(mime.data(MEDIA_MIME)).decode("utf-8").split("\n")
            self.items_moved.emit([i for i in ids if i], bin_id)
        elif mime.hasUrls():
            self.files_dropped.emit([u.toLocalFile() for u in mime.urls() if u.isLocalFile()], bin_id)
        event.acceptProposedAction()


class ProjectPanel(QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.w = window
        self.setObjectName("projectPanel")
        self.setAcceptDrops(True)
        self._signals = _ProbeSignals()
        self._signals.done.connect(self._probed)
        self._thumbs = {}        # norm path -> QImage, filled by the worker threads
        self._pending = set()
        self._current_bin = ""
        self._signature = None
        self._grid = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        bar = QHBoxLayout()
        self.import_button = QPushButton("Import")
        self.import_button.setToolTip("Add files to the project (Ctrl+click a file in the list to insert it)")
        self.bin_button = QToolButton()
        self.bin_button.setText("New Folder")
        self.bin_button.setToolTip("Create a folder inside the current project folder")
        self.view_button = QToolButton()
        self.view_button.setText("List")
        self.view_button.setToolTip("Switch between grid and list view")
        bar.addWidget(self.import_button)
        bar.addWidget(self.bin_button)
        bar.addStretch(1)
        bar.addWidget(self.view_button)
        layout.addLayout(bar)
        self.crumb = QLabel("Project")
        self.crumb.setStyleSheet("color: #8a8f9a;")
        self.crumb.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self.crumb)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search media")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.kind_filter = QComboBox()
        for label, value in (("All media", ""), ("Video", "video"), ("Audio", "audio"),
                             ("Images", "image")):
            self.kind_filter.addItem(label, value)
        self.kind_filter.setToolTip("Show only one kind of media")
        self.sort_box = QComboBox()
        for label, value in (("Name", "name"), ("Type", "type"), ("Newest file", "date"),
                             ("Longest", "duration")):
            self.sort_box.addItem(label, value)
        self.sort_box.setToolTip("Media order")
        tools.addWidget(self.kind_filter, 1)
        tools.addWidget(self.sort_box, 1)
        layout.addLayout(tools)
        self.list = _MediaList()
        layout.addWidget(self.list, 1)
        self.hint = QLabel("Drop files here or press Import")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet("color: #8a8f9a;")
        layout.addWidget(self.hint)
        self._apply_view_mode()

        self.import_button.clicked.connect(self.import_dialog)
        self.bin_button.clicked.connect(self.new_folder)
        self.view_button.clicked.connect(self.toggle_view)
        self.search.textChanged.connect(lambda _t: self.rebuild(force=True))
        self.kind_filter.currentIndexChanged.connect(lambda _i: self.rebuild(force=True))
        self.sort_box.currentIndexChanged.connect(lambda _i: self.rebuild(force=True))
        self.list.itemDoubleClicked.connect(self._double_clicked)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.files_dropped.connect(self._files_dropped)
        self.list.items_moved.connect(self._items_moved)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.rebuild)
        window.bridge.changed.connect(self.schedule)
        window.projects.project_changed.connect(self._project_changed)
        window.waveforms.library.loaded.connect(lambda _p: self.schedule())

    # ---- helpers ---------------------------------------------------------
    @property
    def project(self):
        return self.w.projects.project

    @property
    def engine(self):
        return self.w.engine

    def schedule(self, *_):
        self._timer.start()

    def _project_changed(self):
        self._current_bin = ""
        self._signature = None
        self.rebuild(force=True)

    def _cache_dir(self):
        folder = self.project.folder if self.project else None
        return os.path.join(folder, "Cache", "thumbs") if folder else None

    def _used_paths(self):
        return {norm_path(c.source_path) for c in self.engine.timeline.all_clips() if c.source_path}

    def _sync_library(self):
        """Every clip's media belongs in the library, however it got onto the timeline."""
        project = self.project
        for path in {c.source_path for c in self.engine.timeline.all_clips() if c.source_path}:
            project_io.register_media(self.engine, project.library, path, self.w.waveforms.library.duration_seconds)

    def _is_missing(self, item):
        return bool(item.path) and (item.path in self.engine.missing_paths or not os.path.exists(item.path))

    # ---- building the list --------------------------------------------------
    def rebuild(self, force=False):
        project = self.project
        if project is None:
            self.list.clear()
            self.hint.setVisible(False)
            self._signature = None
            return
        self._sync_library()
        library = project.library
        used = self._used_paths()
        needle = self.search.text().strip().lower()
        signature = (self._current_bin, needle, self._grid, tuple(sorted(used)), len(self._thumbs),
                     tuple((i.id, i.name, i.bin, i.duration, i.width, i.path) for i in library.items.values()),
                     tuple((k, v, library.bin_parent(k)) for k, v in library.bins.items()),
                     len(self.engine.missing_paths))
        if signature == self._signature and not force:
            return
        self._signature = signature
        selected = {self._key(i) for i in self.list.selectedItems()}
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        chain = library.bin_chain(self._current_bin) if self._current_bin else []
        if self._current_bin and self._current_bin not in library.bins:
            # The current folder was deleted elsewhere; never leave the panel stranded.
            self._current_bin = ""
            chain = []
        self.crumb.setText("Project" + "".join(
            f"  /  {library.bins.get(bid, 'Folder')}" for bid in chain))
        self.crumb.setToolTip(self.crumb.text())
        if self._current_bin and not needle:
            up = QListWidgetItem("Back")
            up.setData(Qt.ItemDataRole.UserRole, ("up", "", ""))
            up.setIcon(self._glyph_icon("<"))
            parent = library.bin_parent(self._current_bin)
            up.setToolTip("Back to " + (library.bins.get(parent, "the project root")))
            self.list.addItem(up)
        if not needle:
            for bin_id in library.child_bins(self._current_bin):
                name = library.bins.get(bin_id, "Folder")
                count = sum(1 for i in library.items.values() if i.bin == bin_id)
                entry = QListWidgetItem(f"{name}\n{count} item{'s' if count != 1 else ''}" if self._grid
                                        else f"{name}   ({count})")
                entry.setData(Qt.ItemDataRole.UserRole, ("bin", bin_id, ""))
                entry.setIcon(self._glyph_icon("\u25a4"))
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsDropEnabled)
                self.list.addItem(entry)
        kind = self.kind_filter.currentData()
        order = self.sort_box.currentData()
        shown = 0
        for media in self._arrange(library.items.values(), kind, order):
            if needle:
                if needle not in media.name.lower():
                    continue
            elif media.bin != self._current_bin:
                continue
            shown += 1
            self.list.addItem(self._make_item(media, norm_path(media.path) in used))
        for index in range(self.list.count()):
            if self._key(self.list.item(index)) in selected:
                self.list.item(index).setSelected(True)
        self.list.setUpdatesEnabled(True)
        self.hint.setVisible(not len(library) and not library.bins)

    @staticmethod
    def _key(item):
        return item.data(Qt.ItemDataRole.UserRole)[:2]

    @staticmethod
    def _arrange(items, kind="", order="name"):
        """Filter by media type and order the list; dates come from the files themselves."""
        rows = [item for item in items if not kind or item.type == kind]
        if order == "type":
            return sorted(rows, key=lambda item: (item.type, item.name.lower()))
        if order == "duration":
            return sorted(rows, key=lambda item: (-float(item.duration or 0.0), item.name.lower()))
        if order == "date":
            def modified(item):
                try:
                    return -os.path.getmtime(item.path) if item.path else 0.0
                except OSError:
                    return 0.0
            return sorted(rows, key=lambda item: (modified(item), item.name.lower()))
        return sorted(rows, key=lambda item: item.name.lower())

    def _make_item(self, media, used):
        missing = self._is_missing(media)
        detail = []
        if media.duration:
            detail.append(format_duration(media.duration))
        if media.width and media.height:
            detail.append(f"{media.width}x{media.height}")
        if media.fps:
            detail.append(f"{media.fps:g} fps")
        info = "  \u00b7  ".join(detail)
        if self._grid:
            text = media.name + (f"\n{info}" if info else "")
        else:
            text = f"{media.name}\n{info or media.type}" + ("   (missing)" if missing else "")
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, ("item", media.id, media.path))
        item.setIcon(self._media_icon(media, used, missing))
        item.setToolTip(media.path + ("\nFile not found" if missing else "") + ("\nUsed in the timeline" if used else ""))
        if missing:
            item.setForeground(QColor("#ffb020"))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
        return item

    def _icon_size(self):
        return GRID_ICON if self._grid else LIST_ICON

    def _glyph_icon(self, glyph):
        size = self._icon_size()
        canvas = QPixmap(size)
        canvas.fill(QColor(38, 41, 49))
        painter = QPainter(canvas)
        painter.setPen(QColor("#b8bdc9"))
        font = painter.font()
        font.setPixelSize(max(12, size.height() // 2))
        painter.setFont(font)
        painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        painter.end()
        return QIcon(canvas)

    def _media_icon(self, media, used, missing):
        size = self._icon_size()
        canvas = QPixmap(size)
        canvas.fill(QColor(24, 26, 31))
        painter = QPainter(canvas)
        image = self._thumbs.get(norm_path(media.path))
        if image is not None and not image.isNull():
            pix = QPixmap.fromImage(image).scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation)
            painter.setOpacity(0.4 if missing else 1.0)
            painter.drawPixmap((size.width() - pix.width()) // 2, (size.height() - pix.height()) // 2, pix)
            painter.setOpacity(1.0)
        else:
            glyph = {"audio": "\u266a", "image": "\u25a3"}.get(media.type, "\u25b6")
            painter.setPen(QColor("#7d8493"))
            font = painter.font()
            font.setPixelSize(max(12, size.height() // 2))
            painter.setFont(font)
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
            if image is None and media.type != "audio" and not missing:
                self._request_thumb(media)
        badge = max(4, size.height() // 5)
        painter.setPen(Qt.PenStyle.NoPen)
        if used:
            painter.setBrush(QColor("#3ddc84"))
            painter.drawEllipse(size.width() - badge - 3, 3, badge, badge)
        if missing:
            painter.setBrush(QColor("#ffb020"))
            painter.setPen(QPen(QColor("#3a2a00"), 1))
            painter.drawPolygon(QPolygonF([self._pt(3 + badge / 2, size.height() - badge - 3),
                                           self._pt(3 + badge, size.height() - 3),
                                           self._pt(3, size.height() - 3)]))
        painter.end()
        return QIcon(canvas)

    @staticmethod
    def _pt(x, y):
        from PyQt6.QtCore import QPointF
        return QPointF(float(x), float(y))

    # ---- thumbnails ------------------------------------------------------
    def _request_thumb(self, media):
        key = norm_path(media.path)
        if key in self._pending or not os.path.isfile(media.path):
            return
        self._pending.add(key)
        QThreadPool.globalInstance().start(_ProbeJob(media.id, media.path, self._cache_dir(), self._signals))

    def _probed(self, item_id, info, image):
        project = self.project
        media = project.library.items.get(item_id) if project else None
        if media is None:
            return
        self._pending.discard(norm_path(media.path))
        if info and not media.width:
            media.width, media.height = info.get("width", 0), info.get("height", 0)
            media.fps, media.duration = info.get("fps", 0.0), info.get("duration", 0.0)
        # A failed probe still gets an entry so a broken file is not retried forever.
        self._thumbs[norm_path(media.path)] = image if image is not None else QImage()
        self.schedule()

    # ---- view mode ---------------------------------------------------------
    def toggle_view(self):
        self._grid = not self._grid
        self._apply_view_mode()
        self.rebuild(force=True)

    def _apply_view_mode(self):
        if self._grid:
            self.list.setViewMode(QListWidget.ViewMode.IconMode)
            self.list.setIconSize(GRID_ICON)
            self.list.setGridSize(QSize(GRID_ICON.width() + 24, GRID_ICON.height() + 52))
            self.list.setWordWrap(True)
            self.list.setSpacing(4)
            self.view_button.setText("List")
        else:
            self.list.setViewMode(QListWidget.ViewMode.ListMode)
            self.list.setIconSize(LIST_ICON)
            self.list.setGridSize(QSize())
            self.list.setSpacing(2)
            self.view_button.setText("Grid")
        self.list.setMovement(QListWidget.Movement.Static)

    # ---- importing -----------------------------------------------------------
    def import_dialog(self):
        if self.project is None:
            return
        patterns = " ".join("*" + e for e in ALL_EXT)
        paths = open_files(self, "Import media", "", f"Media ({patterns});;All files (*)")
        if paths:
            self.add_files(paths, self._current_bin)

    def add_files(self, paths, bin_id=""):
        """Add files to the library only; they reach the timeline when dragged or inserted."""
        project = self.project
        if project is None:
            return 0
        added, skipped = 0, []
        for path in paths:
            if not os.path.isfile(path) or not path.lower().endswith(ALL_EXT):
                skipped.append(os.path.basename(path))
                continue
            if project.library.by_path(path) is not None:
                continue
            item = project.library.add(path, bin_id=bin_id)
            if item.type == "audio":
                self.w.waveforms.request(item.path)
            added += 1
        if added:
            self.w.projects.mark_dirty()
        if skipped:
            self.w.toast_message("Ignored unsupported: " + ", ".join(skipped[:3]), kind="error")
        self.rebuild(force=True)
        return added

    def _files_dropped(self, paths, bin_id):
        target = self._current_bin if bin_id == "" else ("" if bin_id == ROOT_BIN else bin_id)
        self.add_files(paths, target)

    def _items_moved(self, ids, bin_id):
        if self.project is None or bin_id == "":
            return  # dropped on empty space: nothing to do
        self.project.library.move_to_bin(ids, "" if bin_id == ROOT_BIN else bin_id)
        self.w.projects.mark_dirty()
        self.rebuild(force=True)

    # ---- actions ---------------------------------------------------------------
    def new_folder(self):
        """Create a virtual project folder inside the folder currently open."""
        if self.project is None:
            return
        parent = self._current_bin if self._current_bin in self.project.library.bins else ""
        name, ok = QInputDialog.getText(self, "New Folder", "Name:", text="New Folder")
        if ok and name.strip():
            self.project.library.add_bin(name.strip(), parent)
            self.w.projects.mark_dirty()
            self.rebuild(force=True)

    # Kept as an alias for callers/tests written before the bin terminology change.
    new_bin = new_folder

    def _selected_media(self):
        out = []
        for item in self.list.selectedItems():
            kind, key, _p = item.data(Qt.ItemDataRole.UserRole)
            if kind == "item" and key in self.project.library.items:
                out.append(self.project.library.items[key])
        return out

    def insert_at_playhead(self, media_items):
        """Insert by media id: no re-import, and a video with sound lands as a linked pair."""
        playhead = self.engine.session.playhead
        usable = []
        for media in media_items:
            if self._is_missing(media):
                self.w.toast_message(f"{media.name} is missing - use Relink", kind="error")
                continue
            usable.append(media)
        if usable:
            self.w.insert_media_items(usable, playhead, None, True)

    def _double_clicked(self, item):
        kind, key, _p = item.data(Qt.ItemDataRole.UserRole)
        if kind == "bin":
            self._current_bin = key
            self.rebuild(force=True)
        elif kind == "up":
            library = self.project.library
            self._current_bin = library.bin_parent(self._current_bin)
            self.rebuild(force=True)
        elif key in self.project.library.items:
            self.insert_at_playhead([self.project.library.items[key]])

    def _menu(self, pos):
        if self.project is None:
            return
        item = self.list.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction("Import...", self.import_dialog)
            menu.addAction("New Folder", self.new_folder)
            menu.exec(self.list.viewport().mapToGlobal(pos))
            return
        kind, key, _path = item.data(Qt.ItemDataRole.UserRole)
        if kind == "bin":
            menu.addAction("Open", lambda: self._double_clicked(item))
            menu.addAction("Rename...", lambda: self._rename_bin(key))
            menu.addAction("Delete Folder...", lambda: self._delete_bin(key))
        elif kind == "item":
            if not item.isSelected():
                self.list.clearSelection()
                item.setSelected(True)
            media = self._selected_media()
            if not media:
                return
            first = media[0]
            menu.addAction("Insert at playhead", lambda: self.insert_at_playhead(media))
            menu.addAction("Show in folder", lambda: self._show_in_folder(first))
            menu.addAction("Rename...", lambda: self._rename(first)).setEnabled(len(media) == 1)
            if any(self._is_missing(m) for m in media):
                menu.addAction("Relink...", lambda: self._relink(next(m for m in media if self._is_missing(m))))
            library = self.project.library
            if library.bins:
                move = menu.addMenu("Move to Folder")
                move.addAction("Project Root", lambda: self._move([m.id for m in media], ""))
                for bin_id, _name in library.bins.items():
                    chain = library.bin_chain(bin_id)
                    label = "    " * (len(chain) - 1) + library.bins.get(bin_id, "Folder")
                    move.addAction(label, lambda _c=False, b=bin_id:
                                   self._move([m.id for m in media], b))
            menu.addSeparator()
            menu.addAction("Remove from Project", lambda: self.remove(media))
            menu.addAction("Delete File from Computer...", lambda: self.delete_files(media))
        else:
            return
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _move(self, ids, bin_id):
        self.project.library.move_to_bin(ids, bin_id)
        self.w.projects.mark_dirty()
        self.rebuild(force=True)

    def _show_in_folder(self, media):
        from .start_window import show_in_folder
        if os.path.exists(media.path):
            show_in_folder(media.path)
        else:
            folder = os.path.dirname(media.path)
            if os.path.isdir(folder):
                from PyQt6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            else:
                self.w.toast_message("The folder no longer exists", kind="error")

    def _rename(self, media):
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=media.name)
        if ok and name.strip() and name.strip() != media.name:
            media.name = name.strip()
            self.w.projects.mark_dirty()
            self.rebuild(force=True)

    def _rename_bin(self, bin_id):
        library = self.project.library
        name, ok = QInputDialog.getText(self, "Rename Folder", "Name:", text=library.bins.get(bin_id, ""))
        if ok and name.strip():
            library.bins[bin_id] = name.strip()
            self.w.projects.mark_dirty()
            self.rebuild(force=True)

    def _delete_bin(self, bin_id):
        """Delete a folder without ever deleting a file from disk.

        Empty folders disappear immediately. A non-empty folder asks whether its contents
        move to the parent folder or leave the project with it; Cancel keeps everything.
        """
        library = self.project.library
        if bin_id not in library.bins:
            return
        subtree = set(library._bin_subtree(bin_id))
        media = [item for item in library.items.values() if item.bin in subtree]
        if media or any(bid != bin_id for bid in subtree):
            box = QMessageBox(self)
            box.setWindowTitle("Delete Folder")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(f"Delete “{library.bins.get(bin_id, 'Folder')}” and "
                        f"{len(library._bin_subtree(bin_id)) - 1} subfolder(s)?")
            box.setInformativeText(
                f"It contains {len(media)} media item(s). Source files on disk are never "
                "deleted by this action.")
            keep = box.addButton("Move Contents to Parent",
                                 QMessageBox.ButtonRole.AcceptRole)
            discard = box.addButton("Remove Folder and Media from Project",
                                    QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is keep:
                library.remove_bin(bin_id, remove_items=False)
            elif box.clickedButton() is discard:
                used = self._used_paths()
                used_media = [item for item in media if norm_path(item.path) in used]
                if used_media:
                    answer = QMessageBox.question(
                        self, "Media Used on Timeline",
                        f"{len(used_media)} of these items are used by clips on the timeline.\n"
                        "Those clips will be removed too (undoable). Continue?")
                    if answer != QMessageBox.StandardButton.Yes:
                        return
                if media:
                    self.remove(media)
                library.remove_bin(bin_id, remove_items=True)
            else:
                return
        else:
            library.remove_bin(bin_id)
        if self._current_bin and self._current_bin not in library.bins:
            self._current_bin = ""
        self.w.projects.mark_dirty()
        self.rebuild(force=True)

    def delete_files(self, media_items):
        """Move selected source files to the OS Recycle Bin after explicit confirmation."""
        from ..project.storage import trash_path
        items = [item for item in media_items or () if item.path and os.path.isfile(item.path)]
        if not items:
            self.w.toast_message("Those items have no file on disk to delete", kind="error")
            return False
        used = self._used_paths()
        used_count = sum(1 for item in items if norm_path(item.path) in used)
        paths = "\n".join(item.path for item in items[:8])
        if len(items) > 8:
            paths += f"\n… and {len(items) - 8} more"
        warning = (f"\n\nWarning: {used_count} item(s) are used by timeline clips." if used_count else "")
        answer = QMessageBox.warning(
            self, "Delete File from Computer?",
            "This moves the actual source file(s) to the Recycle Bin, not the editor's "
            "Undo history:\n\n" + paths + warning +
            "\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        deleted, failed = [], []
        for item in items:
            ok, message = trash_path(item.path)
            if ok:
                deleted.append(item)
            else:
                failed.append(f"{item.name}: {message}")
        if failed:
            QMessageBox.warning(self, "Some Files Were Not Deleted", "\n".join(failed[:6]))
        if deleted:
            # Only remove references for files that actually moved to the Bin.
            self.remove(deleted)
            self.w.toast_message(f"Moved {len(deleted)} file(s) to the Recycle Bin")
        return bool(deleted)

    def _relink(self, media):
        path = open_file(self, f"Locate {media.name}", os.path.dirname(media.path), "All files (*)")
        if path:
            self._thumbs.pop(norm_path(media.path), None)
            self.w.projects.relink(media.id, path)

    def remove(self, media_items):
        project = self.project
        timeline = self.engine.timeline
        removing = {norm_path(m.path) for m in media_items}
        clip_ids = [c.id for c in timeline.all_clips() if norm_path(c.source_path) in removing]
        if clip_ids:
            answer = QMessageBox.question(
                self, "Remove media",
                f"{len(media_items)} item(s) are used by {len(clip_ids)} clip(s) in the timeline.\n"
                "Removing them also deletes those clips (you can undo this).\n\nRemove anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return False
            self.engine.session.stack.run("Remove media", lambda: timeline.delete_clips(clip_ids))
        for media in media_items:
            project.library.remove(media.id)
            self._thumbs.pop(norm_path(media.path), None)
        self.w.projects.mark_dirty()
        self.rebuild(force=True)
        self.w._refresh_audio()
        self.w._request_preview()
        self.w.timeline_panel.canvas.update()
        return True

    # ---- OS drops onto the whole panel ----------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_files([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()], self._current_bin)
        event.acceptProposedAction()
