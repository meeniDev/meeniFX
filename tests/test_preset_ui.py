"""The preset browser, the inspector card, the timeline bars, Split text and the library.

The model itself is covered by test_presets.py and test_preset_text.py; this file drives
the parts a person actually touches: applying from the browser, the Animation card's
knobs and buttons, dragging a bar on the timeline, right-click "Split text", and the
user library (save / import / export).
"""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import build_engine  # noqa: E402

from meenifx.model import preset_library as LIB  # noqa: E402
from meenifx.model import presets as P  # noqa: E402
from meenifx.model import text as TXT  # noqa: E402
from meenifx.processing import text_units  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QComboBox, QFrame, QLabel  # noqa: E402


@pytest.fixture
def engine_and_clip(qapp):
    """One synthetic video clip; `qapp` first, because the panels build real widgets."""
    engine, clip = build_engine(duration=90)
    return engine, clip


def add_text(engine, text="HELLO", start=0, duration=60):
    clip_id = engine.session.add_text_clip(text, start, duration)
    return engine.timeline.find_clip(clip_id)


def rows(clip_id, engine):
    """Instances of a clip by id: undo rebuilds Clip objects, so never hold a reference."""
    return P.instances(engine.timeline.find_clip(clip_id))


# --------------------------------------------------------------------- browser
def test_applying_from_the_browser_adds_one_instance(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel
    from meenifx.model import animation_families as FAMILIES

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    panel = EffectsPanel(engine)
    panel.browser.tabs.setCurrentIndex(1)
    family = next(item for item in FAMILIES.families() if item["key"] == "fade")
    panel._apply_animation_item({"kind": "animation", "id": family["key"],
                                 "family": family, "media": "clip", "mode": "in",
                                 "direction": ""})
    assert [row["category"] for row in rows(clip.id, engine)] == ["in"]
    # A second apply of the same family edits that instance instead of stacking another.
    panel._apply_animation_item({"kind": "animation", "id": family["key"],
                                 "family": family, "media": "clip", "mode": "out",
                                 "direction": ""})
    assert [row["category"] for row in rows(clip.id, engine)] == ["out"]


def test_a_second_in_preset_replaces_the_first_but_loops_stack(engine_and_clip):
    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()["clip_in_fade"], "whole")
    engine.session.apply_preset(LIB.load()["clip_in_zoom"], "whole")
    assert [row["category"] for row in rows(clip.id, engine)] == ["in"]
    assert rows(clip.id, engine)[0]["preset"] == "clip_in_zoom"
    engine.session.apply_preset(LIB.load()["clip_loop_shake"], "whole")
    engine.session.apply_preset(LIB.load()["clip_loop_float"], "whole")
    assert [row["category"] for row in rows(clip.id, engine)] == ["in", "loop", "loop"]


def test_applying_a_preset_is_one_undo_step(engine_and_clip):
    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()["clip_loop_shake"], "whole")
    engine.session.stack.undo()
    assert rows(clip.id, engine) == []


def test_the_unit_menu_reaches_the_text_instance(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel

    engine, _clip = engine_and_clip
    clip = add_text(engine)
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()["text_in_fade"], "word")
    assert rows(clip.id, engine)[0]["knobs"]["unit"] == "word"

    panel = EffectsPanel(engine)
    panel.refresh_properties()
    card = next(frame for frame in panel.properties_layout.parentWidget().findChildren(QFrame)
                if frame.property("effect_key") == "animation")
    unit = next(combo for combo in card.findChildren(QComboBox)
                if combo.findData("letter") >= 0)
    unit.setCurrentIndex(unit.findData("letter"))
    assert rows(clip.id, engine)[0]["knobs"]["unit"] == "letter"


def test_media_clips_ignore_the_text_unit(engine_and_clip):
    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()["clip_in_fade"], "line")
    assert rows(clip.id, engine)[0]["knobs"]["unit"] == "whole"


