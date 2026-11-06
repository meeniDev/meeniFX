"""Tools > Audio: Remove Silences and Normalize.

Both read the audio the mixer already decodes, show what they measured before changing
anything, and commit their edit through the session's undo stack. The source files are
never touched: silence removal splits and trims clips, normalization writes clip gain.
"""

import time

import numpy as np
from PyQt6.QtCore import QObject, QRunnable, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from ..audio import mix, silence
from ..audio.plan import build_segments
from ..model.timeline import SYNC_TOLERANCE
from .. import tokens
from .themed import muted_text

SCOPES = (("linked", "Audio and linked video"),
          ("matching", "Matching source video and audio"),
          ("audio", "Audio only"))


# ------------------------------------------------------------------ shared helpers
def ensure_audio(window, paths, timeout=120.0):
    """Decode `paths` (if needed) without freezing the window; True when all are ready."""
    library = window.waveforms.library
    waiting = [path for path in paths if library.cached(path) is None]
    for path in waiting:
        window.waveforms.request(path)
    deadline = time.monotonic() + timeout
    while waiting and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
        waiting = [path for path in waiting
                   if library.cached(path) is None and library.is_pending(path)]
    return all(library.cached(path) is not None for path in paths)


def clip_audio(window, clip, channels=1):
    """The clip's played audio (trim, speed, reverse and fades applied), mono by default."""
    timeline = window.engine.timeline
    library = window.waveforms.library
    rate = int(library.sample_rate)
    segments = build_segments(timeline, library.cached, rate,
                              window.engine.runtime_effects, only_ids=[clip.id])
    if not segments:
        return np.zeros(0, dtype=np.float32), rate
    count = max(1, int(round(clip.duration / max(1.0, float(timeline.fps)) * rate)))
    block = mix.render(segments, clip.timeline_start / max(1.0, float(timeline.fps)),
                       count, rate, channels)
    return (block[:, 0] if channels == 1 else block), rate


def peak_db(block):
    if block is None or block.size == 0:
        return float("-inf")
    peak = float(np.max(np.abs(block)))
    return 20.0 * np.log10(max(peak, 1e-6))


def anchor_clip(window, clip_id=None):
    """The clip the command was opened for: explicit id, else the best selected one."""
    timeline = window.engine.timeline
    if clip_id is not None:
        return timeline.find_clip(clip_id)
    clips = [timeline.find_clip(cid) for cid in window.engine.session.selected]
    clips = [clip for clip in clips if clip is not None]
    clips.sort(key=lambda clip: (0 if clip.carries_audio else 1,
                                 clip.track.type != "audio", clip.id))
    return clips[0] if clips else None


def synchronised_matches(window, clip, tolerance=SYNC_TOLERANCE):
    """Other-source clips of one media file that sit at the same source moment.

    A reliable match needs the same file, the opposite track type, the same speed and
    direction, and matching sync keys: then the identical timeline frame shows the same
    source frame in both clips. Unrelated clips and drifting offsets are never returned.
    """
    if clip is None:
        return []
    timeline = window.engine.timeline
    matches = []
    for other in timeline.all_clips():
        if other.id == clip.id or other.source_path != clip.source_path:
            continue
        if other.track.type == clip.track.type:
            continue
        if abs(float(other.speed) - float(clip.speed)) > 1e-6:
            continue
        if bool(other.reverse) != bool(clip.reverse) or bool(other.loop) != bool(clip.loop):
            continue
        if abs(other.sync_key() - clip.sync_key()) > tolerance:
            continue
        matches.append(other)
    return matches


def analysis_clip_for(window, anchor):
    """The clip whose audio is measured: the anchor, its linked audio, or a match."""
    if anchor is None:
        return None
    if anchor.carries_audio:
        return anchor
    timeline = window.engine.timeline
    group = timeline.link_group(anchor.link_id) if anchor.link_id else []
    for member in group:
        if member.carries_audio:
            return member
    for match in synchronised_matches(window, anchor):
        if match.carries_audio:
            return match
    return None


