"""Measured benchmarks for the preview pipeline (before/after, cached/uncached, GPU/CPU).

Every number printed here comes from this machine and the real code paths:

* playback goes through MainWindow's timer and the new render thread/cache;
* the "legacy" column reproduces the old behaviour (render on a worker, display only if
  the finished frame still equals the playhead) to show why the picture froze;
* export timing feeds real composited frames to the two writers;
* proxy timings decode a generated 1080p file with and without its proxy.

Usage:  python tests/preview_bench.py [--quick]
"""

import concurrent.futures
import os
import statistics
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import numpy as np
from PyQt6.QtWidgets import QApplication

from conftest import synthetic_frame
from meenifx.media.cache import MediaEntry
from meenifx.ui.main_window import MainWindow

RESULTS = []


def record(group, label, **values):
    RESULTS.append((group, label, values))
    print(f"{group:<22} {label:<34} " + "  ".join(f"{k}={v}" for k, v in values.items()), flush=True)


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.004)


def build_window(app, width, height, frames=90, layers=1, effects=None, animated=False,
                 puppet=False):
    """A window with `layers` video tracks over synthetic frames of the given size."""
    window = MainWindow(app)
    window.resize(1400, 900)
    window.show()
    pump(app, 0.25)
    timeline = window.engine.timeline
    timeline.tracks.clear()
    picture = synthetic_frame(width, height)
    for index in range(layers):
        path = f"synthetic-bench-{index}.png"
        entry = MediaEntry("video", frames, 30)
        entry.frames = [picture.copy() for _ in range(frames)]
        window.engine.media._entries[path] = entry
        track = timeline.add_track("video")
        clip = timeline.new_clip(f"layer{index}", path, track, 0, frames, source_out=frames,
                                 source_fps=30)
        if effects:
            clip.effects = [{"effect_id": key, "enabled": True, "params": dict(params)}
                            for key, params in effects]
        if puppet:
            clip.effects = list(clip.effects) + [{
                "effect_id": "puppet_warp", "enabled": True,
                "params": {"stiffness": 1.0, "mesh_density": 24, "show_mesh": False},
                "pins": [{"id": 1, "x": 0.5, "y": 0.5}, {"id": 2, "x": 0.62, "y": 0.52}]}]
        if animated:
            from meenifx.model.transform import AnimatedValue
            transform = dict(clip.transform)
            for name, keys in (("x", [(0, 0.0), (frames - 1, 0.25)]),
                               ("scale", [(0, 1.0), (frames - 1, 1.4)])):
                value = AnimatedValue(transform.get(name, 0.0))
                for frame_no, amount in keys:
                    value.set_keyframe(frame_no, amount)
                transform[name] = value
            clip.transform = transform
    window.engine.source_width, window.engine.source_height = width, height
    window.engine.session.project_size = (width, height)
    pump(app, 0.4)
    return window


def cold_render_ms(window, scale, samples=8):
    """Median time for one uncached interactive-quality frame through the real engine."""
    times = []
    for frame in range(5, 5 + samples):
        started = time.perf_counter()
        window.engine.render_frame(frame, scale)
        times.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(times)


def playback(window, app, seconds=2.0, clear=True, cold=False):
    """Real playback through the window: measured fps, dropped frames, distinct pictures.

    ``cold`` hides the cache from the reader, so every frame must be rendered during
    playback - that is the "no preview cache" configuration.
    """
    shown = []
    original = window.preview_panel.view.set_frame
    window.preview_panel.view.set_frame = lambda image, **kwargs: (
        shown.append(id(image)), original(image, **kwargs))[1]
    cache = window.frame_cache
    saved = (cache.get, cache.has)
    if cold:
        cache.get = lambda *a, **kw: None
        cache.has = lambda *a, **kw: False
    if clear:
        cache.clear()
    window._begin_playback(0)
    pump(app, seconds)
    window.stop_playback()
    pump(app, 0.2)
    if cold:
        cache.get, cache.has = saved
    window.preview_panel.view.set_frame = original
    head = window.engine.session.playhead
    return {"fps": round(window.metrics.fps(), 1),
            "advanced": head,
            "shown": window.metrics.frames_shown,
            "due": window.metrics.ticks,
            "dropped": max(0, window.metrics.ticks - window.metrics.frames_shown),
            "distinct": len(set(shown))}


def cached_playback(window, app, seconds=2.0, wait=20.0):
    """Cache the range first (background render), then play it back only when ready."""
    window.frame_cache.clear()
    window.render_preview(region=False)
    deadline = time.monotonic() + wait
    pump(app, 0.3)
    while time.monotonic() < deadline:
        if not window.renderer.background_pending and window.frame_cache.pending_count() == 0:
            break
        pump(app, 0.1)
    return playback(window, app, seconds, clear=False)


