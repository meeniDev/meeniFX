"""Advanced Glow: multi-scale bloom, saturation, alpha and preset starting points.

These tests exercise the real BGRA effect, not just the controls: the halo must stay
outside the source shape, wide bloom must add light instead of moving it, and preset
values must be ordinary parameters that can be changed immediately afterward.
"""

import cv2
import numpy as np

from conftest import build_engine, frame_diff, synthetic_frame

from meenifx.effects.glow import GlowEffect


def runtime(engine, scale=1.0, **params):
    effect = engine.runtime_effects["glow"]
    values = dict(effect.defaults)
    values.update(params)
    effect.param_scale = scale
    effect.local_frame = 0
    effect.load_params(values)
    return effect


def glow_layer(size=160, colour=(0, 80, 255)):
    layer = np.zeros((size, size, 4), np.uint8)
    core = (slice(size // 2 - 12, size // 2 + 12),
            slice(size // 2 - 12, size // 2 + 12))
    layer[core[0], core[1], :3] = colour      # BGR
    layer[core[0], core[1], 3] = 255
    return layer, core


def test_bloom_adds_wider_light_without_moving_the_core():
    engine, _clip = build_engine()
    layer, core = glow_layer()
    single = runtime(engine, radius=14.0, threshold=0.0, softness=100.0,
                     bloom=0.0).apply_layer(layer.copy())
    bloomed = runtime(engine, radius=14.0, threshold=0.0, softness=100.0,
                      bloom=100.0).apply_layer(layer.copy())

    assert frame_diff(bloomed, single) > 0.5
    # The core keeps its coverage; the extra light lives in the surrounding halo.
    assert np.array_equal(bloomed[core][:, :, 3], single[core][:, :, 3])
    far = (slice(45, 70), slice(45, 125))
    assert bloomed[far][:, :, 3].sum() > single[far][:, :, 3].sum()


def test_saturation_changes_the_halo_colour_but_not_its_coverage():
    engine, _clip = build_engine()
    layer, _core = glow_layer()
    neutral = runtime(engine, radius=14.0, threshold=0.0, softness=100.0,
                      saturation=0.0).apply_layer(layer.copy())
    vivid = runtime(engine, radius=14.0, threshold=0.0, softness=100.0,
                    saturation=200.0).apply_layer(layer.copy())

    assert np.array_equal(neutral[:, :, 3], vivid[:, :, 3])
    halo = (slice(75, 85), slice(40, 60))
    neutral_pixels = neutral[halo][:, :, :3].astype(np.int16)
    vivid_pixels = vivid[halo][:, :, :3].astype(np.int16)
    assert np.ptp(neutral_pixels, axis=2).max() <= 1, \
        "0% saturation should produce a neutral halo"
    assert np.ptp(vivid_pixels, axis=2).max() > 2, \
        "saturation should preserve more colour"


def test_glow_alpha_is_screened_instead_of_added():
    engine, _clip = build_engine()
    layer, _core = glow_layer()
    out = runtime(engine, radius=14.0, threshold=0.0, softness=100.0,
                  intensity=400.0, opacity=100.0).apply_layer(layer.copy())

    assert out[:, :, 3].max() == 255
    assert out[:, :, 3].dtype == np.uint8
    # A fully covered pixel stays fully covered; the halo cannot push it past 255.
    assert out[80, 80, 3] == 255
    assert out[20, 20, 3] == 0, "the far transparent corner must stay clear"


def test_presets_are_complete_and_remain_editable():
    engine, _clip = build_engine()
    layer, _core = glow_layer(colour=(255, 255, 255))
    names = {spec.name for spec in GlowEffect.PARAMS}
    assert set(GlowEffect.PRESETS) == {
        "soft_glow", "neon_glow", "dreamy_bloom", "cinematic_bloom", "outer_glow"
    }
    for key, (_label, values) in GlowEffect.PRESETS.items():
        assert set(values) <= names, f"{key} writes an unknown parameter"
        applied = runtime(engine, **values).apply_layer(layer.copy())
        assert frame_diff(applied, layer) > 1.0, f"{key} changed nothing"

        # The values are plain parameters, not a locked render-time look.
        looser = runtime(engine, **{**values,
                                  "radius": values["radius"] + 20.0}).apply_layer(layer.copy())
        assert frame_diff(looser, applied) > 0.2, f"{key} ignored an edited radius"


def test_preview_and_full_resolution_produce_the_same_glow():
    engine, clip = build_engine(frame=synthetic_frame(640, 360), duration=30)
    clip.effects = [{"effect_id": "glow", "enabled": True, "params": {
        "radius": 22.0, "threshold": 30.0, "bloom": 70.0, "intensity": 140.0,
        "saturation": 130.0,
    }}]
    full = engine.render_frame(0, 1.0)[1]
    half = engine.render_frame(0, 0.5)[1]
    grown = cv2.resize(half, (full.shape[1], full.shape[0]), interpolation=cv2.INTER_LINEAR)
    assert frame_diff(grown, full) < 8.0


def test_zero_mix_is_a_true_no_op():
    engine, clip = build_engine(frame=synthetic_frame(320, 180), duration=30)
    before = engine.render_frame(0, 1.0)[1]
    clip.effects = [{"effect_id": "glow", "enabled": True,
                     "params": {"radius": 24.0, "mix": 0.0}}]
    after = engine.render_frame(0, 1.0)[1]
    assert np.array_equal(before, after)