def silence_members(window, analysis, scope):
    """Clips one silence pass is allowed to cut, in a stable order.

    * linked: the analysed clip's whole link group;
    * matching: the analysed clip plus every reliably synchronised source match;
    * audio: only the analysed clip.
    """
    if analysis is None:
        return []
    timeline = window.engine.timeline
    if scope == "audio":
        group = [analysis]
    elif scope == "matching":
        group = [analysis, *synchronised_matches(window, analysis)]
    else:
        group = timeline.link_group(analysis.link_id) if analysis.link_id else [analysis]
    unique = []
    for clip in group:
        if clip is not None and all(clip.id != other.id for other in unique):
            unique.append(clip)
    return unique


def audio_clips(window, ids=None):
    """Clips that actually contribute audio, for the given selection or all clips."""
    selected = ids if ids is not None else window.engine.session.selected
    timeline = window.engine.timeline
    clips = [timeline.find_clip(cid) for cid in selected]
    result = []
    for clip in clips:
        if clip is None or not clip.carries_audio or not clip.source_path:
            continue
        if not clip.source_path.lower().endswith((".wav", ".mp3", ".ogg", ".flac", ".aac",
                                                  ".m4a", ".mp4", ".avi", ".mov", ".mkv",
                                                  ".webm", ".m4v")):
            continue
        result.append(clip)
    return result


class _AnalyzeSignals(QObject):
    done = pyqtSignal(object, object, object)     # envelope times, levels, cuts
    failed = pyqtSignal(str)


class _AnalyzeJob(QRunnable):
    def __init__(self, block, rate, settings):
        super().__init__()
        self.block = block
        self.rate = rate
        self.settings = settings
        self.signals = _AnalyzeSignals()
        self.setAutoDelete(False)

    def run(self):
        try:
            times, levels = silence.envelope(self.block, self.rate)
            regions = silence.speech_regions(self.block, self.rate, self.settings)
            duration = len(self.block) / float(self.rate) if self.rate else 0.0
            cuts = silence.cut_plan(regions, duration, self.settings)
        except Exception as exc:      # pragma: no cover - defensive
            self.signals.failed.emit(str(exc))
            return
        self.signals.done.emit(times, levels, cuts)


