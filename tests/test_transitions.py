"""Automatic overlap transitions: model, render, audio, editing and persistence."""

import os
import sys
import tempfile

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.audio import mix as audio_mix  # noqa: E402
from meenifx.audio import plan as audio_plan  # noqa: E402
from meenifx.media.cache import MediaEntry  # noqa: E402
from meenifx.model import transition as TR  # noqa: E402
from meenifx.model.session import TimelineSession  # noqa: E402
from meenifx.model.timeline import Timeline  # noqa: E402
from meenifx.project import container as C  # noqa: E402
from meenifx.project import serialize  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui import timeline_settings as settings_module  # noqa: E402
from meenifx.ui.timeline_settings import TimelineSettings  # noqa: E402
from tests.conftest import build_engine  # noqa: E402


def two_clips(left=(0, 30), right=(20, 30), record=True):
    timeline = Timeline(30)
    track = timeline.add_track("video")
    first = timeline.new_clip("A", "a.png", track, left[0], left[1], source_out=60)
    second = timeline.new_clip("B", "b.png", track, right[0], right[1], source_out=60)
    transition = timeline.add_transition(first.id, second.id) if record else None
    return timeline, track, first, second, transition


def engine_with_pictures(first_frame, second_frame, left=(0, 30), right=(20, 30)):
    """Two video clips on one track, backed by synthetic decoded frames."""
    engine, _clip = build_engine(frames=90)
    track = engine.timeline.tracks[0]
    track.clips.clear()
    for path, frame in (("a.png", first_frame), ("b.png", second_frame)):
        entry = MediaEntry("video", 90, 30)
        entry.frames = [np.asarray(frame, np.uint8).copy() for _ in range(90)]
        engine.media._entries[path] = entry
    first = engine.timeline.new_clip("A", "a.png", track, left[0], left[1], source_out=60)
    second = engine.timeline.new_clip("B", "b.png", track, right[0], right[1], source_out=60)
    return engine, track, first, second


# --------------------------------------------------------------- model and defaults
def test_crossfade_is_the_default_overlap_behaviour():
    session = TimelineSession(Timeline(30))
    assert session.overlap_label.get() == "Crossfade overlap"
    assert session.overlap_mode == "crossfade"
    session.auto_transitions.set(False)
    assert session.overlap_mode == "snap", "the preference disables automatic transitions"


def test_transition_records_survive_snapshot_restore_and_undo():
    timeline, _track, first, second, transition = two_clips(record=False)
    from meenifx.model.timeline import CommandStack
    command = CommandStack(timeline)
    assert transition is None
    command.run("Create transition", lambda: timeline.create_auto_transitions([second.id]))
    assert timeline.transition_between(first.id, second.id) is not None
    snapshot = timeline.snapshot()
    command.undo()
    assert timeline.transitions == []
    command.redo()
    assert timeline.snapshot() == snapshot
    assert timeline.transition_between(first.id, second.id).state() == (
        first.id, second.id, "crossfade", TR.DEFAULT_VIDEO_CURVE, TR.DEFAULT_AUDIO_CURVE)


def test_curves_have_clean_endpoints_and_known_shapes():
    for curve in TR.video_curve_names():
        assert TR.video_weight(0.0, curve) == pytest.approx(0.0)
        assert TR.video_weight(1.0, curve) == pytest.approx(1.0)
        assert TR.video_weight(0.5, curve) == pytest.approx(0.5)
        assert TR.video_weight(0.25, curve) < TR.video_weight(0.75, curve)
    assert TR.video_weight(0.25, "linear") == pytest.approx(0.25)
    assert TR.video_weight(0.25, "ease_in_out") < TR.video_weight(0.25, "smooth")
    incoming = TR.audio_weight(0.5, "equal_power")
    outgoing = np.sqrt(max(0.0, 1.0 - incoming ** 2))
    assert incoming ** 2 + outgoing ** 2 == pytest.approx(1.0)
    assert TR.audio_weight(0.5, "linear") == pytest.approx(0.5)


def test_only_crossfade_is_claimed_as_implemented():
    assert TR.KINDS == ("crossfade",)
    assert TR.KIND_LABELS == {"crossfade": "Crossfade"}
    assert TR.normalise_kind("dip to black") == "crossfade"


