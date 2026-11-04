"""Owns the open project: new/open/save/close, the unsaved-changes state, window title and autosave."""

import os

from PyQt6 import sip
from PyQt6.QtCore import QObject, QRunnable, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from ..config import APP_TITLE
from ..model.timeline import Timeline
from ..project import io as project_io
from ..project import storage
from ..project.container import ProjectFormatError, read_container
from ..project.settings import PROJECT_FILTER, ProjectSettings
from .filedialogs import open_file, save_file
from .project_dialogs import MissingMediaDialog

AUTOSAVE_INTERVAL_MS = 3 * 60 * 1000
PROJECT_FOLDERS = ("Recordings", "Cache")


class _WriteJob(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.finished = False
        self.setAutoDelete(False)

    def run(self):
        try:
            self.function()
        except Exception:
            pass
        self.finished = True


class ProjectController(QObject):
    project_changed = pyqtSignal()   # a project was opened, created, renamed or closed
    closed = pyqtSignal()            # Close Project finished: the app returns to the start window

    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.project = None
        self._extra_dirty = False    # changes that are not on the undo stack (media library, relinks)
        stack = window.qt_undo_stack.undo_stack
        stack.cleanChanged.connect(lambda _clean: self.update_title())
        stack.indexChanged.connect(lambda _index: self.update_title())
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self.autosave_timer.timeout.connect(self.autosave)
        self._autosave_job = None

    # ---- state -------------------------------------------------------
    @property
    def is_open(self):
        return self.project is not None

    def _stack(self):
        """The window's undo stack, or None once Qt has destroyed it (window teardown).

        Signals connected to the stack can still be delivered while the window is being
        destroyed; asking for ``isClean()`` then raises "wrapped C/C++ object has been
        deleted", which used to escape from a slot and take the process with it.
        """
        stack = getattr(getattr(self.w, "qt_undo_stack", None), "undo_stack", None)
        try:
            stack.isClean()
        except (RuntimeError, AttributeError):
            return None
        return stack

    def is_dirty(self):
        if not self.is_open:
            return False
        if self._extra_dirty:
            return True
        stack = self._stack()
        return bool(stack is not None and not stack.isClean())

    def mark_dirty(self):
        self._extra_dirty = True
        self.update_title()

    def _set_clean(self):
        self._extra_dirty = False
        stack = self._stack()
        if stack is not None:
            stack.setClean()
        self.update_title()

    def update_title(self):
        window = self._window()
        if window is None:
            return
        if not self.is_open:
            window.setWindowTitle(APP_TITLE)
            return
        window.setWindowTitle(f"{self.project.name}{'*' if self.is_dirty() else ''} - {APP_TITLE}")

    def _window(self):
        """The window, or None once Qt has deleted it during shutdown.

        The undo stack keeps emitting cleanChanged/indexChanged while the application
        closes; writing the title on a destroyed C++ object raised RuntimeError into a
        slot, which PyQt6 turns into an abort - the log was full of it.
        """
        window = getattr(self, "w", None)
        try:
            if window is None or sip.isdeleted(window):
                return None
            window.windowTitle()          # touching the C++ object proves it is alive
        except (RuntimeError, AttributeError):
            return None
        return window

    # ---- editor reset / install -----------------------------------------
    def _reset_editor(self, fps=30):
        w = self.w
        w.stop_playback()
        stack = self._stack()
        if stack is not None:
            stack.clear()
        session = w.engine.session
        session.set_selection(set())
        session.set_marker_selection(set())
        session.set_playhead(0, silent=True)
        w.engine.reset_media()
        w.engine.timeline.adopt(Timeline(fps))
        w.engine.last_incompatible = []
        w.last_frame = None
        w._pending_audio.clear()
        w.preview_panel.view.set_frame(w.engine.blank_frame(), frame_key=None,
                                       logical_size=w.engine.project_size())

    def _after_install(self):
        w = self.w
        self._set_clean()
        self.autosave_timer.start()
        w._sync_loop_state()
        w._update_timecode()
        disabled = getattr(w.engine, "last_incompatible", None) or []
        if disabled:
            names = ", ".join(sorted({str(key).replace("_", " ").title() for _cid, key in disabled}))
            w.engine.session.status.emit(
                f"{len(disabled)} effect(s) switched off: {names} cannot run on that clip type")
            w.engine.last_incompatible = []
        w.timeline_panel.canvas.update()
        w._refresh_audio()
        w._request_preview()
        self.project_changed.emit()

    def _install_new(self, project):
        self._reset_editor(project.settings.nominal_fps)
        engine = self.w.engine
        engine.apply_project_settings(project.settings, project.folder)
        for kind in ("video", "video", "audio", "audio"):
            engine.timeline.add_track(kind)
        self.project = project
        self.w.restore_ui_state(project)
        self._after_install()

    # ---- new / open ----------------------------------------------------
    def create_project(self, settings, location):
        """Create <location>/<name>/<name>.mfxpf plus Recordings and Cache folders, then open it."""
        if not self.maybe_save():
            return False
        name = storage.safe_name(settings.name)
        folder = os.path.join(location, name)
        path = os.path.join(folder, name + ".mfxpf")
        if os.path.exists(path):
            self._error(f"A project named \"{name}\" already exists in that location.")
            return False
        try:
            for sub in ("",) + PROJECT_FOLDERS:
                os.makedirs(os.path.join(folder, sub), exist_ok=True)
        except OSError as exc:
            self._error(f"Could not create the project folder: {exc}")
            return False
        settings.name = name
        self._discard_current()
        project = project_io.Project(settings, path)
        self._install_new(project)
        return self._write(path, quiet=True)

    def open_sample(self):
        if not self.maybe_save():
            return False
        self._discard_current()
        self._install_new(project_io.Project(ProjectSettings.from_preset("YouTube 1080p 30", "Sample project")))
        self.w.load_sample_media()
        return True

    def browse_open(self):
        if not self.maybe_save():
            return False
        path = open_file(self.w, "Open Project", storage.default_projects_dir(), PROJECT_FILTER)
        return self.open_path(path, checked=True) if path else False

    def open_path(self, path, checked=False):
        if not storage.is_project_file(path):
            self._error("Only meeniFX project files (.mfxpf) can be opened.")
            return False
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            self._error(f"The project file was not found:\n{path}")
            return False
        if not checked and not self.maybe_save():
            return False
        source, recovered = path, False
        # --no-recent (tests, automation): the recovery question would block a scripted run,
        # so the project file itself is opened and the autosave is left alone.
        auto = None if storage.recents_disabled() else storage.newer_autosave(path)
        if auto:
            answer = QMessageBox.question(
                self.w, "Recover unsaved work",
                f"An autosave newer than \"{os.path.basename(path)}\" was found "
                "(meeniFX may have closed unexpectedly).\n\nRecover it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Yes:
                source, recovered = auto, True
            else:
                storage.remove_autosave(path)
        try:
            data = read_container(source)
        except ProjectFormatError as exc:
            self._error(f"Could not open the project:\n{exc}")
            return False
        except OSError as exc:
            self._error(f"Could not read the project file:\n{exc}")
            return False
        self._discard_current()
        self._reset_editor()
        try:
            project, missing = project_io.apply(self.w.engine, data, path)
        except Exception as exc:
            self._reset_editor()
            self._error(f"The project could not be loaded:\n{exc}")
            return False
        self.project = project
        self.w.restore_ui_state(project)
        self._after_install()
        storage.add_recent(path)
        storage.mark_session_open(path)
        if missing:
            self._resolve_missing(missing)
        if recovered:
            self.mark_dirty()
            self.w.toast_message("Recovered unsaved work from the autosave", duration_ms=3500)
        return True

    def _resolve_missing(self, missing):
        dialog = MissingMediaDialog(missing, self.w)
        if dialog.exec() and dialog.relinks:
            for item_id, new_path in dialog.relinks.items():
                self.relink(item_id, new_path)
            self.w.toast_message(f"Relinked {len(dialog.relinks)} file(s)")

    def relink(self, item_id, new_path):
        """Point a media item (and every clip using it) at a new file."""
        library = self.project.library
        item = library.items[item_id]
        old = item.path
        library.set_path(item_id, new_path)
        for clip in self.w.engine.timeline.all_clips():
            if clip.source_path == old:
                clip.source_path = os.path.abspath(new_path)
        engine = self.w.engine
        engine.missing_paths.discard(old)
        engine.media._entries.pop(old, None)
        self.mark_dirty()
        self.w._refresh_audio()
        self.w._request_preview()
        self.w.timeline_panel.canvas.update()
        self.project_changed.emit()

    # ---- settings ------------------------------------------------------
    def _set_settings(self, settings):
        """Install settings without converting clips (used by undo/redo, which restore clip times themselves)."""
        self.project.settings = settings
        self.w.engine.apply_project_settings(settings, self.project.folder)
        self.update_title()
        self.project_changed.emit()

    def apply_settings(self, new):
        """Project > Project Settings. A frame-rate change converts clip times; either change is one undo step."""
        import dataclasses
        engine = self.w.engine
        old = dataclasses.replace(engine.project_settings)
        new.validate()
        if new == old:
            return False
        old_fps, session = engine.timeline.fps, engine.session
        before = engine.timeline.snapshot()
        project_io.change_settings(engine, new)
        self.project.settings = new
        session.playhead = int(round(session.playhead * engine.timeline.fps / max(1, old_fps)))
        self.w.qt_undo_stack.push_settings_change("Project settings", before, old, new, self._set_settings)
        self.update_title()
        self.w.preview_panel.view.set_frame(engine.blank_frame(), frame_key=None, logical_size=engine.project_size())
        self.project_changed.emit()
        return True

    # ---- saving --------------------------------------------------------
    def _ui_snapshot(self):
        return self.w.collect_ui_state()

    def _thumbnail(self):
        engine = self.w.engine
        frame = self.w.last_frame if self.w.last_frame is not None else engine.blank_frame()
        return project_io.encode_thumb(frame)

    def _collect(self, thumb=True):
        ui, state = self._ui_snapshot()
        self.project.ui, self.project.window_state = ui, state
        return project_io.collect(self.w.engine, self.project, ui, state,
                                  self._thumbnail() if thumb else None,
                                  audio_duration=self.w.waveforms.library.duration_seconds)

    def _write(self, path, quiet=False):
        self.project.path = path
        try:
            data = self._collect()
            storage.save_container(path, data)
        except (OSError, ProjectFormatError) as exc:
            self._error(f"Could not save the project:\n{exc}")
            return False
        project_io_thumb = data.thumbnails.get("thumbs/project.jpg")
        if project_io_thumb:
            self.project.thumbnails["thumbs/project.jpg"] = project_io_thumb
        storage.remove_autosave(path)
        storage.add_recent(path)
        storage.mark_session_open(path)
        self._set_clean()
        self.project_changed.emit()
        if not quiet:
            self.w.toast_message(f"Saved {os.path.basename(path)}", duration_ms=1800)
        return True

    def save(self):
        if not self.is_open:
            return False
        if not self.project.path:
            return self.save_as()
        return self._write(self.project.path)

    def save_as(self):
        if not self.is_open:
            return False
        start = self.project.path or os.path.join(storage.default_projects_dir(),
                                                  storage.safe_name(self.project.name) + ".mfxpf")
        path = save_file(self.w, "Save Project As", start, PROJECT_FILTER)
        if not path:
            return False
        path = storage.ensure_extension(path)
        self.project.settings.name = os.path.splitext(os.path.basename(path))[0]
        for sub in PROJECT_FOLDERS:
            try:
                os.makedirs(os.path.join(os.path.dirname(path), sub), exist_ok=True)
            except OSError:
                pass
        return self._write(path)

    def maybe_save(self):
        """Ask Save / Don't Save / Cancel when there are unsaved changes. False means: stop what you were doing."""
        if not self.is_dirty():
            return True
        box = QMessageBox(self.w)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save changes to \"{self.project.name}\"?")
        box.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                               QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Discard).setText("Don't Save")
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        answer = box.exec()
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def _discard_current(self):
        """The previous project is being replaced: its autosave belongs to work the user chose to drop."""
        if self.is_open and self.project.path:
            storage.remove_autosave(self.project.path)

    def close_project(self):
        if not self.maybe_save():
            return False
        self._discard_current()
        self.autosave_timer.stop()
        storage.mark_session_clean()
        self.project = None
        self._reset_editor()
        self.update_title()
        self.project_changed.emit()
        self.closed.emit()
        return True

    def shutdown(self):
        """Called by the window's closeEvent once the user agreed to quit."""
        self.autosave_timer.stop()
        self._discard_current()
        storage.mark_session_clean()

    # ---- autosave ------------------------------------------------------
    def autosave(self):
        if not (self.is_open and self.project.path and self.is_dirty()):
            return False
        if self._autosave_job is not None:
            return False
        try:
            data = self._collect(thumb=False)
        except Exception:
            return False
        path = self.project.path
        job = _WriteJob(lambda: storage.write_autosave(path, data))
        self._autosave_job = job
        self.w.pool.start(job)
        QTimer.singleShot(200, self._poll_autosave)
        return True

    def _poll_autosave(self):
        job = self._autosave_job
        if job is not None and not job.finished:
            QTimer.singleShot(200, self._poll_autosave)
        else:
            self._autosave_job = None

    def _error(self, message):
        QMessageBox.critical(self.w, APP_TITLE, message)
