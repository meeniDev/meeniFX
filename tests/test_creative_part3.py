"""Creative Tools Update Part 3: professional glow, colour, distortion, generators,
matte utilities, browser metadata and the graph editor."""

from __future__ import annotations

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meenifx.engine import EditorEngine
from meenifx.effects import categories as META
from meenifx.effects.color_extra import (ColorWheelsEffect, CurvesEffect, HueSaturationEffect,
                                         LevelsEffect, TintEffect, TritoneEffect,
                                         curve_lut, curve_text, parse_curve)
from meenifx.effects.distort_extra import (DisplacementMapEffect, LensDistortionEffect,
                                           TurbulentDisplaceEffect)
from meenifx.effects.generate import (CheckerboardEffect, FractalNoiseEffect,
                                      GradientRampEffect, GridEffect)
from meenifx.effects.glow import GlowEffect
from meenifx.effects.glow_extra import (DirectionalGlowEffect, LightWrapEffect,
                                        MultiGlowEffect, NeonGlowEffect, SoftBloomEffect)
from meenifx.effects.matte_extra import MatteChokerEffect

NEW_KEYS = ("multi_glow", "neon_glow", "soft_bloom", "light_wrap", "directional_glow",
            "curves", "hue_saturation", "tint", "tritone", "levels", "color_wheels",
            "turbulent_displace", "displacement_map", "lens_distortion",
            "gradient_ramp", "fractal_noise", "checkerboard", "grid", "matte_choker")


def layer(width=160, height=100):
    data = np.zeros((height, width, 4), np.uint8)
    data[:, :, :3] = (35, 75, 150)
    data[:, :, 3] = 255
    data[24:76, 48:112, :3] = (245, 240, 220)
    return data


def effect(engine, key, **params):
    runtime = engine.runtime_effects[key]
    runtime.param_scale = 1.0
    runtime.local_frame = int(params.pop("_frame", 0))
    runtime.load_params({**runtime.defaults, **params})
    return runtime


def test_every_part3_effect_is_registered_in_the_expected_category():
    engine = EditorEngine(30)
    keys = {effect.key for effect in engine.effects}
    assert set(NEW_KEYS) <= keys
    assert META.category_for("multi_glow") == "Blur & Glow"
    assert META.category_for("curves") == "Color & Light"
    assert META.category_for("turbulent_displace") == "Distortion"
    assert META.category_for("gradient_ramp") == "Generate"
    assert META.category_for("matte_choker") == "Keying & Masking"
    # Time-based cards must not collapse to one generic clock glyph.
    icons = {META.icon_for(key) for key in ("wave_warp", "posterize_time", "stutter", "echo")}
    assert len(icons) >= 3


def test_glow_family_produces_distinct_pictures_and_soft_alpha():
    engine = EditorEngine(30)
    source = layer()
    source[:, :, 3] = 0
    source[30:70, 60:100, 3] = 255
    outputs = {}
    for cls in (GlowEffect, MultiGlowEffect, NeonGlowEffect, SoftBloomEffect,
                LightWrapEffect, DirectionalGlowEffect):
        runtime = effect(engine, cls.key)
        outputs[cls.key] = runtime.apply_layer(source.copy())
    assert len({out.tobytes() for out in outputs.values()}) == len(outputs)
    for out in outputs.values():
        assert out.shape == source.shape
        assert (out[:, :, 3] >= source[:, :, 3]).all(), "glow must not erase coverage"


def test_curves_graph_parses_and_moves_tones_monotonically():
    points = parse_curve("0,0;40,20;180,190;255,255")
    assert [p[0] for p in points] == sorted(p[0] for p in points)
    text = curve_text(points)
    lut = curve_lut(text)
    assert np.array_equal(lut, np.sort(lut)), "a monotone curve must not fold"
    engine = EditorEngine(30)
    source = layer()
    curved = effect(engine, "curves", curve_master="0,0;64,96;192,210;255,255").apply_layer(source.copy())
    assert not np.array_equal(curved[:, :, :3], source[:, :, :3])
    assert np.array_equal(curved[:, :, 3], source[:, :, 3])
    reset = effect(engine, "curves", curve_master="0,0;255,255").apply_layer(source.copy())
    assert np.array_equal(reset[:, :, :3], source[:, :, :3])
    # Strength is the numeric, keyframeable control for an otherwise text/LUT effect.
    zero = effect(engine, "curves", strength=0.0,
                  curve_master="0,0;64,96;192,210;255,255").apply_layer(source.copy())
    full = effect(engine, "curves", strength=100.0,
                  curve_master="0,0;64,96;192,210;255,255").apply_layer(source.copy())
    assert np.array_equal(zero[:, :, :3], source[:, :, :3])
    assert not np.array_equal(full[:, :, :3], zero[:, :, :3])


def test_colour_tools_grade_without_touching_alpha():
    engine = EditorEngine(30)
    source = layer()
    cases = {
        "hue_saturation": {"hue": 55.0, "saturation": 45.0},
        "tint": {"strength": 80.0},
        "tritone": {"strength": 80.0},
        "levels": {"input_black": 20.0, "input_white": 230.0, "gamma": 1.4},
        "color_wheels": {"lift_x": 40.0, "gamma_y": -25.0, "gain_x": 30.0},
    }
    for key, params in cases.items():
        out = effect(engine, key, **params).apply_layer(source.copy())
        assert out.shape == source.shape and out.dtype == np.uint8
        assert not np.array_equal(out[:, :, :3], source[:, :, :3]), key
        assert np.array_equal(out[:, :, 3], source[:, :, 3]), key
    # The two wheels' axes are real controls, not labels.
    left = effect(engine, "color_wheels", lift_x=-70.0).apply_layer(source.copy())
    right = effect(engine, "color_wheels", lift_x=70.0).apply_layer(source.copy())
    assert not np.array_equal(left, right)


