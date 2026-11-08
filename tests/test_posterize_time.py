"""Posterize Time: a Frame Rate control in fps that holds pictures without retiming.

The feature request's matrix (60/12, 30/10, 24/8), fractional rates, keyframed rates,
animated transform/effect state, trimmed and split clips, legacy `step` migration,
preview/export parity and unchanged duration/audio are all pinned here. The generic
parameter audit (tests/test_effect_parameters.py) covers the control's range and
wiring; this file covers what only the time domain can show.
"""

import cv2
import numpy as np
import pytest

from conftest import frame_diff

from meenifx.effects.temporal import (DEFAULT_FPS, PosterizeTimeEffect, frame_rate_at,
                                      migrate_effect_params, posterize_hold)
from meenifx.engine import EditorEngine
from meenifx.media.cache import MediaEntry
from meenifx.model.keyframes import AnimatedValue
from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

PATH = "posterize-source.png"


def moving(fps=30, frames=240, size=(96, 54)):
    """A clip whose picture changes on every frame, so a hold is visible."""
    engine = EditorEngine(fps)
    engine.source_width, engine.source_height = size
    pictures = []
    for index in range(frames):
        frame = np.zeros((size[1], size[0], 3), np.uint8)
        x = 4 + (index * 3) % 60
        cv2.rectangle(frame, (x, 10), (x + 18, 40), (250, 250, 250), -1)
        frame[:, :, 0] = np.clip(np.arange(size[0])[None, :] + index * 4, 0, 255)
        pictures.append(frame)
    entry = MediaEntry("video", frames, fps)
    entry.frames = pictures
    engine.media._entries[PATH] = entry
    track = engine.timeline.add_track("video")
    clip = engine.timeline.new_clip("moving", PATH, track, 0, frames,
                                    source_out=frames, source_fps=fps)
    return engine, clip


def posterized(fps):
    return [{"effect_id": "posterize_time", "enabled": True, "params": {"fps": fps}}]


def test_the_primary_control_is_frame_rate_in_fps():
    spec = next(item for item in PosterizeTimeEffect.PARAMS if item.name == "fps")
    assert spec.label == "Frame Rate"
    assert spec.unit.strip() == "fps"
    assert spec.kind == "float", "a fractional frame rate has to be typeable"
    assert spec.default == DEFAULT_FPS == 12.0
    assert spec.minimum > 0 and spec.maximum >= 30
    assert PosterizeTimeEffect(None).fps.get() == 12.0


@pytest.mark.parametrize("project_fps,rate,hold", [(60, 12.0, 5), (30, 10.0, 3),
                                                   (24, 8.0, 3)],
                         ids=["60-12", "30-10", "24-8"])
def test_holds_follow_the_project_frame_rate(project_fps, rate, hold):
    engine, clip = moving(fps=project_fps, frames=max(48, project_fps))
    clip.effects = posterized(rate)
    before = (clip.timeline_start, clip.duration, engine.timeline.duration)
    spans = 3
    samples = [engine.held_frame(clip, frame) for frame in range(hold * spans)]
    assert samples == [start for start in range(0, hold * spans, hold) for _ in range(hold)]
    pictures = [engine.render_frame(frame, 1.0)[1] for frame in range(hold * spans)]
    for start in range(0, hold * spans, hold):
        for other in range(start + 1, start + hold):
            assert frame_diff(pictures[start], pictures[other]) == 0.0, (
                f"{hold}-frame hold released early at {other}")
    assert frame_diff(pictures[hold - 1], pictures[hold]) > 0.2, "the hold never releases"
    assert (clip.timeline_start, clip.duration, engine.timeline.duration) == before


