"""Transparent pre-compositions must keep real alpha all the way to the parent.

The renderer now uses a float RGBA canvas for a composition whose background is
transparent. These tests put a cutout composition over a lower clip and check the lower
picture at a transparent pixel, the subject at an opaque pixel, legacy opaque behaviour,
multiple nesting levels and the serialized flag.
"""

import os

import cv2
import numpy as np

from meenifx.engine import EditorEngine
from meenifx.media.cache import MediaEntry
from meenifx.model import shape as SHAPE
from meenifx.model import text as TXT
from meenifx.preview.invalidation import composition_digest
from meenifx.project.serialize import (compositions_to_sections,
                                       sections_to_compositions)
from meenifx.project.settings import ProjectSettings

WIDTH = HEIGHT = 64
LOWER = [200, 40, 20]
SUBJECT = [20, 200, 40]


def _entry(path, picture, frames=60):
    height, width = picture.shape[:2]
    entry = MediaEntry("video", frames, 30)
    entry.frames = [picture.copy() for _ in range(frames)]
    return entry, width, height


def _cutout():
    picture = np.zeros((HEIGHT, WIDTH, 4), np.uint8)
    picture[:, :, :3] = (255, 0, 255)  # must never show through zero alpha
    picture[16:48, 16:48, :3] = SUBJECT
    picture[16:48, 16:48, 3] = 255
    return picture


def _engine():
    """One upper clip; a lower clip has to be added after pre-composing.

    Pre-compose deliberately refuses to leave an overlapping clip behind, so the test
    builds the transparent composition first and then adds its parent backdrop.
    """
    engine = EditorEngine(30)
    engine.apply_project_settings(ProjectSettings(name="Alpha", width=WIDTH,
                                                  height=HEIGHT, fps=30.0))
    upper_entry, _w, _h = _entry("cutout.png", _cutout())
    engine.media._entries["cutout.png"] = upper_entry
    upper_track = engine.timeline.add_track("video")
    upper_clip = engine.timeline.new_clip("Cutout", "cutout.png", upper_track, 0, 60,
                                          source_out=60, source_fps=30)
    return engine, None, upper_clip


def _add_lower(engine):
    lower = np.empty((HEIGHT, WIDTH, 3), np.uint8)
    lower[:] = LOWER
    lower_entry, _w, _h = _entry("lower.png", lower)
    engine.media._entries["lower.png"] = lower_entry
    lower_track = engine.timeline.add_track("video")
    # add_track puts a lane at the top; this test needs the backdrop underneath.
    engine.timeline.tracks.remove(lower_track)
    engine.timeline.tracks.append(lower_track)
    return engine.timeline.new_clip("Lower", "lower.png", lower_track, 0, 60,
                                    source_out=60, source_fps=30)


def _pixel(frame, x, y):
    return frame[y, x].tolist()


def test_transparent_precomposition_lets_the_parent_show_through():
    engine, _lower, upper = _engine()
    nested_id = engine.session.precompose("Cutout comp", [upper.id])
    assert nested_id is not None
    nested = engine.timeline.find_clip(nested_id)
    comp = engine.compositions.get(nested.composition_id)
    assert comp.transparent is True
    _add_lower(engine)

    _raw, frame = engine.render_frame(0)
    assert _pixel(frame, 4, 4) == LOWER
    assert _pixel(frame, 32, 32) == SUBJECT


def test_legacy_opaque_composition_keeps_its_background():
    engine, _lower, upper = _engine()
    nested_id = engine.session.precompose("Opaque comp", [upper.id])
    nested = engine.timeline.find_clip(nested_id)
    comp = engine.compositions.get(nested.composition_id)
    comp.transparent = False
    comp.background = (10, 20, 30)
    _add_lower(engine)

    _raw, frame = engine.render_frame(0)
    assert _pixel(frame, 4, 4) == [10, 20, 30]
    assert _pixel(frame, 32, 32) == SUBJECT


def test_transparency_survives_multiple_nested_levels():
    engine, _lower, upper = _engine()
    inner_id = engine.session.precompose("Inner", [upper.id])
    assert inner_id is not None
    outer_id = engine.session.precompose("Outer", [inner_id])
    assert outer_id is not None
    outer = engine.timeline.find_clip(outer_id)
    outer_comp = engine.compositions.get(outer.composition_id)
    assert outer_comp.transparent is True
    _add_lower(engine)

    _raw, frame = engine.render_frame(0)
    assert _pixel(frame, 4, 4) == LOWER
    assert _pixel(frame, 32, 32) == SUBJECT


