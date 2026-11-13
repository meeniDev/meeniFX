"""Project panel: the project's media as a list or grid, with bins, thumbnails and drag and drop."""
import hashlib
import os

import cv2
from PyQt6.QtCore import (QEvent, QMimeData, QObject, QPoint, QRectF, QRunnable, QSize, Qt, QThreadPool,
                          QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QBrush, QColor, QDrag, QFontMetrics, QIcon, QImage, QPainter, QPainterPath,
                         QPen, QPixmap)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QInputDialog,
                             QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QStackedLayout, QToolButton, QVBoxLayout, QWidget)

from .. import tokens
from ..constants import AUDIO_EXT, COMPOSITION_MIME, IMAGE_EXT, MEDIA_MIME, VIDEO_EXT
from ..icons import icon as glyph, paint as paint_glyph
from ..project import io as project_io
from ..project.media import media_type_for, norm_path
from .cards import CardDelegate, mix
from .components import Button, Dropdown, EmptyState, IconButton, SearchField
from .filedialogs import open_file, open_files
from .themed import bind_theme

ROOT_BIN = "<root>"
GRID_ICON = QSize(96, 54)
LIST_ICON = QSize(48, 28)
GRID_CELL_MIN = 104     # narrowest grid cell; the grid widens cells to fill the row
GRID_CELL_H = 100
ICON_SCALE = 2          # media pictures are drawn at 2x so cards stay sharp on high-DPI screens
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
        # QAbstractItemView resets the viewport's accept flag inside setDragDropMode;
        # set it afterwards or a real platform drag never reaches the folder rows.
        self.viewport().setAcceptDrops(True)
        self.viewport().setMouseTracking(True)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(True)
        self.setMouseTracking(True)
        self.setItemDelegate(CardDelegate(self, grid=True))
        self._drag_press = None
        self._drop_item = None
        self.current_bin = ""
        self.viewport().installEventFilter(self)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setMovement(QListWidget.Movement.Static)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setFrameShape(QListWidget.Shape.NoFrame)

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction

    def grid_cell(self):
        """Cells share the row width evenly, so cards fill the panel instead of leaving a ragged gap."""
        available = max(GRID_CELL_MIN, self.viewport().width() - 2 * self.spacing() - 2)
        columns = max(1, available // (GRID_CELL_MIN + 2 * self.spacing()))
        return QSize(available // columns - 1, GRID_CELL_H + 20)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.viewMode() == QListWidget.ViewMode.IconMode:
            cell = self.grid_cell()
            if cell != self.gridSize():
                self.setGridSize(cell)

    def eventFilter(self, watched, event):
        """Route platform drag events from the viewport to this list's handlers.

        QAbstractItemView normally translates these, but its internal dispatch can be
        bypassed on some Windows/Qt combinations; an explicit event filter keeps folder
        drops working even when the viewport was just created or re-created by a
        view-mode change.  The filter is deliberately limited to drag/drop events.
        """
        if watched is self.viewport():
            kind = event.type()
            if kind == QEvent.Type.DragEnter:
                self.dragEnterEvent(event)
                return True
            if kind == QEvent.Type.DragMove:
                self.dragMoveEvent(event)
                return True
            if kind == QEvent.Type.DragLeave:
                self._clear_drop_item()
                event.accept()
                return True
            if kind == QEvent.Type.Drop:
                self.dropEvent(event)
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        # QAbstractScrollArea can recreate/reset the viewport while the widget is being
        # shown, which clears the accept flag set in __init__.  A real platform drag is
        # delivered to the viewport, so restore it once the widget is actually visible.
        super().showEvent(event)
        self.viewport().setAcceptDrops(True)
        self.viewport().setMouseTracking(True)

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
                if item is not None and self._kind(item) in ("item", "comp"):
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
        """Every selected media/composition item, or just the one under the pointer."""
        chosen = [i for i in self.selectedItems() if self._kind(i) in ("item", "comp")]
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
            pixmap.fill(QColor(tokens.COLORS["neutral_stage"]))
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
        ids, comp_ids, urls = [], [], []
        for item in items:
            kind, key, path = item.data(Qt.ItemDataRole.UserRole)
            if kind == "item":
                ids.append(key)
                if path and os.path.isfile(path):
                    urls.append(QUrl.fromLocalFile(path))
            elif kind == "comp":
                # Compositions travel as references, never as media files.
                comp_ids.append(key)
        data.setData(MEDIA_MIME, "\n".join(ids).encode("utf-8"))
        data.setData(COMPOSITION_MIME, "\n".join(comp_ids).encode("utf-8"))
        data.setUrls(urls)
        return data

    def _accepts(self, mime):
        return mime.hasFormat(MEDIA_MIME) or mime.hasUrls()

    def _target_bin_at(self, position):
        """Destination for a drop at ``position`` (viewport coordinates).

        A folder takes its own id and Back takes its parent.  Empty space means the
        folder currently being viewed, so moving a clip between bins does not require
        hitting a small row exactly.
        """
        target = self.itemAt(position)
        if target is None:
            return self.current_bin
        kind, key, _path = target.data(Qt.ItemDataRole.UserRole)
        if kind == "bin":
            return key
        if kind == "up":
            return key  # parent bin id, or "" for a top-level folder
        return self.current_bin

    def _set_drop_item(self, item):
        """Highlight the one folder that will receive the drop, without changing selection."""
        if item is self._drop_item:
            return
        if self._drop_item is not None:
            try:
                self._drop_item.setBackground(QBrush())
            except RuntimeError:
                pass
        self._drop_item = item
        if item is not None:
            item.setBackground(QColor(tokens.COLORS["accent_soft"]))
        self.viewport().update()

    def _clear_drop_item(self):
        self._set_drop_item(None)

    def dragEnterEvent(self, event):
        if not self._accepts(event.mimeData()):
            self._clear_drop_item()
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        self._set_drop_item(target if target is not None and self._kind(target) in ("bin", "up", "root")
                            else None)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if not self._accepts(event.mimeData()):
            self._clear_drop_item()
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        self._set_drop_item(target if target is not None and self._kind(target) in ("bin", "up", "root")
                            else None)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._clear_drop_item()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        bin_id = self._target_bin_at(event.position().toPoint())
        self._clear_drop_item()
        mime = event.mimeData()
        if mime.hasFormat(MEDIA_MIME):
            ids = bytes(mime.data(MEDIA_MIME)).decode("utf-8").split("\n")
            self.items_moved.emit([i for i in ids if i], bin_id)
        elif mime.hasUrls():
            self.files_dropped.emit([u.toLocalFile() for u in mime.urls() if u.isLocalFile()], bin_id)
        event.setDropAction(Qt.DropAction.CopyAction)
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
        layout.setContentsMargins(tokens.SP2, tokens.SP2, tokens.SP2, tokens.SP2)
        layout.setSpacing(tokens.SP2)
        bar = QHBoxLayout()
        bar.setSpacing(tokens.SP1)
        self.import_button = Button("Import", "primary", None,
                                    "Add files to the project (Ctrl+click a file in the list to insert it)")
        bind_theme(self.import_button, lambda b: b.setIcon(glyph("import", tokens.COLORS["on_accent"])))
        self.bin_button = IconButton("folder_new", "Create a folder inside the current project folder")
        self.bin_button.setText("")
        self.bin_button.setAccessibleName("New Folder")
        self.back_button = QToolButton()
        self.back_button.setProperty("role", "tool")
        self.back_button.setText("Back")
        self.back_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bind_theme(self.back_button, lambda b: b.setIcon(glyph("up")))
        self.back_button.setToolTip("Go up one project folder")
        self.back_button.setMinimumHeight(tokens.CONTROL_H)
        self.back_button.setVisible(False)
        self.view_button = IconButton("list", "Switch between grid and list view")
        bar.addWidget(self.import_button)
        bar.addWidget(self.bin_button)
        bar.addWidget(self.back_button)
        bar.addStretch(1)
        bar.addWidget(self.view_button)
        layout.addLayout(bar)
        self.crumb = QLabel("Project")
        self.crumb.setProperty("quiet", True)
        self.crumb.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self.crumb)
        self.search = SearchField("Search media")
        layout.addWidget(self.search)
        tools = QHBoxLayout()
        tools.setSpacing(tokens.SP1)
        self.kind_filter = Dropdown((("All media", ""), ("Video", "video"), ("Audio", "audio"),
                                     ("Images", "image")))
        self.kind_filter.setToolTip("Show only one kind of media")
        self.sort_box = Dropdown((("Name", "name"), ("Type", "type"), ("Newest file", "date"),
                                  ("Longest", "duration")))
        self.sort_box.setToolTip("Media order")
        tools.addWidget(self.kind_filter, 1)
        tools.addWidget(self.sort_box, 1)
        layout.addLayout(tools)
        self.list = _MediaList()
        self.hint = EmptyState("No imported media", "Drop videos, pictures or music here, "
                               "or press Import.", "folder")
        self.stack = QStackedLayout()
        self.stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.hint)
        self.hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hint.hide()
        layout.addLayout(self.stack, 1)
        self._apply_view_mode()

        self.import_button.clicked.connect(self.import_dialog)
        self.bin_button.clicked.connect(self.new_folder)
        self.back_button.clicked.connect(self.go_back)
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
        bind_theme(self, lambda panel: panel._theme_changed())

    def _theme_changed(self):
        """Card pictures are pre-rendered in theme colours, so a switch rebuilds them."""
        self._signature = None
        if self.project is not None:
            self.rebuild(force=True)
        self.list.viewport().update()
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
            self.hint.hide()
            self._signature = None
            return
        self._sync_library()
        library = project.library
        used = self._used_paths()
        needle = self.search.text().strip().lower()
        signature = (self._current_bin, needle, self._grid, tuple(sorted(used)), len(self._thumbs),
                     tuple((i.id, i.name, i.bin, i.duration, i.width, i.path) for i in library.items.values()),
                     tuple((k, v, library.bin_parent(k)) for k, v in library.bins.items()),
                     tuple((c.id, c.name, getattr(c, "bin_folder", ""), c.width, c.height,
                            c.fps, c.duration) for c in self.engine.compositions.all()),
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
        self.back_button.setVisible(bool(self._current_bin))
        self.list.current_bin = self._current_bin
        if self._current_bin and not needle:
            parent = library.bin_parent(self._current_bin)
            up = QListWidgetItem("Back")
            up.setData(Qt.ItemDataRole.UserRole, ("up", parent, ""))
            up.setIcon(self._glyph_icon("up"))
            up.setToolTip("Back to " + (library.bins.get(parent, "the project root")))
            up.setFlags(up.flags() | Qt.ItemFlag.ItemIsDropEnabled)
            self.list.addItem(up)
        if not needle:
            for bin_id in library.child_bins(self._current_bin):
                name = library.bins.get(bin_id, "Folder")
                count = sum(1 for i in library.items.values() if i.bin == bin_id)
                entry = QListWidgetItem(f"{name}\n{count} item{'s' if count != 1 else ''}" if self._grid
                                        else f"{name}   ({count})")
                entry.setData(Qt.ItemDataRole.UserRole, ("bin", bin_id, ""))
                entry.setIcon(self._folder_icon())
                entry.setToolTip(f"{name}\nFolder \u00b7 {count} item{'s' if count != 1 else ''}"
                                 "\nDouble-click to open")
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsDropEnabled)
                self.list.addItem(entry)
        kind = self.kind_filter.currentData()
        order = self.sort_box.currentData()
        shown = 0
        # Compositions are project assets with their own icon; they are never mixed up
        # with imported media because their kind is "comp" everywhere.
        for comp in sorted(self.engine.compositions.all(), key=lambda item: item.name.lower()):
            if needle:
                if needle not in comp.name.lower():
                    continue
            elif getattr(comp, "bin_folder", "") != self._current_bin:
                continue
            if kind and kind != "video":
                continue
            shown += 1
            self.list.addItem(self._make_composition_item(comp))
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
        self._show_empty_state(library, needle, shown)

    @staticmethod
    def _key(item):
        return item.data(Qt.ItemDataRole.UserRole)[:2]

    def _show_empty_state(self, library, needle, shown):
        """A friendly message instead of a blank list; drops still land on the panel."""
        folders = sum(1 for i in range(self.list.count())
                      if self.list.item(i).data(Qt.ItemDataRole.UserRole)[0] == "bin")
        if shown or folders:
            self.hint.hide()
            return
        if not len(library) and not library.bins:
            self.hint.set_text("No imported media", "Drop videos, pictures or music here, "
                                                        "or press Import.")
        elif needle or self.kind_filter.currentData():
            self.hint.set_text("Nothing matches", "Try another name, or show all kinds of media.")
        elif self._current_bin:
            self.hint.set_text("This folder is empty", "Drop media here, or drag it in from another folder.")
        else:
            self.hint.set_text("Nothing here yet", "Press Import to add media.")
        self.hint.show()

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
        # Long names are elided in the middle (the extension stays readable) and never
        # wrap into a third or fourth line; the tooltip always has the full name and path.
        available = self._icon_size().width() + 24 if self._grid else max(160, self.list.width() - 80)
        display_name = QFontMetrics(self.list.font()).elidedText(
            media.name, Qt.TextElideMode.ElideMiddle, max(40, available))
        if self._grid:
            text = display_name + (f"\n{info}" if info else "")
        else:
            text = f"{display_name}\n{info or media.type}" + ("   (missing)" if missing else "")
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, ("item", media.id, media.path))
        item.setIcon(self._media_icon(media, used, missing))
        item.setToolTip(media.name + "\n" + media.path +
                        ("\nFile not found" if missing else "") +
                        ("\nUsed in the timeline" if used else ""))
        if missing:
            item.setForeground(QColor(tokens.COLORS["warning"]))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
        return item

    def _make_composition_item(self, comp):
        fps = float(comp.fps or 30)
        detail = [f"{comp.width}x{comp.height}", f"{fps:g} fps",
                  format_duration((comp.duration or 0) / max(1.0, fps))]
        info = "  \u00b7  ".join(part for part in detail if part)
        available = self._icon_size().width() + 24 if self._grid else max(160, self.list.width() - 80)
        name = QFontMetrics(self.list.font()).elidedText(
            comp.name, Qt.TextElideMode.ElideMiddle, max(40, available))
        item = QListWidgetItem(name + (f"\n{info}" if info else ""))
        item.setData(Qt.ItemDataRole.UserRole, ("comp", comp.id, ""))
        item.setIcon(self._composition_icon())
        item.setToolTip(f"{comp.name}\nComposition \u00b7 {info}"
                        "\nDouble-click to open, or drag onto the timeline")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
        return item

    def _composition_icon(self):
        """Stacked frames: a composition reads as a nested timeline, not as media."""
        size = self._icon_size()
        canvas = self._canvas(size, QColor(tokens.COLORS["neutral_stage"]))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = QColor(tokens.COLORS["accent"])
        w, h = float(size.width()), float(size.height())
        fw, fh = w * 0.56, h * 0.60
        painter.setPen(QPen(accent, max(1.0, h / 40)))
        for index, offset in enumerate((0.0, 0.075, 0.15)):
            painter.setBrush(accent.lighter(285 - index * 35))
            rect = QRectF((w - fw) / 2 + w * 0.06 - w * offset * 0.5,
                          (h - fh) / 2 - h * offset * 0.5 + h * 0.02, fw, fh)
            painter.drawRoundedRect(rect, fh * 0.10, fh * 0.10)
        # A play mark on the front frame says "this opens into its own timeline".
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFFFFF"))
        cx, cy, side = w * 0.5, h * 0.5 + h * 0.01, min(w, h) * 0.16
        painter.drawPolygon(QPoint(int(cx - side * 0.42), int(cy - side)),
                            QPoint(int(cx - side * 0.42), int(cy + side)),
                            QPoint(int(cx + side * 0.55), int(cy)))
        painter.end()
        return QIcon(canvas)

    def _icon_size(self):
        return GRID_ICON if self._grid else LIST_ICON

    def _canvas(self, size, fill=None):
        """A transparent (or filled) pixmap drawn at ICON_SCALE for sharp high-DPI cards."""
        canvas = QPixmap(size * ICON_SCALE)
        canvas.setDevicePixelRatio(ICON_SCALE)
        canvas.fill(fill if fill is not None else Qt.GlobalColor.transparent)
        return canvas

    def _glyph_icon(self, name):
        """A card picture for navigation items (the Back card)."""
        size = self._icon_size()
        canvas = self._canvas(size)
        painter = QPainter(canvas)
        side = min(size.width(), size.height()) * 0.55
        paint_glyph(painter, name, QRectF((size.width() - side) / 2, (size.height() - side) / 2, side, side),
                    tokens.COLORS["muted"])
        painter.end()
        return QIcon(canvas)

    def _folder_icon(self):
        """A soft butter-yellow folder, sized like every media thumbnail in the current view."""
        size = self._icon_size()
        canvas = self._canvas(size)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colors = tokens.COLORS
        w, h = float(size.width()), float(size.height())
        fw = min(w * 0.62, h * 1.25)
        fh = fw * 0.72
        left, top = (w - fw) / 2, (h - fh) / 2 + h * 0.03
        line = QColor("#CFC6BA" if tokens.is_dark() else "#6B625A")
        painter.setPen(QPen(line, max(1.0, h / 40)))
        painter.setBrush(QColor(colors["butter"]).darker(112))
        back = QPainterPath()
        back.addRoundedRect(QRectF(left, top, fw, fh), fh * 0.14, fh * 0.14)
        painter.drawPath(back)
        painter.setBrush(QColor(colors["butter"]).darker(112))
        painter.drawRoundedRect(QRectF(left, top - fh * 0.12, fw * 0.42, fh * 0.3), fh * 0.1, fh * 0.1)
        painter.setBrush(QColor(colors["butter"]))
        painter.drawRoundedRect(QRectF(left - fw * 0.02, top + fh * 0.2, fw * 1.04, fh * 0.8), fh * 0.14, fh * 0.14)
        painter.end()
        return QIcon(canvas)

    def _media_icon(self, media, used, missing):
        size = self._icon_size()
        canvas = self._canvas(size, QColor(tokens.COLORS["neutral_stage"]))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        image = self._thumbs.get(norm_path(media.path))
        if image is not None and not image.isNull():
            pix = QPixmap.fromImage(image).scaled(size * ICON_SCALE, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation)
            painter.setOpacity(0.4 if missing else 1.0)
            painter.drawPixmap(int((size.width() - pix.width() / ICON_SCALE) / 2),
                               int((size.height() - pix.height() / ICON_SCALE) / 2),
                               pix.width() // ICON_SCALE, pix.height() // ICON_SCALE, pix)
            painter.setOpacity(1.0)
        else:
            name = {"audio": "music", "image": "image"}.get(media.type, "film")
            side = min(size.width(), size.height()) * 0.5
            paint_glyph(painter, name, QRectF((size.width() - side) / 2, (size.height() - side) / 2, side, side),
                        "#A9B0AD")
            if image is None and media.type != "audio" and not missing:
                self._request_thumb(media)
        badge = max(10.0, size.height() / 4.2)
        if used:
            # Green dot with a tick: "used" is readable without the colour.
            painter.setPen(QPen(QColor("#FFFFFF"), 1.2))
            painter.setBrush(QColor(tokens.COLORS["success"]))
            dot = QRectF(size.width() - badge - 3, 3, badge, badge)
            painter.drawEllipse(dot)
            paint_glyph(painter, "check", dot.adjusted(1.5, 1.5, -1.5, -1.5), "#FFFFFF")
        if missing:
            paint_glyph(painter, "warning", QRectF(3, size.height() - badge - 3, badge, badge),
                        tokens.COLORS["warning"] if tokens.is_dark() else "#F0B65A")
        painter.end()
        return QIcon(canvas)

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
            self.list.setGridSize(self.list.grid_cell())
            self.list.setSpacing(4)
            self.view_button.set_icon_name("list")
            self.view_button.setToolTip("Switch to list view")
        else:
            self.list.setViewMode(QListWidget.ViewMode.ListMode)
            self.list.setIconSize(LIST_ICON)
            self.list.setGridSize(QSize())
            self.list.setSpacing(2)
            self.view_button.set_icon_name("grid")
            self.view_button.setToolTip("Switch to grid view")
        self.list.itemDelegate().grid = self._grid
        # Newlines still make the two-line tile; word wrapping must not add a third line.
        self.list.setWordWrap(False)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
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
        library = self.project.library if self.project else None
        target = "" if bin_id in ("", ROOT_BIN) else bin_id
        if library is not None and target not in ("",) and target not in library.bins:
            target = self._current_bin if self._current_bin in library.bins else ""
        self.add_files(paths, target)

    def _items_moved(self, ids, bin_id):
        if self.project is None:
            return
        library = self.project.library
        destination = "" if bin_id in ("", ROOT_BIN) else bin_id
        if destination and destination not in library.bins:
            return
        self.project.library.move_to_bin(ids, destination)
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

    def go_back(self):
        """Leave the current folder for its parent (the project root at the top level)."""
        if self.project is None:
            return
        self._current_bin = self.project.library.bin_parent(self._current_bin)
        self.rebuild(force=True)

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
        elif kind == "comp":
            self.w.open_composition(key)
        elif kind == "up":
            self._current_bin = self.project.library.bin_parent(self._current_bin)
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
        elif kind == "comp":
            comp = self.engine.compositions.get(key)
            if comp is None:
                return
            menu.addAction("Open Composition", lambda: self.w.open_composition(comp.id))
            menu.addAction("Insert at Playhead", lambda: self._insert_comp(comp.id))
            menu.addAction("Duplicate Composition", lambda: self._duplicate_comp(comp.id))
            menu.addAction("Rename...", lambda: self._rename_comp(comp.id))
            library = self.project.library
            if library.bins:
                move = menu.addMenu("Move to Folder")
                move.addAction("Project Root", lambda: self._move_comp(comp.id, ""))
                for bin_id in library.bins:
                    chain = library.bin_chain(bin_id)
                    label = "    " * (len(chain) - 1) + library.bins.get(bin_id, "Folder")
                    move.addAction(label, lambda _c=False, b=bin_id:
                                   self._move_comp(comp.id, b))
            menu.addSeparator()
            references = self.engine.session.composition_reference_count(comp.id)
            menu.addAction(f"Delete Composition ({references} nested clip"
                           f"{'s' if references != 1 else ''})...",
                           lambda: self._delete_comp(comp.id))
        elif kind == "up":
            menu.addAction("Back", self.go_back)
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

    # ---- composition assets -------------------------------------------------
    def _insert_comp(self, comp_id):
        clip_id = self.engine.session.insert_composition(
            comp_id, self.engine.session.playhead, None)
        if clip_id is not None:
            self.w._request_preview()
            self.w._refresh_audio()
        return clip_id

    def _rename_comp(self, comp_id):
        comp = self.engine.compositions.get(comp_id)
        if comp is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Composition", "Name:", text=comp.name)
        if ok and name.strip() and name.strip() != comp.name:
            self.engine.session.rename_composition(comp_id, name.strip())
            self.w.projects.mark_dirty()
            self.rebuild(force=True)

    def _duplicate_comp(self, comp_id):
        comp = self.engine.session.duplicate_composition(comp_id)
        if comp is not None:
            self.w.projects.mark_dirty()
            self.rebuild(force=True)
            self.w.toast_message(f"Duplicated composition: {comp.name}")

    def _move_comp(self, comp_id, bin_id):
        comp = self.engine.compositions.get(comp_id)
        if comp is None:
            return
        comp.bin_folder = bin_id or ""
        self.w.projects.mark_dirty()
        self.rebuild(force=True)

    def _delete_comp(self, comp_id):
        comp = self.engine.compositions.get(comp_id)
        if comp is None:
            return
        references = self.engine.session.composition_reference_count(comp.id)
        detail = (f"{references} nested clip uses it and will be removed."
                  if references else "Nothing references it.")
        answer = QMessageBox.question(
            self, "Delete Composition",
            f"Delete \"{comp.name}\"?\n\n{detail}\n\n"
            "The clips inside it are deleted with it. This can be undone with Ctrl+Z.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if comp_id in getattr(self.w, "_composition_path", []):
            while getattr(self.w, "_composition_path", []) and self.w._composition_path[-1] != comp_id:
                self.w.back_to_parent()
            self.w.back_to_parent()
        self.engine.session.delete_composition(comp_id)
        self.w.projects.mark_dirty()
        self.rebuild(force=True)
        self.w._request_preview()
        self.w._refresh_audio()

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

    # ---- drops onto the whole panel -------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(MEDIA_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event):
        """A video/file dropped outside the list still imports into the open folder.

        An internal media drag dropped here means the project root, which makes the
        breadcrumb area a large, discoverable target for moving media out of a folder.
        """
        mime = event.mimeData()
        if mime.hasFormat(MEDIA_MIME):
            ids = bytes(mime.data(MEDIA_MIME)).decode("utf-8").split("\n")
            self._items_moved([i for i in ids if i], ROOT_BIN)
        elif mime.hasUrls():
            self.add_files([u.toLocalFile() for u in mime.urls() if u.isLocalFile()],
                           self._current_bin)
        event.acceptProposedAction()
