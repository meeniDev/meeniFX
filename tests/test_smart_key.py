"""Smart Key: border sampling, soft matte, spill and diagnostic views."""

import numpy as np

from conftest import build_engine


def plate(background=(64, 177, 0), foreground=(120, 150, 210), size=(320, 180)):
    width, height = size
    layer = np.zeros((height, width, 4), np.uint8)
    layer[:, :, :3] = background
    layer[:, :, 3] = 255
    layer[36:145, 72:248, :3] = foreground
    # A green-spill fringe along one subject edge, like Chroma Key's fixture.
    layer[80:102, 248:264, :3] = (35, 178, 118)
    layer[80:102, 252:264, 3] = 210
    return layer


def runtime(engine, scale=1.0, **params):
    effect = engine.runtime_effects["smart_key"]
    values = dict(effect.defaults)
    values.update(params)
    effect.param_scale = scale
    effect.local_frame = 0
    effect.load_params(values)
    return effect


def apply(engine, layer, **params):
    return runtime(engine, **params).apply_layer(layer.copy())


def test_auto_sampling_removes_the_edge_colour_without_a_picker():
    engine, _clip = build_engine()
    layer = plate()
    out = apply(engine, layer)
    assert out[10, 10, 3] == 0, "the sampled screen is removed"
    assert out[90, 150, 3] > 250, "the foreground survives"
    assert out[90, 150, 0] > 100, "foreground colour is retained"


def test_border_sample_follows_a_different_screen_colour():
    engine, _clip = build_engine()
    blue = plate(background=(187, 71, 0), foreground=(80, 120, 220))
    out = apply(engine, blue)
    assert out[10, 10, 3] == 0
    assert out[90, 150, 3] > 250


def test_tolerance_changes_matte_coverage():
    engine, _clip = build_engine()
    layer = plate()
    tight = apply(engine, layer, tolerance=8.0, similarity=10.0)
    loose = apply(engine, layer, tolerance=72.0, similarity=30.0)
    assert loose[:, :, 3].sum() < tight[:, :, 3].sum()


def test_matte_views_and_transparency():
    engine, _clip = build_engine()
    layer = plate()
    alpha = apply(engine, layer, matte_view="alpha")
    mono = apply(engine, layer, matte_view="black_white")
    clear = apply(engine, layer, transparency=0.0, spill=0.0)
    assert alpha[90, 150, 3] == 255
    assert int(alpha[90, 150, 0]) > 200
    assert set(np.unique(mono[:, :, 0])).issubset({0, 255})
    assert np.array_equal(clear, layer)


def test_auto_sample_can_be_switched_off():
    engine, _clip = build_engine()
    # Use a non-green screen: the manual fallback is green, so a blue plate makes the
    # difference between auto sampling and the fallback visible.
    layer = plate(background=(187, 71, 0), foreground=(80, 120, 220))
    automatic = apply(engine, layer, auto_key=True)
    manual = apply(engine, layer, auto_key=False)
    assert not np.array_equal(automatic, manual)


def test_spill_suppression_changes_only_the_fringe_colour():
    engine, _clip = build_engine()
    layer = plate()
    plain = apply(engine, layer, spill=0.0)
    cleaned = apply(engine, layer, spill=100.0)
    assert np.array_equal(plain[:, :, 3], cleaned[:, :, 3])
    edge = (slice(80, 102), slice(248, 264))
    assert cleaned[edge][:, :, 1].mean() < plain[edge][:, :, 1].mean() - 3.0