def legacy_playback(window, app, seconds=2.0):
    """The old architecture: render off-thread, show only if the frame still matches."""
    scale = window.preview_scale()
    shown = 0
    distinct = set()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    original = window.preview_panel.view.set_frame

    def display(image, **kwargs):
        nonlocal shown
        if image is None:
            return
        shown += 1
        distinct.add(int(np.asarray(image).sum()) % 100000)
        original(image, **kwargs)

    window.preview_panel.view.set_frame = display
    window.renderer.set_background_enabled(False)
    window.renderer.cancel_background()
    fps = window.engine.timeline.fps
    futures = []
    start = time.perf_counter()
    next_tick = start
    while time.perf_counter() - start < seconds:
        now = time.perf_counter()
        if now >= next_tick:
            next_tick += 1.0 / fps
            frame = int((now - start) * fps)
            frame = min(frame, 88)
            window.engine.session.set_playhead(frame, silent=True)
            futures.append((frame, pool.submit(window.engine.render_frame, frame, scale)))
        for target, future in list(futures):
            if future.done():
                futures.remove((target, future))
                try:
                    _raw, image = future.result()
                except Exception:
                    continue
                # The old rule: discard anything that is not exactly the playhead.
                if target == window.engine.session.playhead:
                    display(image)
        app.processEvents()
        time.sleep(0.004)
    for target, future in futures:
        future.result()
    pool.shutdown(wait=True)
    window.preview_panel.view.set_frame = original
    window.renderer.set_background_enabled(True)
    elapsed = time.perf_counter() - start
    return {"fps_shown": round(shown / elapsed, 1), "shown": shown, "distinct": len(distinct),
            "advanced": window.engine.session.playhead}


def export_timing(window, frames=40, size=(1920, 1080)):
    """Encode the same frames with the hardware writer and the OpenCV writer."""
    width, height = size
    scale = 1.0
    pictures = [window.engine.render_frame(frame, scale)[1] for frame in range(frames)]
    results = {}
    folder = tempfile.mkdtemp(prefix="meenifx-bench-")
    for label in ("h264_nvenc", "mp4v"):
        path = os.path.join(folder, f"{label}.mp4")
        started = time.perf_counter()
        if label == "h264_nvenc":
            from meenifx.media.hardware import FFmpegVideoWriter, nvenc_available
            if not nvenc_available():
                results[label] = "unavailable"
                continue
            writer = FFmpegVideoWriter(path, width, height, 30)
        else:
            import cv2
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (width, height))
        for picture in pictures:
            writer.write(picture)
        writer.release()
        results[label] = round(time.perf_counter() - started, 2)
    return results


def proxy_timing(seconds=3.0):
    """Decode a real 1080p file and its proxy; report the per-frame decode cost."""
    from meenifx.media.hardware import ffmpeg_exe
    from meenifx.media.proxy import make_proxy
    from meenifx.media.reader import StreamFrames
    import cv2
    exe = ffmpeg_exe()
    if not exe:
        return {"source_ms": "no ffmpeg"}
    folder = tempfile.mkdtemp(prefix="meenifx-proxy-")
    source = os.path.join(folder, "source.mp4")
    # Noise is deliberately expensive to decode: it is the worst case for a proxy.
    command = [exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
               "-vf", "noise=alls=40:allf=t", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "16", "-pix_fmt", "yuv420p", source]
    subprocess.run(command, check=False, capture_output=True)
    if not os.path.exists(source):
        return {"source_ms": "generation failed"}
    proxy = os.path.join(folder, "proxy.mp4")
    if not make_proxy(source, proxy, 960):
        return {"source_ms": "proxy failed"}

    def decode_ms(path):
        cap = cv2.VideoCapture(path)
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        started = time.perf_counter()
        index = 0
        while index < count:
            ok, _frame = cap.read()
            if not ok:
                break
            index += 1
        elapsed = time.perf_counter() - started
        cap.release()
        return round(elapsed / max(1, index) * 1000.0, 2), index

    source_ms, source_frames = decode_ms(source)
    proxy_ms, proxy_frames = decode_ms(proxy)
    return {"source_ms": source_ms, "proxy_ms": proxy_ms,
            "speedup": round(source_ms / max(0.01, proxy_ms), 2),
            "frames": f"{source_frames}/{proxy_frames}"}