def test_three_way_overlap_is_not_reinterpreted():
    timeline = Timeline(30)
    track = timeline.add_track("video")
    first = timeline.new_clip("A", "a.png", track, 0, 30, source_out=30)
    second = timeline.new_clip("B", "b.png", track, 10, 30, source_out=30)
    third = timeline.new_clip("C", "c.png", track, 20, 30, source_out=30)
    assert timeline.create_auto_transitions([second.id, third.id]) == []
    assert timeline.transitions == []
    layers = timeline.video_layers_at(track, 25)
    assert len(layers) == 1 and layers[0][0].id == first.id, \
        "an ambiguous overlap keeps the historical single-clip result"


# ------------------------------------------------------------------------ picture
def test_drop_creates_a_crossfade_inside_the_actual_overlap():
    engine, track, first, second = engine_with_pictures(
        np.full((180, 320, 3), 10, np.uint8), np.full((180, 320, 3), 40, np.uint8),
        left=(0, 30), right=(30, 30))
    session = engine.session
    session.set_selection({second.id})
    assert session.move_selection_to([second.id], -10, 0)
    assert second.timeline_start == 20 and second.duration == 30
    transition = engine.timeline.transition_between(first.id, second.id)
    assert transition is not None
    assert engine.timeline.transition_window(transition) == (20, 30)
    session.undo()
    assert engine.timeline.transitions == []
    assert engine.timeline.find_clip(second.id).timeline_start == 30
    session.redo()
    assert engine.timeline.transition_between(first.id, second.id) is not None


def test_render_dissolves_both_source_times_instead_of_fading_to_black():
    ramp = np.zeros((180, 320, 3), np.uint8)
    engine, track, first, second = engine_with_pictures(
        np.full((180, 320, 3), 10, np.uint8), ramp)
    # B is a ramp: its source frame number is written into every channel.
    entry = engine.media._entries["b.png"]
    entry.frames = [np.full((180, 320, 3), (index * 4) % 256, np.uint8)
                    for index in range(90)]
    engine.timeline.create_auto_transitions([second.id])

    frame_25, _ = engine.render_frame(25, 1.0)
    # 25 is halfway through the overlap; B is at source frame 5 (start 20), not 25.
    mid = frame_25.reshape(-1, 3).mean(axis=0)
    assert mid == pytest.approx([10 * 0.5 + 20 * 0.5] * 3, abs=1.2)
    assert mid.min() > 5.0, "an opaque dissolve must never pass through the background"

    frame_29, _ = engine.render_frame(29, 1.0)
    late = frame_29.reshape(-1, 3).mean(axis=0)
    assert late[0] > mid[0] + 10, "B advances at its own source rate through the overlap"

    before, _ = engine.render_frame(19, 1.0)
    assert before.reshape(-1, 3).mean() == pytest.approx(10.0, abs=0.6)
    after, _ = engine.render_frame(31, 1.0)
    assert after.reshape(-1, 3).mean() == pytest.approx(44.0, abs=0.6), \
        "after the overlap only B remains (source frame 11)"


def test_text_and_shape_clips_are_compatible_but_adjustment_layers_are_not():
    timeline = Timeline(30)
    track = timeline.add_track("video")
    text = timeline.new_clip("title", "", track, 0, 30, text={"text": "Hi"})
    shape = timeline.new_clip("shape", "", track, 20, 30, shape={"kind": "rect"})
    assert timeline.transition_compatible(text, shape)
    assert timeline.add_transition(text.id, shape.id) is not None
    adjustment = timeline.new_clip("grade", "", track, 0, 30, adjustment=True)
    plain = timeline.new_clip("plain", "a.png", track, 20, 30)
    assert not timeline.transition_compatible(adjustment, plain)
    assert timeline.add_transition(adjustment.id, plain.id) is None


# --------------------------------------------------------------------------- audio
def tone(seconds=2.0, rate=48000, frequency=220.0):
    times = np.arange(int(seconds * rate)) / float(rate)
    return np.sin(2 * np.pi * frequency * times).astype(np.float32)[:, None]


