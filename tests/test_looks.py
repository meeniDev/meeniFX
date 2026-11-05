"""Looks: shipped bundles, one-click stacks, session Strength and .mfxlook sharing."""

import json
import sys

import numpy as np
import pytest

from conftest import build_engine, frame_diff, synthetic_frame

from meenifx.engine import EditorEngine
from meenifx.model import looks as LOOKS


def look_clip(look_id="vhs_tape"):
    engine, clip = build_engine(frame=synthetic_frame(320, 180), duration=30)
    engine.session.set_selection({clip.id})
    engine.session.apply_look(look_id, ids=[clip.id])
    return engine, engine.timeline.find_clip(clip.id)


def test_at_least_ten_looks_ship_with_real_effects():
    looks = LOOKS.all_looks()
    assert len(looks) >= 10
    assert len({look["name"] for look in looks}) == len(looks)
    engine = EditorEngine()
    for look in looks:
        assert look["stack"], f"{look['id']} has an empty stack"
        for item in look["stack"]:
            assert item["effect_id"] in engine.runtime_effects, (look["id"], item["effect_id"])
            assert item["effect_id"] != "look"


def test_a_look_is_stored_as_a_versioned_document():
    for look in LOOKS.all_looks():
        payload = json.load(open(look["path"], encoding="utf-8"))
        assert payload["format"] == "mfxlook"
        assert payload["version"] == 1
        assert payload["id"] == look["id"]


def test_applying_a_look_creates_an_editable_stack_and_one_group_card():
    engine, clip = look_clip("vhs_tape")
    keys = [entry["effect_id"] for entry in clip.effects]
    look = LOOKS.get("vhs_tape")
    assert keys[0] == "look"
    assert keys[1:] == [item["effect_id"] for item in look["stack"]]
    group = clip.effects[0]
    assert group["name"] == "VHS Tape"
    assert all(entry["look_id"] == group["look_id"] for entry in clip.effects)
    # Every member is a normal effect entry: its own parameters are present.
    assert "scanlines" in clip.effects[1]["params"]


def test_strength_scales_the_whole_stack():
    engine, clip = look_clip("comic_halftone")
    full = engine.render_frame(0, 1.0)[1]
    group = clip.effects[0]
    group["params"]["strength"] = 0
    none = engine.render_frame(0, 1.0)[1]
    group["params"]["strength"] = 50
    half = engine.render_frame(0, 1.0)[1]

    plain_engine, plain_clip = build_engine(frame=synthetic_frame(320, 180), duration=30)
    reference = plain_engine.render_frame(0, 1.0)[1]
    assert frame_diff(none, reference) < 0.5, "0% strength must be the untouched picture"
    assert frame_diff(full, none) > 2.0
    # A stack is composed effect by effect, so 50% is not an exact pixel average - it
    # must simply land clearly between the original and the full look.
    assert 0.2 < frame_diff(half, none) / max(1e-6, frame_diff(full, none)) < 0.8
    assert frame_diff(half, full) > 1.0


def test_strength_is_animatable():
    from meenifx.model.keyframes import AnimatedValue

    engine, clip = look_clip("vhs_tape")
    curve = AnimatedValue()
    curve.set_keyframe(0, 0)
    curve.set_keyframe(10, 100)
    clip.effects[0]["params"]["strength"] = curve
    start = engine.render_frame(0, 1.0)[1]
    end = engine.render_frame(10, 1.0)[1]
    assert frame_diff(start, end) > 2.0


def test_removing_the_group_card_removes_its_effects():
    engine, clip = look_clip("neon_night")
    assert len(clip.effects) > 2
    engine.session.apply_to_clips(
        [clip.id], "Remove look",
        lambda c: setattr(c, "effects",
                          [e for e in c.effects if e.get("look_id") != c.effects[0]["look_id"]]))
    assert engine.timeline.find_clip(clip.id).effects == []


