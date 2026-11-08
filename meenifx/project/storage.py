"""Saving, loading, autosave, recent projects and missing-media helpers (no GUI imports)."""

import json
import os
import shutil
import sys
import tempfile
import time

from .container import ProjectData, ProjectFormatError, read_container, write_container
from .settings import PROJECT_EXTENSION

DATA_DIR = os.path.join(os.path.expanduser("~"), ".meenifx")
RECENTS_FILE = os.path.join(DATA_DIR, "recent_projects.json")
SESSION_FILE = os.path.join(DATA_DIR, "session.json")
MAX_RECENTS = 24


def default_projects_dir():
    return os.path.join(os.path.expanduser("~"), "Documents", "meeniFX Projects")


def safe_name(name):
    """A name usable as a file or folder name on every platform."""
    cleaned = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in str(name)).strip(" .")
    return cleaned or "Untitled"


def ensure_extension(path):
    """Project files always use .mfxpf; it is appended (in lower case) when missing."""
    path = str(path)
    if path.lower().endswith(PROJECT_EXTENSION):
        return path[:-len(PROJECT_EXTENSION)] + PROJECT_EXTENSION
    return path + PROJECT_EXTENSION


def is_project_file(path):
    return str(path).lower().endswith(PROJECT_EXTENSION)


def backup_path(path):
    return path + ".bak"


def autosave_path(path):
    """Autosave sits next to its project and is named after it, so the two stay associated."""
    return path + ".autosave"


def save_container(path, data):
    """Atomic save: write a temp file, keep one .bak of the previous version, then rename over the target."""
    path = ensure_extension(path)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    try:
        write_container(tmp, data)
        read_container(tmp)  # never replace a good project with something unreadable
        if os.path.exists(path):
            shutil.copy2(path, backup_path(path))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def write_autosave(path, data):
    target = autosave_path(path)
    tmp = target + ".tmp"
    write_container(tmp, data)
    os.replace(tmp, target)
    return target


def remove_autosave(path):
    try:
        os.remove(autosave_path(path))
    except OSError:
        pass


def newer_autosave(path):
    """The autosave file when it exists and is newer than the project (i.e. unsaved work was lost)."""
    auto = autosave_path(path)
    try:
        if os.path.exists(auto) and (not os.path.exists(path) or os.path.getmtime(auto) > os.path.getmtime(path)):
            return auto
    except OSError:
        pass
    return None


# ---- recents -------------------------------------------------------------
# ``--no-recent`` (or MEENIFX_NO_RECENT=1) keeps a run out of the recent list completely:
# tests and automation create throwaway projects in the system temp folder, and those used
# to show up in the user's real list as missing or stale entries.
_RECENTS_DISABLED = False
_TRUTHY = ("1", "true", "yes", "on")


def recents_disabled():
    """True when this run must not register recent projects or leave a session marker."""
    if _RECENTS_DISABLED:
        return True
    return str(os.environ.get("MEENIFX_NO_RECENT", "")).strip().lower() in _TRUTHY


def disable_recents():
    """``--no-recent``: no recents, no crash/session marker from this process."""
    global _RECENTS_DISABLED
    _RECENTS_DISABLED = True


def enable_recents():
    """Force the recent list back on for this process (the start-window test needs it).

    Clears both switches: the flag a test set and the ``MEENIFX_NO_RECENT`` environment
    variable the suite runs with, so a script that deliberately drives the list can.
    """
    global _RECENTS_DISABLED
    _RECENTS_DISABLED = False
    os.environ.pop("MEENIFX_NO_RECENT", None)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_recents(file=None):
    items = _read_json(file or RECENTS_FILE, [])
    return [p for p in items if isinstance(p, str) and is_project_file(p)]


def add_recent(path, file=None):
    if recents_disabled():
        return
    path = os.path.abspath(path)
    items = [p for p in load_recents(file) if os.path.normcase(p) != os.path.normcase(path)]
    items.insert(0, path)
    _write_json(file or RECENTS_FILE, items[:MAX_RECENTS])


