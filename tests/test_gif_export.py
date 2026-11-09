"""GIF exports must preserve the timeline's playback duration and sample positions.

These tests read the generated GIF back with Pillow instead of inspecting exporter
settings, because GIF stores delays in centiseconds and the encoder/viewer conversion is
where the original speed bug lived.
"""

import numpy as np
import cv2
import pytest
from PIL import Image

from conftest import build_engine
from meenifx.model import presets as preset_model
from meenifx.model.keyframes import AnimatedValue
from meenifx.processing import gif_export
from meenifx.project.settings import ProjectSettings

WIDTH, HEIGHT = 96, 54


def moving_frame(index):
    """A uniquely changing picture: a moving white marker plus a changing stripe."""
    frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    frame[:] = (24, 24, 24)
    x = 2 + (index * 2) % 80
    cv2.rectangle(frame, (x, 12), (x + 12, 40), (255, 255, 255), -1)
    frame[:, (index * 7) % WIDTH, 0] = 255
    return frame


def animated_engine(fps=30, frames=150, nominal=None):
    """One synthetic moving clip with the project's exact and nominal rates applied."""
    nominal = int(round(fps)) if nominal is None else int(nominal)
    engine, clip = build_engine(frame=np.zeros((HEIGHT, WIDTH, 3), np.uint8),
                                fps=nominal, frames=frames, duration=frames)
    engine.apply_project_settings(ProjectSettings(name="GIF timing", width=WIDTH,
                                                  height=HEIGHT, fps=float(fps)))
    entry = engine.media._entries[clip.source_path]
    entry.frames = [moving_frame(index) for index in range(frames)]
    entry.fps = float(fps)
    # A fractional project still runs on whole timeline frames, exactly as the real
    # project settings do (23.976 -> 24, 29.97 -> 30).
    clip.source_fps = float(fps)
    clip.timeline_fps = float(nominal)
    return engine, clip


def export_gif(engine, tmp_path, name="out.gif", fps=30, use_source=True, **extra):
    settings = {
        "output_path": str(tmp_path / name),
        "format": "gif",
        "use_source_fps": use_source,
        "fps": fps,
        "use_source_size": True,
        "range_start": 0,
        "range_end": engine.timeline.duration,
        "gif_loop": "forever",
        "gif_colors": 256,
        "gif_dither": "floyd",
    }
    settings.update(extra)
    path, count, effective_fps, _size = engine.export(settings)
    return path, count, effective_fps


def decode_gif(path):
    delays, frames, loop = [], [], None
    with Image.open(path) as image:
        loop = image.info.get("loop")
        try:
            while True:
                delays.append(int(image.info.get("duration") or 0))
                frames.append(np.array(image.convert("RGB")))
                image.seek(image.tell() + 1)
        except EOFError:
            pass
    return delays, frames, loop


def marker_x(frame):
    white = (frame[:, :, 0] > 230) & (frame[:, :, 1] > 230) & (frame[:, :, 2] > 230)
    columns = np.where(white)[1]
    return int(columns.min()) if columns.size else None


def expected_marker_x(index):
    return 2 + (index * 2) % 80


@pytest.mark.parametrize("fps,seconds,nominal,export_fps,use_source", [
    (30, 1, 30, 30, True),
    (30, 3, 30, 30, True),
    (30, 5, 30, 30, True),
    (30, 10, 30, 30, True),
    (24, 5, 24, 24, True),
    (30, 5, 30, 24, False),
    (30, 5, 30, 20, False),
    (30, 5, 30, 15, False),
    (30, 5, 30, 10, False),
    (60, 5, 60, 30, False),
    (60, 5, 60, 60, True),
    (23.976, 10, 24, 23.976, True),
    (29.97, 10, 30, 29.97, True),
])
def test_decoded_gif_duration_matches_timeline(tmp_path, fps, seconds, nominal,
                                              export_fps, use_source):
    frames = int(round(seconds * nominal))
    engine, _clip = animated_engine(fps=fps, frames=frames, nominal=nominal)
    expected = frames / float(fps)
    path, count, effective_fps = export_gif(engine, tmp_path, fps=export_fps,
                                            use_source=use_source)
    delays, decoded_frames, _loop = decode_gif(path)

    assert sum(delays) / 1000.0 == pytest.approx(expected, abs=0.011), (
        fps, export_fps, sum(delays), expected)
    assert all(delay >= gif_export.MIN_DELAY_CS * 10 for delay in delays)
    assert len(decoded_frames) <= count
    assert count >= 1
    if export_fps <= gif_export.MAX_RELIABLE_FPS:
        assert effective_fps == pytest.approx(export_fps, rel=0.01)