def test_shape_layer_alpha_passes_through_a_composition():
    engine, _lower, upper = _engine()
    nested_id = engine.session.precompose("Shape comp", [upper.id])
    nested = engine.timeline.find_clip(nested_id)
    comp = engine.compositions.get(nested.composition_id)
    _add_lower(engine)
    track = comp.timeline.add_track("video")
    shape = comp.timeline.new_clip("Dot", "", track, 0, 60, shape=SHAPE.blank("ellipse"))
    shape.shape["width"] = 20.0
    shape.shape["height"] = 20.0
    shape.shape["fill_color"] = "#FFFFFF"
    shape.shape["stroke_enabled"] = False

    _raw, frame = engine.render_frame(0)
    # The corner is outside both the cutout and the dot, so the lower clip is visible.
    assert _pixel(frame, 4, 4) == LOWER
    # The dot is opaque in the middle and sits above the cutout.
    assert _pixel(frame, 32, 32) == [255, 255, 255]


def test_text_layer_alpha_passes_through_a_composition():
    engine, _lower, upper = _engine()
    text_track = upper.track
    text_clip = engine.timeline.new_clip("Title", "", text_track, 0, 60,
                                         text=TXT.blank())
    TXT.set_content(text_clip, "A", 0, False)
    text_clip.text["size"] = 48.0
    text_clip.text["fill_color"] = "#FFFFFF"
    text_clip.text["stroke_width"] = 0.0
    engine.timeline.delete_clips([upper.id])
    nested_id = engine.session.precompose("Text comp", [text_clip.id])
    assert nested_id is not None
    nested = engine.timeline.find_clip(nested_id)
    comp = engine.compositions.get(nested.composition_id)
    assert comp.transparent is True
    _add_lower(engine)

    _raw, frame = engine.render_frame(0)
    assert _pixel(frame, 2, 2) == LOWER
    # A glyph is present somewhere in the centre band; never a black text box.
    centre = frame[16:48, 16:48]
    assert np.any(np.all(centre == 255, axis=2))


def test_transparency_toggle_invalidates_cached_composition_frames():
    engine, _lower, upper = _engine()
    nested_id = engine.session.precompose("Cache key", [upper.id])
    nested = engine.timeline.find_clip(nested_id)
    comp = engine.compositions.get(nested.composition_id)
    opaque = composition_digest(comp)
    comp.transparent = False
    legacy = composition_digest(comp)
    assert legacy != opaque
    comp.transparent = True
    assert composition_digest(comp) == opaque


def test_settings_dialog_exposes_transparent_background():
    from PyQt6.QtWidgets import QApplication
    from meenifx.model.composition import Composition
    from meenifx.ui.composition_dialogs import (CompositionSettingsDialog,
                                                PrecomposeDialog)

    app = QApplication.instance() or QApplication([])
    legacy = Composition(name="Legacy", width=64, height=64)
    dialog = CompositionSettingsDialog(None, legacy)
    assert dialog.values()["transparent"] is False
    dialog.transparent.setChecked(True)
    assert dialog.values()["transparent"] is True
    dialog.deleteLater()

    precompose = PrecomposeDialog(None, name="New", clip_count=1,
                                  range_text="frames 0-10")
    assert precompose.values()["transparent"] is True, \
        "new pre-compositions default to preserving alpha"
    precompose.deleteLater()
    app.processEvents()


def test_transparent_flag_round_trips_and_project_export_keeps_parent(tmp_path):
    engine, _lower, upper = _engine()
    nested_id = engine.session.precompose("Round trip", [upper.id])
    nested = engine.timeline.find_clip(nested_id)
    comp = engine.compositions.get(nested.composition_id)
    assert comp.transparent is True
    _add_lower(engine)

    sections = compositions_to_sections(engine.compositions, lambda path: path)
    loaded = sections_to_compositions(sections, lambda path: path)
    assert loaded.get(comp.id).transparent is True

    out = str(tmp_path / "composition-alpha-test.png")
    result = engine.export({"output_path": out, "format": "png",
                            "use_source_size": True, "range_start": 0, "range_end": 1})
    exported = cv2.imread(result[0])
    assert exported is not None
    assert exported[4, 4].tolist() == LOWER
    assert exported[32, 32].tolist() == SUBJECT
