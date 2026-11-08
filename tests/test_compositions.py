"""Pre-compositions: model, undo, nested rendering and project round-trip."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.model.timeline import Timeline  # noqa: E402
from meenifx.project.serialize import (compositions_to_sections,  # noqa: E402
                                       sections_to_compositions, sections_to_timeline,
                                       timeline_to_sections)
from meenifx.project.settings import ProjectSettings  # noqa: E402
from tests.conftest import build_engine  # noqa: E402


def prepared_engine():
    engine, clip = build_engine(fps=30)
    engine.apply_project_settings(ProjectSettings(name="Comps", width=320, height=180,
                                                  fps=30.0))
    return engine, clip


def test_precompose_replaces_clips_with_one_stable_reference():
    engine, clip = prepared_engine()
    ids = [clip.id]
    nested_id = engine.session.precompose("Intro", ids)
    assert nested_id is not None
    nested = engine.timeline.find_clip(nested_id)
    assert nested is not None and nested.is_composition
    assert engine.timeline.find_clip(clip.id) is None
    comp = engine.compositions.get(nested.composition_id)
    assert comp is not None and comp.name == "Intro"
    assert len(comp.timeline.all_clips()) == 1
    assert comp.timeline.all_clips()[0].timeline_start == 0
    assert comp.duration == clip.duration
    assert nested.duration == clip.duration
    assert nested.source_out == comp.duration


def test_precompose_is_one_undo_step_and_restores_definition():
    engine, clip = prepared_engine()
    original = engine.timeline.snapshot()
    engine.session.precompose("Undo me", [clip.id])
    assert len(engine.compositions) == 1
    engine.session.stack.undo()
    assert len(engine.compositions) == 0
    assert engine.timeline.find_clip(clip.id) is not None
    assert engine.timeline.snapshot() == original


def test_nested_composition_renders_without_flattening_its_source():
    engine, clip = prepared_engine()
    nested_id = engine.session.precompose("Visible", [clip.id])
    nested = engine.timeline.find_clip(nested_id)
    _raw, frame = engine.render_frame(0, 0.5)
    assert frame.shape == (90, 160, 3)
    assert frame.mean() > 1.0
    assert engine.timeline.find_clip(clip.id) is None
    comp = engine.compositions.get(nested.composition_id)
    assert len(comp.timeline.all_clips()) == 1


def test_composition_sections_round_trip_with_reference():
    engine, clip = prepared_engine()
    nested_id = engine.session.precompose("Round trip", [clip.id])
    nested = engine.timeline.find_clip(nested_id)
    path_for = lambda _media_id: clip.source_path
    main = sections_to_timeline(timeline_to_sections(engine.timeline, lambda _p: "m1"),
                                30, path_for)
    comps = sections_to_compositions(
        compositions_to_sections(engine.compositions, lambda _p: "m1"), path_for)
    loaded = Timeline(30)
    loaded.adopt(main)
    loaded.compositions = comps
    loaded_nested = loaded.find_clip(nested_id)
    assert loaded_nested is not None and loaded_nested.is_composition
    comp = comps.get(loaded_nested.composition_id)
    assert comp is not None and comp.name == "Round trip"
    assert len(comp.timeline.all_clips()) == 1
    assert comp.timeline.all_clips()[0].source_path == clip.source_path


def test_cycles_are_rejected():
    engine, clip = prepared_engine()
    nested_id = engine.session.precompose("A", [clip.id])
    outer = engine.compositions.get(engine.timeline.find_clip(nested_id).composition_id)
    assert engine.compositions.would_cycle(outer.id, outer.id)
    assert not engine.compositions.would_cycle(outer.id, "missing")