def audio_pair_timeline(gain=0.5):
    timeline = Timeline(30)
    track = timeline.add_track("audio")
    first = timeline.new_clip("A", "a.wav", track, 0, 40, source_out=60,
                              fade_in=5, fade_out=0, gain=-6.0)
    second = timeline.new_clip("B", "b.wav", track, 30, 40, source_out=60,
                               fade_out=5, gain=-6.0)
    transition = timeline.add_transition(first.id, second.id)
    return timeline, track, first, second, transition


def test_linked_audio_crossfades_with_equal_power_and_keeps_automation():
    timeline, track, first, second, transition = audio_pair_timeline()
    assert transition.audio_curve == "equal_power"
    segments = audio_plan.build_segments(timeline, lambda _path: tone(), 48000)
    outgoing = [s for s in segments if s.transition_out][0]
    incoming = [s for s in segments if s.transition_in][0]
    assert outgoing.transition_out == (1.0, 4.0 / 3.0, "equal_power")
    assert incoming.transition_in == (1.0, 4.0 / 3.0, "equal_power")

    midpoint = int(round((1.0 + 4.0 / 3.0) / 2 * 48000))
    count = int(1.5 * 48000)
    out_block = audio_mix.render([outgoing], 0.0, count, 48000)
    in_block = audio_mix.render([incoming], 0.0, count, 48000)
    out_gain = float(np.abs(out_block[midpoint - 200:midpoint + 200, 0]).max())
    in_gain = float(np.abs(in_block[midpoint - 200:midpoint + 200, 0]).max())
    gain = 10 ** (-6.0 / 20.0)
    assert out_gain == pytest.approx(np.sqrt(0.5) * gain, abs=0.02)
    assert in_gain == pytest.approx(np.sqrt(0.5) * gain, abs=0.02)
    # The clip gain (-6 dB) rides underneath the crossfade, so the pair stays quieter
    # than either clip at unity; the crossfade did not replace the gain headroom.
    both = audio_mix.render(segments, 0.0, count, 48000)
    assert np.isfinite(both).all() and np.abs(both).max() < 1.6
    assert both.shape[0] == count

    # The fade on the incoming clip is still applied after the transition window.
    tail = in_block[-1, 0]
    assert abs(float(tail)) < 0.05


def test_one_sided_audio_keeps_the_video_transition_and_plays_predictably():
    engine, track, first, second = engine_with_pictures(
        np.full((180, 320, 3), 10, np.uint8), np.full((180, 320, 3), 40, np.uint8),
        left=(0, 30), right=(30, 30))
    audio_track = engine.timeline.add_track("audio")
    linked = engine.timeline.new_clip("A audio", "a.wav", audio_track, 0, 30,
                                      source_out=30, stream="audio")
    engine.timeline.assign_link([first.id, linked.id])
    engine.session.set_selection({second.id})
    engine.session.move_selection_to([second.id], -10, 0)
    assert engine.timeline.transition_between(first.id, second.id) is not None
    assert [t for t in engine.timeline.transitions if t.out_id == linked.id
            or t.in_id == linked.id] == [], "a single-sided pair gets no fake audio record"
    segments = audio_plan.build_segments(
        engine.timeline, lambda _path: np.ones((48000, 1), np.float32), 48000)
    assert len(segments) == 1
    # The lone sound follows its picture: the outgoing side fades down over the same
    # overlap instead of playing at full level and cutting when the clip ends.
    assert segments[0].transition_in == ()
    assert segments[0].transition_out == (20 / 30.0, 1.0, "equal_power")


