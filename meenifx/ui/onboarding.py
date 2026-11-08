"""Small, non-blocking first-launch tour.

The tour is intentionally a set of short pages rather than a slideshow of screenshots:
each page points at the real panel it talks about, and the user can keep working while it
is open. Completion lives in the user's preferences (never in a project file), so opening
an existing project or installing an update does not bring it back.

Help > Getting Started opens the same tour on demand; that action ignores the completion
flag and simply restarts it.
"""

import weakref

from PyQt6.QtCore import QRect, QTimer, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

from .. import tokens
from .layout import app_settings

COMPLETED_KEY = "onboarding/completed"

# ``target`` names are resolved by MainWindow.tutorial_target(); None means "no spotlight".
STEPS = (
    {"title": "Welcome to meeniFX Studios",
     "body": "A calm place to turn footage into a finished video. This short tour shows "
             "where the everyday tools live; you can skip it and keep working at any time.",
     "tip": "Everything here is non-modal, so the editor stays usable while you read.",
     "target": None},
    {"title": "Create a Project",
     "body": "Start with New Project, then choose the frame size and frame rate. The "
             "Project panel keeps your media and recent work together.",
     "tip": "Open an existing .mfxpf project from the start window whenever you like.",
     "target": "project"},
    {"title": "Import Media",
     "body": "Use Import, or drop videos, pictures and audio straight onto the Project "
             "panel or the timeline.",
     "tip": "Imported clips become timeline clips without changing the original files.",
     "target": "project"},
    {"title": "Edit on the Timeline",
     "body": "Drag clips to move them, pull their edges to trim, and use the blade to "
             "split. Snapping keeps cuts, markers and the playhead aligned.",
     "tip": "The timeline toolbar also has track height, zoom and overlap settings.",
     "target": "timeline"},
    {"title": "Preview and Transform",
     "body": "The Preview shows the finished frame. Drag the clip or its handles to move, "
             "scale and rotate it; the Properties panel holds the exact values.",
     "tip": "Fit, zoom and guides never change what gets exported.",
     "target": "preview"},
    {"title": "Add Effects and Animations",
     "body": "Pick an effect or animation from the Effects browser. Its controls appear "
             "in Properties, where you can keyframe values and fine-tune the result.",
     "tip": "Effects are applied to the selected clip only, and every change is undoable.",
     "target": "effects"},
    {"title": "Export Your Video",
     "body": "When the timeline looks right, choose Export to set the format, size and "
             "quality. The exported file uses the project frame, not the preview styling.",
     "tip": "You can reopen this tour later from Help > Getting Started.",
     "target": "export"},
)


def _as_bool(value):
    return value in (True, "true", "True", "1", 1)


def has_completed(store=None):
    """True once the tour has been finished, skipped or dismissed."""
    settings = store if store is not None else app_settings()
    return _as_bool(settings.value(COMPLETED_KEY, False))


def mark_completed(store=None):
    settings = store if store is not None else app_settings()
    settings.setValue(COMPLETED_KEY, True)
    sync = getattr(settings, "sync", None)
    if callable(sync):
        sync()


class Spotlight(QWidget):
    """A transparent outline around a real widget, without taking mouse input."""

    def __init__(self, host):
        super().__init__(host)
        self._host = host
        self._target = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._follow)
        self.hide()

    def highlight(self, widget):
        self._target = weakref.ref(widget) if widget is not None else None
        if widget is None or not widget.isVisible():
            self.hide()
            self._timer.stop()
            return
        self.setGeometry(self._host.rect())
        self.show()
        self.raise_()
        self._timer.start()
        self.update()

    def clear(self):
        self._timer.stop()
        self._target = None
        self.hide()

    def _target_widget(self):
        if self._target is None:
            return None
        widget = self._target()
        try:
            return widget if widget is not None and widget.isVisible() else None
        except RuntimeError:
            return None    # the C++ widget was already destroyed

    def _follow(self):
        if self._target_widget() is None:
            self.clear()
            return
        if self.geometry() != self._host.rect():
            self.setGeometry(self._host.rect())
        self.update()

    def paintEvent(self, _event):
        target = self._target_widget()
        if target is None:
            return
        top_left = target.mapTo(self._host, target.rect().topLeft())
        rect = QRect(top_left, target.size()).adjusted(-3, -3, 3, 3)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 2))
        painter.drawRoundedRect(rect, 8, 8)
        painter.end()


