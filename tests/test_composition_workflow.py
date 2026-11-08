"""Pre-composition workflows beyond the basic render: assets, nesting and audio.

These pin the behaviour the Project panel, the breadcrumb and the audio mixer rely on:
inserting references, renaming/duplicating/deleting definitions as undoable edits,
folder membership surviving a save, nested audio routed once at the right time, and
cache digests that follow a composition's contents.
"""

import numpy as np
import pytest

from meenifx.audio.plan import build_segments
from meenifx.preview.invalidation import fingerprint, timeline_digests
from meenifx.project.serialize import sections_to_compositions, compositions_to_sections
from meenifx.project.settings import ProjectSettings
from tests.conftest import build_engine


def prepared():
    engine, clip = build_engine(fps=30, frames=120, duration=60)
    engine.apply_project_settings(ProjectSettings(name="Comps", width=320, height=180,
                                                  fps=30.0))
    return engine, clip


def nest(engine, clip, name="Nested"):
    """Pre-compose one clip and return (composition id, nested clip id)."""
    nested_id = engine.session.precompose(name, [clip.id])
    nested = engine.timeline.find_clip(nested_id)
    return nested.composition_id, nested_id


def tone(_path, seconds=3.0, rate=48000):
    return np.full(int(rate * seconds), 0.5, dtype=np.float32)


# --------------------------------------------------------------------- assets
def test_insert_composition_is_one_undoable_reference():
    engine, clip = prepared()
    comp_id, nested_id = nest(engine, clip)
    extra_id = engine.session.insert_composition(comp_id, 90)
    assert extra_id is not None
    extra = engine.timeline.find_clip(extra_id)
    assert extra.is_composition and extra.timeline_start == 90
    assert extra.composition_id == comp_id
    assert engine.session.composition_reference_count(comp_id) == 2
    engine.session.stack.undo()
    assert engine.timeline.find_clip(extra_id) is None
    engine.session.stack.redo()
    assert engine.timeline.find_clip(extra_id) is not None


def test_reference_count_includes_nested_references():
    engine, clip = prepared()
    comp_id, _nested_id = nest(engine, clip)
    assert engine.session.composition_reference_count(comp_id) == 1
    # Put the nested clip inside another composition: it is still a reference.
    inner_id = engine.timeline.find_clip(_nested_id).id
    engine.session.precompose("Outer", [inner_id])
    assert engine.session.composition_reference_count(comp_id) == 1
    outer = next(c for c in engine.compositions.all() if c.name == "Outer")
    assert engine.session.composition_reference_count(outer.id) == 1


def test_rename_updates_every_referencing_clip_and_undoes():
    engine, clip = prepared()
    comp_id, nested_id = nest(engine, clip)
    assert engine.session.rename_composition(comp_id, "Renamed")
    comp = engine.compositions.get(comp_id)
    assert comp.name == "Renamed"
    assert engine.timeline.find_clip(nested_id).name == "Renamed"
    engine.session.stack.undo()
    assert comp.name == "Nested"
    assert engine.timeline.find_clip(nested_id).name == "Nested"


def test_duplicate_and_delete_are_undoable():
    engine, clip = prepared()
    comp_id, nested_id = nest(engine, clip)
    before = len(engine.compositions)
    copy = engine.session.duplicate_composition(comp_id)
    assert copy is not None and copy.id != comp_id
    assert len(engine.compositions) == before + 1

    assert engine.session.delete_composition(comp_id)
    assert engine.compositions.get(comp_id) is None
    assert engine.timeline.find_clip(nested_id) is None
    engine.session.stack.undo()
    assert engine.compositions.get(comp_id) is not None
    assert engine.timeline.find_clip(nested_id) is not None


def test_composition_settings_are_one_undoable_edit():
    engine, clip = prepared()
    comp_id, nested_id = nest(engine, clip)
    comp = engine.compositions.get(comp_id)
    before = (comp.width, comp.height, comp.fps)
    assert engine.session.set_composition_settings(
        comp_id, name="Wider", width=640, height=360, fps=25, declared_duration=90)
    assert (comp.width, comp.height, comp.fps) == (640, 360, 25)
    assert comp.timeline.fps == 25
    assert comp.declared_duration == 90
    assert engine.timeline.find_clip(nested_id).name == "Wider"
    engine.session.stack.undo()
    assert (comp.width, comp.height, comp.fps) == before
    assert comp.name == "Nested"


def test_bin_folder_survives_the_project_file():
    engine, clip = prepared()
    comp_id, _nested_id = nest(engine, clip)
    comp = engine.compositions.get(comp_id)
    comp.bin_folder = "bin_comps"
    rows = compositions_to_sections(engine.compositions, lambda _path: "m1")
    loaded = sections_to_compositions(rows, lambda _media_id: clip.source_path)
    assert loaded.get(comp_id).bin_folder == "bin_comps"


