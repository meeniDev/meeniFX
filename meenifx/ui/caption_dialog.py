"""Auto Captions: the generation window and the transcript editor.

Generation is deliberately two-stage: the analysed audio and the speech recognition run on
a worker thread with a Cancel button, and captions are written to the timeline only after
transcription has finished. A cancelled run therefore leaves the project exactly as it was.
"""

import os
import time

from PyQt6.QtCore import QEventLoop, QObject, QRunnable, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..audio import mix
from ..audio.plan import build_segments
from ..captions import model as caption_model
from ..captions import styles as caption_styles
from ..captions import subtitles
from ..captions import whisper

MAX_SECONDS = 30 * 60
SOURCES = (("Entire timeline", "timeline"), ("Selected clip", "clip"),
           ("Selected audio track", "track"), ("Selected range", "range"))
GROUPINGS = (("Short phrases", "phrase"), ("Full sentences", "sentence"),
             ("Word-by-word", "word"))


class _CaptionSignals(QObject):
    progress = pyqtSignal(float, str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str, bool)


class _CaptionJob(QRunnable):
    """Render the analysed range and transcribe it; never touches the timeline."""

    def __init__(self, segments, start_seconds, duration_seconds, language, quality, cancel):
        super().__init__()
        self.segments = segments
        self.start_seconds = float(start_seconds)
        self.duration_seconds = float(duration_seconds)
        self.language = language
        self.quality = quality
        self.cancel = cancel
        self.signals = _CaptionSignals()
        self.setAutoDelete(False)

    def run(self):
        try:
            count = max(1, int(round(self.duration_seconds * whisper.SAMPLE_RATE)))
            self.signals.progress.emit(0.01, "Preparing audio…")
            block = mix.render(self.segments, self.start_seconds, count, whisper.SAMPLE_RATE, 1)
            if self.cancel.is_set():
                raise InterruptedError("cancelled")
            result = whisper.transcribe(block[:, 0], whisper.SAMPLE_RATE, self.language,
                                        self.quality,
                                        on_progress=lambda fraction, message:
                                        self.signals.progress.emit(fraction, message),
                                        cancelled=self.cancel.is_set)
        except InterruptedError:
            self.signals.failed.emit("Captions cancelled", True)
            return
        except Exception as exc:
            self.signals.failed.emit(str(exc), False)
            return
        self.signals.done.emit(result)


def _audible_sources(window, clip_ids):
    from ..audio.plan import audible_clips
    allowed = set(clip_ids) if clip_ids is not None else None
    paths = []
    for clip, _gain in audible_clips(window.engine.timeline):
        if allowed is not None and clip.id not in allowed:
            continue
        if clip.source_path and clip.source_path not in paths:
            paths.append(clip.source_path)
    return paths


class CaptionDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Generate Auto Captions")
        self.setMinimumWidth(520)
        self._job = None
        self._cancel = None
        self._running = False
        self._result = None
        self._range = None
        self._job_range = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)

        source_group = QGroupBox("Audio source", self)
        source_form = QFormLayout(source_group)
        source_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.source = QComboBox()
        for label, value in SOURCES:
            self.source.addItem(label, value)
        self.source.setCurrentIndex(0)
        source_form.addRow("Analyse", self.source)
        self.range_label = QLabel("")
        self.range_label.setWordWrap(True)
        self.range_label.setStyleSheet("color: #8a8f9a;")
        source_form.addRow("", self.range_label)
        layout.addWidget(source_group)

        model_group = QGroupBox("Recognition", self)
        model_form = QFormLayout(model_group)
        model_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.language = QComboBox()
        for label, code in whisper.LANGUAGES:
            self.language.addItem(label, code)
        model_form.addRow("Language", self.language)
        self.quality = QComboBox()
        for key in ("fast", "balanced", "accurate"):
            info = whisper.QUALITY[key]
            self.quality.addItem(f"{info['label']} ({info['model']}, {info['size']})", key)
        self.quality.setCurrentIndex(1)
        model_form.addRow("Model quality", self.quality)
        self.backend_note = QLabel("")
        self.backend_note.setWordWrap(True)
        model_form.addRow("", self.backend_note)
        layout.addWidget(model_group)

        caption_group = QGroupBox("Captions", self)
        caption_form = QFormLayout(caption_group)
        caption_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.grouping = QComboBox()
        for label, value in GROUPINGS:
            self.grouping.addItem(label, value)
        caption_form.addRow("Grouping", self.grouping)
        self.max_words = QSpinBox()
        self.max_words.setRange(1, 20)
        self.max_words.setValue(7)
        self.max_words.setSuffix(" words")
        caption_form.addRow("Maximum per caption", self.max_words)
        self.style = QComboBox()
        for style_id in caption_styles.STYLE_ORDER:
            self.style.addItem(caption_styles.STYLES[style_id]["name"], style_id)
        caption_form.addRow("Style", self.style)
        self.position = QComboBox()
        self.position.addItem("Style default", "")
        for key in caption_styles.POSITION_ORDER:
            self.position.addItem(caption_styles.POSITION_LABELS[key], key)
        caption_form.addRow("Position", self.position)
        self.animation = QComboBox()
        self.animation.addItem("Style default", None)
        for label, preset in caption_styles.ANIMATION_LABELS:
            self.animation.addItem(label, preset)
        caption_form.addRow("Animation", self.animation)
        self.replace = QCheckBox("Replace existing captions in the analysed range")
        self.replace.setChecked(True)
        caption_form.addRow("", self.replace)
        layout.addWidget(caption_group)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.transcript_button = QPushButton("Transcript Editor…")
        self.transcript_button.clicked.connect(self.open_transcript)
        buttons.addWidget(self.transcript_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._stop)
        self.cancel_button.setVisible(False)
        self.generate_button = QPushButton("Generate")
        self.generate_button.setProperty("primary", True)
        self.generate_button.clicked.connect(self.generate)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.generate_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.source.currentIndexChanged.connect(self._refresh_range)
        self._refresh_range()
        self._refresh_backend()

    # ---- context --------------------------------------------------------
    def _refresh_backend(self):
        if whisper.backend_available():
            self.backend_note.setText("Transcription runs locally. The first use of a model "
                                      "downloads it after you agree; afterwards it works offline.")
            self.backend_note.setStyleSheet("color: #8a8f9a;")
        else:
            self.backend_note.setText("faster-whisper is not installed, so speech recognition is "
                                      "unavailable in this build. You can still import an SRT.")

    def _refresh_range(self, *_args):
        source = self.source.currentData()
        region = self.w.engine.session.caption_source_range(source)
        self._range = region
        if region is None:
            text = {"clip": "Select a clip on the timeline first.",
                    "track": "Select a clip on the track you want to analyse.",
                    "range": "Set a loop/selection range first (I and O)."}.get(
                        source, "The timeline is empty.")
            self.range_label.setText(text)
            self.generate_button.setEnabled(False)
            return
        start, end, ids = region
        fps = max(1, self.w.engine.timeline.fps)
        seconds = max(0.0, (end - start) / float(fps))
        self.range_label.setText(f"{start / fps:.2f}s – {end / fps:.2f}s  ({seconds:.1f} s)"
                                 + (f", {len(ids)} clip(s)" if ids else ", whole mix"))
        too_long = seconds > MAX_SECONDS
        self.generate_button.setEnabled(not too_long and not self._running)
        if too_long:
            self.range_label.setText(self.range_label.text()
                                     + f"\nThat is longer than {MAX_SECONDS // 60} minutes; "
                                       "select a shorter range.")

    # ---- generation -----------------------------------------------------
    def generate(self):
        if self._running:
            return
        if not whisper.backend_available():
            QMessageBox.information(self, "Auto Captions", whisper.install_hint())
            return
        region = self.w.engine.session.caption_source_range(self.source.currentData())
        if region is None:
            self._refresh_range()
            return
        start, end, ids = region
        fps = max(1, self.w.engine.timeline.fps)
        seconds = max(0.0, (end - start) / float(fps))
        if seconds <= 0.05:
            QMessageBox.information(self, "Auto Captions", "That range has no audio to analyse.")
            return
        timeline = self.w.engine.timeline
        library = self.w.waveforms.library
        paths = _audible_sources(self.w, ids)
        if not paths:
            QMessageBox.information(self, "Auto Captions",
                                    "No audible media was found in that range.")
            return
        self._status("Decoding audio…", 0.0)
        if not self._ensure_audio(paths):
            self._status("The audio for that range could not be decoded.", None)
            return
        segments = build_segments(timeline, library.cached, library.sample_rate,
                                  self.w.engine.runtime_effects, only_ids=ids)
        if not segments:
            QMessageBox.information(self, "Auto Captions",
                                    "That range has no decoded audio to transcribe.")
            return

        name = whisper.model_name(self.quality.currentData())
        if not whisper.model_installed(name):
            size = whisper.QUALITY[self.quality.currentData()]["size"]
            answer = QMessageBox.question(
                self, "Download speech model",
                f"The {name} model ({size}) has to be downloaded once before transcription.\n\n"
                "It is stored in your meeniFX data folder and can be deleted again from "
                "Preferences > Caption Models. Download it now?")
            if answer != QMessageBox.StandardButton.Yes:
                return

        import threading
        self._cancel = threading.Event()
        # Remember the exact range the job was started for: the live range is recomputed
        # when the job ends and can be gone by then (selection cleared, source changed).
        self._job_range = (start, end, ids)
        self._job = _CaptionJob(segments, start / float(fps), seconds,
                                str(self.language.currentData() or ""),
                                str(self.quality.currentData() or "balanced"), self._cancel)
        self._job.signals.progress.connect(self._on_progress)
        self._job.signals.done.connect(self._on_done)
        self._job.signals.failed.connect(self._on_failed)
        self._set_running(True)
        self._status("Starting transcription…", 0.0)
        self.w.pool.start(self._job)

    def _ensure_audio(self, paths):
        library = self.w.waveforms.library
        missing = [path for path in paths if library.cached(path) is None]
        for path in missing:
            self.w.waveforms.request(path)
        deadline = time.monotonic() + 120.0
        loop = QEventLoop()
        while missing and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
            missing = [path for path in missing
                       if library.cached(path) is None and library.is_pending(path)]
            if not missing:
                break
            if time.monotonic() > deadline:
                break
        del loop
        return all(library.cached(path) is not None for path in paths)

    def _set_running(self, running):
        self._running = running
        self.generate_button.setVisible(not running)
        self.cancel_button.setVisible(running)
        self.close_button.setEnabled(not running)
        self.transcript_button.setEnabled(not running)
        self.progress.setVisible(running)
        self.status.setVisible(True)
        if running:
            self.progress.setValue(0)
        else:
            self._refresh_range()

    def _status(self, message, fraction=None):
        self.status.setVisible(True)
        self.status.setText(message)
        if fraction is not None:
            self.progress.setVisible(True)
            self.progress.setValue(int(max(0.0, min(1.0, float(fraction))) * 1000))

    def _on_progress(self, fraction, message):
        self._status(message, fraction)

    def _on_done(self, result):
        self._result = result
        region = self._job_range
        self._set_running(False)
        self._job_range = None
        self.progress.setVisible(False)
        cues = caption_model.cues(result.get("segments") or (),
                                  self.grouping.currentData(),
                                  self.max_words.value())
        if not cues:
            self._status("No speech was recognised in that range.")
            return
        if region is None:
            self._status("The timeline changed while transcribing, so the captions were "
                         "not added. Generate again to caption the current range.")
            self._job = None
            self._cancel = None
            return
        start, end, _ids = region
        style_id = self.style.currentData()
        position = self.position.currentData() or None
        animation = self.animation.currentData()
        if animation is None:
            animation = caption_styles.style(style_id)["animation"]
        width, height = self.w.engine.project_size() or (1920, 1080)
        replace = (start, end) if self.replace.isChecked() else None
        ids = self.w.engine.session.generate_captions(
            cues, style_id=style_id, position=position, animation=animation,
            start_frame=start, width=width, height=height, replace_range=replace)
        self._status(f"{len(ids)} caption{'s' if len(ids) != 1 else ''} created"
                     f" ({str(result.get('language') or 'unknown').upper()}).")
        self.w._refresh_panels()
        self.w._request_preview()
        self.w.timeline_panel.canvas.update()
        self._cancel = None
        self._job = None

    def _on_failed(self, message, cancelled):
        self._job_range = None
        self._set_running(False)
        self.progress.setVisible(False)
        self._status(message if cancelled else f"Captions failed: {message}")
        if not cancelled:
            QMessageBox.warning(self, "Auto Captions", message)
        self._cancel = None
        self._job = None

    def _stop(self):
        if self._cancel is not None:
            self._cancel.set()
            self._status("Cancelling…", 0.0)

    def open_transcript(self):
        TranscriptDialog(self.w, self).exec()

    def closeEvent(self, event):
        if self._cancel is not None:
            self._cancel.set()
        super().closeEvent(event)


