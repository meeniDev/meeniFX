"""Contextual Roto Brush toolbar shown directly above the preview picture."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QSpinBox, QToolButton, QWidget)

from .. import tokens
from ..icons import icon

PREVIEW_MODES = (
    ("original", "Original"),
    ("outline", "Outline"),
    ("isolated", "Isolated Subject"),
    ("alpha", "Alpha Matte"),
    ("checkerboard", "Transparency Checkerboard"),
    ("composite", "Composite"),
)


class RotoToolbar(QWidget):
    """One compact strip for the editing actions; Properties keeps the effect values."""

    tool_changed = pyqtSignal(str)
    brush_size_changed = pyqtSignal(float)
    undo_requested = pyqtSignal()
    redo_requested = pyqtSignal()
    propagate_requested = pyqtSignal(str)
    analyze_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    freeze_changed = pyqtSignal(bool)
    preview_changed = pyqtSignal(str)
    subject_requested = pyqtSignal()
    model_requested = pyqtSignal()
    done_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rotoToolbar")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(tokens.SP1 + 2, 3, tokens.SP1 + 2, 3)
        layout.setSpacing(3)
        title = QLabel("Roto Brush")
        title.setObjectName("rotoToolbarTitle")
        layout.addWidget(title)
        layout.addWidget(self._separator())

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.fg_button = self._button("roto_fg", "Foreground Brush (B): drag to keep the subject")
        self.bg_button = self._button("roto_bg", "Background Brush (Alt+drag): drag to remove")
        self.refine_button = self._button("roto_refine", "Refine Edge: guide a difficult boundary")
        self.subject_button = self._button(
            "subject", "Choose Subject: then click the person, pet or object to isolate")
        for index, button in enumerate((self.fg_button, self.bg_button,
                                        self.refine_button)):
            button.setCheckable(True)
            self._group.addButton(button, index)
            layout.addWidget(button)
        self.fg_button.setChecked(True)
        layout.addWidget(self.subject_button)
        layout.addWidget(self._separator())

        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 35)
        self.size_spin.setValue(5)
        self.size_spin.setSuffix("%")
        self.size_spin.setFixedWidth(55)
        self.size_spin.setToolTip("Brush radius as a percentage of the picture's short edge. "
                                  "Use [ and ] to change it without leaving the picture.")
        self.size_spin.valueChanged.connect(
            lambda value: self.brush_size_changed.emit(float(value) / 100.0))
        layout.addWidget(QLabel("Brush"))
        layout.addWidget(self.size_spin)
        layout.addWidget(self._separator())

        self.undo_button = self._button("undo", "Undo Stroke (Ctrl+Z)")
        self.redo_button = self._button("redo", "Redo Stroke (Ctrl+Shift+Z)")
        self.back_button = self._button("propagate_back", "Propagate Backward: analyze earlier frames")
        self.forward_button = self._button("propagate_forward", "Propagate Forward: analyze later frames")
        self.analyze_button = self._button("play", "Analyze Clip: track the subject through the whole clip")
        self.stop_button = self._button("stop", "Stop Analysis")
        self.stop_button.setVisible(False)
        for button in (self.undo_button, self.redo_button, self.back_button,
                       self.forward_button, self.analyze_button, self.stop_button):
            layout.addWidget(button)
        layout.addWidget(self._separator())

        self.freeze_button = self._button(
            "freeze", "Freeze: keep the analyzed masks on disk and stop re-analysis",
            checkable=True)
        layout.addWidget(self.freeze_button)
        layout.addWidget(QLabel("Preview"))
        self.preview_combo = QComboBox()
        for key, label in PREVIEW_MODES:
            self.preview_combo.addItem(label, key)
        self.preview_combo.setCurrentIndex(self.preview_combo.findData("composite"))
        self.preview_combo.setToolTip("Choose what the preview shows. Export always uses Composite")
        self.preview_combo.setFixedWidth(164)
        self.preview_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self.preview_combo)
        layout.addStretch(1)

        self.status = QLabel("")
        self.status.setObjectName("rotoStatus")
        self.status.setProperty("quiet", True)
        layout.addWidget(self.status)
        self.model_button = self._button(
            "ai", "AI model status and download", text="AI")
        self.model_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.model_button.setMinimumWidth(58)
        layout.addWidget(self.model_button)
        self.done_button = QPushButton("Done")
        self.done_button.setToolTip("Leave Roto Brush and restore the Selection tool (Esc)")
        self.done_button.setFixedHeight(tokens.CONTROL_H)
        self.done_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self.done_button)

        self._group.idClicked.connect(self._tool_clicked)
        self.undo_button.clicked.connect(self.undo_requested.emit)
        self.redo_button.clicked.connect(self.redo_requested.emit)
        self.back_button.clicked.connect(lambda _=False: self.propagate_requested.emit("backward"))
        self.forward_button.clicked.connect(lambda _=False: self.propagate_requested.emit("forward"))
        self.analyze_button.clicked.connect(self.analyze_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.freeze_button.toggled.connect(self.freeze_changed.emit)
        self.preview_combo.currentIndexChanged.connect(
            lambda _index: self.preview_changed.emit(
                str(self.preview_combo.currentData() or "composite")))
        self.subject_button.clicked.connect(self.subject_requested.emit)
        self.model_button.clicked.connect(self.model_requested.emit)
        self.done_button.clicked.connect(self.done_requested.emit)

    def _button(self, icon_name, tooltip, checkable=False, text=""):
        button = QToolButton()
        button.setIcon(icon(icon_name))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text
                                  else Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setText(text)
        button.setProperty("role", "icon")
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
        if text:
            button.setMinimumWidth(58)
            button.setMaximumWidth(72)
        return button

    @staticmethod
    def _separator():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(18)
        line.setStyleSheet("color: rgba(128, 128, 128, 80);")
        return line

    def _tool_clicked(self, index):
        self.tool_changed.emit(("foreground", "background", "refine")[int(index)])

    def set_tool(self, mode):
        mapping = {"foreground": self.fg_button, "background": self.bg_button,
                   "refine": self.refine_button}
        button = mapping.get(str(mode))
        if button is not None and not button.isChecked():
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)

    def set_running(self, running):
        self.analyze_button.setVisible(not bool(running))
        self.stop_button.setVisible(bool(running))
        for button in (self.back_button, self.forward_button, self.freeze_button):
            button.setEnabled(not bool(running))

    def set_model_status(self, label, detail, ready=False):
        self.model_button.setText("AI")
        colour = tokens.COLORS.get("accent") if ready else tokens.COLORS.get("muted")
        self.model_button.setIcon(icon("ai", colour))
        self.model_button.setToolTip(f"{label}. {detail}")

    def set_state(self, analysed=0, total=0, frozen=False, preview="composite",
                  backend="", running=False, model_label="", model_detail=""):
        self.freeze_button.blockSignals(True)
        self.freeze_button.setChecked(bool(frozen))
        self.freeze_button.blockSignals(False)
        index = self.preview_combo.findData(str(preview or "composite"))
        if index < 0:
            index = self.preview_combo.findData("composite")
        self.preview_combo.blockSignals(True)
        self.preview_combo.setCurrentIndex(index)
        self.preview_combo.blockSignals(False)
        marker = {"u2net": "AI", "basic": "Basic"}.get(backend, "")
        text = f"{int(analysed)}/{int(total)}" if total else "No masks"
        if marker:
            text += f" {marker}"
        self.status.setText(text)
        self.set_running(running)
        if model_label:
            self.set_model_status(model_label, model_detail, backend == "u2net")
