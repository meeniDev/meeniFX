"""Project dialogs: missing media, New Project and Project Settings."""

import os

from PyQt6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                             QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                             QSpinBox, QVBoxLayout, QWidget)

from ..project import storage
from ..project.settings import (CUSTOM_PRESET, DEFAULT_PRESET, FRAME_RATES, PRESETS, SAMPLE_RATES,
                                ProjectSettings, fps_text, preset_for)
from . import layout as dock_layout
from .filedialogs import choose_folder


class _SearchSignals(QObject):
    done = pyqtSignal(object)


class _SearchJob(QRunnable):
    def __init__(self, folder, names):
        super().__init__()
        self.folder, self.names, self.signals = folder, names, _SearchSignals()

    def run(self):
        try:
            result = storage.find_by_name(self.folder, self.names)
        except Exception:
            result = {}
        self.signals.done.emit(result)


class MissingMediaDialog(QDialog):
    """One dialog for every missing file: locate a folder to search, relink what is found, or skip."""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Missing media")
        self.setMinimumSize(560, 360)
        self.items = list(items)
        self.relinks = {}   # media id -> new path
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{len(self.items)} media file(s) could not be found. "
                                "Clips that use them stay on the timeline with a warning icon."))
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.status = QLabel("")
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.locate_button = QPushButton("Locate folder...")
        self.locate_button.clicked.connect(self._locate)
        row.addWidget(self.locate_button)
        row.addStretch(1)
        self.skip_button = QPushButton("Skip")
        self.skip_button.clicked.connect(self.reject)
        self.apply_button = QPushButton("Relink found files")
        self.apply_button.setProperty("primary", "true")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.accept)
        row.addWidget(self.skip_button)
        row.addWidget(self.apply_button)
        layout.addLayout(row)
        self._refresh()

    def _refresh(self):
        self.list.clear()
        for item in self.items:
            new = self.relinks.get(item.id)
            text = f"{item.name}   \u2192   {new}" if new else f"{item.name}   ({item.path})"
            entry = QListWidgetItem(text)
            entry.setToolTip(item.path)
            if new:
                entry.setForeground(Qt.GlobalColor.green)
            self.list.addItem(entry)
        found = len(self.relinks)
        self.status.setText(f"{found} of {len(self.items)} found" if found else "")
        self.apply_button.setEnabled(found > 0)

    def _locate(self, folder=None):
        folder = folder or choose_folder(self, "Choose a folder to search", "")
        if not folder:
            return
        self.status.setText("Searching...")
        self.locate_button.setEnabled(False)
        job = _SearchJob(folder, [os.path.basename(i.path) for i in self.items])
        job.signals.done.connect(self._on_found)
        self._job = job
        QThreadPool.globalInstance().start(job)

    def _on_found(self, found):
        self.locate_button.setEnabled(True)
        by_name = {k.lower(): v for k, v in found.items()}
        for item in self.items:
            path = by_name.get(os.path.basename(item.path).lower())
            if path:
                self.relinks[item.id] = path
        self._refresh()


