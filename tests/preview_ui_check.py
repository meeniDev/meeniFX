"""Live checks for the cache bar, the perf overlay and the Playback & Performance page."""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

import numpy as np
from PyQt6.QtWidgets import QApplication

from conftest import synthetic_frame
from meenifx.media.cache import MediaEntry
from meenifx.ui.main_window import MainWindow

FAIL = []


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAIL.append(label)


def pump(app, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def main():
    app = QApplication.instance() or QApplication([])
    MainWindow._load_demo_project = lambda self: None
    window = MainWindow(app)
    window.resize(1500, 950)
    window.show()
    pump(app, 0.3)

    canvas = window.timeline_panel.canvas
    check("cache band sits above the ruler",
          canvas.TRACKS_TOP == canvas.TOP_H + canvas.LOOP_H + canvas.CACHE_H + canvas.RULER_H
          and canvas.cache_bar_top == canvas.TOP_H + canvas.LOOP_H,
          f"cache_top={canvas.cache_bar_top} ruler_top={canvas.ruler_top}")

    picture = synthetic_frame(640, 360)
    path = "synthetic-ui.png"
    entry = MediaEntry("video", 120, 30)
    entry.frames = [picture.copy() for _ in range(120)]
    window.engine.media._entries[path] = entry
    window.engine.source_width, window.engine.source_height = 640, 360
    timeline = window.engine.timeline
    timeline.tracks.clear()
    track = timeline.add_track("video")
    timeline.new_clip("t", path, track, 0, 120, source_out=120, source_fps=30)
    window.set_preview_quality("1/2", save=False)
    pump(app, 0.5)

    # ---- cache bar colours come from real states -----------------------------------
    scale = window.preview_scale()
    window.frame_cache.clear()
    for frame in range(0, 40):
        window.frame_cache.put(frame, scale, picture, to_disk=False)
    window.frame_cache.mark_pending_many(range(40, 60), scale)
    pump(app, 0.2)
    pixmap = canvas.grab()
    image = pixmap.toImage()
    y = canvas.cache_bar_top + max(1, canvas.CACHE_H // 2)

    def colour_at_frame(frame):
        x = int(canvas.x_at(frame)) + 2
        if x >= image.width() - 1:
            x = image.width() - 2
        return image.pixelColor(x, y).getRgb()[:3]

    cached = colour_at_frame(10)
    pending = colour_at_frame(50)
    missing = colour_at_frame(80)
    check("cached frames paint green", cached == (34, 197, 94), str(cached))
    check("queued frames paint yellow", pending == (234, 179, 8), str(pending))
    check("uncached frames paint red", missing == (239, 68, 68), str(missing))

    # ---- performance overlay --------------------------------------------------------
    window.perf_overlay.visible = False
    window.preview_panel.view.update(); pump(app, 0.1)
    plain = window.preview_panel.view.grab().toImage()
    window.perf_overlay.visible = True
    window.preview_panel.view.update(); pump(app, 0.1)
    overlay = window.preview_panel.view.grab().toImage()
    corner = (12, 12)
    check("overlay draws real stats over the picture",
          plain.pixelColor(*corner) != overlay.pixelColor(*corner),
          f"{plain.pixelColor(*corner).getRgb()} -> {overlay.pixelColor(*corner).getRgb()}")
    stats = window._overlay_stats()
    check("overlay numbers are measured", isinstance(stats["fps"], float) and
          stats["target_fps"] == float(timeline.fps) and "backend" in stats, str(stats["backend"]))
    window.perf_overlay.visible = False

    # ---- preferences page ----------------------------------------------------------
    from meenifx.ui.start_window import PreferencesDialog
    dialog = PreferencesDialog(window)
    check("preferences expose the playback section", hasattr(dialog, "gpu_mode") and
          dialog.quality_mode.count() == 5 and dialog.ram_cache.value() > 0)
    dialog.gpu_mode.setCurrentIndex(dialog.gpu_mode.findData("off"))
    dialog.quality_mode.setCurrentIndex(dialog.quality_mode.findData("1/4"))
    dialog.ram_cache.setValue(256)
    dialog.background_render.setChecked(True)
    dialog.accept()
    pump(app, 0.2)
    from meenifx.preview.backend import gpu
    check("gpu mode applied from preferences", gpu.mode == "off", gpu.mode)
    check("preview quality applied", window.preview_scale() == 0.25, str(window.preview_scale()))
    check("ram budget applied", window.frame_cache.ram_budget == 256 * 1024 * 1024,
          str(window.frame_cache.ram_budget))
    from meenifx.preview.settings import PlaybackSettings
    check("preferences persisted", PlaybackSettings.load().quality_mode == "1/4",
          PlaybackSettings.load().quality_mode)

    # ---- adaptive auto quality -----------------------------------------------------
    window.set_preview_quality("Auto", save=False)
    start = window.preview_scale()
    window._adapt_preview_quality(400.0)          # far slower than a frame
    reduced = window.preview_scale()
    check("auto quality drops when frames are slow", reduced < start, f"{start} -> {reduced}")
    for _ in range(6):
        window._adapt_preview_quality(2.0)        # comfortably fast
    check("auto quality climbs back when frames are fast",
          window.preview_scale() > reduced, str(window.preview_scale()))

    window.playback_settings.save()
    window.projects._set_clean()
    window.close()
    pump(app, 0.2)
    print()
    print("RESULT:", "ALL PASS" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
