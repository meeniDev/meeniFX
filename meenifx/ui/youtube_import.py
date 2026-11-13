"""The Import YouTube Video window and its two background jobs.

The dialog is deliberately small: title, channel and thumbnail at the top, three import
modes, the qualities this video actually offers, a start/end range with a draggable bar, and
one permission check box. Exact format ids live behind *Advanced format settings*, which is
collapsed until someone asks for it.

Everything that touches the network happens in :class:`MetadataJob` and :class:`ImportJob`,
so the editor keeps painting while details are read and while a range is downloaded.
"""

from __future__ import annotations

import logging
import os

from PyQt6.QtCore import QObject, QRectF, QRunnable, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                             QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .. import tokens
from . import filedialogs as FD
from ..constants import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
from ..media import link_import, youtube
from .themed import muted_text, themed_style

log = logging.getLogger("meenifx.youtube_ui")

_MODE_ORDER = ("video_audio", "video_only", "audio_only")
_PERMISSION_TEXT = ("I have permission to use this video")
_NOTE = ("meeniFX downloads through yt-dlp and only for media you are allowed to save. It "
         "does not sign in, bypass DRM, paywalls or regional restrictions, and YouTube "
         "import can stop working when YouTube changes.")


# --------------------------------------------------------------------- workers
class _MetaSignals(QObject):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)


class MetadataJob(QRunnable):
    """Read a video's details off the GUI thread; owns nothing on the window."""

    def __init__(self, link):
        super().__init__()
        self.link = link
        self.signals = _MetaSignals()
        self.setAutoDelete(False)

    def run(self):
        try:
            meta = youtube.fetch_metadata(self.link)
        except youtube.YouTubeError as exc:
            self.signals.failed.emit(exc.message)
            return
        except Exception as exc:          # a worker exception must never look like a crash
            log.exception("reading YouTube details failed for %s", self.link.video_id)
            self.signals.failed.emit(f"Unexpected error: {exc}")
            return
        self.signals.ready.emit(meta)


class _ImportSignals(QObject):
    # Every result carries the video URL, exactly as the link downloader's signals do: the
    # slot is keyed by it and the argument count must match or Qt aborts the process.
    progress = pyqtSignal(str, float, float)
    ready = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)
    cancelled = pyqtSignal(str)


class ImportJob(QRunnable):
    """Download and prepare one selection. SmartPaste keeps the job alive until it reports."""

    def __init__(self, link, meta, request, folder):
        super().__init__()
        self.link = link
        self.meta = meta
        self.request = request
        self.folder = folder
        self.signals = _ImportSignals()
        self._cancel = False
        self.setAutoDelete(False)

    def cancel(self):
        self._cancel = True

    def _cancelled(self):
        return self._cancel

    def run(self):
        try:
            imported = youtube.import_media(self.link, self.meta, self.request, self.folder,
                                            progress=self.signals.progress.emit,
                                            cancelled=self._cancelled)
        except youtube.YouTubeCancelled:
            self.signals.cancelled.emit(self.link.url)
            return
        except youtube.YouTubeError as exc:
            self.signals.failed.emit(self.link.url, exc.message)
            return
        except Exception as exc:
            log.exception("YouTube import failed for %s", self.link.video_id)
            self.signals.failed.emit(self.link.url, f"Unexpected error: {exc}")
            return
        self.signals.ready.emit(self.link.url, imported)


