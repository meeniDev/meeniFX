"""Small dialogs for pre-composing clips and editing a composition's settings."""

from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFormLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout)

from .. import tokens


class PrecomposeDialog(QDialog):
    """Name a new composition and say what happens to the selected clips.

    The workflow is explicit and has one behaviour: the selected clips **move** into the
    new composition at the same relative times and are replaced, in the timeline being
    edited, by a single nested composition clip that spans the same range.
    """

    def __init__(self, parent=None, name="Composition", clip_count=1,
                 range_text=""):
        super().__init__(parent)
        self.setWindowTitle("Pre-compose")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SP3, tokens.SP3, tokens.SP3, tokens.SP3)
        layout.setSpacing(tokens.SP2)
        form = QFormLayout()
        form.setSpacing(tokens.SP2)
        self.name_edit = QLineEdit(name)
        self.name_edit.selectAll()
        form.addRow("Composition name", self.name_edit)
        detail = f"{clip_count} clip{'s' if clip_count != 1 else ''}"
        if range_text:
            detail += f" · {range_text}"
        form.addRow("Selected", QLabel(detail))
        layout.addLayout(form)
        note = QLabel(
            "The clips move into the new composition with their timing, effects, "
            "keyframes and linked audio intact. A composition clip replaces them in "
            "this timeline, and you can open it at any time.")
        note.setWordWrap(True)
        note.setProperty("quiet", True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._buttons = buttons
        self.name_edit.textChanged.connect(self._validate)
        self._validate()

    def _validate(self):
        ok = bool(self.name_edit.text().strip())
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def values(self):
        return {"name": self.name_edit.text().strip() or "Composition"}


class CompositionSettingsDialog(QDialog):
    """Name, frame size, rate and duration of one composition definition."""

    def __init__(self, parent=None, comp=None):
        super().__init__(parent)
        self.setWindowTitle("Composition Settings")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SP3, tokens.SP3, tokens.SP3, tokens.SP3)
        layout.setSpacing(tokens.SP2)
        form = QFormLayout()
        form.setSpacing(tokens.SP2)
        self.name_edit = QLineEdit(getattr(comp, "name", "Composition"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 16384)
        self.width_spin.setSuffix(" px")
        self.width_spin.setValue(int(getattr(comp, "width", 1920) or 1920))
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 16384)
        self.height_spin.setSuffix(" px")
        self.height_spin.setValue(int(getattr(comp, "height", 1080) or 1080))
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 240.0)
        self.fps_spin.setDecimals(3)
        self.fps_spin.setSingleStep(0.001)
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.setValue(float(getattr(comp, "fps", 30) or 30))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(0, 1000000)
        self.duration_spin.setSuffix(" frames")
        self.duration_spin.setToolTip(
            "The range the composition covers even when its clips end earlier; "
            "0 means the length of its contents")
        self.duration_spin.setValue(int(getattr(comp, "declared_duration", 0) or 0))
        form.addRow("Name", self.name_edit)
        form.addRow("Width", self.width_spin)
        form.addRow("Height", self.height_spin)
        form.addRow("Frame rate", self.fps_spin)
        form.addRow("Duration", self.duration_spin)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._buttons = buttons
        self.name_edit.textChanged.connect(self._validate)
        self._validate()

    def _validate(self):
        ok = bool(self.name_edit.text().strip())
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def values(self):
        return {
            "name": self.name_edit.text().strip(),
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "fps": self.fps_spin.value(),
            "declared_duration": self.duration_spin.value(),
        }
