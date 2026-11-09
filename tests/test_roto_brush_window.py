"""The acceptance flow, driven through the real window: brush, propagate, freeze, render.

The backend and overlay suites cover the pieces; this one proves they are connected: the
Roto Brush tool adds its effect, mouse strokes on the preview reach the segmenter, the
background worker fills the cache, previews cut the subject out, Freeze/Unfreeze work and
Done gives the transform tools back.
"""

import time

import cv2
import numpy as np

from conftest import build_engine  # noqa: F401 - keeps the shared test path importable
from meenifx.media.cache import MediaEntry
from meenifx.project.settings import ProjectSettings
from meenifx.roto.cache import MaskCache, mask_key

WIDTH, HEIGHT = 320, 180
SUBJECT = (220, 150, 60)


class _Event:
    def __init__(self, point, modifiers=None):
        from PyQt6.QtCore import QPointF, Qt
        self._point = QPointF(point)
        self._modifiers = modifiers or Qt.KeyboardModifier.NoModifier

    def position(self):
        return self._point

    def button(self):
        from PyQt6.QtCore import Qt
        return Qt.MouseButton.LeftButton

    def modifiers(self):
        return self._modifiers


def pump(seconds=0.1):
    from PyQt6.QtWidgets import QApplication
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.004)


def picture(cx):
    frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    frame[:] = (42, 40, 38)
    cv2.circle(frame, (int(cx), 90), 24, SUBJECT, -1)
    return frame


def add_moving_clip(window):
    engine = window.engine
    frames = [picture(40 + index * 6) for index in range(26)]
    path = "synthetic-roto-window.png"
    entry = MediaEntry("video", len(frames), 30.0)
    entry.frames = frames
    engine.media._entries[path] = entry
    engine.source_width, engine.source_height = WIDTH, HEIGHT
    track = engine.timeline.add_track("video")
    clip = engine.timeline.new_clip("roto window", path, track, 0, len(frames),
                                    source_out=len(frames), source_fps=30.0)
    engine.session.set_selection({clip.id})
    engine.session.set_playhead(0)
    pump(0.12)
    return clip


def test_the_window_runs_the_whole_rotoscoping_workflow(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto"))
    from meenifx.ui.main_window import MainWindow

    MainWindow._load_demo_project = lambda self: None
    window = MainWindow(qapp)
    window.resize(1280, 800)
    window.show()
    pump(0.2)
    try:
        settings = ProjectSettings(name="Roto window", width=WIDTH, height=HEIGHT, fps=30.0)
        assert window.projects.create_project(settings, str(tmp_path))
        pump(0.1)
        clip = add_moving_clip(window)

        # 1. Activating the tool enters the mode and creates the effect on the clip.
        window.effects_panel.roto_requested.emit()
        pump(0.12)
        overlay = window.overlay
        assert overlay.mode == "roto"
        entry = next(item for item in clip.effects if item.get("effect_id") == "roto_brush")
        assert entry["base_frame"] == 0

        # 2. A real drag on the preview paints a green stroke on the subject.
        centre = overlay.to_widget(*overlay.roto_to_project(clip, (40.0 / WIDTH, 0.5)))
        end = overlay.to_widget(*overlay.roto_to_project(clip, (40.0 / WIDTH, 0.62)))
        overlay.mouse_press(_Event(centre))
        overlay.mouse_move(_Event(end))
        overlay.mouse_release(_Event(end))
        entry = next(item for item in clip.effects if item.get("effect_id") == "roto_brush")
        assert entry["strokes"] and entry["strokes"][0]["mode"] == "foreground"
        assert overlay.roto_outline(clip, entry, 0), "the outline appears on the base frame"

        # 3. Forward propagation runs in the worker and writes masks for the other frames.
        window.effects_panel.roto_propagate_requested.emit("forward")
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline and window._roto_worker is not None:
            pump(0.05)
        assert window._roto_worker is None, "propagation did not finish"
        entry = next(item for item in clip.effects if item.get("effect_id") == "roto_brush")
        cache = MaskCache()
        analysed = cache.frames(clip, mask_key(entry))
        assert len(analysed) >= 10, f"only {len(analysed)} frames were analysed"
        assert entry["propagation"]["range"] == [0, 25]
        assert 10 in entry["propagation"]["analysed"]

        # 4. The preview really cuts the subject out, and the mask follows the motion.
        # render_frame composites onto the project (the bottom track is black), so removed
        # background pixels turn black while the retained subject keeps its colour.
        _raw, processed = window.engine.render_frame(10, 1.0)
        assert int(processed[90, 100].max()) > 170, "the subject is retained on that frame"
        assert int(processed[6, 6].max()) < 24, "the background is removed on that frame"
        active = next(item for item in clip.effects
                      if item.get("effect_id") == "roto_brush")
        active["enabled"] = False
        _raw, plain = window.engine.render_frame(10, 1.0)
        active["enabled"] = True
        assert int(plain[6, 6].max()) > 24, "without the effect the background is visible"
        assert float(np.abs(plain.astype(np.int16) - processed.astype(np.int16)).mean()) > 1.0
        base_mask = cache.load(clip, 0, mask_key(entry))
        moved_mask = cache.load(clip, 10, mask_key(entry))
        assert base_mask is not None and moved_mask is not None
        assert float(np.abs(base_mask - moved_mask).mean()) > 0.01, \
            "propagation must follow the subject, not reuse the base mask"

        # 5. Freeze and Unfreeze go through the panel signal and the disk cache.
        window.effects_panel.roto_cache_requested.emit("freeze")
        pump(0.08)
        assert cache.frozen(clip)
        entry = next(item for item in clip.effects if item.get("effect_id") == "roto_brush")
        assert entry["cache"]["frozen"] is True and entry["cache"]["frames"] >= 10
        window.effects_panel.roto_cache_requested.emit("unfreeze")
        pump(0.08)
        assert not cache.frozen(clip)

        # 6. Editing a stroke while frozen invalidates the stale masks and says so.
        window.effects_panel.roto_cache_requested.emit("freeze")
        pump(0.08)
        assert cache.frozen(clip)
        correction = overlay.to_widget(*overlay.roto_to_project(clip, (40.0 / WIDTH, 0.42)))
        overlay.mouse_press(_Event(correction))
        overlay.mouse_move(_Event(overlay.to_widget(
            *overlay.roto_to_project(clip, (46.0 / WIDTH, 0.42)))))
        overlay.mouse_release(_Event(overlay.to_widget(
            *overlay.roto_to_project(clip, (46.0 / WIDTH, 0.42)))))
        pump(0.1)
        assert not cache.frozen(clip), "a new stroke must invalidate the frozen masks"
        entry = next(item for item in clip.effects if item.get("effect_id") == "roto_brush")
        assert cache.frames(clip, mask_key(entry)) == []

        # 7. Done leaves the mode and restores the normal transform tools.
        window.effects_panel.roto_exit_requested.emit()
        pump(0.08)
        assert overlay.mode == "select"
        assert overlay.roto_hover is None
    finally:
        window.projects._set_clean()
        window.close()
