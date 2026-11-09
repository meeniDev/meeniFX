"""Adjustment layers grade the composite below them, including in the time domain.

Posterize Time used to run its hold arithmetic on the adjustment clip's own parameters
while the picture beneath stayed at the current frame. These tests render the real
compositor: a held adjustment must re-sample every layer below it, not merely show a
different number in the inspector.
"""

import cv2
import numpy as np
import pytest

from conftest import build_engine, frame_diff
from meenifx.effects.temporal import posterize_hold
from meenifx.engine import EditorEngine
from meenifx.media.cache import MediaEntry
from meenifx.model.keyframes import AnimatedValue
from meenifx.preview.invalidation import timeline_digests
from meenifx.project import container as C
from meenifx.project.serialize import (compositions_to_sections, sections_to_compositions,
                                       sections_to_timeline, timeline_to_sections)
from meenifx.project.settings import ProjectSettings

PATH = "adjustment-source.png"


def moving(fps=24, frames=72, size=(96, 54)):
    """A video whose picture changes every frame, so a temporal hold is visible."""
    engine = EditorEngine(fps)
    engine.source_width, engine.source_height = size
    engine.session.project_size = size
    pictures = []
    for index in range(frames):
        frame = np.zeros((size[1], size[0], 3), np.uint8)
        x = 4 + (index * 3) % 60
        cv2.rectangle(frame, (x, 10), (x + 18, 40), (245, 245, 245), -1)
        frame[:, :, 0] = np.clip(np.arange(size[0])[None, :] + index * 5, 0, 255)
        frame[:, :, 1] = np.clip(30 + index * 3, 0, 255)
        pictures.append(frame)
    entry = MediaEntry("video", frames, fps)
    entry.frames = pictures
    engine.media._entries[PATH] = entry
    track = engine.timeline.add_track("video")
    clip = engine.timeline.new_clip("moving", PATH, track, 0, frames,
                                    source_out=frames, source_fps=fps)
    return engine, clip


def adjustment(engine, start=0, duration=24, effects=None):
    clip_id = engine.session.add_adjustment_clip(start, duration)
    clip = engine.timeline.find_clip(clip_id)
    clip.effects = list(effects or ())
    return clip


def posterized(rate):
    return [{"effect_id": "posterize_time", "enabled": True,
             "params": {"fps": rate}}]


def render(engine, frame):
    return engine.render_frame(frame, 1.0)[1]


def test_posterize_time_on_an_adjustment_holds_the_composited_picture():
    engine, base = moving(fps=24, frames=72)
    plain = [render(engine, frame) for frame in range(3)]
    assert frame_diff(plain[0], plain[1]) > 0.2, "the source itself must animate"

    adjustment(engine, effects=posterized(12.0))
    held = [render(engine, frame) for frame in range(3)]
    assert frame_diff(held[0], held[1]) == 0.0, (
        "Posterize Time on an adjustment layer did not hold the composite below it")
    assert frame_diff(held[1], held[2]) > 0.2, "the adjustment never releases the hold"


def test_glow_and_color_correction_grade_the_layers_below():
    engine, _base = moving(frames=24)
    before = render(engine, 0)
    glow = adjustment(engine, effects=[{
        "effect_id": "glow", "enabled": True,
        "params": {"intensity": 300.0, "radius": 24.0, "threshold": 20.0}}])
    bloomed = render(engine, 0)
    assert frame_diff(before, bloomed) > 1.0, "glow had no picture to bloom"
    glow.effects = [{"effect_id": "color_correct", "enabled": True,
                     "params": {"saturation": -100.0}}]
    graded = render(engine, 0)
    assert frame_diff(before, graded) > 1.0, "colour correction did not reach the composite"


def test_adjustment_respects_position_duration_and_opacity():
    engine, _base = moving(frames=72)
    plain = {frame: render(engine, frame) for frame in (0, 30, 60)}
    adj = adjustment(engine, start=24, duration=24, effects=[
        {"effect_id": "invert", "enabled": True, "params": {}}])
    assert frame_diff(plain[0], render(engine, 0)) == 0.0, (
        "an adjustment affected a frame before its timeline start")
    assert frame_diff(plain[60], render(engine, 60)) == 0.0, (
        "an adjustment affected a frame after its timeline end")
    assert frame_diff(plain[30], render(engine, 30)) > 1.0

    adj.transform["opacity"] = 0
    assert frame_diff(plain[30], render(engine, 30)) == 0.0, (
        "zero opacity must leave the composite untouched")
    adj.transform["opacity"] = 100
    assert frame_diff(plain[30], render(engine, 30)) > 1.0


