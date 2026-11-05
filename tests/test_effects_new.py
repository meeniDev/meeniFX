"""The new effect packs: determinism, temporal behaviour, quality scaling and LUTs.

The parameter audit (tests/test_effect_parameters.py) proves every control reaches the
picture at its extremes. This file proves the things it cannot: that the same seed is
the same picture on every render, that frame-rate parameters hold and release frames,
that pixel parameters keep their framing at a lower preview quality, that every effect
declares its media type, and that .cube files really drive the LUT.
"""

import sys

import cv2
import numpy as np
import pytest

from conftest import build_engine, frame_diff, synthetic_frame

from meenifx.effects.lut import LutEffect, builtin_lut, read_cube
from meenifx.engine import EditorEngine
from meenifx.media.cache import MediaEntry

NEW_KEYS = (
    "chromatic_aberration", "glitch", "wave_warp", "bulge_pinch", "twirl", "kaleidoscope",
    "vhs_crt", "film_grain", "halftone", "posterize", "edge_outline", "duotone", "invert",
    "vignette", "flash", "exposure_flicker", "light_leak", "bloom_streaks",
    "posterize_time", "stutter", "echo", "lut",
)

SEEDED = ("glitch", "film_grain", "vhs_crt", "light_leak")
PIXEL_PARAMS = (
    ("chromatic_aberration", {"amount": 18}),
    ("wave_warp", {"amplitude": 40, "wavelength": 120}),
    ("bulge_pinch", {"amount": 80, "radius": 260}),
    ("twirl", {"angle": 300, "radius": 300}),
    ("halftone", {"dot": 14}),
    ("film_grain", {"amount": 0}),
    ("bloom_streaks", {"length": 200}),
    ("edge_outline", {"thickness": 6}),
)


def runtime(engine, key, local_frame=0, scale=1.0, **params):
    effect = engine.runtime_effects[key]
    merged = dict(effect.defaults)
    merged.update(params)
    effect.param_scale = scale
    effect.local_frame = local_frame
    effect.load_params(merged)
    return effect


def moving_engine(frames=40, size=(128, 72)):
    """Synthetic frames that change every frame, so trails and holds are visible."""
    engine = EditorEngine(30)
    engine.source_width, engine.source_height = size
    pictures = []
    for index in range(frames):
        frame = np.zeros((size[1], size[0], 3), np.uint8)
        x = 6 + index * 2
        cv2.rectangle(frame, (x, 18), (x + 22, 48), (250, 250, 250), -1)
        frame[:, :, 0] = np.clip(np.arange(size[0])[None, :] * 2, 0, 255)
        pictures.append(frame)
    entry = MediaEntry("video", frames, 30)
    entry.frames = pictures
    path = "moving.png"
    engine.media._entries[path] = entry
    track = engine.timeline.add_track("video")
    clip = engine.timeline.new_clip("moving", path, track, 0, frames,
                                    source_out=frames, source_fps=30)
    return engine, clip


def layered(engine, clip, frame):
    """Render one frame with one effect entry applied to `clip`."""
    return engine.render_frame(frame, 1.0)[1]


def test_every_new_effect_is_registered_for_video_only():
    engine, _clip = build_engine()
    for key in NEW_KEYS:
        effect = engine.runtime_effects[key]
        assert effect.MEDIA_TYPES == ("video",), f"{key} declares {effect.MEDIA_TYPES}"
        assert effect.category, f"{key} has no category"
        visible = [spec for spec in effect.PARAMS if not spec.advanced]
        assert 2 <= len(visible) <= 3, f"{key} shows {len(visible)} main controls"


def test_seeded_effects_are_deterministic():
    engine, clip = moving_engine()
    for key in SEEDED:
        clip.effects = [{"effect_id": key, "enabled": True, "params": {}}]
        first = layered(engine, clip, 5)
        second = engine.render_frame(5, 1.0)[1]
        assert frame_diff(first, second) == 0.0, f"{key} is not deterministic"
        clip.effects = [{"effect_id": key, "enabled": True, "params": {"seed": 999}}]
        other = engine.render_frame(5, 1.0)[1]
        assert frame_diff(first, other) > 0.2, f"{key} ignores its seed"