def test_linked_video_and_audio_move_and_crossfade_together():
    engine, video_track, first, second = engine_with_pictures(
        np.full((180, 320, 3), 10, np.uint8), np.full((180, 320, 3), 40, np.uint8),
        left=(0, 30), right=(30, 30))
    audio_track = engine.timeline.add_track("audio")
    first_audio = engine.timeline.new_clip("A audio", "a.wav", audio_track, 0, 30,
                                          source_out=30, stream="audio")
    second_audio = engine.timeline.new_clip("B audio", "b.wav", audio_track, 30, 30,
                                           source_out=30, stream="audio")
    engine.timeline.assign_link([first.id, first_audio.id])
    engine.timeline.assign_link([second.id, second_audio.id])
    engine.session.set_selection({second.id})
    assert engine.session.move_selection_to([second.id], -10, 0)
    assert second_audio.timeline_start == 20, "the linked sound follows the picture"
    video_transition = engine.timeline.transition_between(first.id, second.id)
    audio_transition = engine.timeline.transition_between(first_audio.id, second_audio.id)
    assert video_transition is not None and audio_transition is not None
    assert engine.timeline.transition_window(video_transition) == (20, 30)
    assert engine.timeline.transition_window(audio_transition) == (20, 30)
    assert first_audio.timeline_start == 0, "the unrelated pair does not move"

    # Moving the picture away removes both records, not just the video one.
    engine.session.move_selection_to([second.id], 10, 0)
    assert engine.timeline.transitions == []


def test_reverse_speed_and_frame_rate_clips_still_crossfade():
    ramp = [np.full((180, 320, 3), (index * 3) % 256, np.uint8) for index in range(90)]
    engine, track, first, second = engine_with_pictures(
        np.full((180, 320, 3), 10, np.uint8), ramp)
    engine.media._entries["b.png"].frames = ramp
    # 24 fps source on a 30 fps timeline, reversed and double speed; the outgoing clip
    # is a different resolution and carries an effect.
    second.source_fps = 24.0
    second.reverse = True
    second.speed = 1.5
    first.effects = [{"effect_id": "blur", "enabled": True, "params": {"radius": 2.0}}]
    engine.media._entries["a.png"].frames = [
        np.full((90, 160, 3), 10, np.uint8) for _ in range(90)]
    assert engine.timeline.create_auto_transitions([second.id])
    for frame_no in (20, 25, 29):
        picture, _ = engine.render_frame(frame_no, 1.0)
        assert picture.shape == (180, 320, 3)
        assert picture.reshape(-1, 3).mean() > 3.0
    before, _ = engine.render_frame(19, 1.0)
    assert before.reshape(-1, 3).mean() == pytest.approx(10.0, abs=0.8)


def test_keyframed_opacity_applies_on_top_of_the_transition():
    from meenifx.model import transform as transforms
    from meenifx.model.keyframes import AnimatedValue, Keyframe
    engine, track, first, second = engine_with_pictures(
        np.full((180, 320, 3), 10, np.uint8), np.full((180, 320, 3), 40, np.uint8))
    first.transform["opacity"] = AnimatedValue(
        [Keyframe(20, 100.0), Keyframe(30, 50.0)], 100.0)
    engine.timeline.create_auto_transitions([second.id])
    # The clip's own animation keeps running through the dissolve instead of being
    # replaced by the transition weight.
    assert transforms.opacity_at(first, 20) == pytest.approx(1.0)
    assert transforms.opacity_at(first, 29) == pytest.approx(0.55, abs=0.01)
    for frame_no in (20, 25, 29):
        picture, _ = engine.render_frame(frame_no, 1.0)
        assert picture.reshape(-1, 3).mean() > 3.0


# ------------------------------------------------------------------- editing rules
def test_trimming_updates_then_removes_the_transition():
    timeline, _track, first, second, transition = two_clips()
    timeline.apply_trim(timeline.snapshot(), second.id, "left", 8)
    assert timeline.transition_window(transition) == (28, 30)
    timeline.apply_trim(timeline.snapshot(), first.id, "right", -10)
    assert timeline.transition_between(first.id, second.id) is None


def test_split_keeps_the_surviving_half_and_never_leaves_stale_ids():
    timeline, _track, first, second, _transition = two_clips()
    timeline.split_clip(second.id, 25)
    surviving = timeline.transition_between(first.id, second.id)
    assert surviving is not None and timeline.transition_window(surviving) == (20, 25)
    for transition in timeline.transitions:
        assert timeline.find_clip(transition.out_id) is not None
        assert timeline.find_clip(transition.in_id) is not None


def test_delete_duplicate_and_ripple_prune_cleanly():
    timeline, track, first, second, _transition = two_clips()
    session = TimelineSession(timeline)
    session.set_selection({first.id})
    session.duplicate_selected()
    assert all(timeline.find_clip(t.out_id) and timeline.find_clip(t.in_id)
               for t in timeline.transitions)
    timeline.delete_clips([cid for cid in session.selected if cid != first.id])
    timeline.delete_clips([second.id], ripple=True)
    timeline.delete_clips([second.id], ripple=True)
    assert timeline.transitions == []
    assert timeline.find_clip(first.id).timeline_start >= 0


