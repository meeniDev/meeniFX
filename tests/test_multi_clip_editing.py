"""Batch editing: effects, common properties, animation timing and caption workflows."""

import copy

import pytest
from PyQt6.QtWidgets import QComboBox, QLabel

from conftest import build_engine
from meenifx.captions import model as CAP
from meenifx.model import animation_families as FAMILIES
from meenifx.model import channels as CH
from meenifx.model import presets as PM
from meenifx.model import text as TXT
from meenifx.project.container import read_container, write_container
from meenifx.project.io import Project, apply, collect
from meenifx.project.settings import ProjectSettings
from meenifx.ui.panels import EffectsPanel


def cues():
    return [
        {"start": 0.0, "end": 1.2, "text": "Hello there",
         "words": [{"start": 0.0, "end": 0.5, "word": "Hello"},
                   {"start": 0.5, "end": 1.2, "word": "there"}]},
        {"start": 1.5, "end": 2.6, "text": "Second line",
         "words": [{"start": 1.5, "end": 2.0, "word": "Second"},
                   {"start": 2.0, "end": 2.6, "word": "line"}]},
    ]


def add_video(engine, name, start, duration, source_out=60):
    track = engine.timeline.tracks_of_type("video")[0]
    return engine.timeline.new_clip(name, "synthetic-effect-test.png", track,
                                    start, duration, source_out=source_out,
                                    source_fps=30)


def add_text(engine, content, start, duration):
    return engine.timeline.find_clip(engine.session.add_text_clip(content, start, duration))


def add_audio(engine, name="voice", start=0, duration=60):
    track = engine.timeline.add_track("audio")
    return engine.timeline.new_clip(name, "voice.wav", track, start, duration)


def effects_of(engine, clip):
    return [entry["effect_id"] for entry in engine.timeline.find_clip(clip.id).effects]


def family(key):
    return next(item for item in FAMILIES.families() if item["key"] == key)


class ComboStub:
    def __init__(self, value):
        self.value = value

    def itemData(self, _index):
        return self.value


@pytest.fixture
def panel_factory(qapp):
    panels = []

    def make(engine):
        panel = EffectsPanel(engine)
        panels.append(panel)
        return panel

    yield make
    for panel in panels:
        panel.browser.shutdown()
        panel.close()
        panel.deleteLater()
    qapp.processEvents()


def test_selection_count_survives_opening_browser_and_inspector(panel_factory):
    engine, first = build_engine(duration=90)
    engine.session.project_size = (320, 180)
    second = add_video(engine, "second", 120, 45)
    shape_id = engine.session.add_shape_clip("rectangle", start=30, duration=20)
    engine.session.set_selection({first.id, second.id, shape_id})
    selected = set(engine.session.selected)
    panel = panel_factory(engine)
    panel.refresh_effect_list()
    panel.browser.refresh()
    panel.refresh_properties()
    assert engine.session.selected == selected
    labels = [label.text() for label in panel.properties.findChildren(QLabel)]
    assert "3 clips selected" in labels
    assert any("Batch edits are one undo step" in text for text in labels)


def test_effects_apply_to_compatible_clips_and_report_skips(panel_factory):
    engine, first = build_engine(duration=90)
    second = add_video(engine, "second", 120, 45)
    audio = add_audio(engine)
    engine.session.set_selection({first.id, second.id, audio.id})
    panel = panel_factory(engine)
    panel.refresh_effect_list()
    panel.browser.refresh()
    messages = []
    engine.session.status.connect(messages.append)
    card = next(card for card in panel.browser._cards if card.item["id"] == "mosaic")
    before = len(engine.session.stack._undo)
    panel.browser._card_activated(card.item)
    assert effects_of(engine, first) == ["mosaic"]
    assert effects_of(engine, second) == ["mosaic"]
    assert effects_of(engine, audio) == []
    assert len(engine.session.stack._undo) == before + 1
    assert "Skipped 1 incompatible clip" in messages[-1]

    first_entry = engine.timeline.find_clip(first.id).effects[0]
    second_entry = engine.timeline.find_clip(second.id).effects[0]
    assert first_entry is not second_entry
    panel._set_param(engine.timeline.find_clip(first.id), first_entry, "scale", 25)
    assert first_entry["params"]["scale"] == 25
    assert second_entry["params"].get("scale", 40) != 25

    engine.session.stack.undo()
    assert effects_of(engine, first) == []
    assert effects_of(engine, second) == []
    engine.session.stack.redo()
    assert effects_of(engine, first) == ["mosaic"]
    assert effects_of(engine, second) == ["mosaic"]


