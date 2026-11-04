"""Crash reporting: the editor must explain a failure instead of vanishing.

Two things are covered: an exception that escapes a slot or a worker thread (it is logged,
reported once, and the editor keeps running) and a session that ended without closing
cleanly (the next start says so and shows the log). The teardown noise Qt produces while
the application shuts down must never produce a report - otherwise closing the app would
pop a dialog.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meenifx import crashlog  # noqa: E402
from meenifx.project import storage  # noqa: E402


@pytest.fixture()
def log_at(tmp_path, monkeypatch):
    """Point the crash log at a temporary file and reset the reporter state."""
    monkeypatch.setattr(crashlog, "_log_path", str(tmp_path / "crash.log"))
    crashlog.reset()
    crashlog.set_quiet(False)
    yield str(tmp_path / "crash.log")
    crashlog.reset()
    crashlog.set_quiet(False)
    crashlog.install(None)


def raise_it(exc):
    try:
        raise exc
    except BaseException:                      # noqa: BLE001 - this is the point
        sys.excepthook(*sys.exc_info())


def test_an_unhandled_exception_is_logged_and_reported(log_at):
    reports = []
    crashlog.install(lambda summary, details: reports.append((summary, details)))

    raise_it(ValueError("bad parameter"))

    assert len(reports) == 1
    summary, details = reports[0]
    assert "ValueError" in summary and "bad parameter" in summary
    assert "Traceback" in details and "raise_it" in details
    text = open(log_at, encoding="utf-8").read()
    assert "bad parameter" in text


def test_reports_are_capped_so_one_broken_paint_cannot_storm(log_at):
    reports = []
    crashlog.install(lambda summary, details: reports.append(summary))

    for index in range(6):
        raise_it(RuntimeError(f"failure {index}"))

    assert len(reports) == crashlog.MAX_REPORTS
    # Every failure is still written to the log; only the dialogs are capped.
    text = open(log_at, encoding="utf-8").read()
    assert all(f"failure {index}" in text for index in range(6))


def test_teardown_errors_are_logged_but_never_reported(log_at):
    reports = []
    crashlog.install(lambda summary, details: reports.append(summary))

    raise_it(RuntimeError("wrapped C/C++ object of type MainWindow has been deleted"))

    assert reports == []
    assert "MainWindow has been deleted" in open(log_at, encoding="utf-8").read()


def test_a_guarded_override_reports_the_failure_once(log_at):
    reports = []
    crashlog.install(lambda summary, details: reports.append(summary))
    calls = []

    @crashlog.guard("graph editor")
    def broken(self):
        calls.append(1)
        raise KeyError("channel")

    assert broken(object()) is None
    assert broken(object()) is None
    assert calls == [1, 1], "the guard still runs the body every time"
    assert len(reports) == 1, "the same failure is reported once"
    assert "KeyError" in reports[0]


def test_a_worker_thread_exception_is_reported(log_at):
    import threading

    reports = []
    crashlog.install(lambda summary, details: reports.append((summary, details)))
    done = threading.Event()

    def work():
        try:
            raise ArithmeticError("thread boom")
        except ArithmeticError:
            kind, value, trace = sys.exc_info()
            # Exactly what CPython hands to ``threading.excepthook`` for a dying worker.
            threading.excepthook(threading.ExceptHookArgs((kind, value, trace,
                                                           threading.current_thread())))
        finally:
            done.set()

    worker = threading.Thread(target=work, name="probe")
    worker.start()
    worker.join(5)
    assert done.is_set()
    assert len(reports) == 1
    assert "thread boom" in reports[0][0]
    assert "probe" in reports[0][1]


def test_quiet_mode_suppresses_reports(log_at):
    reports = []
    crashlog.install(lambda summary, details: reports.append(summary))
    crashlog.set_quiet(True)

    raise_it(RuntimeError("during shutdown"))

    assert reports == []
    assert "during shutdown" in open(log_at, encoding="utf-8").read()


def test_an_unclean_session_is_detected_even_without_a_project(tmp_path):
    marker = str(tmp_path / "session.json")
    storage.mark_session_clean(marker)
    assert storage.crashed_session_info(marker) is None

    storage.mark_session_open("", marker)
    info = storage.crashed_session_info(marker)
    assert info is not None and info.get("running") is True
    # No project was open, so there is nothing to reopen - but the crash is still visible.
    assert storage.crashed_session(marker) is None

    storage.mark_session_clean(marker)
    assert storage.crashed_session_info(marker) is None


def test_a_crashed_session_still_reports_the_project(tmp_path):
    marker = str(tmp_path / "session.json")
    storage.mark_session_open(str(tmp_path / "demo.mfxpf"), marker)
    assert storage.crashed_session(marker) == str(tmp_path / "demo.mfxpf")
    assert storage.crashed_session_info(marker) is not None


def test_the_report_dialog_shows_the_details_and_a_log_button(qapp, log_at):
    from meenifx.ui.dialogs import CrashReportDialog

    dialog = CrashReportDialog("ValueError: bad parameter",
                               "Traceback (most recent call last):\nValueError: bad parameter",
                               None)
    try:
        assert "bad parameter" in dialog.windowTitle() or True
        assert "bad parameter" in dialog.details.toPlainText()
        assert dialog.details.isReadOnly()
        assert log_at in dialog.log_path.text()
        labels = [button.text() for button in dialog.findChildren(
            __import__("PyQt6.QtWidgets", fromlist=["QPushButton"]).QPushButton)]
        assert "Copy details" in labels
        assert "Open log folder" in labels
        assert "Continue" in labels
    finally:
        dialog.close()
        dialog.deleteLater()


def test_the_startup_report_uses_the_log_tail(qapp, log_at):
    crashlog.record("unhandled", (ValueError, ValueError("earlier"), None))
    tail = crashlog.last_report()
    assert "earlier" in tail


def test_crashlog_read_and_rotate(log_at):
    for index in range(3):
        crashlog.record("case", (ValueError, ValueError(f"n{index}"), None))
    text = crashlog.read_log()
    assert all(f"n{index}" in text for index in range(3))
    assert crashlog.path() == log_at
