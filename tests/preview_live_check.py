"""Live check: does playback actually advance the preview, and is the cache honest?

Runs the real MainWindow offscreen with a synthetic 1080p clip, presses Play, and samples
the playhead plus the frames the preview widget received. Also exercises the cache bar
state, background prerendering and cancellation.
"""

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
    window.resize(1400, 900)
    window.show()
    pump(app, 0.3)

    picture = synthetic_frame(1920, 1080)
    path = "synthetic-live.mp4"
    entry = MediaEntry("video", 90, 30)
    entry.frames = [picture.copy() for _ in range(90)]
    window.engine.media._entries[path] = entry
    window.engine.source_width, window.engine.source_height = 1920, 1080
    timeline = window.engine.timeline
    timeline.tracks.clear()
    track = timeline.add_track("video")
    clip = timeline.new_clip("test", path, track, 0, 60, source_out=90, source_fps=30)
    window.engine.session.set_selection({clip.id})
    window.set_preview_quality("1/2", save=False)

    # Count distinct frames the preview widget was given.
    shown = []
    original = window.preview_panel.view.set_frame
    window.preview_panel.view.set_frame = lambda image, **kwargs: (
        shown.append(time.perf_counter()), original(image, **kwargs))[1]

    pump(app, 0.5)
    window.engine.session.set_playhead(0, silent=True)
    window._request_preview()
    pump(app, 1.2)
    check("paused preview renders a frame", window.last_frame is not None)

    window._begin_playback(0)
    start_head = window.engine.session.playhead
    pump(app, 2.0)
    end_head = window.engine.session.playhead
    advanced = end_head - start_head
    check("playhead advances during playback", advanced > 20, f"{start_head} -> {end_head}")
    check("preview showed several frames while playing", len(shown) >= 8, f"{len(shown)} frames")
    check("metrics counted shown frames", window.metrics.frames_shown >= 5,
          f"shown={window.metrics.frames_shown} ticks={window.metrics.ticks}")
    fps = window.metrics.fps()
    check("measured playback fps is plausible", 5.0 <= fps <= 40.0, f"{fps:.1f} fps")
    window.stop_playback()
    pump(app, 0.2)
    check("stop is immediate", not window._playing)

    # Cache bar reflects the real cache after pausing.
    playhead = window.engine.session.playhead
    scale = window.preview_scale()
    window._request_preview()
    pump(app, 0.6)
    state = window.frame_cache.state(int(playhead), scale)
    check("cache holds the frame just played", state == "cached", state)
    check("cache state provider agrees", window._cache_state_for_frame(int(playhead)) == "cached",
          window._cache_state_for_frame(int(playhead)))
    far = int(playhead) + 500
    check("uncached frame reports missing", window.frame_cache.state(far, scale) == "missing")

    # Background prerender of a region, then cancel mid-way.
    window.frame_cache.invalidate_ranges([(0, 59)])
    window.render_preview(region=False)
    pump(app, 0.4)
    queued_state = window.frame_cache.state(int(playhead), scale)
    check("prerender marks frames pending/cached", queued_state in ("pending", "cached"),
          queued_state)
    window.cancel_preview_render()
    pump(app, 0.2)
    check("cancel closes the progress dialog", window._prerender_dialog is None)

    # Invalidation: change an effect and only that clip's range disappears.
    window.frame_cache.put(int(playhead), scale, window.last_frame, to_disk=False)
    before = window.frame_cache.state(int(playhead), scale)
    window._clip_digests = {}
    window._on_model_changed()
    window.frame_cache.put(int(playhead), scale, window.last_frame, to_disk=False)
    window._on_model_changed()
    after = window.frame_cache.state(int(playhead), scale)
    check("unchanged model keeps cached frames", before == after == "cached", f"{before}/{after}")

    window._prefetch_timer.stop()
    window.renderer.cancel_background()
    window.frame_cache.put(int(playhead), scale, window.last_frame, to_disk=False)
    timeline.find_clip(clip.id).effects = [{"effect_id": "blur", "enabled": True,
                                            "params": {"radius": 8.0}}]
    window._on_model_changed()
    check("changed clip drops its cached frames",
          not window.frame_cache.has(int(playhead), scale),
          window.frame_cache.state(int(playhead), scale))
    window._prefetch_timer.stop()
    window.renderer.cancel_background()

    # Disk tier: a real folder, visible files, manifest reuse and a safe wipe.
    import tempfile
    folder = tempfile.mkdtemp(prefix="meenifx-cache-")
    window.frame_cache.open_folder(folder, "fingerprint-a")
    window.render_preview(region=False)
    pump(app, 1.5)
    stats = window.frame_cache.stats()
    check("background render wrote disk frames", stats["disk_frames"] > 0, str(stats["disk_frames"]))
    npy = [n for n in os.listdir(folder) if n.endswith(".npy")]
    check("cache files live in the project Cache folder", bool(npy), f"{len(npy)} files")
    window.frame_cache.write_manifest("fingerprint-a", {"fps": 30})
    window.frame_cache.open_folder(folder, "fingerprint-a")
    check("matching manifest keeps the disk cache",
          window.frame_cache.stats()["disk_frames"] == stats["disk_frames"],
          str(window.frame_cache.stats()["disk_frames"]))
    window.frame_cache.open_folder(folder, "fingerprint-b")
    check("outdated manifest wipes the disk cache",
          window.frame_cache.stats()["disk_frames"] == 0, str(window.frame_cache.stats()["disk_frames"]))
    window.frame_cache.clear(disk=True)
    check("clear removes the files", not any(n.endswith(".npy") for n in os.listdir(folder)))

    window.projects._set_clean()
    window.close()
    pump(app, 0.2)
    print()
    print("RESULT:", "ALL PASS" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
