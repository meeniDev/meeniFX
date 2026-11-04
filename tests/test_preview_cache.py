"""Unit tests for the preview cache, the render thread, invalidation and the backend."""

import os
import time

import cv2
import numpy as np
import pytest

from conftest import qapp  # noqa: F401  (offscreen QApplication for the QObject classes)


def _cache(**kwargs):
    from meenifx.preview.cache import PreviewCache
    return PreviewCache(**kwargs)


def frame(value=40, width=64, height=48):
    return np.full((height, width, 3), value, np.uint8)


# ------------------------------------------------------------------ cache basics
def test_put_get_and_states(qapp):  # noqa: F811
    cache = _cache()
    assert cache.state(3, 0.5) == "missing"
    cache.mark_pending(3, 0.5)
    assert cache.state(3, 0.5) == "pending"
    cache.put(3, 0.5, frame(120))
    assert cache.state(3, 0.5) == "cached"
    assert cache.has(3, 0.5) and cache.get(3, 0.5).mean() == pytest.approx(120)
    # Scale matters: the same frame at another quality is a different entry.
    assert cache.state(3, 1.0) == "missing"


def test_ram_budget_evicts_least_recently_used(qapp):  # noqa: F811
    cache = _cache(ram_budget_mb=4)
    big = np.full((720, 1280, 3), 10, np.uint8)      # ~2.7 MB each
    cache.put(1, 1.0, big)
    assert cache.state(1, 1.0) == "cached"
    cache.put(2, 1.0, big)
    assert cache.state(2, 1.0) == "cached"
    # The budget only holds one of them, so the first insertion had to go.
    assert cache.state(1, 1.0) == "missing"


def test_invalidate_drops_pending_and_every_scale(qapp):  # noqa: F811
    cache = _cache()
    cache.put(10, 0.5, frame())
    cache.put(10, 1.0, frame())
    cache.mark_pending(11, 0.5)
    cache.invalidate_ranges([(10, 12)])
    assert cache.state(10, 0.5) == "missing"
    assert cache.state(10, 1.0) == "missing"
    assert cache.state(11, 0.5) == "missing", "an invalidated frame cannot still read 'rendering'"


def test_disk_tier_writes_scan_and_wipes(qapp, tmp_path):  # noqa: F811
    cache = _cache(disk_budget_mb=64)
    cache.open_folder(str(tmp_path), "fingerprint-a")
    cache.put(7, 0.5, frame(), to_disk=True)
    assert cache.stats()["disk_frames"] == 1
    assert [n for n in os.listdir(tmp_path) if n.endswith(".npy")]
    cache.write_manifest("fingerprint-a")
    cache.open_folder(str(tmp_path), "fingerprint-a")
    assert cache.stats()["disk_frames"] == 1, "matching manifest reuses the disk cache"
    cache.open_folder(str(tmp_path), "fingerprint-b")
    assert cache.stats()["disk_frames"] == 0
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".npy")], "stale frames were wiped"
    assert cache.get(7, 0.5) is None


def test_disk_frame_survives_a_ram_flush(qapp, tmp_path):  # noqa: F811
    cache = _cache(disk_budget_mb=64)
    cache.open_folder(str(tmp_path), "fp")
    cache.put(5, 0.5, frame(200), to_disk=True)
    cache.clear(disk=False)                     # RAM only
    assert cache.get(5, 0.5) is not None, "the disk copy is read back into RAM"


# ------------------------------------------------------------------ render thread
def test_renderer_keeps_only_the_newest_interactive_request(qapp):  # noqa: F811
    from meenifx.preview.renderer import PreviewRenderer
    rendered = []
    started = time.monotonic()

    def render(frame_no, scale):
        rendered.append(frame_no)
        time.sleep(0.02)
        return frame(frame_no % 255)

    renderer = PreviewRenderer(render)
    renderer.start()
    try:
        for frame_no in range(40):
            renderer.request(frame_no, 0.5)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not rendered:
            qapp.processEvents()
            time.sleep(0.01)
        time.sleep(0.25)
    finally:
        renderer.stop()
    assert rendered, "the render thread must produce something"
    assert len(rendered) < 40, "stale requests are dropped instead of queueing up"
    assert rendered[-1] in range(30, 40), "the newest request wins"


def test_renderer_prerenders_into_the_cache(qapp):  # noqa: F811
    from meenifx.preview.renderer import PreviewRenderer
    cache = _cache()
    renderer = PreviewRenderer(lambda frame_no, scale: frame(frame_no % 255), cache)
    done = []
    renderer.prerender_done.connect(lambda rendered, skipped: done.append((rendered, skipped)))
    renderer.start()
    try:
        queued = renderer.queue_range([0, 1, 2, 3], 0.5)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and cache.stats()["ram_frames"] < 1 + sum(
                cache.has(n, 0.5) for n in (1, 2, 3)):
            qapp.processEvents()
            time.sleep(0.01)
        time.sleep(0.3)
        qapp.processEvents()
    finally:
        renderer.stop()
    assert queued == 4
    assert all(cache.has(frame_no, 0.5) for frame_no in range(4))
    assert done and done[0][0] == 4


def test_background_rendering_yields_to_playback(qapp):  # noqa: F811
    from meenifx.preview.renderer import PreviewRenderer
    cache = _cache()
    renderer = PreviewRenderer(lambda frame_no, scale: frame(), cache)
    renderer.start()
    try:
        renderer.queue_range(range(50), 0.5)
        renderer.prepare_for_playback()
        assert renderer.background_pending == 0
        assert renderer.stats()["paused"] is True
        renderer.finish_playback()
        assert renderer.stats()["paused"] is False
    finally:
        renderer.stop()