class CutPreview(QWidget):
    """Waveform strip with the proposed cuts painted red."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.times = np.zeros(0)
        self.levels = np.zeros(0)
        self.cuts = []
        self.duration = 0.0

    def set_analysis(self, times, levels, cuts, duration):
        self.times = np.asarray(times)
        self.levels = np.asarray(levels)
        self.cuts = list(cuts)
        self.duration = max(0.0, float(duration))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.fillRect(rect, QColor(tokens.COLORS["panel_alt"]))
        if self.duration <= 0 or self.times.size == 0:
            painter.setPen(QColor(tokens.COLORS["muted"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Analyze a clip to see its cuts")
            return
        span = max(0.001, self.duration)
        for start, end in self.cuts:
            x0 = rect.left() + rect.width() * max(0.0, start / span)
            x1 = rect.left() + rect.width() * min(span, end) / span
            painter.fillRect(int(x0), rect.top(), max(1, int(x1 - x0)), rect.height(),
                             QColor(214, 69, 69, 90))
        floor = -70.0
        painter.setPen(QPen(QColor(tokens.COLORS["success"]), 1))
        step = max(1, self.times.size // max(1, rect.width()))
        for index in range(0, self.times.size, step):
            x = rect.left() + rect.width() * min(1.0, self.times[index] / span)
            level = max(0.0, min(1.0, (self.levels[index] - floor) / -floor))
            half = int(level * rect.height() / 2)
            painter.drawLine(int(x), rect.center().y() - half, int(x), rect.center().y() + half)


class SilenceDialog(QDialog):
    def __init__(self, window, clip_id=None):
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Remove Dead Silence")
        self.resize(680, 590)
        self._anchor = anchor_clip(window, clip_id)
        self._analysis = analysis_clip_for(window, self._anchor)
        self._members = []
        self._block = None
        self._rate = 48000
        self._cuts = []
        self._job = None
        self._applied = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        if self._analysis is None:
            label = QLabel("Select an audio clip, or a video clip with linked or matching audio, "
                           "first. Remove Dead Silence never edits tracks that are not included "
                           "below.")
            label.setWordWrap(True)
            layout.addWidget(label)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            self.scope = None
            self.analyze_button = self.apply_button = None
            return

        source = self._analysis
        self.header = QLabel(f"Analysing: {source.name}")
        self.header.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.header)

        scope_group = QGroupBox("Clips to cut", self)
        scope_form = QFormLayout(scope_group)
        scope_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.scope = QComboBox()
        for key, label in SCOPES:
            self.scope.addItem(label, key)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        scope_form.addRow("Scope", self.scope)
        self.targets = QLabel("")
        self.targets.setWordWrap(True)
        scope_form.addRow("", self.targets)
        layout.addWidget(scope_group)

        group = QGroupBox("Detection", self)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.preset = QComboBox()
        for label, key in silence.PRESET_ORDER:
            self.preset.addItem(label, key)
        self.preset.setCurrentIndex(1)
        form.addRow("Preset", self.preset)

        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-70.0, -6.0)
        self.threshold.setSuffix(" dBFS")
        self.threshold.setSingleStep(1.0)
        form.addRow("Silence below", self.threshold)

        self.min_silence = QDoubleSpinBox()
        self.min_silence.setRange(0.05, 10.0)
        self.min_silence.setSuffix(" s")
        self.min_silence.setSingleStep(0.05)
        form.addRow("Minimum silence", self.min_silence)

        self.min_speech = QDoubleSpinBox()
        self.min_speech.setRange(0.05, 5.0)
        self.min_speech.setSuffix(" s")
        self.min_speech.setSingleStep(0.05)
        form.addRow("Minimum speech", self.min_speech)

        self.pad_before = QDoubleSpinBox()
        self.pad_before.setRange(0.0, 2.0)
        self.pad_before.setSuffix(" s")
        self.pad_before.setSingleStep(0.02)
        form.addRow("Padding before speech", self.pad_before)

        self.pad_after = QDoubleSpinBox()
        self.pad_after.setRange(0.0, 2.0)
        self.pad_after.setSuffix(" s")
        self.pad_after.setSingleStep(0.02)
        form.addRow("Padding after speech", self.pad_after)

        self.keep_pause = QDoubleSpinBox()
        self.keep_pause.setRange(0.0, 2.0)
        self.keep_pause.setSuffix(" s")
        self.keep_pause.setSingleStep(0.05)
        self.keep_pause.setToolTip("Keep this much of every silence as a natural breath "
                                   "(0 removes the gaps completely)")
        form.addRow("Keep natural pause", self.keep_pause)

        self.min_retained = QDoubleSpinBox()
        self.min_retained.setRange(0.0, 5.0)
        self.min_retained.setSuffix(" s")
        self.min_retained.setSingleStep(0.02)
        self.min_retained.setToolTip("Kept pieces shorter than this are merged into the "
                                     "surrounding cuts instead of leaving a sliver")
        form.addRow("Minimum retained segment", self.min_retained)

        self.crossfade = QCheckBox("Crossfade at the cuts (6 frames)")
        self.crossfade.setChecked(True)
        form.addRow("", self.crossfade)
        self.ripple = QCheckBox("Ripple delete: close the gaps")
        self.ripple.setChecked(True)
        form.addRow("", self.ripple)
        layout.addWidget(group)

        self.summary = QLabel("Press Analyze to find the silent parts.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.preview = CutPreview(self)
        layout.addWidget(self.preview, 1)

        self.preset.currentIndexChanged.connect(self._apply_preset)
        for widget in (self.threshold, self.min_silence, self.min_speech, self.pad_before,
                       self.pad_after, self.keep_pause, self.min_retained):
            widget.valueChanged.connect(self._custom_preset)

        buttons = QHBoxLayout()
        self.analyze_button = QPushButton("Analyze")
        self.analyze_button.clicked.connect(self.analyze)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setProperty("primary", True)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.analyze_button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._choose_default_scope()
        self._apply_preset()
        self._refresh_targets()

    # ---- scope ----------------------------------------------------------
    def _choose_default_scope(self):
        """Prefer an existing link, then a reliable source match, then audio only."""
        clip = self._analysis
        group = self.w.engine.timeline.link_group(clip.link_id) if clip.link_id else []
        if len(group) > 1:
            default = "linked"
        elif synchronised_matches(self.w, clip):
            default = "matching"
        else:
            default = "audio"
        index = self.scope.findData(default)
        if index >= 0:
            self.scope.setCurrentIndex(index)

    def _scope_changed(self, *_args):
        self._cuts = []
        self._block = None
        self.preview.set_analysis([], [], [], 0.0)
        self.summary.setText("Press Analyze to find the silent parts.")
        self.apply_button.setEnabled(False)
        self._refresh_targets()

    def _refresh_targets(self):
        if self._analysis is None:
            return
        self._members = silence_members(self.w, self._analysis, self.scope.currentData())
        self.header.setText(f"Analysing: {self._analysis.name}")
        names = ", ".join(f"{clip.name} ({clip.track.type})" for clip in self._members)
        note = ""
        if self.scope.currentData() == "matching" and len(self._members) < 2:
            note = " No reliable source match was found, so only this clip is cut."
        elif self.scope.currentData() == "linked" and len(self._members) < 2:
            note = " This clip is not linked; only it is cut."
        locked = [clip for clip in self._members if clip.track.locked]
        if locked:
            note += (f"  {locked[0].track.name} is locked, so nothing can be applied until "
                     "it is unlocked.")
        self.targets.setText(f"Will cut: {names}.{note}")
        self.apply_button.setEnabled(bool(self._cuts) and not locked)

    # ---- settings -------------------------------------------------------
    def _apply_preset(self):
        key = self.preset.currentData()
        preset = silence.PRESETS.get(key)
        if preset is None:
            return
        widgets = (self.threshold, self.min_silence, self.min_speech, self.pad_before,
                   self.pad_after, self.keep_pause, self.min_retained)
        for widget in widgets:
            widget.blockSignals(True)
        self.threshold.setValue(preset.threshold_db)
        self.min_silence.setValue(preset.min_silence)
        self.min_speech.setValue(preset.min_speech)
        self.pad_before.setValue(preset.pad_before)
        self.pad_after.setValue(preset.pad_after)
        self.keep_pause.setValue(preset.keep_pause)
        self.min_retained.setValue(preset.min_retained)
        for widget in widgets:
            widget.blockSignals(False)
        self.apply_button.setEnabled(False)

    def _custom_preset(self, *_args):
        self.preset.blockSignals(True)
        index = self.preset.findData("custom")
        if index >= 0:
            self.preset.setCurrentIndex(index)
        self.preset.blockSignals(False)
        self.apply_button.setEnabled(False)

    def settings(self):
        return silence.Settings(
            threshold_db=self.threshold.value(), min_silence=self.min_silence.value(),
            min_speech=self.min_speech.value(), pad_before=self.pad_before.value(),
            pad_after=self.pad_after.value(), keep_pause=self.keep_pause.value(),
            crossfade=self.crossfade.isChecked(), ripple=self.ripple.isChecked(),
            min_retained=self.min_retained.value())

    # ---- analysis -------------------------------------------------------
    def analyze(self):
        clip = self._analysis
        self.summary.setText("Decoding audio…")
        if not ensure_audio(self.w, [clip.source_path]):
            self.summary.setText("That clip's audio could not be decoded.")
            return
        self._block, self._rate = clip_audio(self.w, clip)
        if self._block.size == 0:
            self.summary.setText("The selected clip has no audio to analyse.")
            return
        self._job = _AnalyzeJob(self._block, self._rate, self.settings())
        self._job.signals.done.connect(self._on_analyzed)
        self._job.signals.failed.connect(lambda message: self.summary.setText(message))
        self.analyze_button.setEnabled(False)
        self.summary.setText("Analyzing…")
        self.w.pool.start(self._job)

    def _on_analyzed(self, times, levels, cuts):
        self.analyze_button.setEnabled(True)
        self._cuts = cuts
        duration = len(self._block) / float(self._rate) if self._rate else 0.0
        self.preview.set_analysis(times, levels, cuts, duration)
        removed = silence.total_removed(cuts)
        if cuts:
            count = len(self._members)
            self.summary.setText(
                f"{len(cuts)} silent part{'s' if len(cuts) != 1 else ''} found: "
                f"{removed:.2f} s of {duration:.2f} s would be removed from {count} "
                f"clip{'s' if count != 1 else ''} "
                f"({(removed / duration * 100.0) if duration else 0:.0f} %).")
        else:
            self.summary.setText("No silence matched these settings.")
        self.apply_button.setEnabled(bool(cuts) and all(
            not member.track.locked for member in self._members))

    def apply(self):
        if not self._cuts or not self._members:
            return
        settings = self.settings()
        crossfade = 6 if settings.crossfade else 0
        clip = self._analysis
        member_ids = [member.id for member in self._members]
        changed = self.w.engine.session.remove_silence(
            clip.id, self._cuts, members=member_ids, crossfade_frames=crossfade,
            ripple=settings.ripple)
        if not changed:
            self.summary.setText("Nothing was changed. Check locked tracks and that the "
                                 "included clips overlap on the timeline.")
            return
        self._applied = True
        self.w._refresh_panels()
        self.w._refresh_audio()
        self.w._request_preview()
        self.w.timeline_panel.canvas.update()
        self.w.toast_message(f"Removed {silence.total_removed(self._cuts):.2f} s of dead "
                             f"silence from {len(member_ids)} clip(s)")
        self.accept()


class NormalizeDialog(QDialog):
    """Peak normalization with the measurement shown before anything is applied."""

    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Normalize Audio")
        self.resize(520, 360)
        self._clips = audio_clips(window)
        self._targets = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        intro = QLabel("Peak normalization: each clip's gain is set so its loudest sample "
                       "reaches the target. This is not loudness normalization, and the "
                       "audio file itself is not rewritten.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.target = QDoubleSpinBox()
        self.target.setRange(-24.0, 0.0)
        self.target.setValue(-1.0)
        self.target.setSuffix(" dBFS")
        self.target.setSingleStep(0.5)
        form.addRow("Peak target", self.target)
        layout.addLayout(form)

        self.rows = QLabel("")
        self.rows.setWordWrap(True)
        layout.addWidget(self.rows)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        muted_text(self.status)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.measure_button = QPushButton("Measure")
        self.measure_button.clicked.connect(self.measure)
        self.apply_button = buttons.addButton("Normalize", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setProperty("primary", True)
        self.apply_button.setEnabled(False)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.measure_button)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)

        if not self._clips:
            self.status.setText("Select an audio or video clip first.")
            self.measure_button.setEnabled(False)
        else:
            names = ", ".join(clip.name for clip in self._clips[:4])
            self.rows.setText(f"Selected: {names}"
                              + (" and more" if len(self._clips) > 4 else ""))
            self.target.valueChanged.connect(lambda _value: self.apply_button.setEnabled(False))
            self.measure()

    def measure(self):
        self.status.setText("Measuring peaks…")
        QApplication.processEvents()
        paths = [clip.source_path for clip in self._clips]
        if not ensure_audio(self.w, paths):
            self.status.setText("Some selected clips could not be decoded.")
            return
        self._targets = []
        lines = []
        for clip in self._clips:
            block, _rate = clip_audio(self.w, clip)
            level = peak_db(block)
            if not np.isfinite(level):
                continue
            gain = self.target.value() - level
            self._targets.append((clip.id, gain))
            lines.append(f"{clip.name}: peak {level:.1f} dBFS → gain {gain:+.1f} dB")
        self.rows.setText("\n".join(lines) or "Nothing measurable in the selection.")
        self.status.setText("Apply to set these gains as one undoable step."
                            if self._targets else "")
        self.apply_button.setEnabled(bool(self._targets))

    def apply(self):
        if not self._targets:
            return
        self.w.engine.session.normalize_audio(self._targets)
        self.w._refresh_panels()
        self.w._refresh_audio()
        self.w._request_preview()
        self.w.toast_message(f"Normalized {len(self._targets)} clip(s) to "
                             f"{self.target.value():.1f} dBFS peak")
        self.accept()
