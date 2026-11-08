"""PyQt6 entry point for meeniFX Studios."""

import os
import sys

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from . import crashlog
from .project import storage
from .ui.branding import app_icon
from .ui.dialogs import CrashReportDialog
from .ui.filedialogs import open_file
from .ui.main_window import MainWindow
from .version import VERSION


class MosaicApp(MainWindow):
    """Compatibility alias for the application window."""


class CrashReporter(QObject):
    """Marshals a crash report from whatever thread raised it onto the GUI thread.

    ``crashlog`` may be called from a worker thread; Qt signals are the only safe way to
    reach the widgets from there.
    """

    reported = pyqtSignal(str, str)

    def __call__(self, summary, details):
        self.reported.emit(str(summary), str(details))


class AppController:
    """Owns the editor window and the start window and moves between them."""

    def __init__(self, app, argv=()):
        from .ui.start_window import StartWindow, prune_missing_on_startup, show_on_startup
        self.app = app
        argv, quiet_recents = parse_no_recent(argv)
        if quiet_recents:
            # Tests and automation: never touch the user's recent list, and never claim the
            # previous run crashed because of a throwaway session.
            storage.disable_recents()
        self.quiet_recents = quiet_recents or storage.recents_disabled()
        # Read before anything writes the marker back, and before a project is opened.
        unclean = None if self.quiet_recents else storage.crashed_session_info()
        recovered = None if self.quiet_recents else storage.crashed_session()
        # From here on the app is "running": if it dies before aboutToQuit, the next start
        # sees the marker and explains what happened. A clean quit clears it.
        storage.mark_session_open("")
        self.window = MainWindow(app)
        self.start = StartWindow()
        self.projects = self.window.projects
        # No Qt parent: the controller is a plain Python object. It keeps this reference.
        self.reporter = CrashReporter()
        self.reporter.reported.connect(self.show_crash_report)
        self._crash_dialog = None
        crashlog.install(self.reporter)
        self.app.aboutToQuit.connect(lambda: crashlog.set_quiet(True))
        self.app.aboutToQuit.connect(storage.mark_session_clean)
        self.start.open_requested.connect(self.open_project)
        self.start.new_requested.connect(self.new_project)
        self.start.browse_requested.connect(self.browse_project)
        self.projects.project_changed.connect(self._project_changed)
        self.projects.closed.connect(self.show_start)
        self.updater = self.window.update_manager
        # After the first window is up: read the notes if this build is an update, then a
        # background check. Both are single-shot, so opening a project is never delayed.
        # A quiet test/automation run never writes the user's update settings or calls GitHub.
        if not self.quiet_recents:
            QTimer.singleShot(1800, self._startup_updates)

        if unclean:
            # Report after the start window (or the recovered project) is on screen.
            QTimer.singleShot(1400, self.report_previous_crash)
        if not self.quiet_recents and prune_missing_on_startup():
            # Silent, and after the window is up: a few stat calls must not delay the start.
            QTimer.singleShot(0, self._prune_missing_recents)
        paths = [a for a in argv if storage.is_project_file(a) and os.path.isfile(a)]
        if paths:
            QTimer.singleShot(0, lambda: self.open_project(paths[0]))
            return
        if recovered and os.path.isfile(recovered):
            QTimer.singleShot(0, lambda: self.open_project(recovered))
            return
        recents = [p for p in storage.load_recents() if os.path.isfile(p)]
        if not show_on_startup() and recents:
            QTimer.singleShot(0, lambda: self.open_project(recents[0]))
        else:
            self.show_start()

    # ------------------------------------------------------------------ crash reports
    def show_crash_report(self, summary, details):
        """One modal report per failure, never more than the log's own limit."""
        if self._crash_dialog is not None and self._crash_dialog.isVisible():
            return
        parent = self.window if self.window.isVisible() else self.start
        dialog = CrashReportDialog(summary, details, parent)
        dialog.setModal(False)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dialog.finished.connect(lambda _code: setattr(self, "_crash_dialog", None))
        self._crash_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def report_previous_crash(self):
        """The last session never closed cleanly: say so, with whatever the log holds."""
        details = crashlog.last_report()
        if not details:
            details = ("No traceback was recorded, which means the process stopped inside "
                       "native code (a graphics driver or a video library) or was killed "
                       "by the system.\n"
                       f"Log file: {crashlog.path()}")
        parent = self.window if self.window.isVisible() else self.start
        dialog = CrashReportDialog(
            "meeniFX closed unexpectedly last time", details, parent,
            title="Unexpected shutdown", recovered=True)
        dialog.setModal(False)
        self._crash_dialog = dialog
        dialog.finished.connect(lambda _code: setattr(self, "_crash_dialog", None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _prune_missing_recents(self):
        """Preferences: drop recent entries whose file is gone, without a word unless asked."""
        if storage.prune_missing():
            self.start.reload()

    def _startup_updates(self):
        """Record this launch, offer the notes of a real update, and check in the background."""
        settings = self.updater.settings
        due = settings.record_launch(VERSION)
        settings.save(self.updater.store)
        if settings.auto_check and settings.should_auto_check():
            self.updater.check(manual=False)
        if due:
            version = settings.pending_whats_new or VERSION
            self.window.show_whats_new(version)

    def show_start(self):
        self.window.hide()
        self.start.reload()
        self.start.show()
        self.start.raise_()
        self.start.activateWindow()

    def _project_changed(self):
        if self.projects.is_open:
            self.start.hide()
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

    def open_project(self, path):
        ok = self.projects.open_path(path)
        if not self.projects.is_open and not self.window.isVisible():
            self.show_start()
        return ok

    def new_project(self):
        from .ui.project_dialogs import NewProjectDialog
        dialog = NewProjectDialog(self.start if self.start.isVisible() else self.window)
        if dialog.exec():
            self.projects.create_project(dialog.settings(), dialog.location())

    def browse_project(self):
        path = open_file(self.start, "Open Project", storage.default_projects_dir(),
                         "meeniFX Project Files (*.mfxpf)")
        if path:
            self.open_project(path)


def parse_no_recent(argv):
    """(remaining argv, asked for quiet recents) - strips ``--no-recent`` from the arguments."""
    args = [str(a) for a in argv or ()]
    remaining = [a for a in args if a != "--no-recent"]
    return remaining, len(remaining) != len(args)


def install_exception_log():
    """Route every unhandled exception to the crash log, then to the previous hook.

    PyQt aborts the process when an exception escapes a virtual override (a paint, a
    mouse handler), and in a windowed build there is no console to explain it. The log is
    the difference between "it crashed" and a traceback somebody can act on. Plain slot
    exceptions are printed and the editor carries on, as before.
    """
    return crashlog.install()


def main():
    app=QApplication.instance() or QApplication(sys.argv)
    install_exception_log()
    app.setOrganizationName("meeniFX")
    app.setApplicationName("Studios")
    app.setWindowIcon(app_icon())
    from .theme import configure_theme, resolve_theme
    from .ui.layout import app_settings
    configure_theme(app,resolve_theme(app,app_settings().value("ui/theme","light")))
    controller=AppController(app,sys.argv[1:])  # noqa: F841 - keeps the windows alive
    return app.exec()


if __name__=="__main__":
    raise SystemExit(main())