# ------------------------------------------------------------------ invalidation
def test_digest_is_stable_and_effect_changes_shift_it():
    from conftest import build_engine
    from meenifx.preview.invalidation import changed_ranges, timeline_digests

    engine, clip = build_engine()
    before = timeline_digests(engine.timeline)
    assert timeline_digests(engine.timeline) == before, "no edit must mean no invalidation"

    clip.effects = [{"effect_id": "blur", "enabled": True, "params": {"radius": 4.0}}]
    after = timeline_digests(engine.timeline)
    ranges = changed_ranges(before, after)
    assert ranges == [(0, 61)], ranges

    # Unrelated regions stay valid: only the clip that changed is refreshed.
    other = engine.timeline.new_clip("other", "other.png", clip.track, 120, 30, source_out=30)
    before = timeline_digests(engine.timeline)
    other.transform = dict(other.transform)
    from meenifx.model.transform import AnimatedValue
    value = AnimatedValue(0.0)
    value.set_keyframe(0, 0.5)
    other.transform["x"] = value
    ranges = changed_ranges(before, timeline_digests(engine.timeline))
    assert ranges == [(119, 151)], ranges


def test_keyframe_values_change_the_digest():
    from conftest import build_engine
    from meenifx.preview.invalidation import clip_digest
    from meenifx.model.transform import AnimatedValue

    engine, clip = build_engine()
    value = AnimatedValue(0.0)
    value.set_keyframe(0, 0.0)
    clip.transform = dict(clip.transform, x=value)
    first = clip_digest(clip)
    value.set_keyframe(0, 0.25)
    assert clip_digest(clip) != first, "a moved keyframe invalidates the clip"


# ------------------------------------------------------------------ backend
def test_backend_cpu_path_matches_opencv_exactly(qapp):  # noqa: F811
    from meenifx.preview.backend import RenderBackend
    backend = RenderBackend(benchmark=False)
    backend.set_mode("off")
    image = np.random.default_rng(3).integers(0, 255, (120, 160, 3), dtype=np.uint8)
    matrix = np.array([[0.9, -0.1, 5.0], [0.1, 0.9, 3.0]], dtype=np.float64)
    assert np.array_equal(backend.resize(image, (80, 60), interpolation=cv2.INTER_AREA),
                          cv2.resize(image, (80, 60), interpolation=cv2.INTER_AREA))
    assert np.array_equal(backend.warp_affine(image, matrix, (160, 120)),
                          cv2.warpAffine(image, matrix, (160, 120)))
    assert not backend.active


def test_backend_gpu_path_is_visually_consistent(qapp):  # noqa: F811
    from meenifx.preview.backend import RenderBackend
    backend = RenderBackend(benchmark=False)
    if not backend.available:
        pytest.skip("no OpenCL device on this machine")
    backend.set_mode("on")
    image = np.random.default_rng(5).integers(0, 255, (120, 160, 4), dtype=np.uint8)
    matrix = np.array([[0.9, -0.1, 5.0], [0.1, 0.9, 3.0]], dtype=np.float64)
    gpu_out = backend.warp_affine(image, matrix, (160, 120))
    cpu_out = cv2.warpAffine(image, matrix, (160, 120))
    assert gpu_out.shape == cpu_out.shape
    assert backend.active
    # Same picture: only sub-pixel rounding may differ.
    assert float(np.abs(gpu_out.astype(np.int16) - cpu_out.astype(np.int16)).mean()) < 2.0
    backend.set_mode("off")
    assert backend.info()[0] == "CPU"


def test_backend_summary_never_overclaims(qapp):  # noqa: F811
    from meenifx.preview.backend import RenderBackend
    backend = RenderBackend(benchmark=False)
    backend.set_mode("auto")
    text = backend.summary()
    if backend.active:
        assert text.startswith("GPU")
    else:
        assert text.startswith("CPU")
        assert "GPU" not in text.split(" - ")[0]


# ------------------------------------------------------------------ settings/metrics
def test_settings_clamp_and_round_trip():
    from meenifx.preview.settings import PlaybackSettings
    settings = PlaybackSettings({"gpu_mode": "sometimes", "quality_mode": "huge",
                                 "ram_cache_mb": -5, "disk_cache_mb": "nope",
                                 "prefetch_seconds": 999})
    assert settings.gpu_mode == "auto"
    assert settings.quality_mode == "Auto"
    assert settings.ram_cache_mb == 64
    assert settings.disk_cache_mb == 4096
    assert settings.prefetch_seconds == 120
    assert PlaybackSettings(settings.as_dict()).as_dict() == settings.as_dict()


def test_metrics_reports_measured_numbers():
    from meenifx.preview.metrics import PlaybackMetrics
    metrics = PlaybackMetrics()
    now = 100.0
    for index in range(10):
        metrics.note_tick(index)
        metrics.note_shown(clock=now + index / 30.0)
    metrics.note_render(12.5)
    assert 25.0 < metrics.fps() < 35.0
    assert metrics.render_ms == pytest.approx(12.5)
    data = metrics.snapshot(target_fps=30, scale=0.5, resolution=(960, 540))
    assert data["target_fps"] == 30 and data["resolution"] == (960, 540)
    assert data["shown"] == 10