# ------------------------------------------------------------------- range bar
class RangeSelector(QWidget):
    """A small two-handle bar: drag either end to choose the part to import."""

    changed = pyqtSignal(float, float)
    HANDLE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self._drag = None
        self.setMinimumHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Drag either handle to choose the part of the video to import")

    def set_duration(self, seconds):
        self.duration = max(0.0, float(seconds or 0.0))
        self.start, self.end = 0.0, self.duration
        self.update()

    def set_range(self, start, end):
        if self.duration:
            self.start = max(0.0, min(float(start), self.duration))
            self.end = max(self.start, min(float(end), self.duration))
        self.update()

    def _x(self, seconds):
        width = max(1.0, self.width() - 2 * self.HANDLE)
        ratio = seconds / self.duration if self.duration else 0.0
        return self.HANDLE + width * max(0.0, min(1.0, ratio))

    def _seconds(self, x):
        width = max(1.0, self.width() - 2 * self.HANDLE)
        return max(0.0, min(1.0, (x - self.HANDLE) / width)) * self.duration

    def paintEvent(self, event):
        colors = tokens.COLORS
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        mid = self.height() / 2.0
        track = QRectF(self.HANDLE, mid - 4, self.width() - 2 * self.HANDLE, 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors["panel_soft"]))
        painter.drawRoundedRect(track, 4, 4)
        if self.duration:
            left, right = self._x(self.start), self._x(self.end)
            painter.setBrush(QColor(colors["accent_soft"]))
            painter.drawRoundedRect(QRectF(left, mid - 4, max(2.0, right - left), 8), 4, 4)
            painter.setPen(QPen(QColor(colors["accent"]), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(left, mid - 4, max(2.0, right - left), 8), 4, 4)
            for x in (left, right):
                painter.setPen(QPen(QColor(colors["accent"]), 1.2))
                painter.setBrush(QColor(colors["panel_alt"]))
                painter.drawEllipse(QRectF(x - 6, mid - 6, 12, 12))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colors["accent"]))
                painter.drawEllipse(QRectF(x - 2.5, mid - 2.5, 5, 5))
        painter.end()

    def mousePressEvent(self, event):
        if not self.duration:
            return
        x = event.position().x()
        near_start = abs(x - self._x(self.start))
        near_end = abs(x - self._x(self.end))
        self._drag = "start" if near_start <= near_end else "end"
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event):
        if not self.duration or self._drag is None:
            return
        moment = self._seconds(event.position().x())
        if self._drag == "start":
            self.start = min(moment, max(0.0, self.end - 0.05))
        else:
            self.end = max(moment, self.start + 0.05)
        self.update()
        self.changed.emit(self.start, self.end)

    def mouseReleaseEvent(self, event):
        self._drag = None