def test_partial_effect_state_add_enable_parameter_and_remove(panel_factory):
    engine, first = build_engine(duration=90)
    second = add_video(engine, "second", 120, 45)
    first.effects.append({"effect_id": "mosaic", "enabled": True, "params": {"scale": 40}})
    engine.session.set_selection({first.id, second.id})
    panel = panel_factory(engine)
    panel.refresh_effect_list()
    panel.browser.refresh()
    assert "mosaic" in panel.browser.partial_keys
    card = next(card for card in panel.browser._cards if card.item["id"] == "mosaic")
    assert card.item["partial"] is True

    runtime = next(effect for effect in engine.effects if effect.key == "mosaic")
    panel._batch_add_effect([engine.timeline.find_clip(second.id)], "mosaic", runtime)
    assert effects_of(engine, first) == ["mosaic"]
    assert effects_of(engine, second) == ["mosaic"]
    assert "mosaic" not in panel.browser.partial_keys

    spec = next(item for item in runtime.param_specs() if item.name == "scale")
    panel._batch_set_effect_param(
        [engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)],
        "mosaic", spec, 55)
    assert engine.timeline.find_clip(first.id).effects[0]["params"]["scale"] == 55
    assert engine.timeline.find_clip(second.id).effects[0]["params"]["scale"] == 55

    panel._batch_enable_effect(
        [engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)],
        "mosaic", False)
    assert all(entry["enabled"] is False for entry in
               (engine.timeline.find_clip(first.id).effects[0],
                engine.timeline.find_clip(second.id).effects[0]))

    for clip in (engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)):
        clip.effects.append({"effect_id": "glow", "enabled": True, "params": {}})
    panel._batch_remove_effect(
        [engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)],
        "mosaic")
    assert effects_of(engine, first) == ["glow"]
    assert effects_of(engine, second) == ["glow"]


def test_mixed_text_values_are_shown_and_only_explicit_edits_write(panel_factory):
    engine, _clip = build_engine(duration=90)
    first = add_text(engine, "One", 0, 30)
    second = add_text(engine, "Two", 40, 20)
    TXT.ensure_text(first)["font"] = "Arial"
    TXT.ensure_text(second)["font"] = "Georgia"
    TXT.ensure_text(first)["color"] = "#FF0000"
    TXT.ensure_text(second)["color"] = "#00FF00"
    CH.set_value(first, CH.transform_channel("opacity"), 70, 0)
    CH.set_value(second, CH.transform_channel("opacity"), 40, 0)
    engine.session.set_selection({first.id, second.id})
    panel = panel_factory(engine)
    combos = panel.properties.findChildren(QComboBox)
    assert any(combo.currentText() == "Mixed" for combo in combos)
    assert any(label.text() == "Mixed" for label in panel.properties.findChildren(QLabel))
    # Building the batch card must not rewrite either clip.
    assert TXT.ensure_text(engine.timeline.find_clip(first.id))["font"] == "Arial"
    assert TXT.ensure_text(engine.timeline.find_clip(second.id))["font"] == "Georgia"

    texts = [engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)]
    panel._batch_commit_text_choice(texts, ComboStub("Arial"), 0, "font", "Text font")
    panel._batch_set_text_colour(texts, "color", "#112233")
    panel._batch_commit_channel(texts, CH.text_channel("stroke_width"), 4, "Stroke")
    panel._batch_commit_channel(texts, CH.text_channel("size"), 44, "Size")
    for clip in (engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)):
        assert TXT.ensure_text(clip)["font"] == "Arial"
        assert TXT.ensure_text(clip)["color"] == "#112233"
        assert TXT.content_at(clip, 0) in ("One", "Two")
        assert TXT.value_at(clip, "stroke_width", 0) == 4
        assert TXT.value_at(clip, "size", 0) == 44


