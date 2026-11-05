"""Timeline polish: exact duration, Delete Gap, effect/attribute copying, shortcut page."""

import copy

from conftest import build_engine
from meenifx.model import presets as PM
from meenifx.model import text as TXT
from meenifx.ui.shortcuts_dialog import collect_shortcuts, shortcut_conflicts


def make_clip(engine, name, start, duration, track=None, text=None, path="synthetic-effect-test.png"):
    track = track or engine.timeline.tracks_of_type("video")[0]
    clip = engine.timeline.new_clip(name, path, track, start, duration,
                                    source_out=max(duration, 1), source_fps=30)
    if text:
        clip.text = TXT.blank()
        clip.text["text"] = text
    return clip


def test_exact_duration_stretches_a_still():
    engine, clip = build_engine(frames=90, duration=30)
    clip.name = "still.png"
    session = engine.session
    assert session.set_clip_duration(clip.id, 120) == 120
    assert engine.timeline.find_clip(clip.id).duration == 120
    session.undo()
    assert engine.timeline.find_clip(clip.id).duration == 30


def test_exact_duration_changes_both_halves_of_a_pair():
    engine, clip = build_engine(frames=90, duration=30)
    timeline = engine.timeline
    audio_track = timeline.add_track("audio")
    partner = timeline.new_clip("partner", "voice.wav", audio_track, clip.timeline_start, clip.duration,
                                source_out=90, stream="audio")
    timeline.assign_link([clip.id, partner.id])
    engine.session.set_clip_duration(clip.id, 60)
    assert timeline.find_clip(clip.id).duration == 60
    assert timeline.find_clip(partner.id).duration == 60


def test_delete_gap_closes_the_space_before_a_clip():
    engine, clip = build_engine(frames=120, duration=30)
    track = clip.track
    clip.timeline_start = 0
    clip.duration = 20
    later = make_clip(engine, "later", 50, 20, track=track)
    engine.session.set_selection({later.id})
    assert engine.session.delete_gap()
    assert engine.timeline.find_clip(later.id).timeline_start == 20
    engine.session.undo()
    assert engine.timeline.find_clip(later.id).timeline_start == 50


def test_effects_copy_across_clips_in_one_step():
    engine, source = build_engine(frames=90, duration=30)
    target = make_clip(engine, "target", 40, 30)
    source.effects = [{"effect_id": "mosaic", "enabled": True, "params": {"scale": 12}}]
    engine.session.set_selection({source.id})
    assert engine.session.copy_effects()
    engine.session.set_selection({target.id})
    assert engine.session.paste_effects(resolver=engine.runtime_effects) == 1
    pasted = engine.timeline.find_clip(target.id)
    assert pasted.effects[0]["effect_id"] == "mosaic"
    assert pasted.effects[0]["params"]["scale"] == 12
    assert pasted.effects[0] is not source.effects[0], "the pasted stack is an independent copy"
    engine.session.undo()
    assert engine.timeline.find_clip(target.id).effects == []


def test_paste_attributes_respects_the_chosen_parts():
    engine, source = build_engine(frames=90, duration=30)
    target = make_clip(engine, "target", 40, 30)
    source.effects = [{"effect_id": "mosaic", "enabled": True, "params": {"scale": 9}}]
    source.transform["scale_x"] = source.transform["scale_y"] = 55.0
    source.fade_in = 4
    PM.add(source, "clip_in_fade", "in", "fade", {}, {}, duration=8)
    engine.session.set_selection({source.id})
    engine.session.copy_attributes()

    engine.session.set_selection({target.id})
    engine.session.paste_attributes({"effects"}, resolver=engine.runtime_effects)
    after = engine.timeline.find_clip(target.id)
    assert after.effects and not PM.instances(after)
    assert after.transform["scale_x"] == 100.0, "transform was not chosen"

    engine.session.paste_attributes({"transform", "animations"})
    after = engine.timeline.find_clip(target.id)
    assert after.transform["scale_x"] == 55.0
    assert after.transform["scale_y"] == 55.0
    assert len(PM.instances(after)) == 1
    assert after.fade_in == 0, "audio settings were not chosen"


def test_effects_that_do_not_fit_the_media_type_are_skipped():
    engine, source = build_engine(frames=90, duration=30)
    timeline = engine.timeline
    audio_track = timeline.add_track("audio")
    audio_clip = timeline.new_clip("voice", "voice.wav", audio_track, 0, 30,
                                   source_out=90, stream="audio")
    source.effects = [{"effect_id": "mosaic", "enabled": True, "params": {}},
                      {"effect_id": "audio_gain", "enabled": True, "params": {}}]
    engine.session.set_selection({source.id})
    engine.session.copy_effects()
    engine.session.set_selection({audio_clip.id})
    engine.session.paste_effects(resolver=engine.runtime_effects)
    pasted = timeline.find_clip(audio_clip.id)
    assert [entry["effect_id"] for entry in pasted.effects] == ["audio_gain"]


def test_remove_all_effects_clears_stack_and_animations():
    engine, clip = build_engine(frames=90, duration=30)
    clip.effects = [{"effect_id": "mosaic", "enabled": True, "params": {}}]
    PM.add(clip, "clip_in_pop", "in", "pop", {}, {}, duration=6)
    engine.session.set_selection({clip.id})
    assert engine.session.remove_all_effects() == 1
    cleared = engine.timeline.find_clip(clip.id)
    assert cleared.effects == [] and PM.instances(cleared) == []


def test_shortcut_page_reads_the_real_menus_and_reports_conflicts(qapp, monkeypatch, tmp_path):
    from PyQt6.QtCore import QSettings

    from meenifx.ui import layout as dock_layout
    from meenifx.ui.main_window import MainWindow
    from meenifx.ui.shortcuts_dialog import ShortcutsDialog

    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    monkeypatch.setattr(dock_layout, "app_settings",
                        lambda: QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat))
    window = MainWindow(qapp)
    rows = collect_shortcuts(window)
    labels = {label for label, _shortcut, _path in rows}
    assert {"Paste", "Remove Dead Silence...", "Link Selected Clips",
            "Unlink Selected Clips", "Delete Gap", "Generate Auto Captions..."} <= labels
    conflicts = shortcut_conflicts(rows)
    assert all(len(names) > 1 for names in conflicts.values())
    dialog = ShortcutsDialog(window)
    assert dialog.table.rowCount() > 20
    dialog.search.setText("save")
    assert dialog.table.rowCount() >= 1
    dialog.close()
    window.effects_panel.browser.shutdown()
    window.close()