def test_fractional_rate_does_not_accumulate_drift():
    engine, clip = moving(fps=30, frames=400)
    clip.effects = posterized(7.0)
    samples = [engine.held_frame(clip, frame) for frame in range(301)]
    assert samples == sorted(samples), "a hold must never jump backwards"
    assert samples[300] == 300, "the holds drifted away from the clip's own clock"
    # 300 frames at 7 fps hold 71 distinct pictures (70 releases), not 70 or 72.
    assert len(set(samples)) == 71
    runs, current = [], 1
    for left, right in zip(samples, samples[1:]):
        if left == right:
            current += 1
        else:
            runs.append(current)
            current = 1
    runs.append(current)
    # The last hold is cut short by the end of the sampled range.
    assert set(runs[:-1]) <= {4, 5}, f"uneven holds: {sorted(set(runs[:-1]))}"
    assert runs[-1] >= 1


def test_animated_transform_and_effect_parameters_freeze_with_the_picture():
    engine, clip = moving(fps=60, frames=120)
    move = AnimatedValue()
    move.set_keyframe(0, 0.0)
    move.set_keyframe(60, 180.0)
    clip.transform["position_x"] = move
    grain_scale = AnimatedValue()
    grain_scale.set_keyframe(0, 3)
    grain_scale.set_keyframe(60, 40)
    clip.effects = posterized(12.0) + [
        {"effect_id": "mosaic", "enabled": True, "params": {"scale": grain_scale}}]
    pictures = [engine.render_frame(frame, 1.0)[1] for frame in range(15)]
    for start in (0, 5, 10):
        for other in range(start + 1, start + 5):
            assert frame_diff(pictures[start], pictures[other]) == 0.0, (
                "an animated property moved inside a held picture")
    assert frame_diff(pictures[4], pictures[5]) > 0.2, "the animating layer never updates"


def test_a_keyframed_frame_rate_is_deterministic_and_never_retimes_the_clip():
    engine, clip = moving(fps=60, frames=120)
    rate = AnimatedValue()
    rate.set_keyframe(0, 6.0)
    rate.set_keyframe(59, 24.0)
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"fps": rate}}]
    duration_before = (clip.timeline_start, clip.duration, engine.timeline.duration)
    first = [engine.held_frame(clip, frame) for frame in range(60)]
    assert first == [engine.held_frame(clip, frame) for frame in range(60)], (
        "the same timeline frame must always sample the same picture")
    assert first == sorted(first)
    assert all(sample <= frame for frame, sample in enumerate(first))
    assert engine.held_frame(clip, 59) > 20, "the fast end of the curve samples too early"
    assert (clip.timeline_start, clip.duration, engine.timeline.duration) == duration_before


def test_preview_and_export_render_the_same_held_frame(tmp_path):
    engine, clip = moving(fps=60, frames=120)
    clip.effects = posterized(12.0)
    preview = engine.render_frame(7, 1.0)[1]
    target = tmp_path / "held.png"
    _path, count, _fps, _size = engine.export({
        "output_path": str(target), "format": "png", "use_source_fps": True,
        "use_source_size": True, "range_start": 0, "range_end": 8,
    })
    written = cv2.imread(str(target))
    assert written is not None and count == 8
    assert frame_diff(written, preview) < 1.0, "export and preview disagree inside a hold"


def test_a_trimmed_clip_holds_on_its_own_start():
    engine, clip = moving(fps=30, frames=120)
    clip.timeline_start = 50
    clip.source_in = 10
    clip.duration = 40
    clip.effects = posterized(10.0)
    assert [engine.held_frame(clip, frame) for frame in range(6)] == [0, 0, 0, 3, 3, 3]
    first = engine.render_frame(50, 1.0)[1]
    held = engine.render_frame(52, 1.0)[1]
    assert frame_diff(first, held) == 0.0


def test_split_clips_each_hold_from_their_own_start():
    engine, clip = moving(fps=30, frames=180)
    clip.duration = 30
    clip.source_out = 30
    clip.effects = posterized(10.0)
    second = engine.timeline.new_clip("moving", PATH, clip.track, 30, 60,
                                      source_in=30, source_out=120, source_fps=30)
    second.effects = posterized(10.0)
    assert [engine.held_frame(second, frame) for frame in range(6)] == [0, 0, 0, 3, 3, 3]
    start = engine.render_frame(30, 1.0)[1]
    held = engine.render_frame(32, 1.0)[1]
    release = engine.render_frame(33, 1.0)[1]
    assert frame_diff(start, held) == 0.0, "the second piece holds from its own frame 0"
    assert frame_diff(held, release) > 0.2