def test_text_properties_are_not_written_to_media_clips(panel_factory):
    engine, video = build_engine(duration=90)
    text = add_text(engine, "Caption", 0, 30)
    engine.session.set_selection({video.id, text.id})
    panel = panel_factory(engine)
    panel._batch_commit_text_choice(
        [engine.timeline.find_clip(video.id), engine.timeline.find_clip(text.id)],
        ComboStub("Arial"), 0, "font", "Text font")
    assert TXT.ensure_text(engine.timeline.find_clip(text.id))["font"] == "Arial"
    assert video.text is None or not video.is_text


def test_animations_use_each_clips_local_timing_and_keep_keyframes(panel_factory):
    engine, first = build_engine(frames=90, duration=20)
    second = add_video(engine, "second", 100, 50, source_out=60)
    CH.set_value(first, CH.transform_channel("opacity"), 40, 0, auto_key=True)
    engine.session.set_selection({first.id, second.id})
    panel = panel_factory(engine)
    pop = family("pop")
    panel._apply_animation_item({"kind": "animation", "family": pop, "media": "clip",
                                 "mode": "in", "direction": ""})
    rows = [PM.instances(engine.timeline.find_clip(clip.id))[0]
            for clip in (first, second)]
    assert [row["category"] for row in rows] == ["in", "in"]
    assert [row["start"] for row in rows] == [0, 0]
    assert [row["duration"] for row in rows] == [15, 15]
    assert CH.is_animated(engine.timeline.find_clip(first.id),
                          CH.transform_channel("opacity"))

    panel._apply_animation_item({"kind": "animation", "family": pop, "media": "clip",
                                 "mode": "out", "direction": ""})
    rows = [PM.instances(engine.timeline.find_clip(clip.id))[0]
            for clip in (first, second)]
    assert [row["category"] for row in rows] == ["out", "out"]
    assert [row["start"] for row in rows] == [5, 35]
    assert CH.is_animated(engine.timeline.find_clip(first.id),
                          CH.transform_channel("opacity"))


def test_batch_duration_updates_every_matching_preset(panel_factory):
    engine, first = build_engine(duration=60)
    second = add_video(engine, "second", 90, 40)
    engine.session.set_selection({first.id, second.id})
    panel = panel_factory(engine)
    pop = family("pop")
    panel._apply_animation_item({"kind": "animation", "family": pop, "media": "clip",
                                 "mode": "in", "direction": ""})
    texts = [engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)]
    panel._batch_set_animation_duration(texts, "pop", 12)
    assert [PM.instances(clip)[0]["duration"] for clip in texts] == [12, 12]
    panel._batch_set_animation_duration(texts, "pop", 3)
    assert [PM.instances(clip)[0]["duration"] for clip in texts] == [3, 3]


def test_linked_video_audio_pair_keeps_link_and_audio_untouched(panel_factory):
    engine, video = build_engine(duration=90)
    audio = add_audio(engine)
    engine.timeline.assign_link([video.id, audio.id])
    engine.session.set_selection({video.id, audio.id})
    panel = panel_factory(engine)
    panel.refresh_effect_list()
    panel.browser.refresh()
    card = next(card for card in panel.browser._cards if card.item["id"] == "mosaic")
    panel.browser._card_activated(card.item)
    assert effects_of(engine, video) == ["mosaic"]
    assert effects_of(engine, audio) == []
    assert engine.timeline.link_group(video.link_id) != []