def test_distortions_animate_and_displacement_can_use_another_clip():
    engine = EditorEngine(30)
    source = layer()
    first = effect(engine, "turbulent_displace", _frame=0).apply_layer(source.copy())
    later = effect(engine, "turbulent_displace", evolution=90.0,
                   _frame=0).apply_layer(source.copy())
    assert not np.array_equal(first, later)
    lens = effect(engine, "lens_distortion", k1=0.4).apply_layer(source.copy())
    assert not np.array_equal(lens[:, :, :3], source[:, :, :3])
    # A second clip supplies the map; the effect uses its actual rendered layer.
    engine.source_width, engine.source_height = 160, 100
    engine.session.project_size = (160, 100)
    map_id = engine.session.add_shape_clip("rectangle", box=(20, 20, 70, 45))
    target_id = engine.session.add_shape_clip("rectangle", box=(60, 25, 60, 45))
    target = engine.timeline.find_clip(target_id)
    target.effects = [{"effect_id": "displacement_map", "enabled": True,
                       "params": {"map_source": map_id, "amount": 60.0}}]
    rendered, _painted = engine.render_frame(0, 1.0)
    assert rendered is not None and rendered.shape[0] > 0


def test_generators_support_transparency_and_animation():
    engine = EditorEngine(30)
    source = layer()
    transparent = effect(engine, "checkerboard", transparent=True).apply_layer(source.copy())
    opaque = effect(engine, "checkerboard", transparent=False).apply_layer(source.copy())
    assert int(transparent[:, :, 3].min()) == 0 and int(opaque[:, :, 3].min()) == 255
    grid = effect(engine, "grid", transparent=True).apply_layer(source.copy())
    assert int(grid[:, :, 3].min()) == 0
    noise_a = effect(engine, "fractal_noise", evolution=0.0).apply_layer(source.copy())
    noise_b = effect(engine, "fractal_noise", evolution=60.0).apply_layer(source.copy())
    assert not np.array_equal(noise_a, noise_b)
    noise = effect(engine, "fractal_noise", transparent=True).apply_layer(source.copy())
    assert int(noise[:, :, 3].min()) < 255
    ramp = effect(engine, "gradient_ramp", ramp_type="radial").apply_layer(source.copy())
    assert ramp.shape == source.shape


def test_matte_choker_operates_on_alpha_and_expands_without_a_black_rim():
    engine = EditorEngine(30)
    source = layer()
    source[:, :, 3] = 0
    source[20:80, 40:120, 3] = 255
    contracted = effect(engine, "matte_choker", choke=8.0, feather=0.0,
                        edge_softness=1.0, iterations=1).apply_layer(source.copy())
    expanded = effect(engine, "matte_choker", choke=-8.0, feather=0.0,
                      edge_softness=1.0, iterations=1).apply_layer(source.copy())
    assert int(contracted[:, :, 3].sum()) < int(source[:, :, 3].sum())
    assert int(expanded[:, :, 3].sum()) > int(source[:, :, 3].sum())
    assert expanded[50, 34, 3] > 0
    assert tuple(expanded[50, 34, :3]) != (0, 0, 0), "grown edge must not be black"


def test_effects_survive_project_round_trip_and_adjustment_layer():
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections
    engine = EditorEngine(30)
    engine.source_width, engine.source_height = 160, 100
    engine.session.project_size = (160, 100)
    clip_id = engine.session.add_shape_clip("rectangle", box=(20, 20, 100, 60))
    clip = engine.timeline.find_clip(clip_id)
    clip.effects = [{"effect_id": key, "enabled": True, "params": {}}
                    for key in ("curves", "gradient_ramp", "multi_glow")]
    sections = timeline_to_sections(engine.timeline, lambda path: 0)
    restored = sections_to_timeline(sections, 30.0, lambda media_id: "")
    assert [entry["effect_id"] for entry in restored.find_clip(clip_id).effects] == \
        ["curves", "gradient_ramp", "multi_glow"]
    # Adjustment layers run the same stack over the composites below them.
    adjustment_id = engine.session.add_adjustment_clip(0, 30)
    adjustment = engine.timeline.find_clip(adjustment_id)
    adjustment.effects = [{"effect_id": "tritone", "enabled": True, "params": {}}]
    image, _painted = engine.render_frame(0, 1.0)
    assert image is not None and image.shape[2] == 3


def test_browser_metadata_has_descriptions_and_real_preview_entries():
    from meenifx import thumbnails as THUMBS
    engine = EditorEngine(30)
    for key in NEW_KEYS:
        runtime = next(item for item in engine.effects if item.key == key)
        details = META.details_for(runtime)
        assert details["description"] and details["keyframes"]
        entry = THUMBS.preview_entry(runtime)
        assert entry["effect_id"] == key and entry["enabled"]
        preview = THUMBS.render_effect(engine, key, 124, 84)
        assert preview is not None and preview.shape[:2] == (84, 124), key