ANIMATABLE = (
    "chromatic_aberration", "glitch", "wave_warp", "bulge_pinch", "twirl", "kaleidoscope",
    "vhs_crt", "film_grain", "halftone", "posterize", "edge_outline", "duotone", "invert",
    "vignette", "exposure_flicker", "light_leak", "bloom_streaks", "lut",
)


# A hue dial is the same colour at -180 and +180, so duotone animates its intensity.
ANIMATED_PARAM = {"duotone": "intensity"}


def _first_visible_numeric(key):
    engine = EditorEngine()
    effect = engine.runtime_effects[key]
    wanted = ANIMATED_PARAM.get(key)
    if wanted:
        return next(spec for spec in effect.PARAMS if spec.name == wanted)
    return next(spec for spec in effect.PARAMS if not spec.advanced and spec.kind in ("int", "float"))


@pytest.mark.parametrize("key", ANIMATABLE)
def test_the_main_parameter_animates_over_time(key):
    """Every new effect's first slider is a real, animatable channel."""
    from meenifx.model.keyframes import AnimatedValue

    spec = _first_visible_numeric(key)
    engine, clip = build_engine(frame=synthetic_frame(320, 180), duration=40)
    curve = AnimatedValue()
    curve.set_keyframe(0, spec.minimum)
    curve.set_keyframe(20, spec.maximum)
    params = dict(engine.runtime_effects[key].capture_params())
    params[spec.name] = curve
    clip.effects = [{"effect_id": key, "enabled": True, "params": params}]
    start = engine.render_frame(0, 1.0)[1]
    end = engine.render_frame(20, 1.0)[1]
    assert frame_diff(start, end) > 0.2, f"{key}.{spec.name} does not follow its keyframes"


def test_glitch_pattern_changes_from_frame_to_frame():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "glitch", "enabled": True,
                     "params": {"slices": 80, "block": 60, "color": 50, "speed": 1}}]
    frames = [engine.render_frame(index, 1.0)[1] for index in range(4)]
    assert frame_diff(frames[0], frames[1]) > 0.2


@pytest.mark.parametrize("key,params", PIXEL_PARAMS, ids=[key for key, _p in PIXEL_PARAMS])
def test_pixel_parameters_keep_their_framing_at_a_lower_preview_quality(key, params):
    engine, clip = build_engine(frame=synthetic_frame(640, 360), duration=30)
    clip.effects = [{"effect_id": key, "enabled": True, "params": params}]
    full = engine.render_frame(3, 1.0)[1]
    preview = engine.render_frame(3, 0.5)[1]
    assert preview.shape[:2] == (full.shape[0] // 2, full.shape[1] // 2)
    reduced = cv2.resize(full, (preview.shape[1], preview.shape[0]), interpolation=cv2.INTER_AREA)
    assert frame_diff(reduced, preview) < 12.0, f"{key} frames differently at 50% quality"


def test_posterize_time_holds_the_picture_for_its_step():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "posterize_time", "enabled": True, "params": {"step": 4}}]
    frames = [engine.render_frame(index, 1.0)[1] for index in range(9)]
    for index in range(0, 8, 4):
        assert frame_diff(frames[index], frames[index + 1]) == 0.0
        assert frame_diff(frames[index], frames[index + 3]) == 0.0
    assert frame_diff(frames[3], frames[4]) > 0.2, "the hold never releases"


def test_posterize_time_offset_shifts_the_holds():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"step": 4, "offset": 2}}]
    frames = [engine.render_frame(index, 1.0)[1] for index in range(9)]
    assert frame_diff(frames[0], frames[1]) == 0.0
    assert frame_diff(frames[2], frames[4]) == 0.0
    assert frame_diff(frames[5], frames[6]) > 0.2