def test_effect_order_is_the_inspector_order():
    engine, _base = moving(frames=24)
    adj = adjustment(engine, effects=[
        {"effect_id": "invert", "enabled": True, "params": {}},
        {"effect_id": "color_correct", "enabled": True,
         "params": {"brightness": 35.0}}])
    first = render(engine, 0)
    adj.effects = list(reversed(adj.effects))
    second = render(engine, 0)
    assert frame_diff(first, second) > 0.2, "effects ran out of order"


def test_adjustment_spans_multiple_clips():
    engine = EditorEngine(24)
    engine.source_width, engine.source_height = 96, 54
    engine.session.project_size = (96, 54)
    picture = np.zeros((54, 96, 3), np.uint8)
    picture[:, :] = (40, 90, 160)
    entry = MediaEntry("image", 1, 24)
    entry.frames = [picture]
    engine.media._entries[PATH] = entry
    track = engine.timeline.add_track("video")
    for start in (0, 24):
        engine.timeline.new_clip("part", PATH, track, start, 24,
                                 source_out=1, source_fps=24)
    plain = {frame: render(engine, frame) for frame in (6, 30)}
    adjustment(engine, start=0, duration=48, effects=[
        {"effect_id": "invert", "enabled": True, "params": {}}])
    assert frame_diff(plain[6], render(engine, 6)) > 1.0
    assert frame_diff(plain[30], render(engine, 30)) > 1.0


@pytest.mark.parametrize("fps,rate", [(24, 23.976), (30, 29.97), (24, 12.0)],
                         ids=["23.976", "29.97", "12"])
def test_fractional_rates_hold_the_adjustment_composite(fps, rate):
    engine, _base = moving(fps=fps, frames=60)
    adj = adjustment(engine, duration=48, effects=posterized(rate))
    expected = [posterize_hold(frame, {"fps": rate}, fps) for frame in range(24)]
    assert [engine.held_frame(adj, frame) for frame in range(24)] == expected
    pictures = [render(engine, frame) for frame in range(12)]
    for frame in range(1, 12):
        if expected[frame] == expected[frame - 1]:
            assert frame_diff(pictures[frame], pictures[frame - 1]) == 0.0
        else:
            assert frame_diff(pictures[frame], pictures[frame - 1]) > 0.2


def test_keyframed_rate_holds_the_adjustment_composite():
    engine, _base = moving(fps=24, frames=60)
    rate = AnimatedValue()
    rate.set_keyframe(0, 6.0)
    rate.set_keyframe(47, 24.0)
    adj = adjustment(engine, duration=48, effects=[
        {"effect_id": "posterize_time", "enabled": True, "params": {"fps": rate}}])
    samples = [engine.held_frame(adj, frame) for frame in range(48)]
    assert samples == sorted(samples), "the held composite moved backwards in time"
    assert samples[-1] > 0
    first = render(engine, 0)
    second = render(engine, 1)
    assert frame_diff(first, second) == 0.0


def test_animated_text_and_shape_beneath_a_temporal_adjustment():
    engine = EditorEngine(24)
    engine.source_width, engine.source_height = 96, 54
    engine.session.project_size = (96, 54)
    text_id = engine.session.add_text_clip("Held", 0, 48)
    shape_id = engine.session.add_shape_clip("rectangle", box=(8, 8, 28, 20),
                                             start=0, duration=48)
    text = engine.timeline.find_clip(text_id)
    shape = engine.timeline.find_clip(shape_id)
    x = AnimatedValue(); x.set_keyframe(0, 0.0); x.set_keyframe(47, 60.0)
    y = AnimatedValue(); y.set_keyframe(0, 0.0); y.set_keyframe(47, 24.0)
    text.transform["position_x"] = x
    shape.transform["position_y"] = y
    adjustment(engine, duration=48, effects=posterized(12.0))
    held = [render(engine, frame) for frame in range(3)]
    assert frame_diff(held[0], held[1]) == 0.0, (
        "text/shape animation moved inside a held adjustment frame")
    assert frame_diff(held[1], held[2]) > 0.05, "the held animation never updates"


def test_adjustment_and_clip_can_be_precomposed_together():
    engine, clip = moving(frames=60)
    adj = adjustment(engine, duration=60, effects=posterized(12.0))
    engine.session.set_selection({clip.id, adj.id})
    nested_id = engine.session.precompose("With adjustment", [clip.id, adj.id])
    assert nested_id is not None
    nested = engine.timeline.find_clip(nested_id)
    comp = engine.compositions.get(nested.composition_id)
    assert comp is not None
    assert len(comp.timeline.all_clips()) == 2
    frame = render(engine, 5)
    assert frame is not None and frame.shape == (54, 96, 3)


