"""Native (non-effect) motion blur and frame blending.

These exercise the compositor directly: motion blur samples the clip's own transform,
corner pin and shape geometry inside the shutter window, while Frame Mix / Optical Flow
sample between adjacent source frames. The two systems must stay independent.
"""

import numpy as np
import pytest

from meenifx.engine import EditorEngine
from meenifx.media.cache import MediaEntry
from meenifx.model import channels as CH
from meenifx.model import transform as T
from meenifx.project.serialize import sections_to_timeline, timeline_to_sections
from meenifx.project.settings import ProjectSettings


def _engine(width=160, height=90, fps=30):
    engine = EditorEngine(fps)
    engine.apply_project_settings(ProjectSettings(name="Temporal", width=width,
                                                  height=height, fps=float(fps)))
    return engine


def _moving_shape(engine):
    clip_id = engine.session.add_shape_clip("rectangle", box=(60, 32, 36, 24))
    clip = engine.timeline.find_clip(clip_id)
    clip.shape["fill_color"] = "#FFFFFF"
    clip.shape["stroke_enabled"] = False
    T.set_channel(clip, "position_x", -50.0, 0, auto_key=True)
    T.set_channel(clip, "position_x", 50.0, 30, auto_key=True)
    return clip


def _colour_frames(colours, count=40, size=16):
    frames = []
    for index in range(count):
        frame = np.zeros((size, size, 3), np.uint8)
        frame[:] = colours[index % len(colours)]
        frames.append(frame)
    entry = MediaEntry("video", count, 30)
    entry.frames = frames
    entry.frame_count = count
    return entry


def _media_clip(engine, path="source.mp4", speed=1.0, start=0, source_in=0,
                source_out=40, duration=60):
    entry = _colour_frames([(0, 0, 255), (0, 255, 0), (255, 0, 0), (255, 255, 0)])
    engine.media._entries[path] = entry
    track = engine.timeline.add_track("video")
    return engine.timeline.new_clip("Source", path, track, start, duration,
                                    source_in=source_in, source_out=source_out,
                                    source_fps=30, speed=speed)


def test_native_motion_blur_smears_an_animated_shape():
    engine = _engine()
    clip = _moving_shape(engine)
    plain = engine.render_frame(15, 1.0)[0]
    engine.timeline.motion_blur = True
    engine.timeline.motion_blur_shutter = 360.0
    engine.timeline.motion_blur_samples = 12
    blurred = engine.render_frame(15, 1.0)[0]
    assert np.abs(plain.astype(np.int16) - blurred.astype(np.int16)).mean() > 0.5
    # A real smear widens the moving silhouette rather than just shifting it.
    plain_width = np.where((plain > 20).any(axis=0))[0]
    blurred_width = np.where((blurred > 20).any(axis=0))[0]
    assert blurred_width.max() - blurred_width.min() > plain_width.max() - plain_width.min()


def test_motion_blur_precedence_composition_track_and_clip():
    engine = _engine()
    clip = _moving_shape(engine)
    engine.timeline.motion_blur = False
    assert not engine.motion_blur_for_clip(clip)
    engine.timeline.motion_blur = True
    assert engine.motion_blur_for_clip(clip)
    clip.temporal["motion_blur"] = False
    assert not engine.motion_blur_for_clip(clip)
    clip.temporal["motion_blur"] = None
    clip.track.motion_blur = False
    assert not engine.motion_blur_for_clip(clip)
    clip.temporal["motion_blur"] = True
    assert engine.motion_blur_for_clip(clip)


def test_motion_blur_samples_animated_corner_pin_geometry():
    engine = _engine()
    clip_id = engine.session.add_shape_clip("rectangle", box=(45, 25, 70, 45))
    clip = engine.timeline.find_clip(clip_id)
    clip.shape["fill_color"] = "#FFFFFF"
    clip.shape["stroke_enabled"] = False
    clip.effects = [{"effect_id": "corner_pin", "enabled": True, "params": {}}]
    CH.set_value(clip, CH.effect_channel("corner_pin", "corner_tl_x"),
                 0.0, 0, auto_key=True)
    CH.set_value(clip, CH.effect_channel("corner_pin", "corner_tl_x"),
                 0.45, 30, auto_key=True)
    plain = engine.render_frame(15, 1.0)[0]
    engine.timeline.motion_blur = True
    engine.timeline.motion_blur_samples = 12
    engine.timeline.motion_blur_shutter = 360.0
    blurred = engine.render_frame(15, 1.0)[0]
    difference = np.abs(plain.astype(np.int16) - blurred.astype(np.int16)).max(axis=2)
    assert int((difference > 8).sum()) > 20, "corner animation was not sampled"


