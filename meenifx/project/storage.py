"""Saving, loading, autosave, recent projects and missing-media helpers (no GUI imports)."""

import json
import os
import shutil
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
    path = os.path.abspath(path)
    items = [p for p in load_recents(file) if os.path.normcase(p) != os.path.normcase(path)]
    items.insert(0, path)
    _write_json(file or RECENTS_FILE, items[:MAX_RECENTS])


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