def test_stutter_holds_then_jumps():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "stutter", "enabled": True,
                     "params": {"interval": 8, "hold": 5}}]
    frames = [engine.render_frame(index, 1.0)[1] for index in range(16)]
    # Frames 0..4 are held, 5..7 play, then the next cycle holds at frame 8.
    for index in range(4):
        assert frame_diff(frames[index], frames[index + 1]) == 0.0
    assert frame_diff(frames[4], frames[5]) > 0.2
    assert frame_diff(frames[10], frames[11]) == 0.0


def test_echo_trails_the_clips_own_earlier_frames():
    engine, clip = moving_engine()
    plain = engine.render_frame(20, 1.0)[1]
    clip.effects = [{"effect_id": "echo", "enabled": True,
                     "params": {"count": 3, "step": 4, "decay": 80}}]
    trailed = engine.render_frame(20, 1.0)[1]
    assert frame_diff(plain, trailed) > 1.0, "the echo adds no trail"
    # A different count is a different trail, and the first frames have no history.
    clip.effects = [{"effect_id": "echo", "enabled": True,
                     "params": {"count": 6, "step": 2, "decay": 90}}]
    heavier = engine.render_frame(20, 1.0)[1]
    assert frame_diff(trailed, heavier) > 0.5
    clip.effects = [{"effect_id": "echo", "enabled": True, "params": {"count": 4}}]
    assert frame_diff(engine.render_frame(0, 1.0)[1], engine.render_frame(0, 1.0)[1]) == 0.0


def test_flash_fires_once_per_interval_and_fades():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "flash", "enabled": True,
                     "params": {"amount": 100, "duration": 5, "interval": 20, "envelope": "fade"}}]
    peak = engine.render_frame(0, 1.0)[1]
    fading = engine.render_frame(3, 1.0)[1]
    dark = engine.render_frame(10, 1.0)[1]
    assert peak.mean() > fading.mean() > dark.mean()
    assert frame_diff(peak, dark) > 20.0


def test_flash_duration_and_interval_change_the_strobe():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "flash", "enabled": True,
                     "params": {"amount": 100, "duration": 3, "interval": 20}}]
    short = engine.render_frame(6, 1.0)[1]
    clip.effects = [{"effect_id": "flash", "enabled": True,
                     "params": {"amount": 100, "duration": 10, "interval": 20}}]
    long = engine.render_frame(6, 1.0)[1]
    assert frame_diff(short, long) > 5.0
    clip.effects = [{"effect_id": "flash", "enabled": True,
                     "params": {"amount": 100, "duration": 3, "interval": 9}}]
    # Frame 10 is inside the 9-frame strobe's flash and outside the 20-frame one's.
    faster = engine.render_frame(10, 1.0)[1]
    slower = engine.render_frame(10, 1.0)[1]
    assert frame_diff(faster, slower) == 0.0
    clip.effects = [{"effect_id": "flash", "enabled": True,
                     "params": {"amount": 100, "duration": 3, "interval": 20}}]
    assert frame_diff(faster, engine.render_frame(10, 1.0)[1]) > 20.0


def test_exposure_flicker_seed_and_speed_change_over_time():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "exposure_flicker", "enabled": True,
                     "params": {"amount": 90, "speed": 8, "seed": 1}}]
    early = engine.render_frame(4, 1.0)[1]
    late = engine.render_frame(22, 1.0)[1]
    assert frame_diff(early, late) > 1.0
    clip.effects = [{"effect_id": "exposure_flicker", "enabled": True,
                     "params": {"amount": 90, "speed": 8, "seed": 2}}]
    other_seed = engine.render_frame(4, 1.0)[1]
    assert frame_diff(early, other_seed) > 1.0


def test_wave_speed_travels_over_time():
    engine, clip = moving_engine()
    clip.effects = [{"effect_id": "wave_warp", "enabled": True,
                     "params": {"amplitude": 20, "wavelength": 60, "speed": 6}}]
    early = engine.render_frame(2, 1.0)[1]
    later = engine.render_frame(12, 1.0)[1]
    assert frame_diff(early, later) > 0.5