def test_audio_timing_and_duration_never_change():
    engine, clip = moving(fps=30, frames=90)
    audio = engine.timeline.add_track("audio")
    tone = engine.timeline.new_clip("tone", "tone.wav", audio, 10, 40,
                                    source_out=40, source_fps=30)
    before = (clip.timeline_start, clip.duration, tone.timeline_start, tone.duration,
              engine.timeline.duration)
    clip.effects = posterized(7.0)
    for frame in range(0, 30):
        engine.render_frame(frame, 1.0)
    assert (clip.timeline_start, clip.duration, tone.timeline_start, tone.duration,
            engine.timeline.duration) == before, "posterizing retimed the timeline"


def test_a_project_saved_with_step_keeps_its_exact_holds():
    engine, clip = moving(fps=30, frames=120)
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"step": 4, "offset": 2}}]
    legacy = [engine.held_frame(clip, frame) for frame in range(40)]
    sections = timeline_to_sections(engine.timeline, lambda _path: "media-1")
    loaded = sections_to_timeline(sections, 30, lambda _media_id: PATH)
    params = loaded.all_clips()[0].effects[0]["params"]
    assert "step" not in params, "the old value must be migrated, not left behind"
    assert params["fps"] == pytest.approx(7.5)
    assert [posterize_hold(frame, params, 30) for frame in range(40)] == legacy, (
        "a migrated project must hold exactly the frames it held before")


def test_a_keyframed_step_becomes_a_keyframed_frame_rate():
    engine, clip = moving(fps=30, frames=120)
    step = AnimatedValue()
    step.set_keyframe(0, 4)
    step.set_keyframe(20, 8)
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"step": step}}]
    sections = timeline_to_sections(engine.timeline, lambda _path: "media-1")
    loaded = sections_to_timeline(sections, 30, lambda _media_id: PATH)
    params = loaded.all_clips()[0].effects[0]["params"]
    assert "step" not in params
    curve = params["fps"]
    assert isinstance(curve, AnimatedValue), "the keyframed rate must stay animatable"
    assert [key.time for key in curve.keyframes] == [0, 20], "keyframe times moved"
    assert frame_rate_at(params, 0, 30) == pytest.approx(7.5)
    assert frame_rate_at(params, 20, 30) == pytest.approx(3.75)


def test_migration_is_idempotent_and_never_overwrites_a_frame_rate():
    params = {"step": 4}
    entry = {"params": params}
    assert migrate_effect_params(entry, 30) is True
    rate = params["fps"]
    assert "step" not in params
    assert migrate_effect_params(entry, 30) is False
    assert params["fps"] == rate
    kept = {"params": {"fps": 9.0, "step": 2}}
    assert migrate_effect_params(kept, 30) is False
    assert kept["params"]["fps"] == 9.0


def test_a_rate_above_the_project_rate_adds_no_hold():
    engine, clip = moving(fps=30, frames=30)
    clip.effects = posterized(60.0)
    assert [engine.held_frame(clip, frame) for frame in range(10)] == list(range(10))


def test_offset_shifts_where_holds_begin():
    engine, clip = moving(fps=30, frames=30)
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"fps": 7.5, "offset": 2}}]
    assert [engine.held_frame(clip, frame) for frame in range(10)] == [
        0, 0, 2, 2, 2, 2, 6, 6, 6, 6]


def test_holding_is_pure_time_arithmetic(monkeypatch):
    engine, clip = moving(fps=30, frames=30)
    clip.effects = posterized(10.0)

    def fail(*_args, **_kwargs):
        raise AssertionError("held_frame must not decode or render anything")

    monkeypatch.setattr(engine.media, "frame_for_clip", fail)
    assert engine.held_frame(clip, 7) == 6