# ---------------------------------------------------------------------- dialog
class YouTubeImportDialog(QDialog):
    """What to import, which quality, and which part of the video."""

    def __init__(self, parent, link, insert_default=True):
        super().__init__(parent)
        self.link = link
        self.meta = None
        self.error = ""
        self.local_path = ""
        self._problem = ""
        self._status_error = False
        self.setWindowTitle("Import YouTube Video")
        self.setModal(True)
        self.setMinimumWidth(560)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        heading = QLabel("Import YouTube Video", self)
        heading.setObjectName("dialogHeading")
        outer.addWidget(heading)

        card = QFrame(self)
        card.setProperty("card", True)
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 10)
        card_layout.setSpacing(12)
        self.thumbnail = QLabel("No preview", card)
        self.thumbnail.setFixedSize(176, 99)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setObjectName("dialogNote")
        muted_text(self.thumbnail)
        card_layout.addWidget(self.thumbnail)
        details = QVBoxLayout()
        details.setSpacing(3)
        self.title_label = QLabel("Reading video details\u2026", card)
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self.title_label.font()
        font.setBold(True)
        font.setPointSizeF(max(10.0, font.pointSizeF() + 1))
        self.title_label.setFont(font)
        self.channel_label = QLabel("", card)
        self.duration_label = QLabel("", card)
        muted_text(self.channel_label)
        muted_text(self.duration_label)
        details.addWidget(self.title_label)
        details.addWidget(self.channel_label)
        details.addWidget(self.duration_label)
        details.addStretch(1)
        card_layout.addLayout(details, 1)
        outer.addWidget(card)

        self.status = QLabel("Reading video details\u2026 nothing has been downloaded.", self)
        self.status.setWordWrap(True)
        muted_text(self.status)
        outer.addWidget(self.status)

        modes = QFormLayout()
        modes.setHorizontalSpacing(14)
        modes.setVerticalSpacing(6)
        self.mode_combo = QComboBox(self)
        self._fill_modes()
        self.quality_combo = QComboBox(self)
        self.quality_combo.setMinimumContentsLength(22)
        modes.addRow("Import", self.mode_combo)
        modes.addRow("Quality", self.quality_combo)
        outer.addLayout(modes)

        self.advanced_box = QCheckBox("Advanced format settings", self)
        self.advanced_box.setChecked(False)
        self.advanced = QWidget(self)
        advanced_form = QFormLayout(self.advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        advanced_form.setHorizontalSpacing(14)
        advanced_form.setVerticalSpacing(6)
        self.format_combo = QComboBox(self)
        advanced_form.addRow("Exact format", self.format_combo)
        self.fetch_note = QLabel("", self.advanced)
        self.fetch_note.setWordWrap(True)
        muted_text(self.fetch_note)
        advanced_form.addRow("", self.fetch_note)
        self.advanced.setVisible(False)
        outer.addWidget(self.advanced_box)
        outer.addWidget(self.advanced)

        range_group = QFormLayout()
        range_group.setHorizontalSpacing(14)
        range_group.setVerticalSpacing(6)
        self.range_bar = RangeSelector(self)
        self.start_edit = QLineEdit(self)
        self.end_edit = QLineEdit(self)
        self.start_edit.setPlaceholderText("00:00:00.000")
        self.end_edit.setPlaceholderText("00:00:00.000")
        self.start_edit.setMaximumWidth(140)
        self.end_edit.setMaximumWidth(140)
        times = QHBoxLayout()
        times.setSpacing(8)
        times.addWidget(QLabel("Start", self))
        times.addWidget(self.start_edit)
        times.addSpacing(10)
        times.addWidget(QLabel("End", self))
        times.addWidget(self.end_edit)
        self.reset_button = QPushButton("Full Video", self)
        self.reset_button.setToolTip("Select the whole video again")
        times.addWidget(self.reset_button)
        times.addStretch(1)
        self.duration_value = QLabel("", self)
        muted_text(self.duration_value)
        range_group.addRow(self.range_bar)
        range_group.addRow(times)
        range_group.addRow("Selection", self.duration_value)
        self.range_note = QLabel("", self)
        self.range_note.setWordWrap(True)
        muted_text(self.range_note)
        range_group.addRow("", self.range_note)
        outer.addLayout(range_group)

        self.permission = QCheckBox(_PERMISSION_TEXT, self)
        self.permission.setChecked(False)
        self.permission.setToolTip("Required: only import media you have the right to save "
                                   "and edit")
        outer.addWidget(self.permission)
        note = QLabel(_NOTE, self)
        note.setWordWrap(True)
        note.setObjectName("dialogNote")
        muted_text(note)
        outer.addWidget(note)

        self.add_box = QCheckBox("Add to Project Media", self)
        self.add_box.setChecked(True)
        self.insert_box = QCheckBox("Insert at Playhead", self)
        self.insert_box.setChecked(bool(insert_default))
        options = QHBoxLayout()
        options.setSpacing(16)
        options.addWidget(self.add_box)
        options.addWidget(self.insert_box)
        options.addStretch(1)
        outer.addLayout(options)

        footer = QHBoxLayout()
        self.local_button = QPushButton("Use a Local File\u2026", self)
        self.local_button.setToolTip("Import a video file you already have instead")
        footer.addWidget(self.local_button)
        footer.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        footer.addWidget(self.buttons)
        outer.addLayout(footer)

        # Signals last, so filling the widgets above cannot fire them early.
        self.mode_combo.currentIndexChanged.connect(self._refresh_choices)
        self.advanced_box.toggled.connect(self._advanced_toggled)
        self.format_combo.currentIndexChanged.connect(lambda _index: self._validate())
        self.range_bar.changed.connect(self._bar_changed)
        self.start_edit.editingFinished.connect(self._edits_changed)
        self.end_edit.editingFinished.connect(self._edits_changed)
        self.reset_button.clicked.connect(self.reset_range)
        self.permission.toggled.connect(lambda _checked: self._validate())
        self.add_box.toggled.connect(lambda _checked: self._validate())
        self.insert_box.toggled.connect(lambda _checked: self._validate())
        self.local_button.clicked.connect(self._choose_local)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._set_status("Reading video details\u2026 nothing has been downloaded yet.")
        self._validate()

    # ---- metadata -------------------------------------------------------
    def set_meta(self, meta):
        """Fill the window once the details are here; nothing has been downloaded yet."""
        self.meta = meta
        self.error = ""
        self._problem = ""
        self.title_label.setText(meta.title or "YouTube video")
        self.channel_label.setText(meta.channel or "Unknown channel")
        pieces = []
        if meta.duration:
            pieces.append(youtube.format_timecode(meta.duration))
        if meta.heights:
            pieces.append(f"up to {meta.heights[0]}p")
        self.duration_label.setText(" \u00b7 ".join(pieces) or "Duration unknown")
        self._set_thumbnail(meta.thumbnail)
        self._fill_modes()
        self._refresh_choices()
        self.range_bar.set_duration(meta.duration)
        self.reset_range()
        if self.link.start and meta.duration and self.link.start < meta.duration:
            self.set_range(self.link.start, meta.duration)
        self._set_status("Details read from YouTube. Nothing has been downloaded yet.")
        self._fill_formats()
        self._validate()

    def set_error(self, message):
        """A clear explanation instead of a dead window, plus the local-file way out."""
        self.error = message or "The video details could not be read."
        self.meta = None
        self.title_label.setText("Video details unavailable")
        self.channel_label.setText("")
        self.duration_label.setText("")
        self.mode_combo.setEnabled(False)
        self.quality_combo.setEnabled(False)
        self.advanced_box.setEnabled(False)
        self.range_bar.setEnabled(False)
        self.start_edit.setEnabled(False)
        self.end_edit.setEnabled(False)
        self.reset_button.setEnabled(False)
        self._set_status(self.error, error=True)
        self.local_button.setToolTip("Import a video file you already have instead")
        self._validate()

    def _set_thumbnail(self, data):
        if not data:
            self.thumbnail.setText("No preview")
            self.thumbnail.setPixmap(QPixmap())
            return
        picture = QPixmap()
        if not picture.loadFromData(data):
            self.thumbnail.setText("No preview")
            return
        self.thumbnail.setPixmap(picture.scaled(176, 99, Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))

    def _set_status(self, text, *, error=False):
        self._status_error = bool(error)
        self.status.setText(text)
        themed_style(self.status, lambda: "color: "
                      + tokens.COLORS["coral_text" if self._status_error else "muted"] + ";")

    # ---- choices --------------------------------------------------------
    def _fill_modes(self):
        current = self.mode_combo.currentData()
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        available = []
        if self.meta is not None:
            if self.meta.video_formats:
                available += ["video_audio", "video_only"]
            if self.meta.audio_formats or any(f.has_audio for f in self.meta.formats):
                available.append("audio_only")
        for mode in _MODE_ORDER:
            if mode in available:
                self.mode_combo.addItem(youtube.MODE_LABELS[mode], mode)
        if current in available:
            self.mode_combo.setCurrentIndex(available.index(current))
        self.mode_combo.blockSignals(False)
        self.mode_combo.setEnabled(bool(available))

    def mode(self):
        return self.mode_combo.currentData() or "video_audio"

    def select_mode(self, mode):
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        return index >= 0

    def choices(self):
        choices = youtube.choices_for(self.meta, self.mode()) if self.meta else ()
        return tuple(choices)

    def _refresh_choices(self):
        if self.meta is None:
            return
        previous = self.quality_combo.currentData()
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        for choice in self.choices():
            self.quality_combo.addItem(choice.label, choice.key)
        if previous is not None:
            index = self.quality_combo.findData(previous)
            if index >= 0:
                self.quality_combo.setCurrentIndex(index)
        self.quality_combo.blockSignals(False)
        self.quality_combo.setEnabled(self.quality_combo.count() > 0)
        self._fill_formats()
        self._update_note()
        self._validate()

    def _fill_formats(self):
        if self.meta is None:
            self.format_combo.clear()
            return
        previous = self.format_combo.currentData()
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        mode = self.mode()
        for fmt in self.meta.formats:
            if mode == "audio_only" and not fmt.has_audio:
                continue
            if mode != "audio_only" and not fmt.has_video:
                continue
            self.format_combo.addItem(fmt.label, fmt.format_id)
        if previous is not None:
            index = self.format_combo.findData(previous)
            if index >= 0:
                self.format_combo.setCurrentIndex(index)
        self.format_combo.blockSignals(False)
        self._update_note()

    def _advanced_toggled(self, shown):
        self.advanced.setVisible(bool(shown))
        self._update_note()
        self._validate()

    def _update_note(self):
        if self.meta is None:
            self.fetch_note.setText("")
            return
        start, end = self._range_seconds()
        whole = not self.meta.duration or (start <= 0.001
                                           and end >= self.meta.duration - 0.02)
        if whole:
            self.fetch_note.setText("The whole video is downloaded once and kept as it is "
                                    "when no conversion is needed.")
        else:
            self.fetch_note.setText("Only the selected range is requested when the source "
                                    "allows it; otherwise the video is downloaded once and "
                                    "trimmed locally with ffmpeg. Check the free space on the "
                                    "project drive for a full download.")

    # ---- range ----------------------------------------------------------
    def _range_seconds(self):
        if self.meta is None:
            return 0.0, 0.0
        start = youtube.parse_timecode(self.start_edit.text())
        end = youtube.parse_timecode(self.end_edit.text())
        if start < 0:
            start = 0.0
        if end < 0:
            end = self.meta.duration or 0.0
        return start, end

    def _bar_changed(self, start, end):
        self._write_fields(start, end)
        self._validate()

    def _edits_changed(self):
        start = youtube.parse_timecode(self.start_edit.text())
        end = youtube.parse_timecode(self.end_edit.text())
        if start < 0 or end < 0:
            self._validate()
            return
        duration = self.meta.duration if self.meta else 0.0
        start = max(0.0, start)
        if duration:
            start = min(start, duration)
            end = min(end, duration)
        if end <= start:
            self._validate()
            return
        self.range_bar.set_range(start, end)
        self._validate()

    def _write_fields(self, start, end):
        for edit, value in ((self.start_edit, start), (self.end_edit, end)):
            edit.blockSignals(True)
            edit.setText(youtube.format_timecode(value))
            edit.blockSignals(False)

    def set_range(self, start, end):
        """Set both fields and the bar from seconds (used by the link's own timestamp too)."""
        self._write_fields(start, end)
        self.range_bar.set_range(start, end)
        self._update_note()
        self._validate()
        return self._problem

    def reset_range(self):
        duration = self.meta.duration if self.meta else 0.0
        self.set_range(0.0, duration)

    # ---- validation -----------------------------------------------------
    def _validate(self):
        if self.meta is None:
            self._problem = self.error
            self.duration_value.setText("")
            self._enable_import(False)
            return False
        raw_start = youtube.parse_timecode(self.start_edit.text())
        raw_end = youtube.parse_timecode(self.end_edit.text())
        start, end = self._range_seconds()
        if raw_start == -1:
            problem = "The start time is not a timecode like 00:01:30.000."
        elif raw_end == -1:
            problem = "The end time is not a timecode like 00:01:30.000."
        else:
            problem = youtube.check_range(raw_start, raw_end, self.meta.duration)
        if not problem and not self.add_box.isChecked() and not self.insert_box.isChecked():
            problem = "Choose Project Media, the timeline, or both."
        if not problem and not self.permission.isChecked():
            problem = "Confirm that you have permission to use this video."
        self._problem = problem
        if problem:
            self.range_note.setText(problem)
        else:
            self.range_note.setText("")
            self._update_note()
        length = max(0.0, end - start)
        self.duration_value.setText(("Selection: " + youtube.format_timecode(length)
                                     + f" ({length:.2f} s)") if length else "")
        self._enable_import(not problem)
        return not problem

    def _enable_import(self, enabled):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(enabled))

    def range_problem(self):
        return self._problem

    # ---- local fallback -------------------------------------------------
    def _choose_local(self):
        patterns = " ".join("*" + ext for ext in VIDEO_EXT + AUDIO_EXT + IMAGE_EXT)
        path = FD.open_file(self, "Import a local file", "",
                            f"Media ({patterns});;All files (*)")
        if path and os.path.isfile(path):
            self.local_path = path
            self.accept()

    # ---- result ---------------------------------------------------------
    def options(self):
        start, end = self._range_seconds()
        quality = self.quality_combo.currentData() or "best"
        format_id = self.format_combo.currentData() if self.advanced_box.isChecked() else ""
        mode = self.mode()
        request = youtube.ImportRequest(
            mode=mode,
            quality=quality,
            format_id=str(format_id or ""),
            audio_format_id=str(format_id or "") if mode == "audio_only" else "",
            start=start,
            end=end,
            partial=True,
            add=self.add_box.isChecked(),
            insert=self.insert_box.isChecked(),
        )
        return {"request": request, "local_path": self.local_path}