def test_insert_composition_into_a_nested_timeline_uses_the_project_library():
    engine, clip = prepared()
    comp_id, _nested_id = nest(engine, clip)
    comp = engine.compositions.get(comp_id)
    # Edit the composition itself: the session's library must still be available.
    engine.session.timeline = comp.timeline
    engine.session.root_timeline = engine.root_timeline
    inner_id = engine.session.insert_composition(comp_id, 0, None)
    assert inner_id is not None
    assert comp.timeline.find_clip(inner_id) is not None


# ---------------------------------------------------------------------- audio
def test_nested_audio_is_shifted_once_and_kept_in_sync():
    engine, clip = prepared()
    clip.source_path = "tone.wav"
    clip.stream = "both"       # an imported video that carries its own sound
    comp_id, nested_id = nest(engine, clip)
    # Move the reference pre-compose already made instead of adding a second one.
    nested = engine.timeline.find_clip(nested_id)
    nested.timeline_start = 90
    calls = []

    def audio_for(path):
        calls.append(path)
        return tone(path)

    segments = build_segments(engine.timeline, audio_for, 48000,
                              compositions=engine.compositions)
    assert len(segments) == 1, "a nested composition must not play twice"
    assert segments[0].start_seconds == pytest.approx(3.0)
    assert segments[0].end_seconds == pytest.approx(5.0)
    assert calls == ["tone.wav"]
    assert engine.timeline.find_clip(nested_id) is not None


def test_nested_audio_respects_both_volume_levels_and_fades():
    engine, clip = prepared()
    clip.source_path = "tone.wav"
    clip.stream = "both"
    clip.volume = 0.5
    comp_id, nested_id = nest(engine, clip)
    nested_clip = engine.timeline.find_clip(nested_id)
    nested_clip.volume = 0.5
    nested_clip.fade_in = 15       # half a second at 30 fps

    segments = build_segments(engine.timeline, tone, 48000,
                              compositions=engine.compositions)
    assert len(segments) == 1
    points = segments[0].gain_points
    assert points
    values = np.asarray([value for _time, value in points])
    # 0.5 (child clip) x 0.5 (composition clip) = 0.25 at full level.
    assert float(values.max()) == pytest.approx(0.25, abs=1e-3)
    # The parent's fade starts at silence and reaches full level.
    assert float(values[0]) == pytest.approx(0.0, abs=1e-3)


def test_two_levels_of_nesting_route_audio_without_duplicates():
    engine, clip = prepared()
    clip.source_path = "tone.wav"
    clip.stream = "both"
    comp_id, nested_id = nest(engine, clip)
    outer_id = engine.session.precompose("Outer", [nested_id])
    outer = engine.timeline.find_clip(outer_id)
    assert outer.composition_id
    segments = build_segments(engine.timeline, tone, 48000,
                              compositions=engine.compositions)
    assert len(segments) == 1
    assert segments[0].start_seconds == pytest.approx(0.0)
    assert segments[0].end_seconds == pytest.approx(2.0)


def test_audio_cycle_guard_stops_recursion():
    engine, clip = prepared()
    clip.source_path = "tone.wav"
    clip.stream = "both"
    comp_id, _nested_id = nest(engine, clip)
    comp = engine.compositions.get(comp_id)
    lane = comp.timeline.tracks_of_type("video")[0]
    # A composition that (illegally) contains a clip referencing itself must not hang.
    comp.timeline.new_clip("self", "", lane, 0, 10, source_out=10,
                           composition_id=comp.id)
    segments = build_segments(comp.timeline, tone, 48000, compositions=engine.compositions,
                              _stack=(comp.id,))
    # The guard stops the self-reference; the composition's real audio still plays once.
    assert len(segments) == 1


# ----------------------------------------------------------------- invalidation
def test_editing_a_nested_clip_invalidates_the_parent_frames():
    engine, clip = prepared()
    comp_id, nested_id = nest(engine, clip)
    before = timeline_digests(engine.timeline, engine.compositions)[nested_id][0]
    old_print = fingerprint(engine.timeline, None, None, engine.compositions)
    inner = engine.compositions.get(comp_id).timeline.all_clips()[0]
    inner.timeline_start = 5
    after = timeline_digests(engine.timeline, engine.compositions)[nested_id][0]
    assert after != before
    assert fingerprint(engine.timeline, None, None,
                       engine.compositions) != old_print


def test_an_unreferenced_composition_does_not_invalidate_the_root():
    engine, clip = prepared()
    _comp_id, nested_id = nest(engine, clip)
    before = timeline_digests(engine.timeline, engine.compositions)[nested_id][0]
    other = engine.compositions.new("Unrelated")
    other.timeline.add_track("video")
    after = timeline_digests(engine.timeline, engine.compositions)[nested_id][0]
    assert after == before
