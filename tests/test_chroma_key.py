"""Chroma Key: soft matte, spill suppression, edge shaping and diagnostic views."""

import cv2
import numpy as np

from conftest import build_engine, frame_diff


def plate(background=(64, 177, 0), foreground=(120, 150, 210), size=(320, 180)):
    width, height = size
    layer = np.zeros((height, width, 4), np.uint8)
    layer[:, :, :3] = background
    layer[:, :, 3] = 255
    layer[36:145, 72:248, :3] = foreground
    # Green spill on one foreground edge, and a soft hair-like fringe.
    layer[80:102, 248:264, :3] = (35, 178, 118)
    layer[80:102, 252:264, 3] = 210
    return layer


def runtime(engine, scale=1.0, **params):
    effect = engine.runtime_effects["chroma_key"]
    values = dict(effect.defaults)
    values.update(params)
    effect.param_scale = scale
    effect.local_frame = 0
    effect.load_params(values)
    return effect


def test_green_screen_is_removed_and_foreground_survives():
    engine, _clip = build_engine()
    layer = plate()
    out = runtime(engine, edge_softness=1.0).apply_layer(layer.copy())

    assert out[10, 10, 3] == 0, "green background clears"
    assert out[90, 150, 3] > 250, "the foreground stays opaque"
    assert out[90, 150, 0] > 100, "foreground colour is not erased"


def test_blue_screen_and_custom_screen_colours_work():
    engine, _clip = build_engine()
    blue = plate(background=(187, 71, 0), foreground=(80, 120, 220))
    blue_out = runtime(engine, key_r=0, key_g=71, key_b=187).apply_layer(blue.copy())
    assert blue_out[10, 10, 3] == 0
    assert blue_out[90, 150, 3] > 250

    custom = plate(background=(220, 20, 210), foreground=(80, 170, 90))
    custom_out = runtime(engine, key_r=210, key_g=20, key_b=220).apply_layer(custom.copy())
    assert custom_out[10, 10, 3] == 0
    assert custom_out[90, 150, 3] > 250


def test_edge_softness_creates_fractional_alpha():
    engine, _clip = build_engine()
    layer = plate()
    hard = runtime(engine, edge_softness=0.0, cleanup=0.0,
                   similarity=0.0).apply_layer(layer.copy())
    soft = runtime(engine, edge_softness=4.0, cleanup=0.0,
                   similarity=0.0).apply_layer(layer.copy())
    assert np.count_nonzero((soft[:, :, 3] > 5) & (soft[:, :, 3] < 250)) > 0
    assert frame_diff(soft, hard) > 0.4


def test_tolerance_changes_the_matte_coverage():
    engine, _clip = build_engine()
    layer = plate()
    tight = runtime(engine, tolerance=8.0, similarity=10.0).apply_layer(layer.copy())
    loose = runtime(engine, tolerance=75.0, similarity=30.0).apply_layer(layer.copy())
    assert loose[:, :, 3].sum() < tight[:, :, 3].sum()


def test_spill_suppression_removes_green_cast_without_changing_alpha():
    engine, _clip = build_engine()
    layer = plate()
    plain = runtime(engine, spill=0.0).apply_layer(layer.copy())
    cleaned = runtime(engine, spill=100.0).apply_layer(layer.copy())
    assert np.array_equal(plain[:, :, 3], cleaned[:, :, 3])
    edge = (slice(80, 102), slice(248, 264))
    assert cleaned[edge][:, :, 1].mean() < plain[edge][:, :, 1].mean() - 3.0


def test_choke_erodes_and_expands_the_matte():
    engine, _clip = build_engine()
    layer = plate()
    eroded = runtime(engine, edge_softness=3.0, choke=5.0).apply_layer(layer.copy())
    expanded = runtime(engine, edge_softness=3.0, choke=-5.0).apply_layer(layer.copy())
    assert eroded[:, :, 3].sum() < expanded[:, :, 3].sum()


def test_matte_views_are_diagnostic_and_spill_view_keeps_source_alpha():
    engine, _clip = build_engine()
    layer = plate()
    alpha = runtime(engine, matte_view="alpha").apply_layer(layer.copy())
    mono = runtime(engine, matte_view="black_white").apply_layer(layer.copy())
    spill = runtime(engine, matte_view="spill", spill=100.0).apply_layer(layer.copy())

    assert alpha[90, 150, 3] == 255
    assert int(alpha[90, 150, 0]) > 200, "foreground is white in the alpha view"
    assert set(np.unique(mono[:, :, 0])).issubset({0, 255})
    assert np.array_equal(spill[:, :, 3], layer[:, :, 3])


def test_zero_transparency_leaves_the_source_alpha_unchanged():
    engine, _clip = build_engine()
    layer = plate()
    out = runtime(engine, transparency=0.0, spill=0.0).apply_layer(layer.copy())
    assert np.array_equal(out, layer)


def test_preview_and_full_resolution_matrices_agree():
    engine, clip = build_engine(frame=plate()[:, :, :3], duration=30)
    clip.effects = [{"effect_id": "chroma_key", "enabled": True, "params": {
        "tolerance": 35.0, "similarity": 25.0, "edge_softness": 2.0,
    }}]
    full = engine.render_frame(0, 1.0)[1]
    half = engine.render_frame(0, 0.5)[1]
    grown = cv2.resize(half, (full.shape[1], full.shape[0]), interpolation=cv2.INTER_LINEAR)
    assert frame_diff(grown, full) < 12.0
