"""Shared fixtures: an offscreen Qt app and a synthetic project for effect tests."""

import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# These four are app-driving scripts, not collected tests: they call os._exit while
# exercising a real event loop and would terminate pytest during collection.
collect_ignore = [
    "test_project_app.py",
    "test_project_large.py",
    "test_project_panel.py",
    "test_start_window.py",
]

# The preview and panel tests build real widgets; offscreen keeps them headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Projects created by a test must never reach the user's real recent list (nor leave a
# session marker behind): every test that builds one would otherwise appear in the start
# window as a temp-folder entry, and its autosave would be offered for recovery.
os.environ.setdefault("MEENIFX_NO_RECENT", "1")
# Synthetic tests must never spend seconds loading or running the optional 176 MB model
# just because it happens to be installed on the developer's machine. Model-specific
# tests opt back in with monkeypatch.delenv.
os.environ.setdefault("MEENIFX_DISABLE_ROTO_MODEL", "1")

WIDTH, HEIGHT = 320, 180


def pytest_configure(config):
    """Give every Qt fixture one non-quitting application for the whole session."""
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)


@pytest.fixture(autouse=True)
def _close_leaked_panels():
    """Delete test-only top-level panels after each test.

    EffectsPanel and TimelinePanel are deliberately constructed without a window in the
    preset/panel tests. Left alive, they accumulate and the next MainWindow construction
    crashes Qt. Real application panels have a parent, so only parentless test panels are
    touched here.
    """
    yield
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    leaked = [widget for widget in app.topLevelWidgets()
              if widget.parent() is None
              and type(widget).__name__ in ("EffectsPanel", "TimelinePanel")]
    for widget in leaked:
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            pass
    if leaked:
        app.processEvents()


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    # A test is allowed to close its last window. Qt's default would then quit the
    # application, and a later GUI test in the same process would run against a dead
    # event loop (the combined suite exited 127 here; each file passed alone).
    app.setQuitOnLastWindowClosed(False)
    yield app
    for widget in list(app.topLevelWidgets()):
        # QComboBox's popup container is a top-level QFrame owned by the combo. Closing
        # and deleting it here corrupts the combo and crashes Qt's event loop; the
        # combo destroys it itself. Tooltips are the same kind of internal popup.
        if widget.metaObject().className() in _INTERNAL_POPUPS:
            continue
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            pass
    app.processEvents()


# Pytest group markers. The filename groups keep the commands short and document which
# suite owns a test without changing how any individual test runs.
_INTERNAL_POPUPS = {"QComboBoxPrivateContainer", "QComboBoxListView", "QTipLabel"}

_EXPLICIT_GROUPS = {
    "test_packaging.py": "packaged",
    "test_single_instance.py": "packaged",
    "test_roto_model.py": "model",
    "test_smart_key.py": "model",
    "test_roto_brush.py": "model",
}
_GUI_MARKERS = ("_ui.py", "window", "panel", "dialog", "toolbar", "browser",
                "preferences", "timeline_snapping", "project_app", "project_panel")
_RENDERER_MARKERS = ("composition", "compositing", "effect", "export", "gif", "shape",
                     "text", "transform", "blur", "glow", "look", "preset", "chroma",
                     "simple_choker", "adjustment", "transition", "posterize")


def pytest_collection_modifyitems(config, items):
    for item in items:
        name = os.path.basename(str(item.path)).lower()
        if name in _EXPLICIT_GROUPS:
            group = _EXPLICIT_GROUPS[name]
        elif any(marker in name for marker in _GUI_MARKERS):
            group = "gui"
        elif any(marker in name for marker in _RENDERER_MARKERS):
            group = "renderer"
        else:
            group = "unit"
        item.add_marker(getattr(pytest.mark, group))


def synthetic_frame(width=WIDTH, height=HEIGHT, blobs=True, skin=True):
    """A test picture with gradients, bright blobs and skin-coloured patches."""
    import cv2
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:] = (36, 34, 32)
    # a smooth gradient so mosaic/ASCII have something to quantise
    for y in range(height):
        frame[y, :, 0] = np.clip(30 + (y * 140 // max(1, height)), 0, 255)
        frame[y, :, 1] = np.clip(40 + (y * 90 // max(1, height)), 0, 255)
    if blobs:
        for cx, cy, radius in ((40, 40, 10), (110, 50, 18), (200, 60, 26), (270, 130, 34)):
            cv2.circle(frame, (cx, cy), radius, (255, 255, 255), -1)
    if skin:
        frame[95:150, 15:75] = (120, 150, 210)
        frame[85:155, 215:300] = (110, 140, 200)
    cv2.rectangle(frame, (2, 2), (width - 3, height - 3), (255, 255, 255), 1)
    return frame


def build_engine(frame=None, fps=30, frames=60, start=0, duration=60):
    """An EditorEngine with one video clip backed by synthetic frames (no files)."""
    from meenifx.engine import EditorEngine
    from meenifx.media.cache import MediaEntry

    picture = synthetic_frame() if frame is None else frame
    height, width = picture.shape[:2]
    engine = EditorEngine(fps)
    engine.source_width, engine.source_height = width, height
    path = "synthetic-effect-test.png"
    entry = MediaEntry("video", frames, fps)
    entry.frames = [picture.copy() for _ in range(frames)]
    engine.media._entries[path] = entry
    track = engine.timeline.add_track("video")
    clip = engine.timeline.new_clip("test", path, track, start, duration,
                                    source_out=frames, source_fps=fps)
    return engine, clip


def render(engine, clip, effect_id, params, frame_no=0, scale=1.0):
    clip.effects = [{"effect_id": effect_id, "enabled": True, "params": dict(params)}]
    _raw, processed = engine.render_frame(frame_no, scale)
    return processed


def frame_diff(a, b):
    import numpy as np
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())