class OnboardingDialog(QDialog):
    """The first-launch tour: welcome, six short pages, Next/Back/Skip/Finish."""

    def __init__(self, host, store=None, parent=None):
        super().__init__(parent or host)
        self.host = host
        self._store = store
        self._index = 0
        self._spotlight = Spotlight(host)
        self.setObjectName("onboardingDialog")
        self.setWindowTitle("Getting Started")
        self.setModal(False)
        self.setMinimumWidth(420)
        self.setMaximumWidth(500)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(tokens.SP4, tokens.SP3, tokens.SP4, tokens.SP3)
        outer.setSpacing(tokens.SP2)

        heading = QLabel("GETTING STARTED")
        heading.setProperty("role", "section")
        outer.addWidget(heading)
        self.progress = QLabel()
        self.progress.setProperty("quiet", True)
        outer.addWidget(self.progress)
        self.title = QLabel()
        self.title.setProperty("role", "heading")
        self.title.setWordWrap(True)
        outer.addWidget(self.title)
        self.body = QLabel()
        self.body.setWordWrap(True)
        outer.addWidget(self.body)
        self.tip = QLabel()
        self.tip.setProperty("quiet", True)
        self.tip.setWordWrap(True)
        outer.addWidget(self.tip)
        outer.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(tokens.SP1)
        self.skip_button = QPushButton("Skip")
        self.skip_button.setProperty("kind", "ghost")
        self.skip_button.setToolTip("Stop the tour; Help > Getting Started opens it again")
        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        self.next_button.setProperty("primary", True)
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        outer.addLayout(buttons)

        self.skip_button.clicked.connect(self._skip)
        self.back_button.clicked.connect(self._back)
        self.next_button.clicked.connect(self._next)
        self._apply_step()

    # ------------------------------------------------------------------ navigation
    def restart(self):
        self._index = 0
        self._apply_step()
        self._place()
        self.show()
        self.raise_()
        self.activateWindow()

    def _back(self):
        if self._index > 0:
            self._index -= 1
            self._apply_step()

    def _next(self):
        if self._index < len(STEPS) - 1:
            self._index += 1
            self._apply_step()
        else:
            self._finish()

    def _skip(self):
        self._finish()

    def _finish(self):
        mark_completed(self._store)
        self._spotlight.clear()
        self.accept()

    def _apply_step(self):
        step = STEPS[self._index]
        self.progress.setText(f"Step {self._index + 1} of {len(STEPS)}")
        self.title.setText(step["title"])
        self.body.setText(step["body"])
        self.tip.setText(step["tip"])
        self.back_button.setEnabled(self._index > 0)
        self.next_button.setText("Finish" if self._index == len(STEPS) - 1 else "Next")
        target = self.host.tutorial_target(step.get("target")) if self.host is not None else None
        self._spotlight.highlight(target)
        self._place()

    def _place(self):
        """Keep the dialog out of the way, near the lower-right corner of the editor."""
        if self.host is None or not self.host.isVisible():
            return
        self.adjustSize()
        frame = self.host.frameGeometry()
        x = max(frame.left() + 16, frame.right() - self.width() - 24)
        y = max(frame.top() + 16, frame.bottom() - self.height() - 48)
        self.move(x, y)

    def closeEvent(self, event):
        # Closing the window is a dismissal, not a reason to ask again next launch.
        mark_completed(self._store)
        self._spotlight.clear()
        super().closeEvent(event)