def test_auto_caption_batch_workflow_undo_redo_and_preview(panel_factory, tmp_path):
    engine, _clip = build_engine(frames=150, duration=150)
    ids = engine.session.generate_captions(cues(), start_frame=0, width=320, height=180,
                                           animation="", style_id="classic")
    captions = [engine.timeline.find_clip(cid) for cid in ids]
    original = [(clip.id, clip.timeline_start, clip.duration,
                 TXT.content_at(clip, 0)) for clip in captions]
    engine.session.set_selection(set(ids))
    panel = panel_factory(engine)
    undo_before = len(engine.session.stack._undo)
    pop = family("pop")
    panel._apply_animation_item({"kind": "animation", "family": pop, "media": "text",
                                 "mode": "in", "direction": ""})
    assert all(PM.instances(engine.timeline.find_clip(cid)) for cid in ids)

    panel._batch_commit_text_choice(
        [engine.timeline.find_clip(cid) for cid in ids], ComboStub("Arial"), 0,
        "font", "Text font")
    panel._batch_set_text_colour([engine.timeline.find_clip(cid) for cid in ids],
                                 "color", "#FFCC00")
    panel._batch_set_text_colour([engine.timeline.find_clip(cid) for cid in ids],
                                 "stroke_color", "#101010")
    panel._batch_commit_channel([engine.timeline.find_clip(cid) for cid in ids],
                                CH.text_channel("stroke_width"), 7, "Stroke")
    panel._batch_commit_channel([engine.timeline.find_clip(cid) for cid in ids],
                                CH.text_channel("size"), 40, "Size")
    panel._batch_set_animation_duration([engine.timeline.find_clip(cid) for cid in ids],
                                        "pop", 10)

    for cid, start, duration, words in original:
        clip = engine.timeline.find_clip(cid)
        assert (clip.timeline_start, clip.duration, TXT.content_at(clip, 0)) == \
            (start, duration, words)
        assert TXT.ensure_text(clip)["font"] == "Arial"
        assert TXT.ensure_text(clip)["color"] == "#FFCC00"
        assert TXT.ensure_text(clip)["stroke_color"] == "#101010"
        assert TXT.value_at(clip, "stroke_width", 0) == 7
        assert TXT.value_at(clip, "size", 0) == 40
        assert PM.instances(clip)[0]["duration"] == 10

    frame = engine.render_frame(5, 1.0)[1]
    assert frame is not None and frame.size > 0

    # Generation is one step; each explicit batch control is its own step.
    batch_steps = len(engine.session.stack._undo) - undo_before
    assert batch_steps == 7
    for _ in range(batch_steps):
        engine.session.stack.undo()
    for cid, start, duration, words in original:
        clip = engine.timeline.find_clip(cid)
        assert (clip.timeline_start, clip.duration, TXT.content_at(clip, 0)) == \
            (start, duration, words)
        assert not PM.instances(clip)
        assert TXT.ensure_text(clip)["color"] != "#FFCC00"
    for _ in range(batch_steps):
        engine.session.stack.redo()
    assert all(PM.instances(engine.timeline.find_clip(cid))[0]["duration"] == 10
               for cid in ids)

    # Save and reopen the whole batch result.
    project = Project(ProjectSettings.from_preset("720p 30", "Captions"), None)
    data = collect(engine, project)
    path = str(tmp_path / "captions-batch.mfxpf")
    write_container(path, data)
    loaded = read_container(path)
    fresh, _fresh_clip = build_engine(frames=150, duration=150)
    fresh.timeline.tracks.clear()
    applied, missing = apply(fresh, loaded, path)
    assert all(item.name != "Caption" for item in missing)
    restored = CAP.caption_clips(fresh.timeline)
    assert len(restored) == len(ids)
    assert [TXT.content_at(clip, 0) for clip in restored] == [item[3] for item in original]
    assert all(TXT.ensure_text(clip)["font"] == "Arial" for clip in restored)
    assert all(TXT.ensure_text(clip)["color"] == "#FFCC00" for clip in restored)
    assert all(PM.instances(clip)[0]["duration"] == 10 for clip in restored)


