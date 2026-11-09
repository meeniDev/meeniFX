"""Category-based Preferences: a short sidebar on the left, one scrollable page on the right.

The old dialog was a single column of every setting in the application. On a 1366x768
screen it grew past the bottom edge and the category the user asked for could not be
reached. Each page here owns its own QScrollArea, so a long page scrolls without growing
the window, and the dialog remembers the last category that was open.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox,
                             QStackedWidget, QVBoxLayout, QWidget)

from .. import tokens
from ..icons import icon
from .filedialogs import choose_folder, open_file
from .layout import app_settings
from .themed import muted_text

# key, sidebar label, icon name
SECTIONS = (
    ("general", "General", "gear"),
    ("appearance", "Appearance", "image"),
    ("timeline", "Timeline", "graph"),
    ("playback", "Playback", "stopwatch"),
    ("recording", "Recording", "camera"),
    ("paste", "Import", "import"),
    ("captions", "Captions", "captions"),
    ("cache", "Cache", "clean"),
    ("updates", "Updates", "up"),
)
SECTION_KEYS = tuple(key for key, _label, _icon in SECTIONS)
# Old callers asked for a specific page by name. Keep those names working.
SECTION_ALIASES = {"previews": "appearance", "effect previews": "appearance",
                   "effects": "appearance", "playback": "playback", "performance": "playback",
                   "storage": "cache", "models": "captions", "smart paste": "paste"}


def _store():
    return app_settings()


class PreferencesDialog(QDialog):
    """All application settings, one category at a time."""

    def __init__(self, parent=None, section=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumSize(560, 360)
        self._settings = _store()
        self._requested_section = section
        self._recorder_settings = None
        self._caption_whisper = None
        self._build()
        self._select_section(section)
        self._fit_to_screen(parent)

    # ------------------------------------------------------------------ chrome
    def _fit_to_screen(self, parent):
        """Default to a comfortable size, never larger than the screen it opens on."""
        screen = None
        window = parent.window() if parent is not None else None
        if window is not None:
            screen = window.screen()
        if screen is None:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        width, height = 860, 620
        if screen is not None:
            available = screen.availableGeometry()
            width = max(560, min(width, available.width() - 80))
            height = max(360, min(height, available.height() - 80))
        self.resize(width, height)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.sidebar = QListWidget(self)
        self.sidebar.setObjectName("prefSidebar")
        self.sidebar.setFixedWidth(188)
        self.sidebar.setIconSize(QSize(16, 16))
        self.sidebar.setSpacing(2)
        self.sidebar.setUniformItemSizes(True)
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.setStyleSheet(
            f"QListWidget#prefSidebar {{ background: {tokens.COLORS['panel_soft']};"
            f" border: 0; padding: 10px 8px; }}"
            f"QListWidget#prefSidebar::item {{ padding: 8px 10px; border-radius: 6px; }}"
            f"QListWidget#prefSidebar::item:selected {{ background: {tokens.COLORS['accent']};"
            " color: white; }")
        body.addWidget(self.sidebar)

        self.stack = QStackedWidget(self)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        footer = QWidget(self)
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(14, 8, 14, 10)
        footer_row.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=footer)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer_row.addWidget(buttons)
        outer.addWidget(footer)

        self._build_general()
        self._build_appearance()
        self._build_timeline()
        self._build_playback()
        self._build_recording()
        self._build_paste()
        self._build_captions()
        self._build_cache()
        self._build_updates()
        self.sidebar.currentRowChanged.connect(self._category_changed)
        self.sidebar.setCurrentRow(0)

    def _page(self, key, title, description=""):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget(scroll)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(20, 18, 22, 18)
        layout.setSpacing(10)
        heading = QLabel(title, host)
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(heading)
        if description:
            note = QLabel(description, host)
            note.setWordWrap(True)
            muted_text(note)
            layout.addWidget(note)
        scroll.setWidget(host)
        self.stack.addWidget(scroll)
        item = QListWidgetItem(icon(icon_name_for(key)), title)
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setToolTip(title)
        self.sidebar.addItem(item)
        return layout

    def _subheading(self, layout, text, note=""):
        label = QLabel(text)
        label.setStyleSheet("font-weight: 600; margin-top: 4px;")
        layout.addWidget(label)
        if note:
            hint = QLabel(note)
            hint.setWordWrap(True)
            muted_text(hint)
            layout.addWidget(hint)

    @staticmethod
    def _form():
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return form

    @staticmethod
    def _path_field(text, placeholder="", tooltip=""):
        field = QLineEdit(text or "")
        field.setReadOnly(True)
        field.setPlaceholderText(placeholder)
        field.setToolTip(tooltip or text or placeholder)
        field.textChanged.connect(lambda value, f=field: f.setToolTip(value or placeholder))
        return field

    def _select_section(self, section):
        key = SECTION_ALIASES.get(str(section or "").strip().lower(), str(section or "").strip().lower())
        if key not in SECTION_KEYS:
            key = str(self._settings.value("preferences/lastSection", "general"))
        row = SECTION_KEYS.index(key) if key in SECTION_KEYS else 0
        self.sidebar.setCurrentRow(row)

    def _category_changed(self, row):
        if 0 <= row < len(SECTION_KEYS):
            self.stack.setCurrentIndex(row)
            self._settings.setValue("preferences/lastSection", SECTION_KEYS[row])

    # ------------------------------------------------------------------ general
    def _build_general(self):
        from . import start_window as start

        page = self._page("general", "General",
                          "How meeniFX Studios behaves when it starts.")
        self.startup = QCheckBox("Show the start window on startup")
        self.startup.setChecked(start.show_on_startup())
        page.addWidget(self.startup)
        self.prune_missing = QCheckBox("Remove missing projects at startup")
        self.prune_missing.setChecked(start.prune_missing_on_startup())
        self.prune_missing.setToolTip("Drops recent entries whose file has moved or been "
                                      "deleted when the start window opens.")
        page.addWidget(self.prune_missing)
        page.addStretch(1)

    # --------------------------------------------------------------- appearance
    def _build_appearance(self):
        from .. import thumbnails as THUMBS

        page = self._page("appearance", "Appearance",
                          "The reference picture used for effect thumbnails and how the "
                          "browser draws those previews.")
        self.preview_settings = THUMBS.PreviewSettings.load()
        form = self._form()

        self.preview_image = QComboBox()
        self.preview_image.addItem("Default (figure_cafe.png)", "default")
        self.preview_image.addItem("Custom image", "custom")
        self.preview_image.setCurrentIndex(
            max(0, self.preview_image.findData(self.preview_settings.image_mode)))
        form.addRow("Preview image", self.preview_image)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        default_path = str(THUMBS.default_reference() or "")
        self.preview_path = self._path_field(
            self.preview_settings.custom_path,
            default_path or "Default reference image is missing",
            default_path)
        choose = QPushButton("Choose Image...")
        choose.clicked.connect(self._choose_preview_image)
        reset = QPushButton("Reset to Default")
        reset.clicked.connect(self._reset_preview_image)
        row_layout.addWidget(self.preview_path, 1)
        row_layout.addWidget(choose)
        row_layout.addWidget(reset)
        form.addRow("Custom image", row)

        self.preview_thumb = QLabel()
        self.preview_thumb.setFixedSize(168, 112)
        self.preview_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow("Preview", self.preview_thumb)

        self.preview_background = QComboBox()
        for label, value in (("Checkerboard", "checker"), ("Dark", "dark"), ("Light", "light")):
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
        page.addLayout(form)
        page.addStretch(1)
        self._refresh_preview_thumb()

    def _preview_form_settings(self):
        from .. import thumbnails as THUMBS
        return THUMBS.PreviewSettings(
            image_mode=str(self.preview_image.currentData() or "default"),
            custom_path=self.preview_path.text().strip(),
            background=str(self.preview_background.currentData() or "checker"),
            quality=str(self.preview_quality.currentData() or "medium"))

    def _refresh_preview_thumb(self, *_args):
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
        from .. import thumbnails as THUMBS
        self.preview_settings = THUMBS.PreviewSettings(
            image_mode=str(self.preview_image.currentData() or "default"),
            custom_path=self.preview_path.text().strip(),
            background=str(self.preview_background.currentData() or "checker"),
            quality=str(self.preview_quality.currentData() or "medium"))
        self.preview_settings.save()
        reload_thumbs = getattr(self.parent(), "_reload_effect_previews", None)
        if callable(reload_thumbs):
            reload_thumbs()

    # ----------------------------------------------------------------- timeline
    def _build_timeline(self):
        from .timeline_settings import TimelineSettings

        page = self._page("timeline", "Timeline",
                          "Defaults for editing clips on the timeline.")
        self._timeline_settings = TimelineSettings.load()
        self.auto_transitions = QCheckBox("Create a crossfade when clips overlap")
        self.auto_transitions.setChecked(self._timeline_settings.auto_overlap_transitions)
        self.auto_transitions.setToolTip("Dropping a clip over another on the same track "
                                         "adds a crossfade in the overlap.")
        page.addWidget(self.auto_transitions)
        page.addStretch(1)

    def _apply_timeline_settings(self):
        from .timeline_settings import TimelineSettings
        settings = TimelineSettings(
            {"auto_overlap_transitions": self.auto_transitions.isChecked()})
        settings.save()
        self._timeline_settings = settings
        parent = self.parent()
        engine = getattr(parent, "engine", None)
        if engine is not None:
            engine.session.auto_transitions.set(settings.auto_overlap_transitions)
        canvas = getattr(getattr(parent, "timeline_panel", None), "canvas", None)
        if canvas is not None:
            canvas.update()

    # ----------------------------------------------------------------- playback
    def _build_playback(self):
        from ..preview.settings import PlaybackSettings
        self.playback_settings = PlaybackSettings.load()
        settings = self.playback_settings

        page = self._page("playback", "Playback and Performance",
                          "Preview quality, hardware use and playback timing.")
        self._subheading(page, "Rendering")
        form = self._form()

        engine = getattr(self.parent(), "engine", None)
        if engine is not None:
            from .widgets import ScrubField
            self.fps_cap = ScrubField("Playback FPS cap", engine.playback_fps.get(), 12, 60,
                                      default=30, step=1, label_width=0,
                                      tooltip="Upper limit for preview playback refresh")
            self.fps_cap.edited.connect(
                lambda value: engine.playback_fps.set(int(round(value))))
            form.addRow("Playback FPS cap", self.fps_cap)

        self.gpu_mode = QComboBox()
        for label, value in (("Auto (measure and choose)", "auto"),
                             ("On (force OpenCL)", "on"), ("Off (CPU only)", "off")):
            self.gpu_mode.addItem(label, value)
        self.gpu_mode.setCurrentIndex(max(0, self.gpu_mode.findData(settings.gpu_mode)))
        form.addRow("GPU acceleration", self.gpu_mode)

        self.quality_mode = QComboBox()
        for label in ("Auto", "Full", "1/2", "1/4", "1/8"):
            self.quality_mode.addItem(label, label)
        self.quality_mode.setCurrentIndex(
            max(0, self.quality_mode.findData(settings.quality_mode)))
        form.addRow("Preview quality", self.quality_mode)

        self.auto_proxy = QCheckBox("Generate proxy media for heavy sources")
        self.auto_proxy.setChecked(settings.auto_proxy)
        form.addRow("", self.auto_proxy)

        self.proxy_width = QComboBox()
        for width in (640, 960, 1280, 1920):
            self.proxy_width.addItem(f"{width} px wide", width)
        self.proxy_width.setCurrentIndex(
            max(0, self.proxy_width.findData(settings.proxy_width)))
        form.addRow("Proxy quality", self.proxy_width)

        self.hardware_decode = QComboBox()
        self.hardware_encode = QComboBox()
        for combo in (self.hardware_decode, self.hardware_encode):
            for label, value in (("Auto", "auto"), ("On", "on"), ("Off", "off")):
                combo.addItem(label, value)
        self.hardware_decode.setCurrentIndex(
            max(0, self.hardware_decode.findData(settings.hardware_decode)))
        self.hardware_encode.setCurrentIndex(
            max(0, self.hardware_encode.findData(settings.hardware_encode)))
        form.addRow("Hardware decoding", self.hardware_decode)
        form.addRow("Hardware encoding", self.hardware_encode)

        self.gain_headroom = QSpinBox()
        self.gain_headroom.setRange(0, 24)
        self.gain_headroom.setSuffix(" dB")
        self.gain_headroom.setValue(settings.gain_headroom_db)
        self.gain_headroom.setToolTip("Maximum boost the audio gain control and its "
                                      "timeline envelope may apply above 0 dB.")
        form.addRow("Gain headroom", self.gain_headroom)

        backend_row = QWidget()
        backend_layout = QHBoxLayout(backend_row)
        backend_layout.setContentsMargins(0, 0, 0, 0)
        backend_layout.setSpacing(6)
        self.backend_label = QLabel(self._backend_short())
        self.backend_label.setToolTip(self._backend_details())
        details = QPushButton("Details")
        details.clicked.connect(self._show_backend_details)
        backend_layout.addWidget(self.backend_label, 1)
        backend_layout.addWidget(details)
        form.addRow("Renderer", backend_row)
        self.gpu_mode.currentIndexChanged.connect(lambda _i: self._refresh_backend_text())

        page.addLayout(form)
        page.addStretch(1)

    def _backend_short(self):
        from ..preview.backend import gpu
        first = str(gpu.summary() or "CPU").split(" - ", 1)[0].strip()
        return f"Renderer: {first or 'CPU'}"

    def _backend_details(self):
        from ..media import hardware
        from ..media import reader as reader_module
        from ..preview.backend import gpu
        acceleration = reader_module.decode_acceleration()
        decode = {0: "Software decode", 2: "D3D11 decode"}.get(acceleration, "Hardware decode")
        encode = ("NVENC is available" if hardware._CACHE.get("nvenc")
                  else "Hardware encoding is verified on the first export")
        return (f"{gpu.summary()}\n\nDecoding: {decode}\nEncoding: {encode}\n\n"
                "Auto measures the available backend and uses the GPU only when it is "
                "faster than the CPU for this project.")

    def _show_backend_details(self):
        QMessageBox.information(self, "Renderer details", self._backend_details())

    def _refresh_backend_text(self):
        from ..preview.backend import gpu
        gpu.set_mode(self.gpu_mode.currentData() or "auto")
        self.backend_label.setText(self._backend_short())
        self.backend_label.setToolTip(self._backend_details())

    # ------------------------------------------------------------------ cache
    def _build_cache(self):
        page = self._page("cache", "Cache and Storage",
                          "Memory and disk budgets for rendered preview frames.")
        form = self._form()
        settings = self.playback_settings

        self.ram_cache = QSpinBox()
        self.ram_cache.setRange(64, 16384)
        self.ram_cache.setSingleStep(128)
        self.ram_cache.setSuffix(" MB")
        self.ram_cache.setValue(settings.ram_cache_mb)
        form.addRow("RAM cache limit", self.ram_cache)

        self.disk_cache = QSpinBox()
        self.disk_cache.setRange(0, 102400)
        self.disk_cache.setSingleStep(512)
        self.disk_cache.setSuffix(" MB")
        self.disk_cache.setValue(settings.disk_cache_mb)
        form.addRow("Disk cache limit", self.disk_cache)

        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(6)
        self.cache_folder = self._path_field(
            settings.cache_folder, "Project Cache folder (default)",
            settings.cache_folder or "Project Cache folder (default)")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_cache_folder)
        folder_layout.addWidget(self.cache_folder, 1)
        folder_layout.addWidget(browse)
        form.addRow("Cache location", folder_row)

        self.background_render = QCheckBox("Render preview frames in the background")
        self.background_render.setChecked(settings.background_render)
        form.addRow("", self.background_render)

        self.cache_notifications = QCheckBox("Show a message when background caching finishes")
        self.cache_notifications.setChecked(settings.cache_notifications)
        self.cache_notifications.setToolTip("The timeline cache indicator shows the same "
                                            "state without interrupting the edit.")
        form.addRow("", self.cache_notifications)

        clear_row = QWidget()
        clear_layout = QHBoxLayout(clear_row)
        clear_layout.setContentsMargins(0, 0, 0, 0)
        clear = QPushButton("Clear Preview Cache")
        clear.clicked.connect(self._clear_preview_cache)
        clear_layout.addWidget(clear)
        clear_layout.addStretch(1)
        form.addRow("", clear_row)
        self.cache_status = QLabel("")
        muted_text(self.cache_status)
        form.addRow("", self.cache_status)

        page.addLayout(form)
        page.addStretch(1)

    def _browse_cache_folder(self):
        folder = choose_folder(self, "Preview cache folder", self.cache_folder.text() or "")
        if folder:
            self.cache_folder.setText(folder)

    def _clear_preview_cache(self):
        parent = self.parent()
        clear = getattr(parent, "clear_preview_cache", None)
        if callable(clear):
            clear()
            self.cache_status.setText("Preview cache cleared.")
        else:
            self.cache_status.setText("Open Preferences from a project to clear its preview cache.")

    # ---------------------------------------------------------------- recording
    def _build_recording(self):
        from ..recording.settings import QUALITY_KEYS, QUALITY_PRESETS, RecorderSettings

        page = self._page("recording", "Recording",
                          "Where recordings are saved and how the recorder behaves.")
        self._recorder_settings = RecorderSettings.load()
        settings = self._recorder_settings
        form = self._form()

        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(6)
        self.rec_folder = self._path_field(settings.folder, "Project Recordings folder",
                                           settings.folder or "Project Recordings folder")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_recordings)
        folder_layout.addWidget(self.rec_folder, 1)
        folder_layout.addWidget(browse)
        form.addRow("Recordings folder", folder_row)

        self.rec_quality = QComboBox()
        for key in QUALITY_KEYS:
            self.rec_quality.addItem(QUALITY_PRESETS[key]["label"], key)
        self.rec_quality.setCurrentIndex(
            max(0, self.rec_quality.findData(settings.quality_preset)))
        form.addRow("Recording quality", self.rec_quality)

        self.rec_countdown = QCheckBox("Show a countdown before recording starts")
        self.rec_countdown.setChecked(bool(settings.countdown))
        form.addRow("", self.rec_countdown)

        self.rec_float = QCheckBox("Show floating recording controls over other applications")
        self.rec_float.setChecked(bool(settings.floating_bar))
        self.rec_float.setToolTip("The main window already shows recording time and the "
                                  "stop button.")
        form.addRow("", self.rec_float)

        page.addLayout(form)
        page.addStretch(1)

    def _browse_recordings(self):
        folder = choose_folder(self, "Recordings folder", self.rec_folder.text())
        if folder:
            self.rec_folder.setText(folder)

    def _apply_recording_settings(self):
        settings = self._recorder_settings
        settings.folder = self.rec_folder.text().strip()
        settings.floating_bar = self.rec_float.isChecked()
        settings.countdown = self.rec_countdown.isChecked()
        settings.quality_preset = str(self.rec_quality.currentData() or "balanced")
        settings.save()

    # -------------------------------------------------------------- smart paste
    def _build_paste(self):
        from .smart_paste import PasteSettings

        page = self._page("paste", "Import and Smart Paste",
                          "What Ctrl+V does with a picture or a file.")
        self.paste_settings = PasteSettings.load()
        settings = self.paste_settings
        form = self._form()

        self.paste_seconds = QDoubleSpinBox()
        self.paste_seconds.setRange(0.2, 120.0)
        self.paste_seconds.setSingleStep(0.5)
        self.paste_seconds.setDecimals(1)
        self.paste_seconds.setSuffix(" s")
        self.paste_seconds.setValue(settings.seconds)
        self.paste_seconds.setToolTip("Clip length for an image pasted from the clipboard.")
        form.addRow("Default image duration", self.paste_seconds)

        self.paste_placement = QComboBox()
        self.paste_placement.addItem("At the playhead", "playhead")
        self.paste_placement.addItem("After the selected clip", "after")
        self.paste_placement.setCurrentIndex(
            max(0, self.paste_placement.findData(settings.placement)))
        form.addRow("Paste placement", self.paste_placement)

        self.paste_select = QCheckBox("Select the pasted clip")
        self.paste_select.setChecked(settings.select)
        form.addRow("", self.paste_select)

        self.paste_fit = QCheckBox("Fit pasted images to the frame")
        self.paste_fit.setChecked(settings.fit)
        self.paste_fit.setToolTip("Reset the pasted picture's transform to the centred, "
                                  "100 percent fit.")
        form.addRow("", self.paste_fit)

        note = QLabel("Ctrl+V in a text field still pastes text. In the Media panel it only "
                      "adds the file and does not touch the timeline.")
        note.setWordWrap(True)
        muted_text(note)
        page.addLayout(form)
        page.addWidget(note)
        page.addStretch(1)

    def _apply_paste_settings(self):
        from .smart_paste import PasteSettings
        PasteSettings(float(self.paste_seconds.value()),
                      str(self.paste_placement.currentData() or "playhead"),
                      self.paste_select.isChecked(),
                      self.paste_fit.isChecked()).save()

    # ----------------------------------------------------------------- captions
    def _build_captions(self):
        from ..captions import whisper

        page = self._page("captions", "Captions and AI Models",
                          "Speech models downloaded for Auto Captions. They run locally.")
        self._caption_whisper = whisper
        form = self._form()

        backend = ("faster-whisper is installed. Transcription runs locally and offline."
                   if whisper.backend_available() else
                   "faster-whisper is not installed, so Auto Captions cannot transcribe. "
                   "Install it with: pip install faster-whisper")
        self.caption_backend = QLabel(backend)
        self.caption_backend.setWordWrap(True)
        form.addRow("Engine", self.caption_backend)

        folder = self._path_field(whisper.MODEL_DIR, "Models folder", whisper.MODEL_DIR)
        form.addRow("Models folder", folder)

        self._caption_models_host = QWidget()
        self._caption_models_layout = QVBoxLayout(self._caption_models_host)
        self._caption_models_layout.setContentsMargins(0, 0, 0, 0)
        self._caption_models_layout.setSpacing(4)
        form.addRow("Downloaded", self._caption_models_host)
        page.addLayout(form)
        page.addStretch(1)
        self._refresh_caption_models()

    def _refresh_caption_models(self):
        layout = self._caption_models_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        models = self._caption_whisper.installed_models()
        if not models:
            empty = QLabel("No speech model downloaded")
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
            delete.clicked.connect(
                lambda _checked=False, model=name: self._delete_caption_model(model))
            line.addWidget(delete)
            layout.addWidget(row)

    def _delete_caption_model(self, name):
        answer = QMessageBox.question(
            self, "Delete speech model",
            f"Delete the downloaded {name} model?\n\n"
            "Auto Captions will ask to download it again next time.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._caption_whisper.delete_model(name)
        self._refresh_caption_models()

    # ------------------------------------------------------------------ updates
    def _build_updates(self):
        from ..updates.settings import UpdateSettings
        from ..version import display_version

        page = self._page("updates", "Updates",
                          "Automatic checks, the update channel and skipped versions.")
        self.update_settings = UpdateSettings.load()
        settings = self.update_settings
        form = self._form()
        form.addRow("Version", QLabel(display_version()))

        self.update_auto = QCheckBox("Check for updates automatically")
        self.update_auto.setChecked(bool(settings.auto_check))
        self.update_auto.setToolTip("Checks GitHub Releases in the background on startup. "
                                    "A check never interrupts editing.")
        form.addRow("", self.update_auto)

        self.update_channel = QComboBox()
        self.update_channel.addItem("Alpha / Preview", "alpha")
        self.update_channel.addItem("Stable", "stable")
        self.update_channel.setCurrentIndex(max(0, self.update_channel.findData(
            settings.channel if settings.channel in ("alpha", "stable") else "alpha")))
        self.update_channel.setToolTip("Alpha includes prereleases. Stable only offers "
                                       "finished releases.")
        form.addRow("Update channel", self.update_channel)

        self.update_skipped = QLabel(", ".join(settings.skipped) or "None")
        self.update_skipped.setWordWrap(True)
        form.addRow("Skipped versions", self.update_skipped)

        clear = QPushButton("Clear Skipped Versions")
        clear.clicked.connect(self._clear_skipped_updates)
        form.addRow("", clear)

        self.update_last_check = QLabel(settings.last_check or "Never")
        form.addRow("Last check", self.update_last_check)

        check = QPushButton("Check for Updates...")
        check.clicked.connect(self._check_updates_now)
        form.addRow("", check)

        page.addLayout(form)
        page.addStretch(1)

    def _apply_update_settings(self):
        self.update_settings.auto_check = self.update_auto.isChecked()
        self.update_settings.channel = str(self.update_channel.currentData() or "alpha")
        self.update_settings.save()

    def _clear_skipped_updates(self):
        self.update_settings.clear_skipped()
        self.update_settings.save()
        self.update_skipped.setText("None")

    def _check_updates_now(self):
        from PyQt6.QtCore import QTimer
        self._apply_update_settings()
        show = getattr(self.parent(), "show_updates", None)
        self.accept()
        if callable(show):
            QTimer.singleShot(0, show)

    # -------------------------------------------------------------------- apply
    def accept(self):
        from . import start_window as start
        start.set_show_on_startup(self.startup.isChecked())
        start.set_prune_missing_on_startup(self.prune_missing.isChecked())
        self._apply_recording_settings()
        self._apply_playback_settings()
        self._apply_timeline_settings()
        self._apply_paste_settings()
        self._apply_preview_settings()
        self._apply_update_settings()
        self._settings.setValue("preferences/lastSection",
                                self.sidebar.currentItem().data(Qt.ItemDataRole.UserRole))
        super().accept()

    def _apply_playback_settings(self):
        from ..preview.settings import PlaybackSettings
        settings = PlaybackSettings({
            "gpu_mode": self.gpu_mode.currentData(),
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
        apply = getattr(self.parent(), "_apply_playback_settings", None)
        if callable(apply):
            apply(settings)


def icon_name_for(key):
    for section_key, _label, icon_name in SECTIONS:
        if section_key == key:
            return icon_name
    return "gear"
