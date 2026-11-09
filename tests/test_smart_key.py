"""Smart Key: automatic subject isolation, video scanning and shared infrastructure.

The tests without the optional weights prove that Smart Key is not a chroma key: without
the model it changes nothing rather than guessing a screen colour. The model tests run on
real photographs with ordinary multicoloured backgrounds.
"""

import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from conftest import build_engine
from meenifx.media.cache import MediaEntry
from meenifx.roto.cache import MaskCache, mask_key, mask_size
from meenifx.roto.propagate import frame_order
from meenifx.roto.worker import SubjectScanWorker

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "roto-model-test"
MODEL = ARTIFACTS / "u2net.onnx"
IMAGES = ARTIFACTS / "images"

requires_model = pytest.mark.skipif(
    not (MODEL.is_file() and IMAGES.is_dir()),
    reason="optional U^2-Net weights and test images are not installed",
)


def _entry(**params):
    values = {"subject": "auto", "quality": "draft", "smooth": 1.0, "feather": 1.5,
              "choke": 0.0, "matte_view": "composite", "edge_contrast": 25.0}
    values.update(params)
    return {"effect_id": "smart_key", "enabled": True, "params": values}


def _runtime(engine, clip, entry, frame=0):
    effect = engine.runtime_effects["smart_key"]
    effect.clip = clip
    effect.clip_entry = entry
    effect.local_frame = int(frame)
    effect.param_scale = 1.0
    params = dict(effect.defaults)
    params.update(entry.get("params") or {})
    effect.load_params(params)
    return effect


def _layer(picture):
    layer = np.zeros(picture.shape[:2] + (4,), np.uint8)
    layer[:, :, :3] = picture
    layer[:, :, 3] = 255
    return layer


def _model(monkeypatch, tmp_path):
    monkeypatch.delenv("MEENIFX_DISABLE_ROTO_MODEL", raising=False)
    monkeypatch.setenv("MEENIFX_ROTO_MODEL", str(MODEL))
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "smart-key-cache"))
    from meenifx.roto import model as model_module
    model_module._STATUS_CACHE.clear()
    model_module._HASH_CACHE.clear()
    model_module._MODELS.clear()
    return model_module


def _image(name, max_edge=480):
    picture = cv2.imread(str(IMAGES / f"{name}.jpg"))
    assert picture is not None
    height, width = picture.shape[:2]
    factor = min(1.0, float(max_edge) / max(width, height))
    if factor < 1.0:
        picture = cv2.resize(picture, (max(1, int(width * factor)),
                                       max(1, int(height * factor))),
                             interpolation=cv2.INTER_AREA)
    return picture


def test_smart_key_is_not_chroma_key_without_the_model():
    engine, clip = build_engine()
    # A perfect chroma-green plate: a colour key would erase the background immediately.
    layer = np.zeros((180, 320, 4), np.uint8)
    layer[:, :, :3] = (64, 177, 0)
    layer[36:145, 72:248, :3] = (120, 150, 210)
    layer[:, :, 3] = 255
    entry = _entry()
    out = _runtime(engine, clip, entry).apply_layer(layer.copy())
    np.testing.assert_array_equal(out, layer), \
        "Smart Key must not fall back to guessing a screen colour"
    effect = engine.runtime_effects["smart_key"]
    assert not hasattr(effect, "KEY_COLOR_NAMES")
    names = {spec.name for spec in effect.PARAMS}
    assert not ({"key_r", "key_g", "key_b", "tolerance"} & names), \
        "key colour controls belong exclusively to Chroma Key"
    assert "model" in effect.last_note.lower()


def test_the_two_effects_keep_separate_mask_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto"))
    engine, clip = build_engine(duration=10)
    smart = _entry()
    roto = {"effect_id": "roto_brush", "enabled": True, "params": {}, "base_frame": 0,
            "strokes": []}
    size = mask_size(320, 180)
    matte = np.zeros((180, 320), np.float32)
    matte[40:140, 80:240] = 1.0
    smart_cache = MaskCache(scope="smart_key")
    smart_cache.store(clip, 0, matte, mask_key(smart), size)
    assert smart_cache.load(clip, 0, mask_key(smart)) is not None
    assert MaskCache().load(clip, 0, mask_key(smart)) is None
    assert mask_key(smart) != mask_key(roto)