class AspectPreview(QWidget):
    """Small outline of the frame shape, so landscape / portrait / square is obvious at a glance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ratio = 16 / 9
        self.setFixedSize(QSize(96, 96))

    def set_size(self, width, height):
        self.ratio = max(0.05, width / max(1, height))
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = 80.0
        w, h = (box, box / self.ratio) if self.ratio >= 1 else (box * self.ratio, box)
        rect = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        p.setBrush(self.palette().mid())
        p.setPen(QPen(self.palette().text().color(), 1.5))
        p.drawRoundedRect(rect, 3, 3)


class ProjectFieldsDialog(QDialog):
    """Shared fields of New Project and Project Settings."""

    SETTINGS_KEY = ""

    def __init__(self, settings, parent=None, title="Project", ok_text="OK", with_location=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._busy = False
        self._lock_ratio = settings.width / max(1, settings.height)
        self._background = settings.background
        form = QFormLayout()
        self.name = QLineEdit(settings.name)
        form.addRow("Name", self.name)
        self.location_edit = None
        if with_location:
            self.location_edit = QLineEdit(storage.default_projects_dir())
            browse = QPushButton("Browse...")
            browse.setAutoDefault(False)
            browse.clicked.connect(self._browse)
            row = QHBoxLayout()
            row.addWidget(self.location_edit, 1)
            row.addWidget(browse)
            form.addRow("Location", row)
        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS) + [CUSTOM_PRESET])
        form.addRow("Preset", self.preset)
        self.width_spin, self.height_spin = QSpinBox(), QSpinBox()
        for spin in (self.width_spin, self.height_spin):
            spin.setRange(16, 16384)
            spin.setSingleStep(2)
            spin.setSuffix(" px")
        self.lock = QPushButton("Lock aspect")
        self.lock.setCheckable(True)
        self.lock.setChecked(True)
        self.lock.setAutoDefault(False)
        self.swap = QPushButton("Swap")
        self.swap.setToolTip("Swap width and height (landscape / portrait)")
        self.swap.setAutoDefault(False)
        size_row = QHBoxLayout()
        for widget in (self.width_spin, QLabel("\u00d7"), self.height_spin, self.lock, self.swap):
            size_row.addWidget(widget)
        self.preview = AspectPreview()
        top = QHBoxLayout()
        top.addLayout(size_row, 1)
        top.addWidget(self.preview)
        form.addRow("Size", top)
        self.fps = QComboBox()
        self.fps.setEditable(True)
        self.fps.addItems([fps_text(f) for f in FRAME_RATES])
        self.fps.setToolTip("Pick a frame rate or type a custom one")
        form.addRow("Frame rate", self.fps)
        self.color_button = QPushButton()
        self.color_button.setAutoDefault(False)
        self.color_button.clicked.connect(self._pick_color)
        form.addRow("Background", self.color_button)
        self.sample_rate = QComboBox()
        for rate in SAMPLE_RATES:
            self.sample_rate.addItem(f"{rate / 1000:g} kHz", rate)
        form.addRow("Audio sample rate", self.sample_rate)
        self.import_mode = QComboBox()
        self.import_mode.addItem("Link to original files", "link")
        self.import_mode.addItem("Copy into project folder", "copy")
        form.addRow("Imported media", self.import_mode)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self._load(settings)
        self.preset.currentTextChanged.connect(self._on_preset)
        self.width_spin.valueChanged.connect(lambda v: self._on_size("w"))
        self.height_spin.valueChanged.connect(lambda v: self._on_size("h"))
        self.fps.currentTextChanged.connect(self._on_fps)
        self.swap.clicked.connect(self._swap)
        self.lock.toggled.connect(self._on_lock)

    # ---- values ---------------------------------------------------------
    def _load(self, s):
        self._busy = True
        self.preset.setCurrentText(s.preset if s.preset in PRESETS else preset_for(s.width, s.height, s.fps))
        self.width_spin.setValue(s.width)
        self.height_spin.setValue(s.height)
        self.fps.setCurrentText(fps_text(s.fps))
        self.sample_rate.setCurrentIndex(max(0, self.sample_rate.findData(s.sample_rate)))
        self.import_mode.setCurrentIndex(max(0, self.import_mode.findData(s.import_mode)))
        self._set_color(s.background)
        self.preview.set_size(s.width, s.height)
        self._busy = False

    def _set_color(self, color):
        self._background = color
        self.color_button.setText(color.upper())
        self.color_button.setStyleSheet(f"background:{color}; color:{'#000' if QColor(color).lightness() > 128 else '#fff'};")

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(self._background), self, "Background color",
                                      QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if color.isValid():
            self._set_color(color.name())

    def _on_preset(self, name):
        if self._busy or name not in PRESETS:
            return
        w, h, f = PRESETS[name]
        self._busy = True
        self.width_spin.setValue(w)
        self.height_spin.setValue(h)
        self.fps.setCurrentText(fps_text(f))
        self._lock_ratio = w / h
        self.preview.set_size(w, h)
        self._busy = False

    def _to_custom(self):
        self._busy = True
        self.preset.setCurrentText(CUSTOM_PRESET)
        self._busy = False

    def _on_size(self, which):
        if self._busy:
            return
        self._busy = True
        if self.lock.isChecked():
            if which == "w":
                self.height_spin.setValue(max(16, int(round(self.width_spin.value() / self._lock_ratio))))
            else:
                self.width_spin.setValue(max(16, int(round(self.height_spin.value() * self._lock_ratio))))
        else:
            self._lock_ratio = self.width_spin.value() / max(1, self.height_spin.value())
        self._busy = False
        self._to_custom()
        self.preview.set_size(self.width_spin.value(), self.height_spin.value())

    def _on_lock(self, on):
        if on:
            self._lock_ratio = self.width_spin.value() / max(1, self.height_spin.value())

    def _on_fps(self, _text):
        if not self._busy:
            self._to_custom()

    def _swap(self):
        w, h = self.width_spin.value(), self.height_spin.value()
        self._busy = True
        self.width_spin.setValue(h)
        self.height_spin.setValue(w)
        self._lock_ratio = h / max(1, w)
        self._busy = False
        self._to_custom()
        self.preview.set_size(h, w)

    def _browse(self):
        folder = choose_folder(self, "Project location", self.location_edit.text())
        if folder:
            self.location_edit.setText(folder)

    def _fps_value(self):
        try:
            return max(1.0, min(240.0, float(self.fps.currentText().replace(",", "."))))
        except ValueError:
            return 30.0

    def settings(self):
        w, h, fps = self.width_spin.value(), self.height_spin.value(), self._fps_value()
        return ProjectSettings(
            name=self.name.text(), width=w, height=h, fps=fps, background=self._background,
            sample_rate=self.sample_rate.currentData(), import_mode=self.import_mode.currentData(),
            preset=self.preset.currentText()).validate()

    def location(self):
        return self.location_edit.text().strip() if self.location_edit else ""


class NewProjectDialog(ProjectFieldsDialog):
    """Name, location and the format of a new project; remembers the last values used."""

    KEYS = ("preset", "width", "height", "fps", "background", "sampleRate", "importMode", "location")

    def __init__(self, parent=None):
        self._store = dock_layout.app_settings()
        super().__init__(self._remembered(), parent, "New Project", "Create", with_location=True)
        saved_location = self._store.value("newProject/location", "")
        if saved_location:
            self.location_edit.setText(str(saved_location))
        self.name.selectAll()

    def _remembered(self):
        s = self._store
        preset = str(s.value("newProject/preset", DEFAULT_PRESET))
        base = ProjectSettings.from_preset(preset if preset in PRESETS else DEFAULT_PRESET, "Untitled project")
        try:
            if preset == CUSTOM_PRESET:
                base.width = int(s.value("newProject/width", base.width))
                base.height = int(s.value("newProject/height", base.height))
                base.fps = float(s.value("newProject/fps", base.fps))
                base.preset = CUSTOM_PRESET
            base.background = str(s.value("newProject/background", base.background))
            base.sample_rate = int(s.value("newProject/sampleRate", base.sample_rate))
            base.import_mode = str(s.value("newProject/importMode", base.import_mode))
        except (TypeError, ValueError):
            pass
        return base.validate()

    def accept(self):
        if not self.name.text().strip():
            self.name.setFocus()
            return
        s, store = self.settings(), self._store
        store.setValue("newProject/preset", s.preset)
        store.setValue("newProject/width", s.width)
        store.setValue("newProject/height", s.height)
        store.setValue("newProject/fps", s.fps)
        store.setValue("newProject/background", s.background)
        store.setValue("newProject/sampleRate", s.sample_rate)
        store.setValue("newProject/importMode", s.import_mode)
        store.setValue("newProject/location", self.location())
        store.sync()
        super().accept()


class ProjectSettingsDialog(ProjectFieldsDialog):
    """The same fields as New Project, for an open project."""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent, "Project Settings", "Apply")
        self.setMinimumWidth(460)