def test_removing_overlap_by_moving_a_clip_removes_the_transition():
    timeline, _track, first, second, _transition = two_clips()
    session = TimelineSession(timeline)
    session.set_selection({second.id})
    session.move_selection_to([second.id], 10, 0)
    assert timeline.transitions == []
    session.undo()
    assert timeline.transition_between(first.id, second.id) is not None


# --------------------------------------------------------------- project and prefs
def test_transitions_round_trip_and_old_overlaps_stay_untouched():
    timeline, track, first, second, transition = two_clips()
    sections = serialize.timeline_to_sections(timeline, lambda _path: 1)
    assert sections["transitions"]["transitions"][0]["out"] == first.id
    loaded = serialize.sections_to_timeline(sections, 30, lambda _media: "a.png")
    assert loaded.transition_between(first.id, second.id).state() == transition.state()
    assert loaded.transition_window(loaded.transitions[0]) == (20, 30)

    old = dict(sections)
    old.pop("transitions")
    migrated = C.migrate(old, 8)
    assert migrated["transitions"] == {"transitions": []}
    old_timeline = serialize.sections_to_timeline(migrated, 30, lambda _media: "a.png")
    assert old_timeline.transitions == []
    assert len(old_timeline.video_layers_at(old_timeline.tracks[0], 25)) == 1, \
        "an old overlap still renders as the topmost clip, not as a new transition"


def test_container_writes_and_reads_transition_settings(tmp_path):
    timeline, track, first, second, transition = two_clips()
    transition.curve = "linear"
    transition.audio_curve = "linear"
    library = __import__("meenifx.project", fromlist=["MediaLibrary"]).MediaLibrary()
    data = C.ProjectData()
    settings = ProjectSettings.from_preset("HD 1080p 30", "Transitions")
    data["settings"] = settings.to_dict()
    for key, value in serialize.timeline_to_sections(timeline, lambda path: library.add(path).id).items():
        data[key] = value
    data["media"] = library.to_dict()
    data["meta"] = C.make_meta("Transitions", settings, timeline.duration, 2)
    data["ui"] = {}
    path = os.path.join(tmp_path, "transitions.mfxpf")
    C.write_container(path, data)
    read = C.read_container(path)
    loaded = serialize.sections_to_timeline(read.sections, 30, lambda media: "a.png")
    assert len(loaded.transitions) == 1
    assert loaded.transitions[0].state() == (first.id, second.id, "crossfade",
                                             "linear", "linear")
    assert loaded.transitions[0].label == "Crossfade"


def test_timeline_preference_defaults_on_and_saves(tmp_path, monkeypatch):
    from PyQt6.QtCore import QSettings
    path = os.path.join(tmp_path, "prefs.ini")
    monkeypatch.setattr(settings_module, "app_settings",
                        lambda: QSettings(path, QSettings.Format.IniFormat))
    assert TimelineSettings.load().auto_overlap_transitions is True
    changed = TimelineSettings({"auto_overlap_transitions": False})
    changed.save()
    assert TimelineSettings.load().auto_overlap_transitions is False


# ------------------------------------------------------------------------- UI smoke
def test_widget_hit_tests_and_paints_the_transition_region(qapp):
    engine, track, first, second = engine_with_pictures(
        np.full((180, 320, 3), 10, np.uint8), np.full((180, 320, 3), 40, np.uint8))
    engine.timeline.create_auto_transitions([second.id])
    from meenifx.ui.timeline_widget import TimelineCanvas
    widget = TimelineCanvas(engine)
    widget.resize(900, 500)
    widget.ppf = 6.0
    y = int(widget.header_y(track) + widget.track_height(track) / 2)
    x = int(widget.x_at(25))
    assert widget._transition_at(x, y) is not None
    assert widget._transition_at(int(widget.x_at(5)), y) is None
    pixmap = widget.grab()
    assert not pixmap.isNull()
    widget.deleteLater()
