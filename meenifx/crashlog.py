"""Last-resort crash reporting.

A PyQt6 application aborts the whole process when a Python exception escapes a virtual
C++ override (``paintEvent``, ``mouseMoveEvent``, a slot invoked from C++). There is no
traceback and, in a windowed build, no console either - the window simply disappears.
That is exactly how "the Graph Editor crashes" presented itself.

Two things are needed, and this module provides both:

* ``install()`` sets ``sys.excepthook`` and ``threading.excepthook`` so every unhandled
  exception is appended to ``<app data>/crash.log`` with its full traceback, then passed
  to the previous hook (so a development console still prints it).
* ``guard()`` is a decorator for Qt overrides: it runs the body, and if it raises, it logs
  the traceback once per ``(widget, context)`` and keeps the application alive. A broken
  paint must cost one repaint, not the user's project.

Nothing here is a substitute for fixing a bug: the log is how the next bug is found.
"""

from __future__ import annotations

import functools
import os
import sys
import threading
import traceback

from .version import diagnostics

LOG_NAME = "crash.log"
MAX_BYTES = 512 * 1024
# A broken paint can fail on every frame; without a cap the user would get a dialog storm.
MAX_REPORTS = 3
_seen = set()
_installed = False
_log_path = None
_reporter = None
_reported = 0
_quiet = False


def log_folder():
    """Per-user folder for logs; created on demand so a read-only install still works."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "meeniFX")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = os.path.dirname(os.path.abspath(__file__))
    return folder


def path():
    global _log_path
    if _log_path is None:
        _log_path = os.path.join(log_folder(), LOG_NAME)
    return _log_path


def read_log():
    """The current log text (used by the About window's "Open log" affordance and tests)."""
    try:
        with open(path(), encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _rotate():
    try:
        if os.path.getsize(path()) <= MAX_BYTES:
            return
        keep = path() + ".1"
        if os.path.exists(keep):
            os.remove(keep)
        os.replace(path(), keep)
    except OSError:
        pass


def record(context, exc_info=None):
    """Append one traceback to the crash log and return the file path."""
    kind, value, tb = exc_info or sys.exc_info()
    text = "".join(traceback.format_exception(kind, value, tb)) if kind else ""
    try:
        _rotate()
        with open(path(), "a", encoding="utf-8") as handle:
            # The version and platform make a pasted log actionable: which alpha, on which
            # Windows and Python, is the first question every report raises.
            handle.write(f"\n== {context} | {diagnostics()} ==\n{text}")
    except OSError:
        return path()
    return path()


def is_teardown(exc):
    """True for the harmless "object deleted while closing" errors.

    Qt keeps emitting into widgets that Python has already torn down during shutdown. Those
    are not crashes and must never raise a report, or closing the app would pop a dialog.
    """
    text = str(exc)
    return isinstance(exc, RuntimeError) and ("has been deleted" in text or "wrapped C/C++" in text)


def set_quiet(flag=True):
    """Stop showing reports (called while the application is closing)."""
    global _quiet
    _quiet = bool(flag)


def _notify(summary, details):
    global _reported
    if _reporter is None or _quiet or _reported >= MAX_REPORTS:
        return False
    _reported += 1
    try:
        _reporter(summary, details)
    except Exception:                              # noqa: BLE001 - reporting must never fail
        return False
    return True


def _hook(kind, value, tb):
    record("unhandled", (kind, value, tb))
    if not is_teardown(value):
        details = "".join(traceback.format_exception(kind, value, tb))
        summary = f"{kind.__name__}: {value}" if kind else "Unexpected error"
        _notify(summary, details)
    previous = getattr(_hook, "previous", None)
    if previous is not None and not _quiet:
        previous(kind, value, tb)


def _thread_hook(args):
    record(f"thread {getattr(args.thread, 'name', '?')}",
           (args.exc_type, args.exc_value, args.exc_traceback))
    if not is_teardown(args.exc_value):
        details = "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                     args.exc_traceback))
        _notify(f"{args.exc_type.__name__}: {args.exc_value}",
                f"in thread {getattr(args.thread, 'name', '?')}\n{details}")


def install(reporter=None):
    """Make unhandled exceptions recoverable, logged and (optionally) reported.

    ``reporter`` is called as ``reporter(summary, details)`` for a real failure - never for
    the teardown noise of a closing application. Safe to call more than once.
    """
    global _installed, _reporter
    if reporter is not None:
        _reporter = reporter
    if _installed:
        return path()
    _hook.previous = sys.excepthook
    sys.excepthook = _hook
    threading.excepthook = _thread_hook
    _installed = True
    return path()


def guard(context, fallback=None):
    """Decorator for Qt overrides: log a failure once, never let it abort the process.

    ``fallback`` is returned when the body raises (``None`` is right for paint/mouse
    handlers, which rarely return anything meaningful).
    """
    def decorate(function):
        key = (getattr(function, "__module__", ""), function.__qualname__)

        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as exc:                   # noqa: BLE001 - boundary by design
                if key not in _seen:
                    _seen.add(key)
                    record(f"{context}: {function.__qualname__}")
                    if not is_teardown(exc):
                        details = "".join(traceback.format_exception(type(exc), exc,
                                                                     exc.__traceback__))
                        _notify(f"{type(exc).__name__} in {context}", details)
                return fallback
        return wrapper
    return decorate


def reset():
    """Tests only: forget which failures were reported so a case can be asserted twice."""
    global _reported
    _seen.clear()
    _reported = 0


def last_report(limit=4000):
    """Tail of the crash log, for the report shown after an unclean shutdown."""
    text = read_log().strip()
    return text[-limit:] if text else ""
