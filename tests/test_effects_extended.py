"""Colour correction, blur and glow: real behaviour with alpha, order and scale.

These are the checks the parameter audit cannot make: what the effects do to
transparency, how they respond to keyframes and animated clips, that disabled effects
cost nothing, that the pipeline order is the order the user sees, and that a reduced
quality preview is the same picture as the full-resolution export.
"""

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import build_engine, frame_diff, synthetic_frame  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402


# --------------------------------------------------------------------- helpers
def runtime(engine, key, local_frame=0, scale=1.0, **params):
    effect = engine.runtime_effects[key]
    merged = dict(effect.defaults)
    merged.update(params)
    effect.param_scale = scale
    effect.local_frame = local_frame
    effect.load_params(merged)
    return effect


def alpha_layer(size=64):
    """Transparent black border, opaque bright core: the shape a glow/blur must respect."""
    layer = np.zeros((size, size, 4), np.uint8)
    core = (slice(size // 4, size * 3 // 4), slice(size // 4, size * 3 // 4))
    layer[core[0], core[1], 0] = 210      # blue
    layer[core[0], core[1], 1] = 200
    layer[core[0], core[1], 2] = 160      # warm, bright enough to pass a glow threshold
    layer[core[0], core[1], 3] = 255
    return layer


def grey_layer(value, size=32):
    layer = np.zeros((size, size, 4), np.uint8)
    layer[:, :, :3] = value
    layer[:, :, 3] = 255
    return layer


def blurred(layer, engine, key, **params):
    return runtime(engine, key, **params).apply_layer(layer.copy())


# --------------------------------------------------------------------- colour
def test_colour_identity_is_a_no_op():
    engine, _clip = build_engine()
    layer = alpha_layer()
    out = runtime(engine, "color_correct").apply_layer(layer.copy())
    assert np.array_equal(out, layer)


def test_every_colour_preset_changes_the_picture():
    engine, _clip = build_engine()
    base = alpha_layer()
    plain = runtime(engine, "color_correct").apply_layer(base.copy())
    for key in ("black_white", "warm", "cool", "high_contrast", "vintage",
                "desaturated", "cinematic"):
        out = runtime(engine, "color_correct", preset=key).apply_layer(base.copy())
        assert frame_diff(out, plain) > 1.0, f"preset {key} changed nothing"
        assert out[:, :, 3].max() == 255 and np.array_equal(out[:, :, 3], base[:, :, 3])


def test_colour_keeps_transparency():
    engine, _clip = build_engine()
    layer = alpha_layer()
    out = runtime(engine, "color_correct", brightness=40, saturation=50).apply_layer(layer.copy())
    assert np.array_equal(out[:, :, 3], layer[:, :, 3])
    assert out[0, 0, 3] == 0, "the transparent border stays transparent"
    assert frame_diff(out[24:40, 24:40], layer[24:40, 24:40]) > 5.0



def test_exposure_doubles_the_light():
    engine, _clip = build_engine()
    out = runtime(engine, "color_correct", exposure=1.0).apply_layer(grey_layer(100))
    assert abs(int(out[16, 16, 0]) - 200) <= 2


def test_hue_rotation_spins_the_colour_wheel_and_keeps_brightness():
    engine, _clip = build_engine()
    base = alpha_layer()
    out = runtime(engine, "color_correct", hue=120.0).apply_layer(base.copy())
    assert frame_diff(out, base) > 5.0
    # HSV rotation moves the hue angle and keeps value, so the peak channel survives.
    assert abs(int(out[32, 32, :3].max()) - int(base[32, 32, :3].max())) <= 3


def test_shadows_and_highlights_are_tone_curves():
    engine, _clip = build_engine()
    ramp = np.zeros((16, 256, 4), np.uint8)
    ramp[:, :, :3] = np.arange(256, dtype=np.uint8)[None, :, None]
    ramp[:, :, 3] = 255
    lifted = runtime(engine, "color_correct", shadows=60.0).apply_layer(ramp.copy())
    dark_gain = int(lifted[8, 10, 0]) - 10
    bright_gain = int(lifted[8, 245, 0]) - 245
    assert dark_gain > 10, "shadows must lift the dark end"
    assert abs(bright_gain) <= 3, "shadows must leave the highlights alone"
    pulled = runtime(engine, "color_correct", highlights=-60.0).apply_layer(ramp.copy())
    assert int(pulled[8, 245, 0]) < 235
    assert abs(int(pulled[8, 10, 0]) - 10) <= 3


def test_colour_parameters_can_be_keyframed():
    engine, clip = build_engine()
    clip.effects = [{"effect_id": "color_correct", "enabled": True, "params": {}}]
    CH.animate(clip, CH.effect_channel("color_correct", "brightness"), 0)
    CH.set_value(clip, CH.effect_channel("color_correct", "brightness"), -60.0, 0)
    CH.set_value(clip, CH.effect_channel("color_correct", "brightness"), 60.0, 20, auto_key=True)
    first = engine.render_frame(0, 1.0)[1]
    later = engine.render_frame(20, 1.0)[1]
    assert frame_diff(first, later) > 10.0


# ----------------------------------------------------------------------- blur
def test_gaussian_blur_softens_the_inside_and_fades_the_edge():
    engine, _clip = build_engine()
    layer = alpha_layer()
    out = runtime(engine, "gaussian_blur", radius=6.0).apply_layer(layer.copy())
    assert out.shape == layer.shape and out.dtype == np.uint8
    # The core stays opaque, the border fades towards transparent, the corner stays clear.
    assert out[32, 32, 3] > 200
    assert 0 < out[16, 32, 3] < 255
    assert out[0, 0, 3] == 0


def test_gaussian_blur_keep_edges_leaves_the_alpha_crisp():
    engine, _clip = build_engine()
    layer = alpha_layer()
    out = runtime(engine, "gaussian_blur", radius=6.0, keep_edges=True).apply_layer(layer.copy())
    assert np.array_equal(out[:, :, 3], layer[:, :, 3])
    assert frame_diff(out, layer) > 1.0, "the colour must still be blurred"


def test_every_blur_changes_the_picture():
    engine, _clip = build_engine()
    layer = np.zeros((80, 80, 4), np.uint8)
    layer[:, :, :3] = 30
    layer[:, :, 3] = 255
    layer[20:40, 20:40, :3] = 240          # one bright square to smear
    base = layer.copy()
    cases = {
        "box_blur": {"radius": 5.0},
        "directional_blur": {"length": 12.0, "angle": 0.0},
        "radial_blur": {"amount": 12.0},
        "zoom_blur": {"amount": 25.0},
        "motion_blur": {"follow_motion": False, "length": 14.0, "angle": 0.0},
    }
    for key, params in cases.items():
        out = blurred(base, engine, key, **params)
        assert frame_diff(out, base) > 1.0, f"{key} changed nothing"
        assert out.shape == base.shape
        assert np.array_equal(out[:, :, 3], base[:, :, 3])


def test_directional_blur_angle_picks_the_axis():
    engine, _clip = build_engine()
    layer = np.zeros((64, 64, 4), np.uint8)
    layer[:, :, :3] = 20
    layer[:, :, 3] = 255
    layer[30:34, 30:34, :3] = 255
    horizontal = blurred(layer, engine, "directional_blur", length=16.0, angle=0.0)
    vertical = blurred(layer, engine, "directional_blur", length=16.0, angle=90.0)
    row = 32
    col = 32
    assert horizontal[row, 22, 0] > vertical[row, 22, 0], "0 degrees must smear sideways"
    assert vertical[22, col, 0] > horizontal[22, col, 0], "90 degrees must smear up/down"


def test_radial_blur_streaks_around_the_centre():
    engine, _clip = build_engine()
    layer = np.zeros((64, 64, 4), np.uint8)
    layer[:, :, :3] = 10
    layer[:, :, 3] = 255
    layer[8:16, 30:34, :3] = 255            # an off-centre highlight
    out = blurred(layer, engine, "radial_blur", amount=60.0, samples=24)
    assert out[12, 25, 0] > 20, "the smear must appear beside the highlight"
    assert out[12, 32, 0] < 200, "and the highlight itself must soften"


def test_motion_blur_follows_the_animated_clip():
    engine, clip = build_engine()
    clip.effects = [{"effect_id": "motion_blur", "enabled": True, "params": {}}]
    CH.set_value(clip, "transform.position_x", 0.0, 0, auto_key=False)
    CH.animate(clip, "transform.position_x", 0)
    CH.set_value(clip, "transform.position_x", 0.0, 0)
    CH.set_value(clip, "transform.position_x", 160.0, 12, auto_key=True)
    following = engine.render_frame(6, 1.0)[1]
    manual = dict(clip.effects[0]["params"])
    manual["follow_motion"] = False
    clip.effects[0]["params"] = manual
    still = engine.render_frame(6, 1.0)[1]
    assert frame_diff(following, still) > 1.0, "measured motion must smear the frame"


def test_blur_radius_is_scaled_for_reduced_preview_quality():
    engine, clip = build_engine()
    clip.effects = [{"effect_id": "gaussian_blur", "enabled": True,
                     "params": {"radius": 8.0}}]
    full = engine.render_frame(0, 1.0)[1]
    half = engine.render_frame(0, 0.5)[1]
    grown = cv2.resize(half, (full.shape[1], full.shape[0]), interpolation=cv2.INTER_LINEAR)
    assert frame_diff(grown, full) < 6.0, "a 50% preview must look like the export"


# ----------------------------------------------------------------------- glow
def test_glow_raises_alpha_outside_the_shape():
    engine, _clip = build_engine()
    layer = alpha_layer()
    out = runtime(engine, "glow", radius=10.0).apply_layer(layer.copy())
    assert layer[10, 32, 3] == 0, "the sample point starts fully transparent"
    assert out[10, 32, 3] > 0, "the halo must be visible outside the shape"
    assert out[:, :, 3].max() == 255 and out[:, :, 3].min() >= layer[:, :, 3].min()


def test_glow_threshold_gates_a_dark_picture():
    engine, _clip = build_engine()
    dark = alpha_layer()
    dark[:, :, :3] = 12
    out = runtime(engine, "glow", radius=10.0, threshold=60.0).apply_layer(dark.copy())
    assert np.array_equal(out, dark), "nothing is bright enough to glow"


def test_glow_intensity_is_keyframeable_and_blends_differently():
    engine, _clip = build_engine()
    layer = alpha_layer()
    soft = runtime(engine, "glow", radius=8.0, intensity=40.0).apply_layer(layer.copy())
    strong = runtime(engine, "glow", radius=8.0, intensity=250.0).apply_layer(layer.copy())
    assert frame_diff(strong, soft) > 2.0
    screen = runtime(engine, "glow", radius=8.0, blend="screen").apply_layer(layer.copy())
    overlay = runtime(engine, "glow", radius=8.0, blend="overlay").apply_layer(layer.copy())
    assert frame_diff(screen, overlay) > 2.0


def test_glow_colour_tints_the_halo():
    engine, _clip = build_engine()
    layer = alpha_layer()
    warm = runtime(engine, "glow", radius=8.0, glow_r=255, glow_g=40, glow_b=0).apply_layer(layer.copy())
    cool = runtime(engine, "glow", radius=8.0, glow_r=0, glow_g=40, glow_b=255).apply_layer(layer.copy())
    assert warm[10, 32, 2] > cool[10, 32, 2], "an orange glow carries red"
    assert cool[10, 32, 0] > warm[10, 32, 0], "a cool glow carries blue (BGR channel 0)"


# ----------------------------------------------------------- pipeline behaviour
def test_pipeline_order_is_respected():
    engine, clip = build_engine()
    # Blurring first removes the fine detail the glow builds on; glowing first and then
    # blurring smears the whole halo. The two orders are visibly different pictures.
    blur = {"effect_id": "gaussian_blur", "enabled": True, "params": {"radius": 6.0}}
    glow = {"effect_id": "glow", "enabled": True, "params": {"radius": 10.0, "threshold": 50.0}}
    clip.effects = [blur, glow]
    blur_then_glow = engine.render_frame(0, 1.0)[1]
    clip.effects = [dict(glow), dict(blur)]
    glow_then_blur = engine.render_frame(0, 1.0)[1]
    assert frame_diff(blur_then_glow, glow_then_blur) > 1.0


def test_disabled_effects_are_not_run(monkeypatch):
    engine, clip = build_engine()
    clip.effects = [{"effect_id": "gaussian_blur", "enabled": False,
                     "params": {"radius": 9.0}}]
    effect = engine.runtime_effects["gaussian_blur"]
    calls = []
    original = effect.apply_layer
    monkeypatch.setattr(effect, "apply_layer", lambda layer: calls.append(1) or original(layer))
    engine.render_frame(0, 1.0)
    assert calls == []
    clip.effects[0]["enabled"] = True
    engine.render_frame(0, 1.0)
    assert calls == [1]


# ----------------------------------------------------------------- puppet warp
def puppet_clip(engine, **params):
    clip = engine.timeline.tracks_of_type("video")[0].clips[0]
    clip.effects = [{"effect_id": "puppet_warp", "enabled": True,
                     "params": {**engine.runtime_effects["puppet_warp"].defaults, **params},
                     "pins": [{"id": 1, "src": [0.35, 0.5], "x": 0.35, "y": 0.5, "locked": False},
                              {"id": 2, "src": [0.7, 0.45], "x": 0.7, "y": 0.45, "locked": False}]}]
    return clip


def test_puppet_warp_deforms_only_when_a_pin_moves():
    engine, _clip = build_engine()
    clip = puppet_clip(engine)
    rest = engine.render_frame(0, 1.0)[1]
    clip.effects[0]["pins"][0]["x"] = 0.5
    moved = engine.render_frame(0, 1.0)[1]
    assert frame_diff(rest, moved) > 1.0


def test_puppet_mesh_density_and_stiffness_change_the_deformation():
    engine, _clip = build_engine()
    clip = puppet_clip(engine)
    clip.effects[0]["pins"][0]["x"] = 0.55
    clip.effects[0]["params"]["mesh_density"] = 6
    coarse = engine.render_frame(0, 1.0)[1]
    clip.effects[0]["params"]["mesh_density"] = 64
    dense = engine.render_frame(0, 1.0)[1]
    assert frame_diff(coarse, dense) > 0.5
    clip.effects[0]["params"].update(mesh_density=24, stiffness=0.3)
    soft = engine.render_frame(0, 1.0)[1]
    clip.effects[0]["params"]["stiffness"] = 3.5
    stiff = engine.render_frame(0, 1.0)[1]
    assert frame_diff(soft, stiff) > 0.5


def test_puppet_show_mesh_is_never_exported():
    engine, _clip = build_engine()
    clip = puppet_clip(engine, show_mesh=True)
    clip.effects[0]["pins"][0]["x"] = 0.5
    shown = engine.render_frame(0, 1.0)[1]
    clip.effects[0]["params"]["show_mesh"] = False
    hidden = engine.render_frame(0, 1.0)[1]
    assert frame_diff(shown, hidden) == 0.0


def test_puppet_pin_keyframes_animate_the_deformation():
    engine, _clip = build_engine()
    clip = puppet_clip(engine)
    channel = CH.pin_channel("puppet_warp", 1, "x")
    CH.animate(clip, channel, 0)
    CH.set_value(clip, channel, 0.35, 0)
    CH.set_value(clip, channel, 0.62, 12, auto_key=True)
    start = engine.render_frame(0, 1.0)[1]
    middle = engine.render_frame(6, 1.0)[1]
    end = engine.render_frame(12, 1.0)[1]
    assert frame_diff(start, end) > 2.0
    assert frame_diff(start, middle) > 0.5
    rows = {row["id"]: row for row in CH.list_channels(clip, engine.runtime_effects)}
    assert rows[channel]["animated"] and rows[channel]["group"].endswith("pins")
