"""Animation presets: evaluation, stacking, determinism, baking and persistence.

The render tests go through the real compositor, so they cover the thing that matters:
a preset is a layer on top of the clip's own transform, it is deterministic, and a clip
at rest renders exactly what it rendered with no preset at all.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import build_engine  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import presets as P  # noqa: E402
from meenifx.model import text as TXT  # noqa: E402
from meenifx.model import transform as T  # noqa: E402
from meenifx.model.keyframes import AnimatedValue  # noqa: E402


def text_engine(text="HELLO", start=0, duration=60):
    """An engine holding one text clip (and the synthetic clip underneath it)."""
    engine, base = build_engine()
    track = engine.timeline.add_track("video")
    engine.session.set_playhead(start)
    text_id = engine.session.add_text_clip(text, start=start, duration=duration, track=track)
    assert text_id is not None
    return engine, engine.timeline.find_clip(text_id)


def clip_engine(duration=60):
    engine, clip = build_engine(duration=duration)
    clip.duration = duration
    return engine, clip


def frame_of(engine, frame_no, scale=1.0):
    return engine.render_frame(frame_no, scale)[1]


# ------------------------------------------------------------------ the model
def test_an_instance_adds_to_the_clip_transform_instead_of_replacing_it():
    engine, clip = clip_engine()
    T.set_channel(clip, "position_x", 40.0, 0, False)
    P.add(clip, "shake", "loop", program="shake", knobs={"intensity": 1.0}, duration=60,
          name="Shake")
    effect = P.evaluate(clip, 10, project_size=(1920, 1080))
    assert abs(effect.pose.dx) > 0.0, "the shake has to move the picture"
    state = T.evaluate(clip, 10)
    assert state.position_x == 40.0, "the clip's own position stays untouched"
    # The compositor adds the two: the clip transform is still 40 plus the shake, and the
    # shake never grows into a re-position of its own.
    assert 0.1 < abs(effect.pose.dx) < 40.0


def test_in_loop_and_out_stack_on_one_clip():
    engine, clip = clip_engine(duration=90)
    P.add(clip, "fade", "in", program="fade", duration=15, name="Fade")
    P.add(clip, "shake", "loop", program="shake", duration=90, name="Shake")
    P.add(clip, "zoom", "out", program="zoom", args={"from": 0.5}, duration=15, name="Zoom out")
    first = P.evaluate(clip, 0, project_size=(1920, 1080))
    middle = P.evaluate(clip, 45, project_size=(1920, 1080))
    last = P.evaluate(clip, 89, project_size=(1920, 1080))
    assert first.pose.opacity < 0.05, "the fade-in starts hidden"
    assert abs(middle.pose.opacity - 1.0) < 0.05, "the middle frame is at full opacity"
    assert abs(middle.pose.scale_x - 1.0) < 0.02, "nothing zooms in the middle of the clip"
    assert last.pose.scale_x < 0.9, "the zoom-out has to shrink the picture at the end"


def test_the_same_seed_gives_the_same_frames_every_time():
    engine, clip = clip_engine(duration=120)
    P.add(clip, "wiggle", "loop", program="wiggle", knobs={"seed": 41}, duration=120)
    first = [P.evaluate(clip, frame, project_size=(1280, 720)).pose.key()
             for frame in range(0, 60, 3)]
    # A second clip with the same instance must evaluate identically, frame by frame.
    engine2, other = clip_engine(duration=120)
    P.add(other, "wiggle", "loop", program="wiggle", knobs={"seed": 41}, duration=120)
    second = [P.evaluate(other, frame, project_size=(1280, 720)).pose.key()
              for frame in range(0, 60, 3)]
    assert first == second
    P.set_knob(P.instances(other)[0], "seed", 42)
    third = [P.evaluate(other, frame, project_size=(1280, 720)).pose.key()
             for frame in range(0, 60, 3)]
    assert first != third, "a different seed has to give a different motion"


def test_an_instance_is_idle_outside_its_window():
    engine, clip = clip_engine(duration=90)
    instance = P.add(clip, "pop", "in", program="pop", duration=20, name="Pop")
    assert not P.active_at(instance, 40, 90)
    assert P.evaluate(clip, 40).pose.is_identity()
    assert P.active_at(instance, 10, 90)


def test_an_out_instance_is_anchored_to_the_clip_end():
    engine, clip = clip_engine(duration=90)
    instance = P.add(clip, "fade", "out", program="fade", duration=20)
    start, length = P.window(instance, 90)
    assert (start, length) == (70, 20)


# --------------------------------------------------------------- rendering
def test_a_finished_preset_renders_exactly_like_no_preset_at_all():
    engine, clip = text_engine(text="QUIET")
    rest = frame_of(engine, 30).copy()
    P.add(clip, "pop", "in", program="pop", duration=10, knobs={"unit": "whole"})
    assert P.evaluate(clip, 30).pose.is_identity(), "frame 30 is past the In window"
    assert np.array_equal(frame_of(engine, 30), rest), "at rest the picture must not move"


def test_a_running_preset_changes_the_picture():
    engine, clip = text_engine(text="QUIET")
    rest = frame_of(engine, 6).copy()
    P.add(clip, "slide", "in", program="slide", args={"dir": "left"}, duration=20,
          knobs={"unit": "whole"})
    moved = frame_of(engine, 6)
    assert not np.array_equal(moved, rest)


def test_preview_and_export_agree_for_a_noisy_loop():
    engine, clip = clip_engine(duration=90)
    P.add(clip, "camera_shake", "loop", program="camera_shake",
          knobs={"seed": 5, "intensity": 1.0}, duration=90)
    for frame in (10, 25, 40):
        preview = frame_of(engine, frame, scale=0.5)
        export = frame_of(engine, frame, scale=1.0)
        assert preview.shape[:2] == (export.shape[0] // 2, export.shape[1] // 2)


def test_the_cache_digest_notices_a_preset():
    from meenifx.preview import invalidation
    engine, clip = clip_engine()
    before = invalidation.clip_digest(clip)
    P.add(clip, "shake", "loop", program="shake", duration=30)
    after = invalidation.clip_digest(clip)
    assert before != after
    P.set_knob(P.instances(clip)[0], "seed", 999)
    assert invalidation.clip_digest(clip) != after


# ------------------------------------------------------------------ baking
def test_bake_writes_keyframes_and_removes_the_instance():
    engine, clip = clip_engine(duration=60)
    instance = P.add(clip, "slide", "in", program="slide", args={"dir": "left"},
                     duration=20, knobs={"unit": "whole"}, name="Slide")
    before = [P.evaluate(clip, frame, project_size=(640, 360)).pose.key()
              for frame in range(0, 21)]
    written = P.bake(clip, instance, project_size=(640, 360))
    assert "transform.position_x" in written
    assert not P.instances(clip), "the instance is gone once baked"
    for frame in range(0, 21):
        x = CH.value_at(clip, "transform.position_x", frame)
        # The baked curve has to evaluate to the same offset the preset produced.
        assert abs(float(x) - before[frame][0]) < 0.01, f"baked frame {frame} must match"


def test_baking_bounce_keeps_the_vertical_motion():
    """Regression: Bounce bakes position_y even though position_x stays static.

    position_at() used to read a static X and then return a raw Y, so the freshly baked
    Y curve evaluated as 0 and the subject stayed vertically centred after Bake.
    """
    engine, clip = clip_engine(duration=60)
    instance = P.add(clip, "bounce", "in", program="bounce", args={"px": 1.0},
                     knobs={"unit": "whole"}, duration=30, name="Bounce")
    before = [P.evaluate(clip, frame, project_size=(640, 360)).pose
              for frame in range(0, 31)]
    written = P.bake(clip, instance, project_size=(640, 360))
    assert "transform.position_y" in written
    assert "transform.opacity" in written
    assert not P.instances(clip)
    for frame, pose in enumerate(before):
        y = float(CH.value_at(clip, "transform.position_y", frame))
        opacity = float(CH.value_at(clip, "transform.opacity", frame))
        assert abs(y - pose.dy) < 0.01, f"baked Y at frame {frame} must match Bounce"
        assert abs(opacity - pose.opacity * 100.0) < 0.01
    assert T.position_at(clip, 0)[1] < T.position_at(clip, 30)[1] - 20.0, \
        "the first frame must visibly sit above the resting frame"


def test_bake_is_one_undo_step():
    engine, clip = clip_engine(duration=60)
    instance = P.add(clip, "fade", "in", program="fade", duration=15)
    snapshot = engine.timeline.snapshot()
    P.bake(clip, instance, project_size=(640, 360))
    assert not P.instances(clip)
    assert isinstance(CH.stored(clip, "transform.opacity"), AnimatedValue)
    engine.timeline.restore(snapshot)
    restored = engine.timeline.find_clip(clip.id)
    assert len(P.instances(restored)) == 1
    assert not isinstance(CH.stored(restored, "transform.opacity"), AnimatedValue)


def test_per_unit_instances_report_bake_as_unavailable():
    engine, clip = text_engine()
    instance = P.add(clip, "pop", "in", program="pop", knobs={"unit": "letter"})
    assert not P.bakeable(instance, clip)


# ----------------------------------------------------------- persistence
def test_presets_survive_a_project_round_trip():
    from meenifx.project import MediaLibrary
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = clip_engine(duration=60)
    P.add(clip, "shake", "loop", program="shake", args={"amp": 0.01},
          knobs={"seed": 12, "intensity": 0.8, "unit": "whole"}, duration=40, name="Shake")
    library = MediaLibrary()
    sections = timeline_to_sections(engine.timeline, lambda path: library.add(path).id)
    restored = sections_to_timeline(sections, 30, lambda mid: library.items[mid].path)
    loaded = restored.find_clip(clip.id)
    assert len(P.instances(loaded)) == 1
    row = P.instances(loaded)[0]
    assert row["preset"] == "shake" and row["category"] == "loop"
    assert row["duration"] == 40 and row["name"] == "Shake"
    assert row["knobs"]["seed"] == 12
    assert row["knobs"]["intensity"] == pytest.approx(0.8)
    assert row["args"] == {"amp": 0.01}
    assert P.evaluate(loaded, 20, project_size=(640, 360)).pose.key() == \
        P.evaluate(clip, 20, project_size=(640, 360)).pose.key()


def test_a_split_keeps_the_presets_on_both_halves():
    engine, clip = clip_engine(duration=90)
    P.add(clip, "fade", "in", program="fade", duration=20)
    P.add(clip, "shake", "loop", program="shake", duration=90)
    P.add(clip, "fade2", "out", program="fade", duration=20)
    left, right = P.split_instances(P.instances(clip), 40, 40, 50)
    assert [row["category"] for row in left] == ["in", "loop"]
    assert [row["category"] for row in right] == ["loop", "out"]
