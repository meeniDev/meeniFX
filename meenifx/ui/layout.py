"""Dock layout policy: the default arrangement, versioned persistence and presets.

QMainWindow owns the docks and this module only decides how they are arranged and
where their state is stored, so MainWindow stays readable and a new panel can be added
to ``DOCK_SPECS`` without touching the save/restore code.

Persistence uses an INI file in the user config folder (``QStandardPaths``), not the
Windows registry, and every state blob is stamped with ``LAYOUT_VERSION`` so a layout
saved by an older arrangement falls back to the default instead of breaking.
"""

import json
import os

from PyQt6.QtCore import QSettings, QStandardPaths, Qt
from PyQt6.QtWidgets import QApplication

LAYOUT_VERSION = 5

# key -> (dock title, default area, minimum size in pixels).
# Preview is a normal dock (never the central widget) so it stays movable, floatable
# and tabbable; the layout simply gives it the space the empty central widget used to
# waste.
DOCK_SPECS = {
    "project": ("Project", Qt.DockWidgetArea.LeftDockWidgetArea, (220, 200)),
    "preview": ("Preview", Qt.DockWidgetArea.RightDockWidgetArea, (300, 220)),
    "effects": ("Effects", Qt.DockWidgetArea.RightDockWidgetArea, (320, 200)),
    "timeline": ("Timeline", Qt.DockWidgetArea.BottomDockWidgetArea, (380, 150)),
    "graph": ("Graph Editor", Qt.DockWidgetArea.BottomDockWidgetArea, (360, 150)),
}

SIDE_KEYS = ("project", "preview", "effects")
# Docks that share the bottom area with the timeline (tabbed with it by default).
BOTTOM_KEYS = ("timeline", "graph")

# Roughly: left 18 %, preview the rest, right 20 %; timeline 30 % of the height.
LEFT_SHARE, RIGHT_SHARE, TIMELINE_SHARE = 0.16, 0.24, 0.30
MIN_WINDOW = (1000, 600)


def app_settings():
    """Ini-backed per-user settings file (used for the layout and UI options)."""
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "meeniFX", "Studios")


def settings_path(settings=None):
    """Absolute path of the INI file, handy for docs, logs and the status bar."""
    return (settings or app_settings()).fileName()


def _config_dir():
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return location or os.path.expanduser("~")


def apply_dock_minimums(window):
    """Give every dock a floor so no panel can be dragged down to nothing."""
    for key, (_title, _area, (width, height)) in DOCK_SPECS.items():
        dock = window.docks.get(key)
        if dock is not None:
            dock.setMinimumSize(int(width), int(height))
    window.setMinimumSize(*MIN_WINDOW)