def test_multi_clip_effects_and_text_survive_project_round_trip(tmp_path):
    engine, first = build_engine(frames=150, duration=150)
    second = add_video(engine, "second", 30, 45, source_out=60)
    text = add_text(engine, "Together", 90, 30)
    engine.session.set_selection({first.id, second.id, text.id})
    engine.session.apply_to_clips(
        [first.id, second.id, text.id], "Add mosaic",
        lambda clip: clip.effects.append({"effect_id": "mosaic", "enabled": True,
                                          "params": {"scale": 55}}))
    TXT.ensure_text(text)["font"] = "Arial"
    TXT.ensure_text(text)["color"] = "#ABCDEF"
    project = Project(ProjectSettings.from_preset("720p 30", "Batch"), None)
    path = str(tmp_path / "batch.mfxpf")
    write_container(path, collect(engine, project))
    loaded = read_container(path)
    fresh, _fresh_clip = build_engine(frames=150, duration=150)
    fresh.timeline.tracks.clear()
    applied, missing = apply(fresh, loaded, path)
    assert applied
    assert all(item.name != "Caption" for item in missing)
    restored = [clip for clip in fresh.timeline.all_clips() if clip.id in (first.id, second.id, text.id)]
    assert len(restored) == 3
    assert sum(1 for clip in restored if clip.effects and clip.effects[0]["effect_id"] == "mosaic") == 3
    assert all(clip.effects[0]["params"]["scale"] == 55 for clip in restored)
    restored_text = fresh.timeline.find_clip(text.id)
    assert TXT.ensure_text(restored_text)["font"] == "Arial"
    assert TXT.ensure_text(restored_text)["color"] == "#ABCDEF"


def test_effect_batch_covers_image_and_shape_clips(panel_factory):
    engine, first = build_engine(duration=90)
    engine.session.project_size = (320, 180)
    second = add_video(engine, "second-image", 120, 45)
    shape_id = engine.session.add_shape_clip("rectangle", start=30, duration=25)
    shape = engine.timeline.find_clip(shape_id)
    engine.session.set_selection({first.id, second.id, shape.id})
    panel = panel_factory(engine)
    panel.refresh_effect_list()
    panel.browser.refresh()
    card = next(card for card in panel.browser._cards if card.item["id"] == "mosaic")
    panel.browser._card_activated(card.item)
    assert effects_of(engine, first) == ["mosaic"]
    assert effects_of(engine, second) == ["mosaic"]
    assert effects_of(engine, shape) == ["mosaic"]
    assert engine.timeline.find_clip(shape.id).shape is not None


def test_loop_presets_stay_clip_local_and_keep_transform_keyframes(panel_factory):
    engine, first = build_engine(frames=90, duration=20)
    second = add_video(engine, "second", 100, 50, source_out=60)
    CH.set_value(first, CH.transform_channel("opacity"), 35, 0, auto_key=True)
    engine.session.set_selection({first.id, second.id})
    panel = panel_factory(engine)
    loop = next(item for item in FAMILIES.families() if "loop" in item["modes"])
    panel._apply_animation_item({"kind": "animation", "family": loop, "media": "clip",
                                 "mode": "loop", "direction": ""})
    for clip in (engine.timeline.find_clip(first.id), engine.timeline.find_clip(second.id)):
        row = PM.instances(clip)[0]
        assert row["category"] == "loop"
        assert row["start"] == 0
        start, length = PM.window(row, clip.duration)
        assert start == 0 and length == clip.duration
    assert CH.is_animated(engine.timeline.find_clip(first.id),
                          CH.transform_channel("opacity"))


def test_batch_text_renders_at_preview_and_full_resolution(panel_factory):
    engine, _clip = build_engine(frames=60, duration=60)
    first = add_text(engine, "Preview", 0, 30)
    second = add_text(engine, "Export", 35, 20)
    engine.session.set_selection({first.id, second.id})
    panel = panel_factory(engine)
    panel._batch_set_text_colour([engine.timeline.find_clip(first.id),
                                  engine.timeline.find_clip(second.id)],
                                 "color", "#33CCFF")
    preview = engine.render_frame(5, 0.5)[1]
    full = engine.render_frame(5, 1.0)[1]
    assert preview is not None and full is not None
    assert (preview.shape[1], preview.shape[0]) == (full.shape[1] // 2, full.shape[0] // 2)
    assert preview.tobytes() != full.tobytes()