def test_frame_mix_blends_adjacent_source_frames_for_slow_motion():
    engine = _engine(16, 16)
    clip = _media_clip(engine, speed=0.5)
    engine.timeline.frame_blending = "mix"
    image = engine.render_frame(1, 1.0)[0]
    # Source position 0.5: half of frame 0 (green in BGR) and frame 1 (green).
    assert image[8, 8].tolist() == [0, 128, 128]
    assert engine.frame_blend_mode(clip) == "mix"


def test_frame_blending_clip_override_and_composition_master():
    engine = _engine(16, 16)
    clip = _media_clip(engine, speed=0.5)
    engine.timeline.frame_blending = "mix"
    clip.temporal["frame_blend"] = "off"
    plain = engine.render_frame(1, 1.0)[0]
    assert plain[8, 8].tolist() == [0, 0, 255], "clip Off must override the composition"
    clip.temporal["frame_blend"] = None
    clip.track.frame_blend = "off"
    assert engine.render_frame(1, 1.0)[0][8, 8].tolist() == [0, 0, 255]
    clip.track.frame_blend = None
    assert engine.render_frame(1, 1.0)[0][8, 8].tolist() == [0, 128, 128]
    engine.timeline.frame_blending = "off"
    assert engine.frame_blend_mode(clip) == "off"


def test_optical_flow_synthesises_a_frame_without_crossing_clip_boundaries():
    engine = _engine(16, 16)
    _media_clip(engine, speed=0.5)
    engine.timeline.frame_blending = "flow"
    middle = engine.render_frame(1, 1.0)[0]
    assert middle.shape == (16, 16, 3)
    assert middle[8, 8].tolist() == [0, 128, 128]
    # At the first frame there is no earlier source frame to bleed in.
    first = engine.render_frame(0, 1.0)[0]
    assert first[8, 8].tolist() == [0, 0, 255]


def test_motion_blur_and_frame_blending_can_run_together():
    engine = _engine(16, 16)
    clip = _media_clip(engine, speed=0.5)
    T.set_channel(clip, "position_x", -3.0, 0, auto_key=True)
    T.set_channel(clip, "position_x", 3.0, 30, auto_key=True)
    engine.timeline.motion_blur = True
    engine.timeline.motion_blur_samples = 5
    engine.timeline.frame_blending = "mix"
    image = engine.render_frame(1, 1.0)[0]
    assert image.shape == (16, 16, 3)
    assert engine.motion_blur_for_clip(clip)
    assert engine.frame_blend_mode(clip) == "mix"


def test_temporal_settings_round_trip_through_the_project_format():
    engine = _engine()
    clip = _moving_shape(engine)
    clip.temporal = {"motion_blur": False, "frame_blend": "flow"}
    clip.track.motion_blur = True
    clip.track.frame_blend = "mix"
    engine.timeline.motion_blur = True
    engine.timeline.motion_blur_shutter = 240.0
    engine.timeline.motion_blur_phase = 45.0
    engine.timeline.motion_blur_samples = 16
    engine.timeline.frame_blending = "mix"
    sections = timeline_to_sections(engine.timeline, lambda path: 1)
    loaded = sections_to_timeline(sections, 30, lambda media_id: "")
    assert loaded.motion_blur and loaded.motion_blur_shutter == 240.0
    assert loaded.motion_blur_phase == 45.0 and loaded.motion_blur_samples == 16
    assert loaded.frame_blending == "mix"
    reopened = loaded.all_clips()[0]
    assert reopened.temporal == {"motion_blur": False, "frame_blend": "flow"}
    assert reopened.track.motion_blur is True
    assert reopened.track.frame_blend == "mix"