def test_duotone_tint_direction():
    engine, clip = build_engine(frame=synthetic_frame(320, 180))
    clip.effects = [{"effect_id": "duotone", "enabled": True,
                     "params": {"shadow_hue": -120, "highlight_hue": 30}}]
    warm_high = engine.render_frame(0, 1.0)[1]
    clip.effects = [{"effect_id": "duotone", "enabled": True,
                     "params": {"shadow_hue": 120, "highlight_hue": -30}}]
    cold_high = engine.render_frame(0, 1.0)[1]
    assert frame_diff(warm_high, cold_high) > 5.0


def test_lut_file_drives_the_grade(tmp_path):
    size = 2
    rows = []
    for b in (0.0, 1.0):
        for g in (0.0, 1.0):
            for r in (0.0, 1.0):     # red varies fastest, as the format requires
                rows.append(f"{1.0 - r:.4f} {1.0 - g:.4f} {1.0 - b:.4f}")
    path = tmp_path / "invert.cube"
    path.write_text("TITLE \"invert\"\nLUT_3D_SIZE 2\n" + "\n".join(rows) + "\n",
                    encoding="utf-8")
    parsed_size, table = read_cube(str(path))
    assert parsed_size == size and table.shape == (2, 2, 2, 3)

    engine, clip = build_engine(frame=np.full((18, 32, 3), 64, np.uint8))
    clip.effects = [{"effect_id": "lut", "enabled": True,
                     "params": {"lut_file": str(path), "intensity": 100}}]
    inverted = engine.render_frame(0, 1.0)[1][0, 0].tolist()
    clip.effects = [{"effect_id": "lut", "enabled": True,
                     "params": {"lut_file": str(path), "intensity": 0}}]
    untouched = engine.render_frame(0, 1.0)[1][0, 0].tolist()
    assert untouched == [64, 64, 64]
    assert inverted == [pytest.approx(191, abs=3)] * 3


def test_lut_maps_red_to_red_and_blue_to_blue(tmp_path):
    """A .cube row is an output RGB triple: the channel order must never be swapped."""
    rows = []
    for b in (0.0, 1.0):
        for g in (0.0, 1.0):
            for r in (0.0, 1.0):
                rows.append(f"{1.0 - r:.4f} {g:.4f} {b:.4f}")   # invert red only
    path = tmp_path / "red_invert.cube"
    path.write_text("LUT_3D_SIZE 2\n" + "\n".join(rows) + "\n", encoding="utf-8")
    engine, clip = build_engine(frame=np.zeros((4, 4, 3), np.uint8))
    # One pixel with a strong red channel: BGR (10, 20, 200).
    clip.effects = [{"effect_id": "lut", "enabled": True,
                     "params": {"lut_file": str(path), "intensity": 100}}]
    frame = np.zeros((4, 4, 3), np.uint8)
    frame[:, :] = (10, 20, 200)
    engine.media._entries["synthetic-effect-test.png"].frames = [frame] * 60
    out = engine.render_frame(0, 1.0)[1][0, 0].tolist()
    assert out[2] == pytest.approx(55, abs=4), f"red came back as {out}"
    assert out[0] == pytest.approx(10, abs=2), f"blue came back as {out}"
    assert out[1] == pytest.approx(20, abs=2)


def test_lut_built_in_tables_are_different_looks():
    teal = builtin_lut("teal_orange")
    warm = builtin_lut("warm_film")
    grey = builtin_lut("black_white")
    assert teal.shape == (33, 33, 33, 3)
    assert frame_diff(teal, warm) > 1.0
    luma = grey[..., 0]
    assert np.allclose(luma, grey[..., 1], atol=1.0) and np.allclose(luma, grey[..., 2], atol=1.0)


def test_lut_reports_a_broken_file_without_failing(tmp_path):
    bad = tmp_path / "bad.cube"
    bad.write_text("not a cube file\n", encoding="utf-8")
    engine, clip = build_engine()
    effect = runtime(engine, "lut", lut_file=str(bad))
    size, _table = effect.table()
    assert size == 33, "a broken file must fall back to the built-in table"
    assert effect.load_error
    assert LutEffect in EditorEngine.EFFECT_TYPES
