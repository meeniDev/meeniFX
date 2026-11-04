"""Qt undo commands adapting the toolkit-neutral timeline snapshots to QUndoStack."""

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QUndoCommand, QUndoStack


class _SnapshotCommand(QUndoCommand):
    def __init__(self, timeline, before, after, label, changed, applied=False):
        super().__init__(label)
        self.timeline, self.before, self.after, self.changed = timeline, before, after, changed
        # Pushing a command calls redo() immediately. The edit that created it has
        # already been applied to the live model, so restoring the "after" snapshot
        # would only replace every Clip object and detach the widgets editing them.
        self._skip_first_redo = bool(applied)

    def undo(self):
        self.timeline.restore(self.before)
        self.changed.emit()

    def redo(self):
        if self._skip_first_redo:
            self._skip_first_redo = False
            self.changed.emit()
            return
        self.timeline.restore(self.after)
        self.changed.emit()


class _SettingsCommand(QUndoCommand):
    """Project settings change: the settings and the converted timeline come back together."""

    def __init__(self, timeline, before, after, old, new, apply_settings, label, changed):
        super().__init__(label)
        self.timeline, self.before, self.after = timeline, before, after
        self.old, self.new, self.apply_settings, self.changed = old, new, apply_settings, changed
        self._skip_first_redo = True

    def undo(self):
        self.apply_settings(self.old)
        self.timeline.restore(self.before)
        self.changed.emit()

    def redo(self):
        if self._skip_first_redo:
            self._skip_first_redo = False
            self.changed.emit()
            return
        self.apply_settings(self.new)
        self.timeline.restore(self.after)
        self.changed.emit()


class QtCommandStack(QObject):
    """QUndoStack surface matching CommandStack.run/commit for session operations."""
    changed = pyqtSignal()

    def __init__(self, timeline, limit=200, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(limit)
        # Forward through a method instead of the bound ``changed.emit``: while the window
        # is being destroyed the underlying QObject may already be gone, and Qt then asks
        # this object for a ``changed()`` signal that no longer exists (an AttributeError
        # raised inside a queued delivery, which used to be reported as a crash).
        self.undo_stack.canUndoChanged.connect(self._forward_changed)
        self.undo_stack.canRedoChanged.connect(self._forward_changed)
        self.undo_stack.indexChanged.connect(self._forward_changed)

    def _forward_changed(self, *_args):
        """Re-emit the stack's own signals as one ``changed``; silent after teardown."""
        try:
            self.changed.emit()
        except (RuntimeError, AttributeError):
            pass

    def commit(self, label, before):
        after = self.timeline.snapshot()
        if after == before:
            return False
        self.undo_stack.push(_SnapshotCommand(self.timeline, before, after, label,
                                              self.changed, applied=True))
        return True

    def run(self, label, action):
        before = self.timeline.snapshot()
        result = action()
        self.commit(label, before)
        return result

    def push_settings_change(self, label, before_snap, old, new, apply_settings):
        """One undo step for a project settings change (settings plus every converted clip time)."""
        after = self.timeline.snapshot()
        self.undo_stack.push(_SettingsCommand(self.timeline, before_snap, after, old, new, apply_settings,
                                              label, self.changed))

    @property
    def can_undo(self):
        return self.undo_stack.canUndo()

    @property
    def can_redo(self):
        return self.undo_stack.canRedo()

    def undo(self):
        self.undo_stack.undo()
        return self.undo_stack.undoText()

    def redo(self):
        self.undo_stack.redo()
        return self.undo_stack.redoText()