def main():
    quick = "--quick" in sys.argv
    app = QApplication.instance() or QApplication([])
    MainWindow._load_demo_project = lambda self: None

    effects = [("blur", {"radius": 6.0, "blur_type": "gaussian"}),
               ("glow", {"intensity": 40.0, "radius": 12.0}),
               ("color_correct", {"brightness": 10.0, "contrast": 1.15, "saturation": 1.2})]

    print("== measured on this machine " + "=" * 46)
    from meenifx.preview.backend import gpu
    gpu.benchmark()
    record("backend", "GPU probe", summary=gpu.summary())
    record("backend", "OpenCL ratios (cpu/gpu)", **{k: round(v, 2) for k, v in gpu.ratios().items()})

    # ---- 1080p, no effects -------------------------------------------------------
    window = build_window(app, 1920, 1080)
    window.set_preview_quality("Full", save=False)
    record("1080p no effects", "render ms (full, cold)",
           ms=round(cold_render_ms(window, 1.0), 1))
    record("1080p no effects", "render ms (half, cold)",
           ms=round(cold_render_ms(window, 0.5), 1))
    record("1080p no effects", "legacy playback (old rule)",
           **legacy_playback(window, app, 1.5 if quick else 2.0))
    record("1080p no effects", "playback no cache (new)", **playback(window, app, 1.5 if quick else 2.0, cold=True))
    record("1080p no effects", "playback cached (new)", **cached_playback(window, app, 1.5 if quick else 2.0))
    window.playback_settings.save()
    window.projects._set_clean()
    window.close()
    pump(app, 0.2)

    # ---- 1080p with effects ------------------------------------------------------
    window = build_window(app, 1920, 1080, effects=effects)
    window.set_preview_quality("Full", save=False)
    record("1080p + 3 effects", "render ms (full, cold)", ms=round(cold_render_ms(window, 1.0, 5), 1))
    record("1080p + 3 effects", "render ms (half, cold)", ms=round(cold_render_ms(window, 0.5, 5), 1))
    record("1080p + 3 effects", "playback no cache (new)", **playback(window, app, 1.5 if quick else 2.0, cold=True))
    record("1080p + 3 effects", "playback cached (new)", **cached_playback(window, app, 1.5 if quick else 2.0))
    window.projects._set_clean()
    window.close()
    pump(app, 0.2)

    # ---- 4K with effects ---------------------------------------------------------
    window = build_window(app, 3840, 2160, frames=45, effects=effects)
    window.set_preview_quality("1/2", save=False)
    record("4K + 3 effects", "render ms (half, cold)", ms=round(cold_render_ms(window, 0.5, 3), 1))
    record("4K + 3 effects", "playback no cache (new)", **playback(window, app, 1.5 if quick else 2.0, cold=True))
    record("4K + 3 effects", "playback cached (new)", **cached_playback(window, app, 1.5 if quick else 2.0))
    window.projects._set_clean()
    window.close()
    pump(app, 0.2)

    # ---- three simultaneous layers ----------------------------------------------
    window = build_window(app, 1920, 1080, layers=3, effects=[("color_correct",
                                                              {"brightness": 6.0})])
    window.set_preview_quality("1/2", save=False)
    record("3 layers 1080p", "render ms (half, cold)", ms=round(cold_render_ms(window, 0.5, 5), 1))
    record("3 layers 1080p", "playback no cache (new)", **playback(window, app, 1.5 if quick else 2.0, cold=True))
    record("3 layers 1080p", "playback cached (new)", **cached_playback(window, app, 1.5 if quick else 2.0))
    window.projects._set_clean()
    window.close()
    pump(app, 0.2)

    # ---- animated transform ------------------------------------------------------
    window = build_window(app, 1920, 1080, animated=True)
    window.set_preview_quality("1/2", save=False)
    record("animated transform", "render ms (half, cold)", ms=round(cold_render_ms(window, 0.5, 5), 1))
    record("animated transform", "playback cached (new)", **cached_playback(window, app, 1.5 if quick else 2.0))
    window.projects._set_clean()
    window.close()
    pump(app, 0.2)

    # ---- puppet warp animation ---------------------------------------------------
    window = build_window(app, 1920, 1080, puppet=True)
    window.set_preview_quality("1/2", save=False)
    record("puppet warp", "render ms (half, cold)", ms=round(cold_render_ms(window, 0.5, 3), 1))
    record("puppet warp", "playback cached (new)", **cached_playback(window, app, 1.5 if quick else 2.0))

    # ---- GPU on vs off for the same frames --------------------------------------
    window.set_preview_quality("1/2", save=False)
    gpu.set_mode("off")
    cpu_ms = cold_render_ms(window, 0.5, 5)
    gpu.set_mode("on")
    gpu_ms = cold_render_ms(window, 0.5, 5)
    gpu.set_mode("auto")
    record("gpu vs cpu", "frame ms (cpu)", ms=round(cpu_ms, 1))
    record("gpu vs cpu", "frame ms (forced OpenCL)", ms=round(gpu_ms, 1))

    # ---- export encoding ---------------------------------------------------------
    from meenifx.media.hardware import nvenc_available
    record("export 200x1080p", "NVENC available", value=nvenc_available())
    record("export 200x1080p", "encode seconds", **export_timing(window, 80 if quick else 200))

    window.projects._set_clean()
    window.close()
    pump(app, 0.2)

    # ---- 4K encode: the case where a hardware encoder has room to win ------------
    window = build_window(app, 3840, 2160, frames=120)
    window.set_preview_quality("1/2", save=False)
    record("export 80x4K", "encode seconds", **export_timing(window, 40 if quick else 80,
                                      size=(3840, 2160)))
    window.projects._set_clean()
    window.close()
    pump(app, 0.2)

    # ---- proxy decode ------------------------------------------------------------
    record("proxy media", "decode 1080p", **proxy_timing(2.0 if quick else 3.0))

    print()
    print(f"{len(RESULTS)} measurements recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
