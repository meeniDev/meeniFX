"""Qt dialogs: export settings, the marker list and the branded About box."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                             QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from .. import tokens
from ..config import APP_TITLE, APP_VERSION
from ..version import display_version
from ..tokens import COLORS
from ..model.timeline import timecode_text
from .branding import wordmark
from .filedialogs import save_file
from .. import crashlog
from .widgets import ScrubField
from .themed import muted_text


def span_text(start, end, fps):
    """Describe a frame range as ``start - end (duration)`` in seconds."""
    fps = max(1.0, float(fps or 1.0))
    start, end = float(start), float(end)

    def stamp(frame):
        seconds = max(0.0, frame) / fps
        return f"{int(seconds // 60)}:{seconds % 60:06.3f}"

    return f"{stamp(start)} - {stamp(end)}  ({max(0.0, end - start) / fps:.3f} s)"


class ExportRangeDialog(QDialog):
    """Asked first when a loop region exists: whole project or just that region?"""

    def __init__(self, start, end, fps, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Range")
        self.setMinimumWidth(460)
        self.scope = None
        label = QLabel("A loop region is set and looping is enabled.\n\n"
                       f"Region: {span_text(start, end, fps)}\n\nWhat should the exported file contain?")
        label.setWordWrap(True)
        self.whole = QPushButton("Export whole project")
        self.region = QPushButton(f"Export selected region ({span_text(start, end, fps)})")
        self.whole.clicked.connect(lambda: self._pick("whole"))
        self.region.clicked.connect(lambda: self._pick("region"))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(self.whole)
        layout.addWidget(self.region)
        layout.addWidget(cancel)
        (self.region if self.region.isEnabled() else self.whole).setDefault(True)

    def _pick(self, scope):
        self.scope = scope
        self.accept()


class ExportDialog(QDialog):
    def __init__(self, engine, parent=None, export_range=None):
        super().__init__(parent)
        self.engine=engine
        self.export_range=export_range
        self.setWindowTitle("Export Selected Region" if export_range else "Export Full Project")
        self.setMinimumWidth(500)
        form=QFormLayout()
        self.use_source_fps=QCheckBox("Use source FPS")
        self.use_source_fps.setChecked(engine.render_use_source_fps.get())
        self.fps=ScrubField("Render FPS", engine.render_fps.get(), 1, 240, default=30, step=1,
                             unit=" fps",
                             tooltip="Frame rate written to the exported file")
        self.use_source_size=QCheckBox("Use source size")
        self.use_source_size.setChecked(engine.render_use_source_size.get())
        self.width=ScrubField("Width", max(2,engine.render_width.get()), 2, 16384, default=1280,
                               step=2, unit=" px",
                               tooltip="Pixels across the exported frame")
        self.height=ScrubField("Height", max(2,engine.render_height.get()), 2, 16384, default=720,
                                step=2, unit=" px",
                                tooltip="Pixels down the exported frame")
        self.format=QComboBox(); self.format.addItems(["mp4","avi","gif","png","jpg","jpeg"])
        self.format.setCurrentText(engine.render_format.get())
        self.path=QLineEdit(engine.render_output_path.get())
        browse=QPushButton("Browse")
        browse.clicked.connect(self._browse)
        path_row=QHBoxLayout(); path_row.addWidget(self.path,1); path_row.addWidget(browse)
        form.addRow(self.use_source_fps)
        form.addRow(self.fps)
        form.addRow(self.use_source_size)
        form.addRow(self.width)
        form.addRow(self.height)
        form.addRow("Format",self.format)
        self.gif_box=QWidget()
        gif_form=QFormLayout(self.gif_box)
        gif_form.setContentsMargins(0,0,0,0)
        self.gif_rate=QComboBox()
        self.gif_rate.addItems(["Project FPS","10","15","20","24","30","Custom"])
        self.gif_rate.setToolTip("GIF frame rate. Rates above 50 fps are capped so common viewers do not stretch short delays.")
        stored_fps=float(engine.render_fps.get())
        if engine.render_use_source_fps.get():
            self.gif_rate.setCurrentText("Project FPS")
        elif any(abs(stored_fps-value)<1e-6 for value in (10,15,20,24,30)):
            self.gif_rate.setCurrentText(str(int(stored_fps)))
        else:
            self.gif_rate.setCurrentText("Custom")
        self.gif_loop=QComboBox()
        for label,value in (("Forever","forever"),("Once","once"),("Custom","custom")):
            self.gif_loop.addItem(label,value)
        loop_index=self.gif_loop.findData(engine.render_gif_loop.get())
        self.gif_loop.setCurrentIndex(max(0,loop_index))
        self.gif_loops=ScrubField("Repeats", engine.render_gif_loops.get(), 1, 10000,
                                  default=2, step=1, unit="x",
                                  tooltip="Additional loops stored in the GIF (Custom loop mode)")
        self.gif_colors=QComboBox()
        for label,value in (("256 colors (best)",256),("128 colors",128),
                            ("64 colors",64),("32 colors (smallest)",32)):
            self.gif_colors.addItem(label,value)
        color_index=self.gif_colors.findData(int(engine.render_gif_colors.get()))
        self.gif_colors.setCurrentIndex(max(0,color_index))
        self.gif_dither=QComboBox()
        self.gif_dither.addItem("Floyd-Steinberg","floyd")
        self.gif_dither.addItem("None","none")
        dither_index=self.gif_dither.findData(engine.render_gif_dither.get())
        self.gif_dither.setCurrentIndex(max(0,dither_index))
        self.gif_alpha=QCheckBox("Transparent background")
        self.gif_alpha.setEnabled(False)
        self.gif_alpha.setToolTip("GIF export is flattened onto the project background; transparency is not available for this composite.")
        self.gif_warning=QLabel("GIF is limited to 256 colours and 1/100-second frame delays. "
                                "Rates above 50 fps are capped so common viewers do not turn short delays into slow motion.")
        self.gif_warning.setWordWrap(True)
        muted_text(self.gif_warning)
        gif_form.addRow("GIF frame rate",self.gif_rate)
        gif_form.addRow("Loop",self.gif_loop)
        gif_form.addRow(self.gif_loops)
        gif_form.addRow("Colours",self.gif_colors)
        gif_form.addRow("Dithering",self.gif_dither)
        gif_form.addRow(self.gif_alpha)
        gif_form.addRow(self.gif_warning)
        form.addRow(self.gif_box)
        self.format.currentTextChanged.connect(self._sync_format)
        self.gif_rate.currentTextChanged.connect(self._apply_gif_rate)
        self.gif_loop.currentIndexChanged.connect(self._sync_loop)
        self.fps.committed.connect(self._fps_edited)
        self._sync_format(self.format.currentText())
        if export_range:
            start, end = export_range
            range_label = QLabel(span_text(start, end, engine.timeline.fps))
            range_label.setToolTip("Only this part of the timeline is rendered, starting at 0")
            form.addRow("Export range", range_label)
        else:
            form.addRow("Export range", QLabel(
                span_text(0, max(1, engine.timeline.duration), engine.timeline.fps) + "  - whole project"))
        form.addRow("Output path",path_row)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout=QVBoxLayout(self); layout.addLayout(form); layout.addWidget(buttons)

    def _sync_format(self, fmt):
        is_gif = str(fmt).lower() == "gif"
        self.gif_box.setVisible(is_gif)
        if is_gif:
            self._apply_gif_rate(self.gif_rate.currentText())
            self._sync_loop()
        else:
            self.fps.setEnabled(True)
            self.use_source_fps.setEnabled(True)

    def _apply_gif_rate(self, text):
        if self.format.currentText().lower() != "gif":
            return
        self.use_source_fps.setEnabled(False)
        if text == "Project FPS":
            self.use_source_fps.setChecked(True)
            self.fps.setEnabled(False)
        elif text in {"10","15","20","24","30"}:
            self.use_source_fps.setChecked(False)
            self.fps.set_value(float(text))
            self.fps.setEnabled(False)
        else:
            self.use_source_fps.setChecked(False)
            self.fps.setEnabled(True)

    def _fps_edited(self, _value):
        if self.format.currentText().lower() == "gif" and self.gif_rate.currentText() != "Custom":
            self.gif_rate.setCurrentText("Custom")

    def _sync_loop(self):
        self.gif_loops.setEnabled(self.gif_loop.currentData() == "custom")

    def _browse(self):
        ext=self.format.currentText()
        path=save_file(self,"Choose render output",self.path.text(),
                       f"{ext.upper()} (*.{ext});;All files (*)")
        if path:self.path.setText(path)

    def settings(self):
        engine=self.engine
        fmt=self.format.currentText()
        use_source_fps=self.use_source_fps.isChecked()
        fps_value=self.fps.value()
        if fmt.lower()=="gif":
            rate=self.gif_rate.currentText()
            if rate=="Project FPS":
                use_source_fps=True
            elif rate in {"10","15","20","24","30"}:
                use_source_fps=False; fps_value=float(rate)
            else:
                use_source_fps=False
        engine.render_use_source_fps.set(use_source_fps)
        engine.render_fps.set(fps_value)
        engine.render_use_source_size.set(self.use_source_size.isChecked())
        engine.render_width.set(self.width.value())
        engine.render_height.set(self.height.value())
        engine.render_output_path.set(self.path.text())
        engine.render_format.set(fmt)
        loop_mode=self.gif_loop.currentData() or "forever"
        engine.render_gif_loop.set(loop_mode)
        engine.render_gif_loops.set(self.gif_loops.value())
        engine.render_gif_colors.set(self.gif_colors.currentData())
        engine.render_gif_dither.set(self.gif_dither.currentData())
        start, end = self.export_range if self.export_range else (0, None)
        return {"use_source_fps":use_source_fps,"fps":fps_value,
                "use_source_size":self.use_source_size.isChecked(),"width":self.width.value(),
                "height":self.height.value(),"output_path":self.path.text(),"format":fmt,
                "gif_loop":loop_mode,"gif_loops":self.gif_loops.value(),
                "gif_colors":self.gif_colors.currentData(),"gif_dither":self.gif_dither.currentData(),
                "range_start":start,"range_end":end}


class MarkerListDialog(QDialog):
    """Small non-modal list of every marker; double-click jumps the playhead to it."""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Markers")
        self.setModal(False)
        self.resize(360, 300)
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.itemDoubleClicked.connect(self._jump)
        self.list.itemClicked.connect(self._select)
        self.hint = QLabel("Double-click a marker to jump to it.")
        muted_text(self.hint)
        self.empty = QLabel("No markers yet. Press M on the timeline to add one.")
        muted_text(self.empty)
        delete = QPushButton("Delete Selected")
        delete.clicked.connect(lambda: session.delete_markers())
        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty)
        layout.addWidget(self.hint)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(delete)
        layout.addLayout(row)
        session.changed.connect(self.refresh)
        session.marker_selection_changed.connect(lambda _ids: self._sync_selection())
        self.refresh()

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def refresh(self):
        """Rebuild from the model; cheap because projects have only a few dozen markers."""
        self.list.clear()
        fps = self.session.timeline.fps
        for marker in self.session.timeline.markers:
            label = f"{timecode_text(marker.frame, fps)}   {marker.name or '(unnamed)'}"
            item = QListWidgetItem(label)
            swatch = QPixmap(10, 10)
            swatch.fill(QColor(marker.color))
            item.setIcon(QIcon(swatch))
            item.setData(Qt.ItemDataRole.UserRole, marker.id)
            self.list.addItem(item)
        has_markers = bool(self.session.timeline.markers)
        self.list.setVisible(has_markers)
        self.empty.setVisible(not has_markers)
        self.hint.setVisible(has_markers)
        self._sync_selection()

    def _sync_selection(self):
        selected = self.session.selected_markers()
        current = selected[0].id if selected else None
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) == current)

    def _marker_id(self, item):
        return item.data(Qt.ItemDataRole.UserRole)

    def _select(self, item):
        self.session.set_marker_selection({self._marker_id(item)})

    def _jump(self, item):
        self.session.go_to_marker(self._marker_id(item))


class PasteAttributesDialog(QDialog):
    """Choose which attribute groups Paste Attributes transfers to the selected clips."""

    PARTS = (("transform", "Transform", "Position, scale, rotation, anchor and opacity"),
             ("effects", "Effects", "The effect stack, colour adjustments included"),
             ("animations", "Animations", "Animation preset instances"),
             ("audio", "Audio", "Volume, gain, fades and fade curves"),
             ("text", "Text style", "Font, size, colours, outline, shadow and spacing"))

    def __init__(self, parent=None, available=None):
        super().__init__(parent)
        self.setWindowTitle("Paste Attributes")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)
        title = QLabel("Copy the chosen attributes from the copied clip onto every selected clip.")
        title.setWordWrap(True)
        layout.addWidget(title)
        self.boxes = {}
        for key, label, tip in self.PARTS:
            box = QCheckBox(label)
            box.setToolTip(tip)
            box.setChecked(key != "text")
            box.setEnabled(True if available is None else key in available)
            self.boxes[key] = box
            layout.addWidget(box)
        note = QLabel("Effects that cannot run on a target clip's media type are skipped.")
        note.setWordWrap(True)
        muted_text(note)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def parts(self):
        return {key for key, box in self.boxes.items() if box.isChecked() and box.isEnabled()}


class AboutDialog(QDialog):
    """Branded About box: the horizontal wordmark, the app name and its version.

    The wordmark is resampled from the 2200 px source for the screen's device pixel ratio, so
    the lettering is drawn from more pixels than it needs instead of being an enlarged small
    bitmap. Nothing here is upscaled, and the layout only uses theme spacing steps.
    """

    LOGO_WIDTH = 396

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_TITLE}")
        self.setMinimumWidth(470)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setStyleSheet("QLabel{background:transparent;}")
        dpr = max(1.0, self.devicePixelRatioF())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(10)

        logo = wordmark(self.LOGO_WIDTH, dpr)
        if logo is not None:
            mark = QLabel()
            mark.setPixmap(logo)
            mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
            mark.setFixedHeight(int(round(logo.height() / max(1.0, dpr))))
            layout.addWidget(mark)
            layout.addSpacing(6)

        title = QLabel(APP_TITLE)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size:{tokens.FONT_TITLE + 6}px;font-weight:600;")
        layout.addWidget(title)

        version = QLabel(f"Version {display_version(APP_VERSION)}")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version.setStyleSheet(f"color:{COLORS['muted']};font-size:{tokens.FONT_SMALL}px;")
        layout.addWidget(version)

        subtitle = QLabel("Simple yet powerful video editor.")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(f"color:{COLORS['muted']};")
        layout.addWidget(subtitle)

        layout.addSpacing(8)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setMinimumWidth(96)
            close.setDefault(True)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class CrashReportDialog(QDialog):
    """The report a professional app shows instead of vanishing.

    Two situations use it: an unhandled exception while the editor is running (the app
    stays open and this explains what happened), and a session that ended without closing
    cleanly - a native crash, a power cut - which is detected on the next start.
    """

    def __init__(self, summary, details, parent=None, *, title="meeniFX hit a problem",
                 recovered=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(620)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(tokens.SP2)

        heading = QLabel(summary)
        heading.setWordWrap(True)
        heading.setStyleSheet(f"font-size:{tokens.FONT_TITLE + 2}px;font-weight:600;")
        layout.addWidget(heading)

        note = QLabel(("Your project is still open and your last saved edit is untouched. "
                       "The details below were written to the crash log.")
                      if recovered else
                      ("The editor is still running. Nothing was lost from the timeline, but "
                       "the last action may not have completed. The details below were "
                       "written to the crash log."))
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{COLORS['muted']};")
        layout.addWidget(note)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlainText(details or "(no details were recorded)")
        self.details.setMinimumHeight(200)
        self.details.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        layout.addWidget(self.details, 1)

        self.log_path = QLabel(crashlog.path())
        self.log_path.setWordWrap(True)
        self.log_path.setStyleSheet(f"color:{COLORS['muted']};font-size:{tokens.FONT_SMALL}px;")
        self.log_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.log_path)

        row = QHBoxLayout()
        copy = QPushButton("Copy details")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        open_log = QPushButton("Open log folder")
        open_log.clicked.connect(self._open_folder)
        row.addWidget(open_log)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setText("Continue")
            close.setMinimumWidth(110)
            close.setDefault(True)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        row.addWidget(buttons)
        layout.addLayout(row)

    def _copy(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.details.toPlainText())

    def _open_folder(self):
        import os
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(crashlog.path())))