@pytest.mark.parametrize("fps,expected_count,allowed_delays", [
    (10, 50, {10}),
    (15, 75, {6, 7}),
    (20, 100, {5}),
    (24, 120, {4, 5}),
    (30, 150, {3, 4}),
])
def test_centisecond_remainder_is_distributed(fps, expected_count, allowed_delays):
    timing = gif_export.plan_timing(5.0, fps)
    assert timing.frame_count == expected_count
    assert sum(timing.delays_cs) == 500
    assert set(timing.delays_cs) <= allowed_delays
    assert all(delay >= gif_export.MIN_DELAY_CS for delay in timing.delays_cs)


def test_fractional_rates_keep_the_timeline_average():
    for duration, fps, expected_count, total_cs in (
            (10.01, 23.976, 240, 1001), (10.01, 29.97, 300, 1001)):
        timing = gif_export.plan_timing(duration, fps)
        assert timing.frame_count == expected_count
        assert sum(timing.delays_cs) == total_cs
        expected_delays = {4, 5} if fps < 25 else {3, 4}
        assert set(timing.delays_cs) <= expected_delays
        assert timing.effective_fps == pytest.approx(fps, rel=0.0002)


def test_extremely_short_delays_are_capped_without_slowing_the_gif():
    timing = gif_export.plan_timing(5.0, 60.0)
    assert timing.frame_count == 250
    assert sum(timing.delays_cs) == 500
    assert set(timing.delays_cs) == {2}
    assert timing.effective_fps == pytest.approx(50.0)
    assert timing.clamped


def test_lower_export_rate_resamples_instead_of_stretching_time(tmp_path):
    engine, _clip = animated_engine(fps=30, frames=150)
    path, count, _effective = export_gif(engine, tmp_path, fps=10, use_source=False)
    delays, frames, _loop = decode_gif(path)

    assert count == 50
    assert delays == [100] * 50
    assert sum(delays) == 5000
    # Output frame 20 is at 2.0 s, which is source frame 60, not source frame 20.
    assert marker_x(frames[20]) == expected_marker_x(60)
    assert marker_x(frames[0]) == expected_marker_x(0)


def test_speed_change_shortens_the_gif_by_the_timeline_speed(tmp_path):
    engine, _clip = animated_engine(fps=30, frames=150)
    engine.speed_percent.set(200)
    path, count, _effective = export_gif(engine, tmp_path, fps=10, use_source=False)
    delays, _frames, _loop = decode_gif(path)

    assert count == 25
    assert sum(delays) == 2500
    assert sum(delays) / 1000.0 == pytest.approx(2.5, abs=0.011)


def test_reverse_timeline_uses_reversed_source_positions(tmp_path):
    engine, _clip = animated_engine(fps=30, frames=30)
    engine.reverse_timeline.set(True)
    path, _count, _effective = export_gif(engine, tmp_path, fps=10, use_source=False)
    _delays, frames, _loop = decode_gif(path)

    assert marker_x(frames[0]) == expected_marker_x(29)
    assert marker_x(frames[-1]) == expected_marker_x(2)


def test_posterize_time_holds_do_not_change_gif_duration(tmp_path):
    engine, clip = animated_engine(fps=30, frames=60)
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"fps": 6.0}}]
    path, count, _effective = export_gif(engine, tmp_path, fps=30)
    delays, frames, _loop = decode_gif(path)

    assert count == 60
    assert sum(delays) == 2000
    # The GIF writer merges identical held frames, but the merged delay conserves the
    # timeline duration instead of adding playback time.
    assert len(frames) < count
    assert sum(delays) / 1000.0 == pytest.approx(2.0, abs=0.011)


def test_precomposition_export_preserves_duration(tmp_path):
    engine, clip = animated_engine(fps=30, frames=90)
    nested_id = engine.session.precompose("GIF nested", [clip.id])
    assert nested_id is not None
    path, count, _effective = export_gif(engine, tmp_path, fps=24, use_source=False)
    delays, _frames, _loop = decode_gif(path)

    assert count == 72
    assert sum(delays) / 1000.0 == pytest.approx(3.0, abs=0.011)