def build_default_layout(window, apply_sizes=False):
    """Arrange Project | Preview | Effects over a full-width Timeline.

    Safe to call any time: it re-docks every panel, restores the corner ownership and
    (after the window has a real geometry) resizes the docks to the default shares.
    """
    docks = window.docks
    window.setDockNestingEnabled(True)
    # Bottom corners belong to the Timeline so it spans the whole width; the side
    # panels own the top corners so they can run the full height.
    window.setCorner(Qt.Corner.TopLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
    window.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
    window.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
    window.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
    for key, (_title, area, _minimum) in DOCK_SPECS.items():
        dock = docks.get(key)
        if dock is None:
            continue
        if dock.isFloating():
            dock.setFloating(False)
        dock.show()
        window.addDockWidget(area, dock)
    # Explicit split: Preview takes the middle, Effects hugs the right edge. The
    # Timeline is only *added* to the bottom area - splitting it against the preview
    # would move it inside the right column and it would stop spanning the window.
    window.splitDockWidget(docks["preview"], docks["effects"], Qt.Orientation.Horizontal)
    docks["project"].raise_()
    graph = docks.get("graph")
    if graph is not None and graph not in docks["timeline"].parentWidget().findChildren(type(graph)):
        pass
    if graph is not None:
        # Tabbed behind the timeline: one dock row, no lost vertical space.
        window.tabifyDockWidget(docks["timeline"], graph)
        docks["timeline"].raise_()
    # resizeDocks() only works once the window has its real geometry, so the caller
    # decides when to apply the shares (MainWindow does it on the first show).
    if apply_sizes:
        apply_default_sizes(window)


def apply_default_sizes(window):
    """Resize the docks to the default proportions using the live window geometry."""
    docks = window.docks
    width, height = max(window.width(), MIN_WINDOW[0]), max(window.height(), MIN_WINDOW[1])
    left = max(DOCK_SPECS["project"][2][0], round(width * LEFT_SHARE))
    right = max(DOCK_SPECS["effects"][2][0], round(width * RIGHT_SHARE))
    preview = max(DOCK_SPECS["preview"][2][0], width - left - right)
    window.resizeDocks([docks["project"], docks["preview"], docks["effects"]],
                       [left, preview, right], Qt.Orientation.Horizontal)
    _apply_vertical_sizes(window, docks, height)


def _apply_vertical_sizes(window, docks, window_height):
    """Split the dock area so the Timeline takes ``TIMELINE_SHARE`` of the window.

    The dock area is shorter than the window (menu bar, toolbars, status bar), and
    ``resizeDocks`` silently drops a request whose sizes add up to more than that area.
    Always ask for exactly the space the docks already have, or the Timeline keeps
    whatever height it was given before.
    """
    available = docks["preview"].height() + docks["timeline"].height()
    if available <= 0:
        available = max(MIN_WINDOW[1], window_height)
    timeline = max(DOCK_SPECS["timeline"][2][1], round(window_height * TIMELINE_SHARE))
    timeline = min(timeline, available - DOCK_SPECS["preview"][2][1])
    timeline = max(DOCK_SPECS["timeline"][2][1], timeline)
    window.resizeDocks([docks["preview"], docks["timeline"]],
                       [max(DOCK_SPECS["preview"][2][1], available - timeline), timeline],
                       Qt.Orientation.Vertical)


def _encode(blob):
    return bytes(blob.toBase64()).decode("ascii")


def _decode(text):
    from PyQt6.QtCore import QByteArray
    return QByteArray.fromBase64(str(text).encode("ascii"))


def save_state(window, settings):
    """Stores window geometry plus the dock state under the current version."""
    settings.setValue("layoutVersion", LAYOUT_VERSION)
    settings.setValue("geometry", window.saveGeometry())
    settings.setValue("windowState", window.saveState())


def restore_state(window, settings):
    """Applies a saved layout. Returns ``"restored"`` or ``"default"``.

    Anything suspicious - a version change, an unreadable state blob, or a dock that
    ended up in no area at all - reports ``"default"`` so the caller can rebuild the
    default arrangement rather than leaving a half-broken window behind.
    """
    geometry = settings.value("geometry")
    if geometry is not None:
        window.restoreGeometry(geometry)
        ensure_on_screen(window)
    version = str(settings.value("layoutVersion", ""))
    state = settings.value("windowState")
    if state is None or version != str(LAYOUT_VERSION):
        return "default"
    if not window.restoreState(state):
        return "default"
    if not docks_are_placed(window):
        return "default"
    return "restored"


def docks_are_placed(window):
    """True when every visible dock is either floating or sitting in a real area."""
    for key in DOCK_SPECS:
        dock = window.docks.get(key)
        if dock is None:
            return False
        if dock.isFloating() or dock.isHidden():
            continue
        if window.dockWidgetArea(dock) == Qt.DockWidgetArea.NoDockWidgetArea:
            return False
    return True


def ensure_on_screen(window):
    """Move the window back onto a real screen when the saved geometry is off-screen."""
    geometry = window.normalGeometry() if (window.isMaximized() or window.isFullScreen()) else window.frameGeometry()
    visible = width = height = 0
    for screen in QApplication.screens():
        overlap = screen.availableGeometry().intersected(geometry)
        if overlap.width() * overlap.height() > visible:
            visible, width, height = overlap.width() * overlap.height(), overlap.width(), overlap.height()
    if width >= 120 and height >= 80:
        return False
    primary = QApplication.primaryScreen()
    if primary is None:
        return False
    area = primary.availableGeometry()
    size = geometry.size()
    size.setWidth(min(size.width() or MIN_WINDOW[0], area.width()))
    size.setHeight(min(size.height() or MIN_WINDOW[1], area.height()))
    window.resize(size)
    window.move(area.x() + max(0, (area.width() - size.width()) // 2),
                area.y() + max(0, (area.height() - size.height()) // 2))
    return True


# --------------------------------------------------------------------- presets
def default_preset_dir():
    folder = os.path.join(_config_dir(), "layouts")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return os.path.expanduser("~")
    return folder


def save_preset(window, path, extras=None):
    """Writes a named layout preset (geometry + dock state + optional UI extras)."""
    payload = {
        "version": LAYOUT_VERSION,
        "geometry": _encode(window.saveGeometry()),
        "state": _encode(window.saveState()),
    }
    if extras:
        payload.update(extras)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)


def read_preset(path):
    """Returns the decoded preset dict, or None when the file is not a valid preset."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or "state" not in payload or "geometry" not in payload:
        return None
    if str(payload.get("version")) != str(LAYOUT_VERSION):
        return None
    return payload


def apply_preset(window, payload):
    """Applies a preset payload; returns False if the dock state could not be restored."""
    window.restoreGeometry(_decode(payload["geometry"]))
    ensure_on_screen(window)
    if not window.restoreState(_decode(payload["state"])):
        return False
    return docks_are_placed(window)
