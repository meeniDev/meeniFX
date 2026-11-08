"""Duplicating a clip copies its whole editable state, as an independent object.

The duplicate command round-trips a clipboard record through the timeline, so this file
checks every editable field that record has to carry: transform, effects and their
animations, presets, generated text/shape data, audio settings, links, undo/redo, nested
composition references and the .mfxpf round trip.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.model import preset_library as LIB  # noqa: E402
from meenifx.model.keyframes import AnimatedValue, Keyframe  # noqa: E402
from meenifx.project import container as C  # noqa: E402
from meenifx.project.serialize import (compositions_to_sections,  # noqa: E402
                                       sections_to_compositions, sections_to_timeline,
                                       timeline_to_sections)
from meenifx.project.settings import ProjectSettings  # noqa: E402
from tests.conftest import build_engine  # noqa: E402

SOURCE = "synthetic-effect-test.png"
FIELDS = ("name", "source_path", "duration", "source_in", "source_out",
          "source_fps", "timeline_fps", "speed", "reverse", "loop", "freeze",
          "source_offset", "source_min", "source_max", "effects", "volume", "fade_in",
          "fade_out", "fade_in_curve", "fade_out_curve", "stream", "transform",
          "transform_meta", "text", "gain", "presets", "blend", "adjustment", "shape",
          "composition_id")


def plain(value):
    """Deep, comparable form of a stored value (AnimatedValue has no __eq__)."""
    if isinstance(value, AnimatedValue):
        return ("animated", value.base_value,
                tuple((key.time, key.value, key.interpolation, key.in_influence,
                       key.out_influence, key.in_value_handle, key.out_value_handle)
                      for key in value.keyframes))
    if isinstance(value, dict):
        return {name: plain(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def editable(clip):
    """Every editable clip field, in a form two clips can be compared with."""
    return {name: plain(getattr(clip, name)) for name in FIELDS}


def curve(keys, base=None):
    """An AnimatedValue from (frame, value, interpolation) triples."""
    return AnimatedValue([Keyframe(time, value, interpolation) for time, value, interpolation in keys],
                         keys[0][1] if base is None else base)


def bezier_curve():
    """A curve that also exercises the easing handles a plain Keyframe call leaves out."""
    return AnimatedValue([Keyframe(0, 0.0, "bezier", 0.2, 0.4, 0.1, 0.9),
                          Keyframe(20, 80.0, "linear")], 0.0)


def duplicate(engine, ids):
    """Select `ids`, duplicate, and return only the newly created clips."""
    before = {clip.id for clip in engine.timeline.all_clips()}
    engine.session.set_selection(set(ids))
    engine.session.duplicate_selected()
    return [clip for clip in engine.timeline.all_clips() if clip.id not in before]


def decorated_engine():
    """One video clip carrying every kind of editable state a picture clip can hold."""
    engine, clip = build_engine(frames=60, duration=40)
    clip.name = "Picture"
    clip.speed = 1.25
    clip.reverse = True
    clip.loop = True
    clip.source_in = 3
    clip.source_out = 50
    clip.source_min = 2
    clip.source_max = 55
    clip.source_offset = 1.5
    clip.transform.update({"position_x": bezier_curve(), "position_y": 12.0,
                           "scale": 1.35, "rotation": 12.5, "opacity": 0.55,
                           "anchor_x": 25.0, "anchor_y": -10.0})
    clip.transform_meta = {"position": {"in": [0.1, 0.2], "out": [0.3, 0.4]},
                           "rotation": {"in": [0.2, 0.0], "out": [0.8, 0.0]}}
    clip.effects = [
        {"effect_id": "mosaic", "enabled": True,
         "params": {"scale": curve([(0, 24.0, "linear"), (15, 60.0, "bezier")], 24.0),
                    "colors": 16}},
        {"effect_id": "puppet_warp", "enabled": False, "params": {"strength": 0.4},
         "pins": [{"id": 1, "src": [0.3, 0.4], "locked": False, "y": 0.4,
                   "x": curve([(0, 0.3, "linear"), (10, 0.5, "smooth")], 0.3)}]},
    ]
    clip.blend = "screen"
    clip.adjustment = True
    clip.volume = 0.5
    clip.gain = curve([(0, -6.0, "linear"), (25, 0.0, "bezier")], -6.0)
    clip.fade_in, clip.fade_out = 5, 6
    clip.fade_in_curve, clip.fade_out_curve = 0.5, -0.5
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()["clip_in_fade"], "whole")
    return engine, clip


def write_project(engine, path):
    """Save the live timeline (and compositions) as a real .mfxpf and load it back."""
    settings = ProjectSettings(name="Duplicates", width=320, height=180,
                               fps=float(engine.timeline.fps))
    data = C.ProjectData()
    data["settings"] = settings.to_dict()
    for key, value in timeline_to_sections(engine.timeline, lambda _path: "m1").items():
        data[key] = value
    data["compositions"] = compositions_to_sections(engine.compositions, lambda _path: "m1")
    data["media"] = {"items": []}
    data["meta"] = C.make_meta("Duplicates", settings, engine.timeline.duration,
                               len(engine.timeline.all_clips()))
    C.write_container(str(path), data)

    loaded = C.read_container(str(path))
    path_for = lambda _media_id: SOURCE
    timeline = sections_to_timeline(loaded.sections, engine.timeline.fps, path_for)
    timeline.compositions = sections_to_compositions(
        loaded.sections.get("compositions"), path_for)
    return timeline


# ------------------------------------------------------------------ state fidelity
def test_duplicate_copies_the_whole_editable_state():
    engine, clip = decorated_engine()
    original = editable(clip)

    dup, = duplicate(engine, [clip.id])

    assert dup.id != clip.id
    assert dup.timeline_start == clip.end, "duplicate lands after the original"
    assert editable(dup) == original
    assert clip.blend == "screen" and clip.adjustment is True
    assert dup.effects is not clip.effects
    assert dup.effects[0] is not clip.effects[0]
    assert dup.effects[1]["pins"][0] is not clip.effects[1]["pins"][0]
    assert dup.presets is not clip.presets


def test_duplicate_text_and_shape_clips_keep_generated_content():
    engine, _clip = build_engine(frames=60, duration=40)
    engine.session.project_size = (320, 180)
    text = engine.timeline.find_clip(engine.session.add_text_clip("HELLO", 0, 40))
    text.text["text"] = "Duplicate me"
    text.text["font"] = "Georgia"
    text.text["color"] = "#22CC88"
    text.text["size"] = curve([(0, 24.0, "linear"), (18, 90.0, "bezier")], 24.0)
    text.text["keys"] = {0: "One", 12: "Two"}
    shape = engine.timeline.find_clip(
        engine.session.add_shape_clip("ellipse", box=(40, 30, 120, 80)))
    shape.shape["width"] = curve([(0, 60.0, "linear"), (20, 140.0, "bezier")], 60.0)
    shape.shape["fill_color"] = "#ff8844"
    shape.shape["stroke_color"] = "#223344"
    shape.transform["opacity"] = 0.7

    for source in (text, shape):
        dup, = duplicate(engine, [source.id])
        assert dup.id != source.id
        assert editable(dup) == editable(source)
        if source.is_text:
            assert dup.text is not source.text
        if source.is_shape:
            assert dup.shape is not source.shape


def test_duplicate_audio_clip_keeps_volume_gain_fades_and_effects():
    engine, _clip = build_engine(frames=60, duration=40)
    track = engine.timeline.add_track("audio")
    audio = engine.timeline.new_clip(
        "Sound", SOURCE, track, 0, 40, source_out=60, source_fps=30, stream="audio",
        volume=0.65, gain=curve([(0, -8.0, "linear"), (20, -2.0, "bezier")], -8.0),
        fade_in=5, fade_out=7, fade_in_curve=0.5, fade_out_curve=-0.5,
        effects=[{"effect_id": "audio_reverb", "enabled": True, "params": {"mix": 0.3}},
                 {"effect_id": "audio_eq", "enabled": False, "params": {"low": -2.0}}])

    dup, = duplicate(engine, [audio.id])

    assert dup.timeline_start == audio.end
    assert editable(dup) == editable(audio)
    assert dup.volume == audio.volume
    for local in (0, 10, 25, 39):
        assert (dup.gain_db_at(dup.timeline_start + local) ==
                audio.gain_db_at(audio.timeline_start + local))
        assert (dup.fade_gain_at(dup.timeline_start + local) ==
                audio.fade_gain_at(audio.timeline_start + local))


def test_editing_a_duplicate_leaves_the_original_untouched():
    engine, clip = decorated_engine()
    original = editable(clip)

    dup, = duplicate(engine, [clip.id])
    dup.effects[0]["params"]["scale"].keyframes[0].value = 9.0
    dup.effects[1]["pins"][0]["x"].keyframes[0].value = 0.9
    dup.transform["position_x"].keyframes[0].value = 5.0
    dup.presets.clear()
    dup.gain.keyframes[0].value = 12.0

    assert editable(clip) == original
    assert dup.effects[0]["params"]["scale"].keyframes[0].value == 9.0
    assert dup.transform["position_x"].keyframes[0].value == 5.0


def test_duplicate_renders_like_the_original_at_the_same_local_time():
    engine, clip = build_engine(frames=60, duration=40)
    clip.effects = [{"effect_id": "mosaic", "enabled": True,
                     "params": {"scale": curve([(0, 20.0, "linear"), (30, 48.0, "smooth")],
                                               20.0)}}]
    clip.transform["rotation"] = 15.0
    clip.transform["position_x"] = bezier_curve()

    dup, = duplicate(engine, [clip.id])

    for local in (0, 10, 25, 39):
        raw_a, frame_a = engine.render_frame(clip.timeline_start + local, 0.5)
        raw_b, frame_b = engine.render_frame(dup.timeline_start + local, 0.5)
        assert np.array_equal(raw_a, raw_b)
        assert np.array_equal(frame_a, frame_b)


# ------------------------------------------------------------------------- links
def test_duplicating_a_linked_pair_makes_a_new_independent_link_group():
    engine, video = build_engine(frames=60, duration=40)
    audio_track = engine.timeline.add_track("audio")
    audio = engine.timeline.new_clip("Sound", SOURCE, audio_track, 2, 38,
                                     source_out=60, source_fps=30, stream="audio",
                                     volume=0.8)
    link_id = engine.timeline.assign_link([video.id, audio.id])
    assert link_id

    before = {clip.id for clip in engine.timeline.all_clips()}
    engine.session.set_selection({video.id})  # Linked Selection pulls in the audio partner
    engine.session.duplicate_selected()
    dups = [clip for clip in engine.timeline.all_clips() if clip.id not in before]

    assert len(dups) == 2
    dup_video = next(clip for clip in dups if clip.track.type == "video")
    dup_audio = next(clip for clip in dups if clip.track.type == "audio")
    assert dup_video.link_id == dup_audio.link_id
    assert dup_video.link_id != link_id, "duplicates must not join the original pair"
    assert (dup_video.timeline_start - dup_audio.timeline_start ==
            video.timeline_start - audio.timeline_start)
    assert dup_video.timeline_start == max(video.end, audio.end)
    assert editable(dup_video) == editable(video)
    assert editable(dup_audio) == editable(audio)


# ------------------------------------------------------------------- undo / redo
def test_duplicate_is_one_undo_step_that_restores_everything():
    engine, clip = decorated_engine()
    audio_track = engine.timeline.add_track("audio")
    audio = engine.timeline.new_clip("Sound", SOURCE, audio_track, 0, 40,
                                     source_out=60, source_fps=30, stream="audio",
                                     volume=0.75)
    engine.timeline.assign_link([clip.id, audio.id])
    before = {clip.id: editable(clip) for clip in engine.timeline.all_clips()}

    engine.session.set_selection({clip.id})
    engine.session.duplicate_selected()
    dups = [clip for clip in engine.timeline.all_clips() if clip.id not in before]
    assert len(dups) == 2
    after = {clip.id: editable(clip) for clip in dups}
    dup_link = dups[0].link_id

    assert engine.session.stack.undo() == "Duplicate"
    assert {clip.id for clip in engine.timeline.all_clips()} == set(before)
    assert {clip.id: editable(clip) for clip in engine.timeline.all_clips()} == before

    engine.session.stack.redo()
    restored = {clip.id: editable(clip) for clip in engine.timeline.all_clips()
                if clip.id in after}
    assert restored == after
    assert all(clip.link_id == dup_link for clip in engine.timeline.all_clips()
               if clip.id in after)


# ------------------------------------------------------- compositions, save/reopen
def test_duplicate_nested_composition_clip_keeps_its_shared_reference(tmp_path):
    engine, clip = build_engine(frames=60, duration=40)
    engine.apply_project_settings(ProjectSettings(name="Comps", width=320, height=180,
                                                  fps=30.0))
    nested = engine.timeline.find_clip(engine.session.precompose("Shared", [clip.id]))

    dup, = duplicate(engine, [nested.id])

    assert dup.composition_id == nested.composition_id
    assert dup.duration == nested.duration
    assert len(engine.compositions) == 1, "duplicating a reference must not copy the composition"

    timeline = write_project(engine, tmp_path / "nested.mfxpf")
    assert len(timeline.compositions) == 1
    restored = timeline.find_clip(dup.id)
    assert restored is not None and restored.composition_id == dup.composition_id


def test_duplicates_survive_the_mfxpf_round_trip(tmp_path):
    engine, clip = decorated_engine()
    engine.session.project_size = (320, 180)
    duplicate(engine, [clip.id])
    text = engine.timeline.find_clip(engine.session.add_text_clip("Round Trip", 0, 40))
    text.text["size"] = curve([(0, 22.0, "linear"), (15, 64.0, "bezier")], 22.0)
    text.text["font"] = "Georgia"
    duplicate(engine, [text.id])
    shape = engine.timeline.find_clip(
        engine.session.add_shape_clip("star", box=(30, 20, 120, 100)))
    shape.shape["width"] = curve([(0, 50.0, "linear"), (10, 120.0, "bezier")], 50.0)
    duplicate(engine, [shape.id])

    expected = {clip.id: editable(clip) for clip in engine.timeline.all_clips()}
    starts = {clip.id: clip.timeline_start for clip in engine.timeline.all_clips()}
    links = {clip.id: clip.link_id for clip in engine.timeline.all_clips()}

    timeline = write_project(engine, tmp_path / "duplicates.mfxpf")

    assert {clip.id: editable(clip) for clip in timeline.all_clips()} == expected
    assert {clip.id: clip.timeline_start for clip in timeline.all_clips()} == starts
    assert {clip.id: clip.link_id for clip in timeline.all_clips()} == links