# -------------------------------------------------------------------- progress
class YouTubeProgressDialog(QDialog):
    """Download and conversion progress with a Cancel that really stops the work."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent, meta):
        super().__init__(parent)
        self.setWindowTitle("Importing YouTube Video")
        self.setModal(False)
        self.setMinimumWidth(420)
        self._finished = False
        self.meta = meta
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        self.title = QLabel(meta.title or "YouTube video", self)
        self.title.setWordWrap(True)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        outer.addWidget(self.title)
        self.stage = QLabel("Starting\u2026", self)
        self.stage.setObjectName("dialogNote")
        outer.addWidget(self.stage)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        outer.addWidget(self.bar)
        self.detail = QLabel("", self)
        self.detail.setObjectName("dialogNote")
        outer.addWidget(self.detail)
        note = QLabel("The download goes to a temporary folder and is removed if you cancel.",
                      self)
        note.setWordWrap(True)
        muted_text(note)
        outer.addWidget(note)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self._cancel)
        outer.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignRight)

    def set_progress(self, stage, done, total):
        if self._finished:
            return
        labels = {"Downloading": "Downloading", "Preparing": "Preparing the streams",
                  "Trimming": "Trimming the selection", "Finishing": "Finishing"}
        label = labels.get(stage, stage or "Working")
        self.stage.setText(label + "\u2026")
        if total and total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(max(0.0, min(1.0, done / total)) * 1000))
            self.detail.setText(f"{int(max(0.0, min(1.0, done / total)) * 100)}%")
        else:
            self.bar.setRange(0, 0)
            self.detail.setText("")

    def _cancel(self):
        if self._finished:
            return
        self.cancel_button.setEnabled(False)
        self.stage.setText("Cancelling\u2026")
        self.cancel_requested.emit()

    def closeEvent(self, event):
        if not self._finished:
            self._cancel()
        super().closeEvent(event)

    def finish(self):
        self._finished = True
        self.close()
        self.deleteLater()