def test_a_look_applies_to_an_adjustment_layer_but_not_to_audio():
    engine, _clip = build_engine()
    track = engine.timeline.add_track("video")
    adjust = engine.timeline.new_clip("Adjustment", "", track, 0, 20, adjustment=True)
    assert engine.session.apply_look("retro_film", ids=[adjust.id]) == [adjust.id]
    assert engine.timeline.find_clip(adjust.id).effects[0]["effect_id"] == "look"

    audio_track = engine.timeline.add_track("audio")
    audio = engine.timeline.new_clip("sound", "sound.wav", audio_track, 0, 20)
    assert engine.session.apply_look("retro_film", ids=[audio.id]) == []
    assert audio.effects == []


def test_look_entries_survive_save_and_load():
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = look_clip("dreamy_bloom")
    sections = timeline_to_sections(engine.timeline, lambda path: path)
    restored = sections_to_timeline(sections, 30, lambda media: media)
    loaded = restored.find_clip(clip.id)
    assert [entry["effect_id"] for entry in loaded.effects] == \
           [entry["effect_id"] for entry in clip.effects]
    group = loaded.effects[0]
    assert group["look_id"] == clip.effects[0]["look_id"]
    assert group["name"] == "Dreamy Bloom"
    assert group["params"]["strength"] == 100


def test_saving_the_current_stack_as_a_user_look(tmp_path, monkeypatch):
    monkeypatch.setattr(LOOKS, "user_folder", lambda: str(tmp_path / "looks"))
    LOOKS.load(force=True)
    engine, clip = build_engine()
    clip.effects = [
        {"effect_id": "invert", "enabled": True, "params": {"amount": 40}},
        {"effect_id": "vignette", "enabled": True, "params": {"amount": 30}},
    ]
    stack = LOOKS.from_effects(clip.effects)
    assert [item["effect_id"] for item in stack] == ["invert", "vignette"]
    path, look = LOOKS.write_user_look("My Grade", stack, "saved from a clip")
    assert path and look is not None
    assert LOOKS.get(look["id"])["name"] == "My Grade"
    assert LOOKS.get(look["id"])["builtin"] is False


def test_mfxlook_export_and_import_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(LOOKS, "user_folder", lambda: str(tmp_path / "user"))
    LOOKS.load(force=True)
    target = tmp_path / "share.mfxlook"
    written = LOOKS.export_file("vhs_tape", target)
    assert written and str(written).endswith(".mfxlook")
    payload = json.load(open(written, encoding="utf-8"))
    assert payload["format"] == "mfxlook" and payload["version"] == 1

    exported = tmp_path / "exported-look.json"
    exported.write_text(json.dumps({"format": "mfxlook", "version": 1, "id": "shared_look",
                                    "name": "Shared Look",
                                    "stack": [{"effect_id": "invert", "params": {}}]}),
                        encoding="utf-8")
    _path, imported = LOOKS.import_file(str(exported))
    assert imported is not None and imported["name"] == "Shared Look"
    assert LOOKS.get("shared_look") is not None


def test_a_newer_look_file_is_refused():
    payload = {"format": "mfxlook", "version": 99, "id": "future",
               "stack": [{"effect_id": "invert"}]}
    assert LOOKS.normalise(payload) is None
    assert LOOKS.normalise({"format": "mfxlook", "stack": []}) is None
    assert LOOKS.normalise({"format": "mfxlook", "stack": [{"effect_id": "look"}]}) is None


def test_the_effects_panel_lists_looks_and_applies_one(qapp):
    pytest.importorskip("PyQt6.QtWidgets")
    from conftest import build_engine as make_engine
    from meenifx.ui.panels import EffectsPanel

    engine, clip = make_engine()
    engine.session.set_selection({clip.id})
    panel = EffectsPanel(engine)
    browser = panel.browser
    browser.tabs.setCurrentIndex(2)          # Styles
    assert browser.tab == "styles"
    panel.refresh_effect_list()
    browser.refresh()
    cards = browser._cards
    assert len(cards) >= 10
    assert all(card.item["kind"] == "style" for card in cards)
    browser._card_activated(cards[0].item)
    applied = engine.timeline.find_clip(clip.id).effects
    assert applied and applied[0]["effect_id"] == "look"
    panel.refresh_properties()
    labels = [row.get("name") for row in panel._rows if row.get("kind") == "param"]
    assert "strength" in labels
