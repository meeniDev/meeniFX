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

from PyQt6.QtCore import QSettings, QStandardPaths, Qt, QTimer
from PyQt6.QtWidgets import QApplication

LAYOUT_VERSION = 8

# key -> (dock title, default area, minimum size in pixels).
# Preview is a normal dock (never the central widget) so it stays movable, floatable
# and tabbable; the layout simply gives it the space the empty central widget used to
# waste.
DOCK_SPECS = {
    "project": ("Project", Qt.DockWidgetArea.LeftDockWidgetArea, (220, 200)),
    "preview": ("Preview", Qt.DockWidgetArea.RightDockWidgetArea, (300, 220)),
    "effects": ("Effects", Qt.DockWidgetArea.RightDockWidgetArea, (320, 200)),
    "properties": ("Properties", Qt.DockWidgetArea.RightDockWidgetArea, (320, 160)),
    "timeline": ("Timeline", Qt.DockWidgetArea.BottomDockWidgetArea, (380, 150)),
    "graph": ("Graph Editor", Qt.DockWidgetArea.BottomDockWidgetArea, (360, 150)),
}

SIDE_KEYS = ("project", "preview", "effects", "properties")
# Docks that share the bottom area with the timeline (tabbed with it by default).
BOTTOM_KEYS = ("timeline", "graph")

# Roughly: left 16 %, preview the rest, right 24 %; timeline 30 % of the dock area.
LEFT_SHARE, RIGHT_SHARE, TIMELINE_SHARE = 0.16, 0.24, 0.30
# The browser gets this much of the right column; the inspector takes the rest.
INSPECTOR_SHARE = 0.55
# QWIDGETSIZE_MAX: the "no maximum" value a pinned dock is released with.
_UNBOUNDED = 16777215
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
    # The browser and the inspector share the right column, stacked: both stay visible
    # while an effect is edited, and each keeps its own title bar and View entry.
    window.splitDockWidget(docks["effects"], docks["properties"], Qt.Orientation.Vertical)
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
    """Resize the docks to the default proportions using their live geometry.

    Two things made the old version miss: ``resizeDocks`` silently ignores a request
    whose sizes do not add up to the space the dock area actually has, and it ignores
    one that asks a column for less than it can physically shrink to. The window's size
    is not the dock area's size (menu bar, toolbars and title bars are part of neither
    share), so the Timeline used to stay at whatever height it was given - 60 % of the
    window on a fresh layout. Every size below is measured from the docks themselves.
    """
    docks = window.docks
    project, preview, effects = docks["project"], docks["preview"], docks["effects"]
    total_width = project.width() + preview.width() + effects.width()
    if total_width > 0:
        left, middle, right = _column_sizes(total_width, _column_minimums(docks))
        window.resizeDocks([project, preview, effects], [left, middle, right],
                           Qt.Orientation.Horizontal)
    _apply_inspector_split(window, docks)
    _apply_timeline_split(window, docks)


def _column_minimums(docks):
    """The narrowest each side column can actually get.

    ``DOCK_SPECS`` holds the design floor, but a panel can need much more (the Project
    column's button row needs 384 px). Asking for less makes Qt drop the whole resize.
    """
    keys = ("project", "preview", "effects")
    minimums = []
    for key in keys:
        dock = docks[key]
        minimums.append(max(DOCK_SPECS[key][2][0], dock.minimumSizeHint().width(),
                            dock.minimumWidth()))
    return minimums


def _column_sizes(total, minimums):
    """Project | Preview | Effects widths that add up to exactly ``total``."""
    if sum(minimums) >= total:
        return list(minimums)
    shares = (LEFT_SHARE, 1.0 - LEFT_SHARE - RIGHT_SHARE, RIGHT_SHARE)
    sizes = [max(minimum, round(total * share)) for minimum, share in zip(minimums, shares)]
    excess = sum(sizes) - total
    for index in (0, 2, 1):  # give the slack back to the side columns first
        take = min(excess, sizes[index] - minimums[index])
        sizes[index] -= take
        excess -= take
        if excess <= 0:
            break
    sizes[1] = total - sizes[0] - sizes[2]
    return sizes


def _apply_inspector_split(window, docks):
    """Give the browser slightly more of the right column than the inspector."""
    effects, props = docks["effects"], docks["properties"]
    total = effects.height() + props.height()
    if total <= 0:
        return
    top = max(DOCK_SPECS["effects"][2][1], round(total * INSPECTOR_SHARE))
    top = min(top, total - DOCK_SPECS["properties"][2][1])
    top = max(DOCK_SPECS["effects"][2][1], top)
    window.resizeDocks([effects, props], [top, total - top], Qt.Orientation.Vertical)


def _apply_timeline_split(window, docks):
    """Split the dock area so the Timeline takes ``TIMELINE_SHARE`` of its height."""
    total = docks["preview"].height() + docks["timeline"].height()
    if total <= 0:
        return
    set_timeline_height(window, round(total * TIMELINE_SHARE))


def timeline_limits(docks):
    """Shortest and tallest the Timeline may become for the current dock area."""
    total = docks["preview"].height() + docks["timeline"].height()
    lowest = DOCK_SPECS["timeline"][2][1]
    highest = max(lowest, total - DOCK_SPECS["preview"][2][1])
    return lowest, highest


def pin_timeline_height(window, height):
    """Hold the Timeline at ``height`` px; call release_timeline_height() to free it.

    ``resizeDocks`` on a docked pair moves the separator only part of the way, while a
    fixed height is obeyed exactly, so both the default split and the separator drag
    pin the dock for the duration and then hand it back to the layout.
    """
    timeline = window.docks["timeline"]
    lowest, highest = timeline_limits(window.docks)
    if highest <= lowest:
        return
    target = int(max(lowest, min(int(height), highest)))
    # Pin both ends: a maximum alone would only cap the dock, it would not grow it.
    timeline.setMinimumHeight(target)
    timeline.setMaximumHeight(target)


def release_timeline_height(window):
    """Undo pin_timeline_height() so the user can drag the Timeline again."""
    timeline = window.docks["timeline"]
    timeline.setMaximumHeight(_UNBOUNDED)
    timeline.setMinimumHeight(DOCK_SPECS["timeline"][2][1])


def set_timeline_height(window, height):
    """Set the Timeline height now and leave it resizable afterwards."""
    pin_timeline_height(window, height)
    QTimer.singleShot(0, lambda: release_timeline_height(window))


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
