"""Capture the real Properties panel for Posterize Time, in light and dark themes.

Run offscreen with::

    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python tests/posterize_ui_shot.py

The PNGs land in ``artifacts/`` and are meant to be looked at, not asserted on: the
label, the typed precision, the unit and the contrast are what this checks.
"""

import os
import sys
import tempfile
import time

import numpy as np
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from meenifx.media.cache import MediaEntry
from meenifx.project.settings import ProjectSettings
from meenifx.ui import layout as dock_layout
from meenifx.ui.main_window import MainWindow

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "artifacts")


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def main():
    os.makedirs(OUT, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    MainWindow._load_demo_project = lambda self: None
    dock_layout.app_settings = lambda: QSettings(os.path.join(OUT, "shot-layout.ini"),
                                                 QSettings.Format.IniFormat)
    window = MainWindow(app)
    window.resize(1600, 980)
    window.show()
    pump(app, 0.4)
    window.projects.autosave_timer.stop()
    # A fresh folder avoids the "replace existing project?" modal on a second run.
    window.projects.create_project(
        ProjectSettings(name="Shot", width=640, height=360, fps=30.0),
        tempfile.mkdtemp(prefix="meenifx-shot-"))
    print("project ready", flush=True)
    pump(app, 0.3)

    picture = np.zeros((180, 320, 3), np.uint8)
    picture[:, :] = (40, 90, 160)
    entry = MediaEntry("image", 1, 30)
    entry.frames = [picture]
    path = "posterize-shot.png"
    window.engine.media._entries[path] = entry
    timeline = window.engine.timeline
    lanes = timeline.tracks_of_type("video")
    lane = lanes[0] if lanes else timeline.add_track("video")
    clip = timeline.new_clip("Moving shot", path, lane,
                             0, 120, source_out=120, source_fps=30)
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"fps": 23.976}}]
    # Also exercise the adjustment-layer path in the real window: a temporal effect on
    # the adjustment must hold the composite below it, not just its own parameters.
    adjustment_id = window.add_adjustment_layer()
    adjustment = window.engine.timeline.find_clip(adjustment_id)
    adjustment.timeline_start, adjustment.duration = 0, 120
    adjustment.effects = [{"effect_id": "posterize_time", "enabled": True,
                           "params": {"fps": 12.0}}]
    first = window.engine.render_frame(0, 1.0)[1]
    second = window.engine.render_frame(1, 1.0)[1]
    print("adjustment hold diff:", float(np.abs(first.astype(np.int16) -
                                                second.astype(np.int16)).mean()), flush=True)
    window.engine.session.set_selection({adjustment.id})
    window.effects_panel.refresh_properties()
    pump(app, 0.3)
    window.grab().save(os.path.join(OUT, "adjustment-window-light.png"))

    window.engine.session.set_selection({clip.id})
    window.engine.session.set_playhead(0)
    window.effects_panel.refresh_properties()
    pump(app, 0.4)

    row = next(item for item in window.effects_panel._rows if item.get("name") == "fps")
    field = row["field"]
    print("control:", field.label.text(), "value:", field.value(),
          "unit:", field.unit, "decimals:", field.decimals)
    window.effects_panel.props_scroll.ensureWidgetVisible(field)
    pump(app, 0.2)
    field.grab().save(os.path.join(OUT, "posterize-frame-rate-light.png"))
    window.docks["properties"].grab().save(
        os.path.join(OUT, "posterize-properties-light.png"))
    window.grab().save(os.path.join(OUT, "posterize-window-light.png"))

    window.set_theme_choice("dark")
    window.effects_panel.refresh_properties()
    pump(app, 1.0)
    window.repaint()
    pump(app, 0.2)
    row = next(item for item in window.effects_panel._rows if item.get("name") == "fps")
    window.effects_panel.props_scroll.ensureWidgetVisible(row["field"])
    pump(app, 0.2)
    row["field"].grab().save(os.path.join(OUT, "posterize-frame-rate-dark.png"))
    window.docks["properties"].grab().save(
        os.path.join(OUT, "posterize-properties-dark.png"))
    window.grab().save(os.path.join(OUT, "posterize-window-dark.png"))
    window.set_theme_choice("light")

    # The adjustment spans the clip, and pre-compose refuses to leave an overlapping
    # layer behind, so both go into the composition.
    nested_id = window.engine.session.precompose("Breadcrumb comp",
                                                [clip.id, adjustment.id])
    # Use the editing context: a background nested render can swap engine.timeline.
    nested = window.engine.session.timeline.find_clip(nested_id)
    comp = window.engine.compositions.get(nested.composition_id)
    window.open_composition(comp.id)
    pump(app, 0.4)
    print("canvas shows composition:",
          window.timeline_panel.canvas.timeline is comp.timeline)
    window.composition_bar.grab().save(os.path.join(OUT, "composition-breadcrumb.png"))
    window.grab().save(os.path.join(OUT, "composition-window.png"))
    print("breadcrumb:", " > ".join(action.text() for action in
                                    window.composition_bar.actions() if action.text()),
          flush=True)
    print("saved to", OUT, flush=True)
    # Closing would offer to save the throwaway screenshot project; the images are
    # already on disk, so end the helper without entering that modal.
    os._exit(0)


if __name__ == "__main__":
    main()