class TranscriptDialog(QDialog):
    """Correct recognised words and nudge caption timing, in sync with the timeline."""

    def __init__(self, window, parent=None):
        super().__init__(parent or window)
        self.w = window
        self.setWindowTitle("Transcript Editor")
        self.resize(680, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        self.rows = caption_model.transcript_rows(window.engine.timeline,
                                                  window.engine.timeline.fps)
        self.table = QTableWidget(len(self.rows), 3, self)
        self.table.setHorizontalHeaderLabels(["Start (s)", "End (s)", "Text"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for row, entry in enumerate(self.rows):
            start = QDoubleSpinBox()
            start.setDecimals(2)
            start.setRange(0.0, 100000.0)
            start.setSingleStep(0.05)
            start.setValue(entry["start"])
            end = QDoubleSpinBox()
            end.setDecimals(2)
            end.setRange(0.0, 100000.0)
            end.setSingleStep(0.05)
            end.setValue(entry["end"])
            self.table.setCellWidget(row, 0, start)
            self.table.setCellWidget(row, 1, end)
            self.table.setItem(row, 2, QTableWidgetItem(entry["text"]))
        self.table.setColumnWidth(0, 92)
        self.table.setColumnWidth(1, 92)
        layout.addWidget(self.table, 1)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style for all captions"))
        self.style = QComboBox()
        for style_id in caption_styles.STYLE_ORDER:
            self.style.addItem(caption_styles.STYLES[style_id]["name"], style_id)
        style_row.addWidget(self.style, 1)
        self.apply_style = QPushButton("Apply style")
        self.apply_style.clicked.connect(self._apply_style)
        style_row.addWidget(self.apply_style)
        layout.addLayout(style_row)

        note = QLabel("Times are in seconds. Changes are applied to the timeline as one undo step.")
        note.setStyleSheet("color: #8a8f9a;")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox()
        apply_button = buttons.addButton("Apply changes", QDialogButtonBox.ButtonRole.AcceptRole)
        apply_button.setProperty("primary", True)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if not self.rows:
            self.table.setDisabled(True)
            apply_button.setDisabled(True)

    def _edits(self):
        edits = []
        for row, entry in enumerate(self.rows):
            start = self.table.cellWidget(row, 0).value()
            end = self.table.cellWidget(row, 1).value()
            item = self.table.item(row, 2)
            edits.append({"id": entry["id"],
                          "start": int(round(start * self.w.engine.timeline.fps)),
                          "end": int(round(max(start + 0.05, end) * self.w.engine.timeline.fps)),
                          "text": item.text() if item is not None else entry["text"]})
        return edits

    def _apply(self):
        changes = self._edits()
        if changes:
            self.w.engine.session.update_captions(changes)
            self.w._refresh_panels()
            self.w._request_preview()
            self.w.timeline_panel.canvas.update()
        self.accept()

    def _apply_style(self):
        style_id = self.style.currentData()
        ids = {entry["id"] for entry in self.rows}
        if ids:
            self.w.engine.session.restyle_captions(ids, style_id)
            self._refresh_texts()
            self.w._refresh_panels()
            self.w._request_preview()

    def _refresh_texts(self):
        from ..model import text as TXT
        timeline = self.w.engine.timeline
        for row, entry in enumerate(self.rows):
            clip = timeline.find_clip(entry["id"])
            if clip is None:
                continue
            item = self.table.item(row, 2)
            if item is not None:
                item.setText(TXT.content_at(clip, 0))