def test_adjustment_over_a_nested_composition_renders_once():
    engine, clip = moving(frames=60)
    engine.session.set_selection({clip.id})
    nested_id = engine.session.precompose("Inner", [clip.id])
    nested = engine.timeline.find_clip(nested_id)
    adjustment(engine, start=0, duration=nested.duration, effects=[
        {"effect_id": "invert", "enabled": True, "params": {}}])
    frame = render(engine, 5)
    assert frame is not None and frame.shape == (54, 96, 3)
    assert frame.mean() > 0


def test_adjustment_edit_invalidates_the_parent_digest():
    engine, clip = moving(frames=24)
    adj = adjustment(engine, duration=24, effects=[
        {"effect_id": "invert", "enabled": True, "params": {}}])
    before = timeline_digests(engine.timeline, engine.compositions)[adj.id][0]
    adj.effects[0]["params"]["amount"] = 1
    after = timeline_digests(engine.timeline, engine.compositions)[adj.id][0]
    assert after != before


def test_adjustment_add_is_undoable():
    engine, _base = moving(frames=24)
    before = engine.timeline.snapshot()
    adj = adjustment(engine, duration=24, effects=posterized(12.0))
    assert engine.timeline.find_clip(adj.id) is not None
    engine.session.stack.undo()
    assert engine.timeline.find_clip(adj.id) is None
    assert engine.timeline.snapshot() == before
    engine.session.stack.redo()
    assert engine.timeline.find_clip(adj.id) is not None


def test_two_levels_of_nested_compositions_render():
    engine, clip = moving(frames=60)
    engine.session.set_selection({clip.id})
    inner_id = engine.session.precompose("Inner", [clip.id])
    inner_clip = engine.timeline.find_clip(inner_id)
    engine.session.set_selection({inner_clip.id})
    outer_id = engine.session.precompose("Outer", [inner_clip.id])
    outer = engine.timeline.find_clip(outer_id)
    assert outer is not None and outer.is_composition
    assert outer.duration == engine.compositions.get(inner_clip.composition_id).duration
    frame = render(engine, 5)
    assert frame is not None and frame.shape == (54, 96, 3)
    assert frame.mean() > 0


def test_adjustment_layer_survives_the_mfxpf_round_trip(tmp_path):
    engine, _base = moving(frames=24)
    rate = AnimatedValue()
    rate.set_keyframe(0, 12.0)
    rate.set_keyframe(23, 23.976)
    adj = adjustment(engine, duration=24, effects=[
        {"effect_id": "posterize_time", "enabled": True, "params": {"fps": rate}}])
    settings = ProjectSettings(name="Adjustments", width=96, height=54,
                               fps=float(engine.timeline.fps))
    data = C.ProjectData()
    data["settings"] = settings.to_dict()
    for key, value in timeline_to_sections(engine.timeline, lambda _path: "m1").items():
        data[key] = value
    data["compositions"] = compositions_to_sections(engine.compositions,
                                                     lambda _path: "m1")
    data["media"] = {"items": []}
    data["meta"] = C.make_meta("Adjustments", settings, engine.timeline.duration,
                               len(engine.timeline.all_clips()))
    path = tmp_path / "adjustments.mfxpf"
    C.write_container(str(path), data)
    loaded = C.read_container(str(path))
    timeline = sections_to_timeline(loaded.sections, engine.timeline.fps,
                                    lambda _path: PATH)
    timeline.compositions = sections_to_compositions(
        loaded.sections.get("compositions"), lambda _path: PATH)
    restored = timeline.find_clip(adj.id)
    assert restored is not None and restored.adjustment is True
    assert restored.timeline_start == adj.timeline_start
    assert restored.duration == adj.duration
    stored = restored.effects[0]["params"]["fps"]
    assert isinstance(stored, AnimatedValue)
    assert stored.value_at(23) == pytest.approx(23.976)

    reopened = EditorEngine(24)
    reopened.source_width, reopened.source_height = 96, 54
    reopened.session.project_size = (96, 54)
    reopened.media._entries[PATH] = engine.media._entries[PATH]
    reopened.timeline = timeline
    reopened.root_timeline = timeline
    reopened.session.timeline = timeline
    assert frame_diff(render(reopened, 5), render(engine, 5)) < 0.01


def test_preview_and_export_agree_on_an_adjustment_layer(tmp_path):
    engine, _base = moving(frames=24)
    adjustment(engine, effects=[{
        "effect_id": "color_correct", "enabled": True,
        "params": {"brightness": 30.0}}])
    preview = render(engine, 7)
    target = tmp_path / "adjustment.png"
    _path, count, _fps, _size = engine.export({
        "output_path": str(target), "format": "png", "use_source_fps": True,
        "use_source_size": True, "range_start": 0, "range_end": 8,
    })
    written = cv2.imread(str(target))
    assert written is not None and count == 8
    assert frame_diff(written, preview) < 1.0, (
        "export and preview disagree about the adjustment layer")
