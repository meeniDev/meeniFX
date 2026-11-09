"""Smart Key card and preview interaction.

These checks drive the real widget tree: the one-click actions are emitted from the effect
card, Choose Subject arms the preview, and the click is converted through the same picture
geometry the renderer uses.
"""

import time

import numpy as np

from meenifx.media.cache import MediaEntry
from meenifx.project.settings import ProjectSettings


class _Event:
    def __init__(self, point):
        from PyQt6.QtCore import QPointF, Qt
        self._point = QPointF(point)
        self._modifiers = Qt.KeyboardModifier.NoModifier

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


def add_clip(window):
    engine = window.engine
    picture = np.zeros((180, 320, 3), np.uint8)
    picture[:] = (32, 42, 54)
    picture[40:150, 90:240] = (70, 140, 210)
    frames = [picture.copy() for _ in range(12)]
    path = "smart-key-ui.png"
    media = MediaEntry("video", len(frames), 30.0)
    media.frames = frames
    engine.media._entries[path] = media
    engine.source_width, engine.source_height = 320, 180
    track = engine.timeline.add_track("video")
    clip = engine.timeline.new_clip("smart key", path, track, 0, 12,
                                    source_out=12, source_fps=30.0)
    engine.session.set_selection({clip.id})
    engine.session.set_playhead(0)
    pump(0.12)
    return clip


def test_smart_key_card_actions_and_preview_subject_pick(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto"))
    from meenifx.ui.main_window import MainWindow

    MainWindow._load_demo_project = lambda self: None
    window = MainWindow(qapp)
    window.resize(1280, 800)
    window.show()
    pump(0.2)
    try:
        settings = ProjectSettings(name="Smart Key", width=320, height=180, fps=30.0)
        assert window.projects.create_project(settings, str(tmp_path))
        pump(0.1)
        clip = add_clip(window)
        clip.effects = [{"effect_id": "smart_key", "enabled": True,
                         "params": {"subject": "auto", "quality": "draft"}}]
        window.effects_panel.refresh_properties()
        pump(0.12)
        panel = window.effects_panel
        ui = getattr(panel, "_smart_key", None)
        assert ui is not None, "the Smart Key card builds its one-click actions"
        assert ui["remove"].text() == "Remove Background"
        assert ui["reanalyze"].text() == "Reanalyze"
        assert "model" in ui["status"].text().lower()

        actions = []
        panel.smart_key_requested.connect(actions.append)
        # The test environment disables the optional weights. The real button offers the
        # download dialog in that case; stub it so this widget test never opens a modal.
        window._roto_model_action = lambda: actions.append("model-dialog")
        ui["remove"].click()
        pump(0.06)
        assert "analyze" in actions
        ui["choose"].click()
        pump(0.06)
        assert window.overlay.subject_pick_effect == "smart_key"

        centre = window.overlay.to_widget(160, 90)
        assert window.overlay.mouse_press(_Event(centre))
        pump(0.08)
        entry = next(item for item in clip.effects
                     if item.get("effect_id") == "smart_key")
        point = entry["subject_point"]
        assert abs(point[0] - 0.5) < 0.01 and abs(point[1] - 0.5) < 0.01, \
            "the click is mapped through the clip transform into picture coordinates"
        assert entry["params"]["subject"] == "auto"
        assert window.overlay.subject_pick_effect is None

        # Switching the Subject choice back to Auto clears the clicked point.
        entry["subject_point"] = [0.5, 0.5]
        window._smart_key_action("auto")
        assert "subject_point" not in entry
    finally:
        window.projects._set_clean()
        window.close()