def missing_recents(file=None):
    """Recent projects whose file is gone (deleted, moved, or a temp copy of a test)."""
    return [p for p in load_recents(file) if not os.path.isfile(p)]


def prune_missing(file=None):
    """Drop recent entries whose file no longer exists; returns how many were removed."""
    items = load_recents(file)
    keep = [p for p in items if os.path.isfile(p)]
    removed = len(items) - len(keep)
    if removed:
        _write_json(file or RECENTS_FILE, keep)
    return removed


def temp_root():
    """The system temp folder, resolved, so paths can be compared reliably."""
    try:
        return os.path.realpath(tempfile.gettempdir())
    except (OSError, ValueError):
        return ""


def is_in_temp(path, root=None):
    """True when `path` is inside the system temp folder (case-insensitive on Windows).

    Compared by components (``os.path.commonpath``), so a sibling such as ``TempX`` next
    to ``Temp`` never counts as being inside it.
    """
    if not path:
        return False
    root = temp_root() if root is None else root
    if not root:
        return False
    try:
        target = os.path.realpath(os.path.abspath(str(path)))
        base = os.path.realpath(os.path.abspath(str(root)))
        common = os.path.commonpath([base, target])
    except (OSError, ValueError):      # different drives, or a path that cannot be resolved
        return False
    if os.path.normcase(common) != os.path.normcase(base):
        return False
    return os.path.normcase(target) != os.path.normcase(base)


def temp_recents(file=None, root=None):
    """Recent projects that live in the system temp folder."""
    return [p for p in load_recents(file) if is_in_temp(p, root)]


def remove_temp_recents(file=None, root=None):
    """Drop the recent entries that point into the temp folder; returns the count."""
    items = load_recents(file)
    keep = [p for p in items if not is_in_temp(p, root)]
    removed = len(items) - len(keep)
    if removed:
        _write_json(file or RECENTS_FILE, keep)
    return removed


def remove_recent(path, file=None):
    items = [p for p in load_recents(file) if os.path.normcase(p) != os.path.normcase(os.path.abspath(path))]
    _write_json(file or RECENTS_FILE, items)


def replace_recent(old, new, file=None):
    items = [os.path.abspath(new) if os.path.normcase(p) == os.path.normcase(os.path.abspath(old)) else p
             for p in load_recents(file)]
    _write_json(file or RECENTS_FILE, items)


# ---- crash detection -----------------------------------------------------
def mark_session_open(project_path, file=None):
    """Remember the open project and that the app is running; cleared on a clean exit."""
    if file is None and recents_disabled():
        # A throwaway run must not leave a marker that makes the next start claim it crashed.
        # An explicit path is the caller's own temporary marker (tests do this), so it is
        # still written - otherwise the crash-detection helpers could never be tested.
        return
    _write_json(file or SESSION_FILE, {"project": project_path or "", "running": True, "time": time.time()})


def mark_session_clean(file=None):
    _write_json(file or SESSION_FILE, {"project": "", "running": False, "time": time.time()})


def crashed_session(file=None):
    """Project path of a session that did not exit cleanly, or None."""
    s = _read_json(file or SESSION_FILE, {})
    if isinstance(s, dict) and s.get("running") and s.get("project"):
        return s["project"]
    return None


def crashed_session_info(file=None):
    """The leftover marker of an unclean exit, or None - project open or not.

    ``crashed_session`` only notices a session that had a project; the editor also dies
    while the start window is up (a driver fault during launch), and that deserves the
    same report.
    """
    s = _read_json(file or SESSION_FILE, {})
    if isinstance(s, dict) and s.get("running"):
        return s
    return None


# ---- trashing temp projects ----------------------------------------------
PROJECT_SIDECARS = (".bak", ".autosave", ".tmp")
OWN_SUBFOLDERS = ("cache", "recordings", "media")