@requires_model
@pytest.mark.parametrize("name", ["person-indoor", "person-outdoor", "cat", "dog"])
def test_model_isolates_a_subject_on_a_natural_background(name, monkeypatch, tmp_path):
    status = _model(monkeypatch, tmp_path)
    assert status.model_status(verify=True).available
    picture = _image(name)
    height, width = picture.shape[:2]
    engine, clip = build_engine(frame=picture, duration=4)
    entry = _entry()
    clip.effects = [entry]
    effect = _runtime(engine, clip, entry)
    layer = _layer(picture)
    out = effect.apply_layer(layer.copy())

    assert effect.last_backend == "u2net"
    assert int(out[4, 4, 3]) == 0, f"{name}: the corner background is transparent"
    assert int(out[height // 2, width // 2, 3]) > 240, f"{name}: the subject stays opaque"
    assert int(out[height // 2, width // 2, :3].max()) > 30
    np.testing.assert_array_equal(out[4, 4, :3], layer[4, 4, :3]), \
        "the matte changes alpha, never paints green or black over the pixels"
    coverage = float((out[:, :, 3] > 127).mean())
    assert 0.03 < coverage < 0.92, f"{name}: implausible subject coverage {coverage}"


@requires_model
def test_choose_subject_selects_one_animal(monkeypatch, tmp_path):
    status = _model(monkeypatch, tmp_path)
    assert status.model_status(verify=True).available
    picture = _image("multiple-cats")
    height, width = picture.shape[:2]
    engine, clip = build_engine(frame=picture, duration=4)
    entry = _entry()
    clip.effects = [entry]
    wide = _runtime(engine, clip, entry).apply_layer(_layer(picture))
    binary = (wide[:, :, 3] > 127).astype(np.uint8) * 255
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    components = sorted((int(stats[index, cv2.CC_STAT_AREA]), index)
                        for index in range(1, count)
                        if stats[index, cv2.CC_STAT_AREA] > 200)
    assert len(components) >= 2, "the multi-cat picture must yield separate subjects"
    _area, chosen_label = components[0]
    _other_area, other_label = components[-1]
    point = [float(centroids[chosen_label][0]) / max(1, width - 1),
             float(centroids[chosen_label][1]) / max(1, height - 1)]
    chosen_entry = _entry(subject="choose")
    chosen_entry["subject_point"] = point
    clip.effects = [chosen_entry]
    narrowed = _runtime(engine, clip, chosen_entry).apply_layer(_layer(picture))

    wide_alpha = wide[:, :, 3] > 127
    narrow_alpha = narrowed[:, :, 3] > 127
    assert float(narrow_alpha.mean()) < float(wide_alpha.mean())
    chosen_y = int(point[1] * (height - 1))
    chosen_x = int(point[0] * (width - 1))
    assert bool(narrow_alpha[chosen_y, chosen_x]), "the clicked animal stays opaque"
    other_y, other_x = int(centroids[other_label][1]), int(centroids[other_label][0])
    assert not bool(narrow_alpha[other_y, other_x]), "the unclicked animal is removed"


@requires_model
def test_transparent_composite_and_export_over_a_lower_clip(monkeypatch, tmp_path):
    status = _model(monkeypatch, tmp_path)
    assert status.model_status(verify=True).available
    picture = _image("person-indoor")
    height, width = picture.shape[:2]
    engine, clip = build_engine(frame=picture, duration=10)
    lower_track = engine.timeline.add_track("video", index=len(engine.timeline.tracks))
    lower = np.full((height, width, 3), (180, 40, 120), np.uint8)
    media = MediaEntry("video", 10, 30.0)
    media.frames = [lower.copy() for _ in range(10)]
    engine.media._entries["smart-key-lower.png"] = media
    engine.timeline.new_clip("lower", "smart-key-lower.png", lower_track, 0, 10,
                             source_out=10, source_fps=30.0)
    clip.effects = [_entry()]
    _raw, preview = engine.render_frame(0, 1.0)
    np.testing.assert_array_equal(preview[4, 4], lower[4, 4]), \
        "the removed background shows the lower clip in the timeline"
    assert int(preview[height // 2, width // 2].max()) > 30

    # A diagnostic view is selected deliberately: export must still write Composite.
    clip.effects = [_entry(matte_view="alpha")]
    written, count, _fps, size = engine.export({
        "output_path": str(tmp_path / "smart-key.png"), "format": "png",
        "range_start": 0, "range_end": 1, "use_source_size": True,
        "use_source_fps": True})
    assert count == 1 and size[0] == width
    exported = cv2.imread(written)
    assert exported is not None
    assert exported.shape[:2] == (size[1], size[0])
    np.testing.assert_array_equal(exported[4, 4], lower[4, 4]), \
        "the exported background is the lower clip, not the black alpha preview"
    assert int(exported[height // 2, width // 2].max()) > 30


@requires_model
def test_video_scan_caches_a_moving_subject(qapp, monkeypatch, tmp_path):
    status = _model(monkeypatch, tmp_path)
    assert status.model_status(verify=True).available
    subject = _image("cat", max_edge=170)
    target_h, target_w = subject.shape[:2]
    frames = []
    for index in range(5):
        canvas = np.full((260, 560, 3), (34, 42, 48), np.uint8)
        x = 30 + index * 55
        canvas[40:40 + target_h, x:x + target_w] = subject
        frames.append(canvas)
    engine, clip = build_engine(frame=frames[0], duration=len(frames),
                                frames=len(frames))
    engine.media._entries[clip.source_path].frames = [frame.copy() for frame in frames]
    entry = _entry()
    clip.effects = [entry]
    analyzer = _runtime(engine, clip, entry)
    cache = MaskCache(scope="smart_key")
    key = mask_key(entry)
    size = mask_size(frames[0].shape[1], frames[0].shape[0])
    cache.configure(clip, key, size)
    order = frame_order(clip.source_in, clip.source_out - 1, 0, "all")
    progress = []
    worker = SubjectScanWorker(engine, clip, key, 0, order,
                               analyzer._analysis_settings(), cache, analyzer)
    worker.progressed.connect(lambda done, total: progress.append((done, total)))
    worker.start()
    deadline = time.monotonic() + 120.0
    while worker.isRunning() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert not worker.isRunning(), "the subject scan did not finish"
    worker.wait(2000)
    for _index in range(20):
        qapp.processEvents()
        time.sleep(0.005)

    stored = cache.frames(clip, key)
    assert len(stored) >= 4, f"only {len(stored)} frames were cached"
    assert progress and progress[-1][0] == progress[-1][1] == len(order) + 1
    masks = {frame: cache.load(clip, frame, key) for frame in stored}

    def centre_x(matte):
        columns = np.nonzero((matte > 0.5).any(axis=0))[0]
        return float(columns.mean()) if columns.size else 0.0

    assert centre_x(masks[4]) > centre_x(masks[0]) + 25.0, \
        "the matte must follow the moving subject"
    assert float(np.abs(masks[4] - masks[0]).mean()) > 0.01
    assert analyzer.last_backend == "u2net"
