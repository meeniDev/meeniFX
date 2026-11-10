"""Options for Text > Split Text.

Splitting is destructive by nature (one clip becomes many), so the dialog asks first:
which unit, whether the original is replaced, hidden or kept as a duplicate, whether the
pieces get their own track, and - if the user wants it - a stagger and an entrance
animation. With the animation left on None nothing is animated.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLabel,
                             QRadioButton, QSpinBox, QVBoxLayout, QWidget, QHBoxLayout)

from .. import tokens
from ..model import animation as ANIM
from ..model import text as TXT
from ..processing.text import needs_shaping


class SplitTextDialog(QDialog):
    """Collect the Split Text options; :meth:`options` is what the session receives."""

    def __init__(self, clip, parent=None, default_mode="letter"):
        super().__init__(parent)
        self.setWindowTitle("Split Text")
        self.setMinimumWidth(380)
        self._clip = clip
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SP3, tokens.SP3, tokens.SP3, tokens.SP3)
        layout.setSpacing(tokens.SP2)

        self.mode = QComboBox()
        for label, value in (("Characters", "letter"), ("Words", "word"), ("Lines", "line")):
            self.mode.addItem(label, value)
        self.mode.setCurrentIndex(max(0, self.mode.findData(default_mode)))
        self.mode.setToolTip("What each generated text clip contains")
        layout.addWidget(self._row("Split into", self.mode))

        layout.addWidget(self._label("Original clip"))
        self.replace = QRadioButton("Replace the original")
        self.hide = QRadioButton("Keep the original, hidden")
        self.duplicate = QRadioButton("Duplicate and split (keep the original visible)")
        self.replace.setChecked(True)
        for button in (self.replace, self.hide, self.duplicate):
            layout.addWidget(button)
        self.duplicate.setToolTip("The pieces go on a new track above the original picture")

        self.new_track = QCheckBox("Put the split clips on their own track")
        self.new_track.setChecked(True)
        self.new_track.setToolTip("Keeps the timeline tidy; the original track is untouched")
        layout.addWidget(self.new_track)

        layout.addWidget(self._label("Stagger (optional)"))
        self.stagger = QComboBox()
        for label, value in (("Off", "off"), ("By characters", "letter"),
                             ("By words", "word")):
            self.stagger.addItem(label, value)
        self.stagger.setToolTip("Delay each piece a little more than the previous one")
        layout.addWidget(self._row("Stagger", self.stagger))
        self.delay = QSpinBox()
        self.delay.setRange(0, 120)
        self.delay.setValue(3)
        self.delay.setSuffix(" f")
        self.delay.setToolTip("Frames between neighbouring pieces")
        layout.addWidget(self._row("Delay", self.delay))
        self.direction = QComboBox()
        for label, value in (("Forward", "forward"), ("Reverse", "reverse"),
                             ("From the centre", "center"), ("Random", "random")):
            self.direction.addItem(label, value)
        layout.addWidget(self._row("Direction", self.direction))
        self.preset = QComboBox()
        self.preset.addItem("None (no animation)", "None")
        for name in ANIM.TEXT_IN_PRESETS:
            self.preset.addItem(name, name)
        self.preset.setToolTip("Entrance animation applied to each piece; None by default")
        layout.addWidget(self._row("Animation", self.preset))
        self.duration = QSpinBox()
        self.duration.setRange(1, max(1, int(clip.duration) - 1))
        self.duration.setValue(max(1, min(12, max(1, int(clip.duration) - 1))))
        self.duration.setSuffix(" f")
        self.duration.setToolTip("Length of each piece's entrance animation")
        layout.addWidget(self._row("Animation length", self.duration))

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setProperty("quiet", True)
        layout.addWidget(self.note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.mode.currentIndexChanged.connect(self._update)
        self.preset.currentIndexChanged.connect(self._update)
        self.stagger.currentIndexChanged.connect(self._update)
        self._update()

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setStyleSheet(f"font-weight: 600; font-size: {tokens.FONT_TITLE}px;")
        return label

    @staticmethod
    def _row(text, widget):
        row = QWidget()
        holder = QHBoxLayout(row)
        holder.setContentsMargins(0, 0, 0, 0)
        holder.setSpacing(tokens.SP2)
        label = QLabel(text)
        label.setFixedWidth(120)
        holder.addWidget(label)
        holder.addWidget(widget, 1)
        return row

    def _update(self):
        animated = self.preset.currentData() not in (None, "None")
        for widget in (self.stagger, self.delay, self.direction, self.duration):
            widget.setEnabled(animated)
        if not animated:
            self.stagger.setCurrentIndex(0)
        notes = []
        mode = self.mode.currentData()
        text = TXT.content_at(self._clip, 0)
        if mode == "letter" and needs_shaping(text):
            notes.append("This script needs contextual shaping. Splitting characters can "
                         "change letter joining; Words or Lines keep the shaping intact.")
        if float(TXT.ensure_text(self._clip).get("bg_opacity", 0) or 0) > 0.001:
            notes.append("The background plate covers the whole block, so it is removed from "
                         "the pieces rather than duplicated behind every character.")
        if TXT.content_keys(self._clip):
            notes.append("Only the words shown at the playhead are split; text keyframes "
                         "stay on the original clip.")
        self.note.setText(" ".join(notes))
        self.note.setVisible(bool(notes))

    def options(self):
        preset = self.preset.currentData()
        stagger = None
        if preset not in (None, "None"):
            unit = self.stagger.currentData()
            if unit != "off":
                stagger = {"unit": unit, "delay": int(self.delay.value()),
                           "direction": self.direction.currentData(),
                           "preset": preset, "duration": int(self.duration.value()),
                           "easing": ANIM.DEFAULT_EASING}
            else:
                # A chosen animation without stagger still applies to every piece at once.
                stagger = {"unit": "off", "delay": 0,
                           "direction": "forward", "preset": preset,
                           "duration": int(self.duration.value()),
                           "easing": ANIM.DEFAULT_EASING}
        if self.duplicate.isChecked():
            action = "duplicate"
        elif self.hide.isChecked():
            action = "hide"
        else:
            action = "replace"
        return {"action": action, "new_track": bool(self.new_track.isChecked()),
                "stagger": stagger}

    def mode_value(self):
        return str(self.mode.currentData() or "letter")