def test_keyframed_text_and_shape_export_with_timeline_timing(tmp_path):
    engine, _clip = animated_engine(fps=30, frames=60)
    text_id = engine.session.add_text_clip("GIF", 0, 60)
    shape_id = engine.session.add_shape_clip("rectangle", box=(8, 8, 24, 18),
                                             start=0, duration=60)
    text = engine.timeline.find_clip(text_id)
    x = AnimatedValue()
    x.set_keyframe(0, -30.0)
    x.set_keyframe(59, 30.0)
    text.transform["position_x"] = x
    shape = engine.timeline.find_clip(shape_id)
    shape.transform["position_y"] = x

    path, count, _effective = export_gif(engine, tmp_path, fps=15, use_source=False)
    delays, frames, _loop = decode_gif(path)

    assert count == 30
    assert sum(delays) == 2000
    assert len(frames) == count
    assert max(float(frame.std()) for frame in frames) > 10.0


def test_bounce_preset_export_preserves_duration(tmp_path):
    engine, clip = animated_engine(fps=30, frames=60)
    preset_model.add(clip, "bounce", "in", program="bounce", args={"px": 1.0},
                     knobs={"unit": "whole"}, duration=30, name="Bounce")
    path, count, _effective = export_gif(engine, tmp_path, fps=15, use_source=False)
    delays, frames, _loop = decode_gif(path)

    assert count == 30
    assert sum(delays) == 2000
    assert len(frames) == count
    assert sum(delays) / 1000.0 == pytest.approx(2.0, abs=0.011)


def test_adjustment_layer_export_preserves_duration(tmp_path):
    engine, _clip = animated_engine(fps=30, frames=60)
    adjustment_id = engine.session.add_adjustment_clip(0, 60)
    adjustment = engine.timeline.find_clip(adjustment_id)
    adjustment.effects = [{"effect_id": "invert", "enabled": True, "params": {}}]

    path, count, _effective = export_gif(engine, tmp_path, fps=15, use_source=False)
    delays, frames, _loop = decode_gif(path)

    assert count == 30
    assert sum(delays) == 2000
    assert len(frames) == count


def test_loop_modes_are_written_to_the_gif(tmp_path):
    engine, _clip = animated_engine(fps=30, frames=30)
    forever_path, *_ = export_gif(engine, tmp_path, name="forever.gif", fps=10,
                                  gif_loop="forever")
    once_path, *_ = export_gif(engine, tmp_path, name="once.gif", fps=10,
                               gif_loop="once")
    custom_path, *_ = export_gif(engine, tmp_path, name="custom.gif", fps=10,
                                 gif_loop="custom", gif_loops=3)

    assert decode_gif(forever_path)[2] == 0
    assert decode_gif(once_path)[2] is None
    assert decode_gif(custom_path)[2] == 3


def test_colour_limit_reaches_the_encoder(tmp_path):
    engine, _clip = animated_engine(fps=30, frames=30)
    path, *_ = export_gif(engine, tmp_path, fps=10, gif_colors=16, gif_dither="none")
    _delays, frames, _loop = decode_gif(path)
    palette_sizes = [len({tuple(pixel) for pixel in frame.reshape(-1, 3)}) for frame in frames]
    assert max(palette_sizes) <= 16


def test_export_dialog_maps_gif_controls(qapp):
    from meenifx.ui.dialogs import ExportDialog

    engine, _clip = animated_engine(fps=30, frames=30)
    dialog = ExportDialog(engine)
    dialog.format.setCurrentText("gif")
    dialog.gif_rate.setCurrentText("24")
    dialog.gif_loop.setCurrentText("Once")
    dialog.gif_loop.setCurrentIndex(dialog.gif_loop.findData("custom"))
    dialog.gif_loops.set_value(4)
    dialog.gif_colors.setCurrentIndex(dialog.gif_colors.findData(128))
    dialog.gif_dither.setCurrentIndex(dialog.gif_dither.findData("none"))

    settings = dialog.settings()
    assert settings["format"] == "gif"
    assert settings["use_source_fps"] is False
    assert settings["fps"] == 24
    assert settings["gif_loop"] == "custom"
    assert settings["gif_loops"] == 4
    assert settings["gif_colors"] == 128
    assert settings["gif_dither"] == "none"
    assert not dialog.gif_alpha.isEnabled()
