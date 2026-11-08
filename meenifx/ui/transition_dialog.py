"""Settings for one overlap transition (double-click its region on the timeline)."""

from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                             QVBoxLayout)

from ..model import transition as TR


class TransitionDialog(QDialog):
    """Crossfade type, picture curve and audio curve for an existing transition."""

    def __init__(self, transition, window=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Transition")
        layout = QVBoxLayout(self)
        form = QFormLayout()

        form.addRow("Type", QLabel(transition.label))
        self.curve = QComboBox()
        for key, label in TR.VIDEO_CURVES:
            self.curve.addItem(label, key)
        self.curve.setCurrentIndex(max(0, self.curve.findData(transition.curve)))
        form.addRow("Picture curve", self.curve)

        self.audio_curve = QComboBox()
        for key, label in TR.AUDIO_CURVES:
            self.audio_curve.addItem(label, key)
        self.audio_curve.setCurrentIndex(
            max(0, self.audio_curve.findData(transition.audio_curve)))
        form.addRow("Audio curve", self.audio_curve)

        if window is not None:
            frames = max(0, int(window[1]) - int(window[0]))
            form.addRow("Duration", QLabel(f"{frames} frames"))
        layout.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def values(self):
        return {"curve": self.curve.currentData(), "audio_curve": self.audio_curve.currentData()}
