"""Real U^2-Net integration checks, skipped when the optional weights are absent.

The model is not committed and is not required for the rest of the suite. These tests run
the actual ONNX weights through the same effect path the editor uses, including a moving
subject propagated with the model as the per-frame refiner.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest

from conftest import build_engine

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "roto-model-test"
MODEL = ARTIFACTS / "u2net.onnx"
IMAGE = ARTIFACTS / "images" / "person-indoor.jpg"

pytestmark = pytest.mark.skipif(
    not (MODEL.is_file() and IMAGE.is_file()),
    reason="optional U^2-Net weights and test image are not installed",
)


def _model(monkeypatch, tmp_path):
    monkeypatch.delenv("MEENIFX_DISABLE_ROTO_MODEL", raising=False)
    monkeypatch.setenv("MEENIFX_ROTO_MODEL", str(MODEL))
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto-model-cache"))
    from meenifx.roto import model as model_module
    model_module._STATUS_CACHE.clear()
    model_module._HASH_CACHE.clear()
    model_module._MODELS.clear()
    return model_module


def _entry(frame_width, frame_height, point=(0.5, 0.5)):
    return {
        "effect_id": "roto_brush", "enabled": True, "base_frame": 0,
        "strokes": [{"frame": 0, "points": [[point[0], point[1]]], "radius": 0.045,
                     "mode": "foreground"}],
        "params": {"backend": "auto", "quality": "draft", "refine": True,
                   "search_radius": 24.0, "tolerance": 28.0},
    }


def test_u2net_effect_produces_a_real_alpha_cutout(monkeypatch, tmp_path):
    status = _model(monkeypatch, tmp_path)
    ready = status.model_status(verify=True)
    assert ready.available and ready.verified, ready.error

    picture = cv2.imread(str(IMAGE))
    height, width = picture.shape[:2]
    engine, clip = build_engine(frame=picture, duration=4)
    clip.effects = [_entry(width, height)]
    entry = clip.effects[0]

    from meenifx.effects.roto_brush import RotoBrushEffect
    effect = RotoBrushEffect(engine)
    effect.clip = clip
    effect.clip_entry = entry
    effect.local_frame = 0
    effect.param_scale = 1.0
    params = dict(effect.defaults)
    params.update(entry["params"])
    effect.load_params(params)

    layer = np.zeros((height, width, 4), np.uint8)
    layer[:, :, :3] = picture
    layer[:, :, 3] = 255
    cutout = effect.apply_layer(layer)
    assert effect.last_backend == "u2net"
    assert int(cutout[height // 2, width // 2, 3]) > 240, "the subject keeps full alpha"
    assert int(cutout[4, 4, 3]) == 0, "the background becomes transparent"
    assert int(cutout[height // 2, width // 2, :3].max()) > 40
    np.testing.assert_array_equal(cutout[4, 4, :3], layer[4, 4, :3]), \
        "the cutout changes alpha, never the subject pixels"
    coverage = float((cutout[:, :, 3] > 127).mean())
    assert 0.05 < coverage < 0.95, f"implausible subject coverage: {coverage}"


def test_model_propagation_follows_a_moving_subject(monkeypatch, tmp_path):
    status = _model(monkeypatch, tmp_path)
    assert status.model_status(verify=True).available

    picture = cv2.imread(str(IMAGE))
    target_w, target_h = 300, 280
    image = cv2.resize(picture, (target_w, target_h), interpolation=cv2.INTER_AREA)
    frames = []
    for index in range(5):
        canvas = np.full((360, 640, 3), (35, 42, 48), np.uint8)
        x = 40 + index * 30
        canvas[30:30 + target_h, x:x + target_w] = image
        frames.append(canvas)
    point = ((40 + target_w * 0.5) / 640.0, (30 + target_h * 0.5) / 360.0)

    from meenifx.roto.propagate import propagate_seed
    from meenifx.roto.strokes import rasterise
    from meenifx.roto.subject import segment_subject

    settings = {"backend": "auto", "quality": "draft", "refine": False,
                "search_radius": 24.0, "tolerance": 28.0}
    foreground, background, _refine = rasterise(
        [{"frame": 0, "points": [list(point)], "radius": 0.045, "mode": "foreground"}],
        640, 360, (0, 0, 640, 360))
    seed, backend = segment_subject(frames[0], foreground, background, None, settings)
    assert backend == "u2net" and seed is not None

    refiner = lambda _frame, prior, rgb: segment_subject(
        rgb, None, None, None, settings, prior=prior)[0]
    masks = {0: seed}
    done = propagate_seed(seed, 0, range(1, 5), lambda index: frames[index], settings,
                          on_mask=lambda index, matte: masks.__setitem__(index, matte),
                          mask_refiner=refiner)
    assert done == [1, 2, 3, 4]

    def centre_x(matte):
        return float((matte > 0.5).sum() and
                     np.nonzero((matte > 0.5).any(axis=0))[0].mean())

    assert centre_x(masks[4]) > centre_x(seed) + 40.0, \
        "the propagated matte must move with the subject"
    assert float(np.abs(masks[4] - seed).mean()) > 0.02


def test_choose_subject_narrows_a_picture_with_several_animals(monkeypatch, tmp_path):
    status = _model(monkeypatch, tmp_path)
    assert status.model_status(verify=True).available

    picture = cv2.imread(str(ARTIFACTS / "images" / "multiple-cats.jpg"))
    assert picture is not None
    height, width = picture.shape[:2]
    from meenifx.effects.roto_brush import RotoBrushEffect
    from meenifx.roto.subject import segment_subject

    settings = {"backend": "auto", "quality": "draft", "refine": False,
                "search_radius": 24.0, "tolerance": 28.0}
    everything, backend = segment_subject(picture, None, None, None, settings)
    assert backend == "u2net" and everything is not None
    binary = (everything > 0.5).astype(np.uint8) * 255
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    components = sorted((int(stats[index, cv2.CC_STAT_AREA]), index)
                        for index in range(1, count)
                        if stats[index, cv2.CC_STAT_AREA] > 200)
    assert len(components) >= 2, "the multi-cat picture must yield separate subjects"
    _area, chosen_label = components[0]
    _other_area, other_label = components[-1]
    chosen = (float(centroids[chosen_label][0]) / max(1, width - 1),
              float(centroids[chosen_label][1]) / max(1, height - 1))

    engine, clip = build_engine(frame=picture, duration=4)
    entry = _entry(width, height, point=chosen)
    entry["params"]["subject"] = "choose"

    def cutout(item):
        effect = RotoBrushEffect(engine)
        effect.clip = clip
        effect.clip_entry = item
        effect.local_frame = 0
        effect.param_scale = 1.0
        params = dict(effect.defaults)
        params.update(item.get("params") or {})
        effect.load_params(params)
        layer = np.zeros((height, width, 4), np.uint8)
        layer[:, :, :3] = picture
        layer[:, :, 3] = 255
        return effect.apply_layer(layer)

    wide = cutout(dict(entry, subject_point=None))
    narrowed = cutout(dict(entry, subject_point=[chosen[0], chosen[1]]))
    wide_alpha, narrow_alpha = wide[:, :, 3] > 127, narrowed[:, :, 3] > 127
    assert float(narrow_alpha.mean()) < float(wide_alpha.mean()), \
        "choosing one subject must drop the other animals"
    assert bool(narrow_alpha[int(chosen[1] * (height - 1)),
                             int(chosen[0] * (width - 1))]), \
        "the clicked animal stays opaque"
    other_y = int(centroids[other_label][1])
    other_x = int(centroids[other_label][0])
    wide_other = int(wide_alpha[max(0, other_y - 3):other_y + 4,
                                max(0, other_x - 3):other_x + 4].sum())
    narrow_other = int(narrow_alpha[max(0, other_y - 3):other_y + 4,
                                    max(0, other_x - 3):other_x + 4].sum())
    assert narrow_other < wide_other, "the unclicked animal becomes transparent"