def test_the_search_box_filters_the_animation_families(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    panel = EffectsPanel(engine)
    browser = panel.browser
    browser.tabs.setCurrentIndex(1)
    total = len(browser._cards)
    assert total >= 15

    browser.search.setText("glitch")
    hits = browser._cards
    assert 0 < len(hits) < total
    assert any("glitch" in card.item["name"].lower() for card in hits)

    browser.search.setText("")
    assert len(browser._cards) == total


# --------------------------------------------------------------- inspector card
def test_the_animation_card_lists_every_instance(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel
    from meenifx.ui.widgets import ScrubField

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    P.add(clip, "fade", "in", program="fade", duration=15, name="Fade")
    P.add(clip, "shake", "loop", program="shake", duration=40, name="Shake")
    panel = EffectsPanel(engine)
    panel.refresh_properties()
    # Only cards in the active layout count; replaced cards await Qt DeferredDelete.
    cards = [panel.properties_layout.itemAt(i).widget()
             for i in range(panel.properties_layout.count())]
    cards = [frame for frame in cards
             if frame is not None and frame.property("effect_key") == "animation"]
    assert len(cards) == 1
    # In: Duration + Intensity + Seed. Loop: Cycle + Span + Cycles + Gap + Phase +
    # Intensity + Seed. Combos: one Easing and one Mode per instance.
    assert len(cards[0].findChildren(ScrubField)) == 10
    assert len(cards[0].findChildren(QComboBox)) == 4
    texts = [combo.currentText() for combo in cards[0].findChildren(QComboBox)]
    assert "In" in texts, "the entrance offers its mode"
    assert "Repeat" in texts, "the loop offers Repeat / Ping-Pong"
    texts = [label.text() for label in cards[0].findChildren(QLabel)]
    assert any("Fade" in text and "In" in text for text in texts)
    assert any("Shake" in text and "Loop" in text for text in texts)


def test_the_card_shows_stagger_direction_and_unit_for_per_unit_text(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel
    from meenifx.ui.widgets import ScrubField

    engine, _clip = engine_and_clip
    clip = add_text(engine)
    engine.session.set_selection({clip.id})
    P.add(clip, "fade", "in", program="fade", duration=15, name="Fade",
          knobs={"unit": "letter", "stagger": 0.5})
    panel = EffectsPanel(engine)
    panel.refresh_properties()
    card = next(frame for frame in panel.properties_layout.parentWidget().findChildren(QFrame)
                if frame.property("effect_key") == "animation")
    labels = [field.label.text().rstrip("\u2026") for field in card.findChildren(ScrubField)]
    assert any("Stag".startswith(text) or text.startswith("Stag") for text in labels)
    assert len(labels) == 4           # Duration, Intensity, Stagger, Seed
    combos = [combo.currentText() for combo in card.findChildren(QComboBox)]
    assert "Letter" in combos and "Left to right" in combos


def test_the_animation_card_switches_the_mode_in_place(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()["clip_in_slide"], "whole")
    panel = EffectsPanel(engine)
    panel.refresh_properties()
    card = next(frame for frame in panel.properties_layout.parentWidget().findChildren(QFrame)
                if frame.property("effect_key") == "animation")
    mode = next(combo for combo in card.findChildren(QComboBox)
                if combo.findData("out") >= 0)
    mode.setCurrentIndex(mode.findData("out"))
    applied = rows(clip.id, engine)
    assert len(applied) == 1, "switching the mode must not stack a second animation"
    assert applied[0]["category"] == "out"
    assert applied[0]["preset"] == "clip_out_slide"
    assert applied[0]["program"] == "slide"
    engine.session.stack.undo()
    assert rows(clip.id, engine)[0]["category"] == "in"


def test_the_animation_card_switches_the_direction_in_place(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()["clip_in_slide"], "whole")
    panel = EffectsPanel(engine)
    panel.refresh_properties()
    card = next(frame for frame in panel.properties_layout.parentWidget().findChildren(QFrame)
                if frame.property("effect_key") == "animation")
    direction = next(combo for combo in card.findChildren(QComboBox)
                     if combo.findData("up") >= 0)
    direction.setCurrentIndex(direction.findData("up"))
    applied = rows(clip.id, engine)
    assert len(applied) == 1
    assert applied[0]["args"]["dir"] == "up"
    assert applied[0]["preset"] == "clip_in_slide_up"
    engine.session.stack.undo()
    assert rows(clip.id, engine)[0]["args"]["dir"] == "left"


def test_committing_a_knob_changes_the_instance_once(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    P.add(clip, "shake", "loop", program="shake", duration=40, name="Shake")
    panel = EffectsPanel(engine)
    panel._commit_preset(clip.id, 0, "Preset settings", knobs={"intensity": 0.4})
    assert rows(clip.id, engine)[0]["knobs"]["intensity"] == pytest.approx(0.4)
    engine.session.stack.undo()
    assert rows(clip.id, engine)[0]["knobs"]["intensity"] == pytest.approx(1.0)
    engine.session.stack.redo()
    assert rows(clip.id, engine)[0]["knobs"]["intensity"] == pytest.approx(0.4)


def test_the_remove_button_drops_the_instance(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    P.add(clip, "fade", "in", program="fade", duration=15, name="Fade")
    P.add(clip, "shake", "loop", program="shake", duration=40, name="Shake")
    panel = EffectsPanel(engine)
    panel._remove_preset(clip.id, 0)
    assert [row["category"] for row in rows(clip.id, engine)] == ["loop"]
    engine.session.stack.undo()
    assert [row["category"] for row in rows(clip.id, engine)] == ["in", "loop"]


def test_bake_from_the_card_matches_the_preset_frames(engine_and_clip):
    from meenifx.ui.panels import EffectsPanel

    engine, clip = engine_and_clip
    engine.session.set_selection({clip.id})
    P.add(clip, "shake", "loop", program="shake",
          knobs={"seed": 5, "intensity": 1.0, "unit": "whole"}, duration=60, name="Shake")
    engine.session.set_playhead(30)
    before = engine.compose_frame(30)
    panel = EffectsPanel(engine)
    panel._bake_preset(clip.id, 0)
    assert rows(clip.id, engine) == []
    after = engine.compose_frame(30)
    assert before.shape == after.shape
    assert np.abs(before.astype(np.int16) - after.astype(np.int16)).max() <= 2


# -------------------------------------------------------------------- timeline
def test_timeline_bars_sit_where_the_instances_do(engine_and_clip):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, clip = engine_and_clip
    P.add(clip, "fade", "in", program="fade", duration=30, name="Fade")
    P.add(clip, "shake", "loop", program="shake", duration=45, name="Shake")
    P.add(clip, "fade2", "out", program="fade", duration=30, name="Fade out")
    canvas = TimelinePanel(engine, None).canvas
    content = canvas._clip_rect(clip)
    bars = {row[1]["category"]: row[2] for row in canvas.preset_bar_rects(clip)}
    assert set(bars) == {"in", "loop", "out"}
    assert abs(bars["in"].left() - (content.left() + 4)) < 1
    # A loop repeats to the end of the clip, so its bar spans the whole lane.
    assert abs(bars["loop"].width() - (content.width() - 8)) < 2
    assert abs(bars["loop"].left() - (content.left() + 4)) < 1
    assert abs(bars["out"].right() - (content.right() - 4)) < 1


def test_dragging_the_inner_edge_changes_the_duration(engine_and_clip):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, clip = engine_and_clip
    P.add(clip, "fade", "in", program="fade", duration=30, name="Fade")
    canvas = TimelinePanel(engine, None).canvas
    bar = canvas.preset_bar_rects(clip)[0][2]
    canvas._begin_preset_drag(clip, 0, "end", int(bar.right()), int(bar.center().y()))
    canvas._preset_move(int(bar.left() + bar.width() * 0.5), int(bar.center().y()),
                        Qt.KeyboardModifier.ShiftModifier)
    canvas._preset_release()
    assert rows(clip.id, engine)[0]["duration"] < 30
    engine.session.stack.undo()
    assert rows(clip.id, engine)[0]["duration"] == 30


def test_dragging_an_out_bar_keeps_it_anchored_to_the_end(engine_and_clip):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, clip = engine_and_clip
    P.add(clip, "fade", "out", program="fade", duration=30, name="Fade out")
    canvas = TimelinePanel(engine, None).canvas
    bar = canvas.preset_bar_rects(clip)[0][2]
    canvas._begin_preset_drag(clip, 0, "start", int(bar.left()), int(bar.center().y()))
    canvas._preset_move(int(bar.left() + 20), int(bar.center().y()),
                        Qt.KeyboardModifier.ShiftModifier)
    canvas._preset_release()
    instance = rows(clip.id, engine)[0]
    assert instance["start"] + instance["duration"] == clip.duration
    assert instance["duration"] != 30


def test_dropping_a_preset_onto_a_clip_applies_it(engine_and_clip):
    from PyQt6.QtCore import QMimeData, QPointF
    from PyQt6.QtGui import QDropEvent

    from meenifx.constants import PRESET_MIME
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, clip = engine_and_clip
    canvas = TimelinePanel(engine, None).canvas
    rect = canvas._clip_rect(clip)
    mime = QMimeData()
    mime.setData(PRESET_MIME, b"clip_loop_shake|whole")
    event = QDropEvent(QPointF(rect.center().x(), rect.center().y()),
                       Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    canvas.dropEvent(event)
    applied = rows(clip.id, engine)
    assert [row["preset"] for row in applied] == ["clip_loop_shake"]
    assert applied[0]["category"] == "loop"


def test_a_loop_bar_can_be_shortened_from_either_edge(engine_and_clip):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, clip = engine_and_clip
    P.add(clip, "shake", "loop", program="shake", duration=60, name="Shake")
    instance = P.instances(clip)[0]
    instance["start"] = 10
    P.set_loop_coverage(instance, 50)
    canvas = TimelinePanel(engine, None).canvas
    bar = canvas.preset_bar_rects(clip)[0][2]
    canvas._begin_preset_drag(clip, 0, "start", int(bar.left()), int(bar.center().y()))
    canvas._preset_move(int(bar.left() + 25), int(bar.center().y()),
                        Qt.KeyboardModifier.ShiftModifier)
    canvas._preset_release()
    instance = rows(clip.id, engine)[0]
    start, span = P.window(instance, clip.duration)
    assert start > 10, "the start edge moved"
    assert start + span == 60, "the far edge stayed where it was"
    assert instance["duration"] == 60, "dragging the bar never changes the cycle"


# ---------------------------------------------------------------- split text
def test_split_text_keeps_every_letter_where_it_was(engine_and_clip):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = engine_and_clip
    clip = add_text(engine, "HELLO")
    canvas = TimelinePanel(engine, None).canvas
    width, height = engine.project_size()
    units = text_units.layout_units(TXT.resolve(clip, 0), width, height, 1.0)["letter"]
    created = canvas.split_text(clip, "letter")
    assert len(created) == len(units) == 5
    for clip_id, (centre_x, _centre_y, letter) in zip(created, units):
        piece = engine.timeline.find_clip(clip_id)
        assert piece.text["text"] == letter
        assert width / 2.0 + piece.text["offset_x"] == pytest.approx(centre_x, abs=0.6)


def test_the_split_pieces_render_the_same_picture(engine_and_clip):
    """The letters must land where they were: same ink, same centre, sub-pixel aside."""
    import cv2

    from meenifx.processing import text as TEXT
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = engine_and_clip
    clip = add_text(engine, "HELLO")
    width, height = engine.project_size()
    original = TEXT.render(TXT.resolve(clip, 0), width, height)[:, :, 3]
    canvas = TimelinePanel(engine, None).canvas
    pieces = [engine.timeline.find_clip(cid) for cid in canvas.split_text(clip, "letter")]
    joined = np.zeros_like(original)
    for piece in pieces:
        joined = np.maximum(joined, TEXT.render(TXT.resolve(piece, 0), width, height)[:, :, 3])

    ink_original = (original > 24).astype(np.uint8)
    ink_split = (joined > 24).astype(np.uint8)
    grown = cv2.dilate(ink_original, np.ones((5, 5), np.uint8))
    # Every bit of the split text sits on top of a letter of the original (antialias aside).
    assert ink_split[grown == 0].sum() <= max(40, ink_split.sum() * 0.01)
    # And the picture as a whole has not moved.
    def centroid(ink):
        ys, xs = np.nonzero(ink)
        return float(xs.mean()), float(ys.mean())
    before_x, before_y = centroid(ink_original)
    after_x, after_y = centroid(ink_split)
    assert abs(before_x - after_x) < 1.5 and abs(before_y - after_y) < 1.5


def test_split_text_is_one_undo_step(engine_and_clip):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = engine_and_clip
    clip = add_text(engine, "AB CD")
    canvas = TimelinePanel(engine, None).canvas
    assert len(canvas.split_text(clip, "word")) == 2
    engine.session.stack.undo()
    assert len([c for t in engine.timeline.tracks for c in t.clips if c.is_text]) == 1
    engine.session.stack.redo()
    assert len([c for t in engine.timeline.tracks for c in t.clips if c.is_text]) == 2


def test_split_text_turns_a_per_unit_preset_into_whole_clip_presets(engine_and_clip):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = engine_and_clip
    clip = add_text(engine, "HELLO")
    P.add(clip, "fade", "in", program="fade", duration=15, name="Fade",
          knobs={"unit": "letter", "stagger": 0.4})
    canvas = TimelinePanel(engine, None).canvas
    for clip_id in canvas.split_text(clip, "letter"):
        instance_rows = rows(clip_id, engine)
        assert instance_rows and instance_rows[0]["knobs"]["unit"] == "whole"


# --------------------------------------------------------------------- library
def test_the_shipped_library_has_the_required_presets():
    presets = LIB.load()
    assert len(presets) >= 50
    counted = LIB.by_category(presets)
    for key in LIB.CATEGORY_KEYS:
        assert len(counted.get(key, [])) >= 8, key
    names = {preset["name"] for preset in presets.values()}
    for required in ("Fade", "Typewriter", "Pop", "Bounce", "Slide In Left", "Slide In Right",
                     "Slide In Up", "Slide In Down", "Scale Up Overshoot", "Blur In",
                     "Rotate In", "Drop In", "Wave", "Flip", "Spiral", "Tracking In",
                     "Scramble Decode", "Glitch Reveal", "Mask Wipe", "Kinetic Punch",
                     "Vertical Slide", "Scatter", "Tracking Out", "Flicker", "Color Cycle",
                     "Swing", "Camera Shake", "Heartbeat", "Punch Zoom", "Strobe", "Sway",
                     "Zoom Breath", "Whip In", "Whip Out", "Flash In", "Flash Out",
                     "Glitch In", "Glitch Out", "Glitch Stutter", "Breathe"):
        assert required in names, required


def test_every_shipped_preset_evaluates_at_both_ends(engine_and_clip):
    engine, clip = engine_and_clip
    text_clip = add_text(engine, "AB", start=0, duration=90)
    for preset in LIB.load().values():
        category, program, args, knobs = LIB.preset_spec(preset, "letter")
        assert program in P.PROGRAMS, preset["id"]
        target = text_clip if LIB.CATEGORY_INFO[preset["category"]][1] == "text" else clip
        before = len(P.instances(target))
        P.add(target, preset["id"], category, program, args, knobs, duration=20,
              name=preset["name"])
        instance = P.instances(target)[-1]
        start = int(instance["start"])
        end = start + int(instance["duration"])
        # An entrance is back to normal when it finishes, an exit starts from normal, and a
        # loop is exactly the clip when it is not running: at rest the text never moves.
        rest = {"in": end, "out": start, "loop": max(end, int(target.duration))}[category]
        assert P.evaluate(target, int(rest), project_size=(640, 360)).pose.is_identity(), \
            preset["id"]
        P.evaluate(target, start, project_size=(640, 360))     # and never a crash at the start
        P.evaluate(target, (start + end) // 2, project_size=(640, 360))
        del P.instances(target)[before:]


def test_a_user_preset_round_trips_through_a_file(tmp_path, monkeypatch, engine_and_clip):
    engine, clip = engine_and_clip
    monkeypatch.setattr(LIB, "user_folder", lambda: str(tmp_path / "user"))
    P.add(clip, "shake", "loop", program="shake", args={"amp": 0.02},
          knobs={"seed": 7, "intensity": 0.55, "unit": "whole"}, duration=45, name="My Shake")
    payload = P.saved_as_preset(clip, "my_shake", name="My Shake")
    assert payload is not None
    shared = tmp_path / "shared.mfxpreset"
    shared.write_text(json.dumps(payload), encoding="utf-8")
    written, preset = LIB.import_file(str(shared))
    assert written and os.path.exists(written)
    assert preset["name"] == "My Shake"
    assert preset["knobs"]["seed"] == 7

    exported = LIB.export_file(preset["id"], str(tmp_path / "out.mfxpreset"))
    stored = json.loads(open(exported, encoding="utf-8").read())
    assert stored["format"] == LIB.FORMAT and stored["version"] == LIB.VERSION
    assert stored["args"] == {"amp": 0.02}

    # And the imported preset is a working library entry that applies to a clip.
    engine.session.set_selection({clip.id})
    engine.session.apply_preset(LIB.load()[preset["id"]], "whole")
    assert rows(clip.id, engine)[-1]["name"] == "My Shake"


def test_a_newer_preset_file_is_refused(tmp_path):
    path = tmp_path / "future.mfxpreset"
    path.write_text(json.dumps({"format": LIB.FORMAT, "version": LIB.VERSION + 5,
                                "name": "Future", "category": "clip_in",
                                "program": "fade"}), encoding="utf-8")
    assert LIB.import_file(str(path)) == (None, None)


def test_a_file_that_is_not_a_preset_is_refused(tmp_path):
    path = tmp_path / "notes.mfxpreset"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert LIB.import_file(str(path)) == (None, None)


def test_old_projects_without_presets_still_open(engine_and_clip):
    from meenifx.project import MediaLibrary
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = engine_and_clip
    library = MediaLibrary()
    sections = timeline_to_sections(engine.timeline, lambda path: library.add(path).id)
    # A format-5 project has no preset column at all.
    sections["clips"].pop("presets", None)
    restored = sections_to_timeline(sections, 30, lambda mid: library.items[mid].path)
    loaded = restored.find_clip(clip.id)
    assert loaded is not None and P.instances(loaded) == []