def project_own_folder(path, root=None):
    """The folder that clearly belongs to this project (safe to trash), or "".

    Safe means: the folder is inside the temp folder but is not the temp folder itself,
    and everything in it is this project - its file, its `.bak`/`.autosave`/`.tmp`, and
    its `Cache`/`Recordings` folders. One unrelated file is enough to answer "" and leave
    the folder alone.
    """
    path = os.path.abspath(str(path))
    folder = os.path.dirname(path)
    root = temp_root() if root is None else root
    if not folder or not is_in_temp(folder, root):
        return ""
    try:
        base = os.path.realpath(os.path.abspath(str(root)))
        if os.path.normcase(os.path.realpath(folder)) == os.path.normcase(base):
            return ""
        entries = os.listdir(folder)
    except (OSError, ValueError):
        return ""
    name = os.path.basename(path)
    allowed_files = {os.path.normcase(name + suffix) for suffix in ("",) + PROJECT_SIDECARS}
    allowed_dirs = {os.path.normcase(folder_name) for folder_name in OWN_SUBFOLDERS}
    for entry in entries:
        full = os.path.join(folder, entry)
        key = os.path.normcase(entry)
        if os.path.isdir(full):
            if key in allowed_dirs:
                continue
            return ""
        if key not in allowed_files:
            return ""
    return folder


def project_trash_items(path, root=None):
    """[paths] to move to the Recycle Bin for one temp project; [] when it is not one.

    The project's own folder is moved when it holds nothing else; otherwise only the
    project file and the files named after it (.bak/.autosave). Never anything else, and
    never a path outside the temp folder.
    """
    path = os.path.abspath(str(path))
    root = temp_root() if root is None else root
    if not is_project_file(path) or not is_in_temp(path, root):
        return []
    own = project_own_folder(path, root)
    if own:
        return [own]
    items = [path] + [path + suffix for suffix in (".bak", ".autosave")]
    return [item for item in items if os.path.exists(item) and is_in_temp(item, root)]


def _shell_trash(path):
    """Windows shell delete with FOF_ALLOWUNDO, i.e. into the Recycle Bin."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = (("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR))

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x0004, 0x0010, 0x0040, 0x0400
    source = ctypes.create_unicode_buffer(str(path) + "\0", len(str(path)) + 2)
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except (AttributeError, OSError) as exc:      # not Windows, or no shell
        return False, str(exc)
    if operation.fAnyOperationsAborted:
        return False, "the operation was cancelled"
    if code != 0:
        return False, f"Windows error {code}"
    return True, ""


def trash_path(path):
    """Move one file or folder to the Recycle Bin. Returns (ok, message).

    Uses send2trash when it is installed and the Windows shell otherwise; on a platform
    with no Recycle Bin it refuses rather than deleting anything for good.
    """
    path = os.path.abspath(str(path))
    if not os.path.exists(path) and not os.path.islink(path):
        return True, ""        # already gone: nothing to move, and nothing to report
    try:
        from send2trash import send2trash       # optional dependency
    except ImportError:
        send2trash = None
    if send2trash is not None:
        try:
            send2trash(path)
            return True, ""
        except Exception as exc:
            return False, str(exc)
    if sys.platform == "win32":
        return _shell_trash(path)
    return False, "this system has no Recycle Bin"


def trash_project(path, root=None):
    """Move one temp project (its own folder, or just its files) to the Recycle Bin.

    Returns (moved, messages): 1 when the project itself went, and one message per
    failure. The sidecar files are best effort - the project is already gone without them.
    """
    items = project_trash_items(path, root)
    if not items:
        return 0, []
    moved, messages = 0, []
    for item in items:
        ok, message = trash_path(item)
        if ok:
            moved = max(moved, 1)
        else:
            messages.append(f"{os.path.basename(item)}: {message}")
    return moved, messages


# ---- missing media -------------------------------------------------------
def find_by_name(folder, names, limit=200000):
    """Search `folder` recursively for files matching `names` (case-insensitive). Returns {name: path}."""
    wanted = {n.lower(): n for n in names}
    found, seen = {}, 0
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            seen += 1
            key = fn.lower()
            if key in wanted and wanted[key] not in found:
                found[wanted[key]] = os.path.join(root, fn)
        if len(found) == len(wanted) or seen > limit:
            break
    return found
