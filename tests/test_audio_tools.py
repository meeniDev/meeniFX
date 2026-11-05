"""Remove Silences and Normalize Audio: detection, one-step undo, link safety."""

import numpy as np
import pytest

from conftest import build_engine
from meenifx.audio import silence
from meenifx.ui.audio_tools import peak_db


def tone(seconds, rate=48000, amplitude=0.4, frequency=220.0):
    times = np.arange(int(seconds * rate)) / float(rate)
    return (amplitude * np.sin(2 * np.pi * frequency * times)).astype(np.float32)


def sample_audio():
    """0.5 s speech, 1.0 s silence, 0.5 s speech."""
    return np.concatenate([tone(0.5), np.zeros(48000, np.float32), tone(0.5)])


def test_silence_detection_finds_the_gap():
    audio = sample_audio()
    settings = silence.Settings()
    regions = silence.speech_regions(audio, 48000, settings)
    assert len(regions) == 2
    assert regions[0][0] == pytest.approx(0.0, abs=0.05)
    assert regions[0][1] == pytest.approx(0.5, abs=0.06)
    cuts = silence.cut_plan(regions, 2.0, settings)
    assert len(cuts) == 1
    start, end = cuts[0]
    assert start == pytest.approx(0.6, abs=0.08), "the cut starts after the speech + padding"
    assert end == pytest.approx(1.4, abs=0.08), "and ends before the next speech + padding"


def test_keeping_natural_pauses_removes_less():
    audio = sample_audio()
    regions = silence.speech_regions(audio, 48000, silence.Settings())
    full = silence.cut_plan(regions, 2.0, silence.Settings())
    polite = silence.cut_plan(regions, 2.0, silence.Settings(keep_pause=0.3))
    assert silence.total_removed(polite) < silence.total_removed(full)
    assert silence.total_removed(polite) == pytest.approx(
        silence.total_removed(full) - 0.3, abs=0.02)


def test_quiet_audio_is_not_all_silence():
    audio = tone(1.0, amplitude=0.01)          # about -40 dBFS
    quiet = silence.speech_regions(audio, 48000, silence.Settings(threshold_db=-38.0))
    assert quiet == [], "a quiet but continuous signal counts as silence at -38 dB"
    loud = silence.speech_regions(audio, 48000, silence.Settings(threshold_db=-48.0))
    assert loud, "lowering the threshold finds it again"


def test_remove_silence_is_one_undo_step():
    engine, clip = build_engine(frames=60, duration=60)
    session = engine.session
    session.remove_silence(clip.id, [(0.0, 0.5)], crossfade_frames=6)      # first 15 frames
    remaining = engine.timeline.all_clips()
    assert len(remaining) == 1
    assert remaining[0].timeline_start == 0 and remaining[0].duration == 45
    assert remaining[0].fade_in == 6, "a crossfade was applied at the cut"
    session.undo()
    restored = engine.timeline.all_clips()
    assert len(restored) == 1 and restored[0].duration == 60


def _linked_pair(engine):
    timeline = engine.timeline
    video_track = timeline.tracks_of_type("video")[0]
    audio_track = timeline.add_track("audio")
    video = timeline.new_clip("pair video", "synthetic-effect-test.png", video_track, 0, 60,
                              source_out=60, link_id=0, stream="video")
    audio = timeline.new_clip("pair audio", "synthetic-effect-test.png", audio_track, 0, 60,
                              source_out=60, stream="audio")
    link = timeline.assign_link([video.id, audio.id])
    return video, audio, link


def test_removing_silence_keeps_a_linked_pair_synced_and_repaired():
    engine, _clip = build_engine(frames=60, duration=60)
    engine.timeline.tracks[0].clips.clear()        # only the pair takes part
    video, audio, old_link = _linked_pair(engine)
    engine.session.remove_silence(video.id, [(10 / 30.0, 20 / 30.0)], crossfade_frames=0)
    clips = engine.timeline.all_clips()
    assert len(clips) == 4, "two surviving pieces on each lane, the cut in between"
    by_start = {}
    for clip in clips:
        by_start.setdefault(clip.timeline_start, []).append(clip)
    assert sorted(by_start) == [0, 10], "the 10-frame gap is gone and the tail moved up"
    for start, group in by_start.items():
        assert len(group) == 2, "each surviving piece keeps its partner"
        assert group[0].link_id == group[1].link_id != 0
        assert group[0].link_id != old_link
        assert group[0].duration == group[1].duration

    tail = by_start[10]
    engine.session.set_selection({tail[0].id})
    engine.session.move_selection_to([tail[0].id], 10, 0)
    for clip in tail:
        assert engine.timeline.find_clip(clip.id).timeline_start == 20, \
            "the rebuilt pair still moves together"
    head = by_start[0]
    assert engine.timeline.find_clip(head[0].id).timeline_start == 0, \
        "and the other piece does not"


def test_split_linked_pair_creates_two_independent_pairs():
    engine, clip = build_engine(frames=60, duration=60)
    engine.timeline.tracks[0].clips.clear()
    video, audio, old_link = _linked_pair(engine)
    engine.session.set_selection({video.id})
    engine.session.set_playhead(20)
    engine.session.split_at_playhead()
    clips = engine.timeline.all_clips()
    assert len(clips) == 4
    left = [c for c in clips if c.timeline_start == 0]
    right = [c for c in clips if c.timeline_start == 20]
    assert len(left) == 2 and len(right) == 2
    assert left[0].link_id == left[1].link_id
    assert right[0].link_id == right[1].link_id
    assert left[0].link_id != right[0].link_id, "each half is its own pair"

    right_video, right_audio = right
    engine.session.set_selection({right_video.id})
    engine.session.move_selection_to([right_video.id], 10, 0)
    assert engine.timeline.find_clip(right_video.id).timeline_start == 30
    assert engine.timeline.find_clip(right_audio.id).timeline_start == 30
    assert engine.timeline.find_clip(left[0].id).timeline_start == 0, \
        "the other pair must not move with it"


def _pair_for_scope(engine, video_start=0, audio_start=0, video_path="cam.mp4",
                    audio_path="mic.wav", duration=60):
    timeline = engine.timeline
    video = timeline.new_clip("video", video_path, timeline.tracks_of_type("video")[0],
                              video_start, duration, source_out=duration, stream="video")
    audio_track = (timeline.tracks_of_type("audio") or [timeline.add_track("audio")])[0]
    audio = timeline.new_clip("audio", audio_path, audio_track, audio_start, duration,
                              source_out=duration, stream="audio")
    return video, audio


def test_silence_removal_covers_a_manually_linked_pair_from_two_files():
    engine, _clip = build_engine(frames=60, duration=60)
    engine.timeline.tracks[0].clips.clear()
    for track in engine.timeline.tracks[1:]:
        track.clips.clear()
    video, audio = _pair_for_scope(engine)
    engine.timeline.assign_link([video.id, audio.id])
    engine.session.remove_silence(audio.id, [(10 / 30.0, 20 / 30.0)], crossfade_frames=0)
    clips = engine.timeline.all_clips()
    assert len(clips) == 4
    by_start = {}
    for clip in clips:
        by_start.setdefault(clip.timeline_start, []).append(clip)
    assert sorted(by_start) == [0, 10]
    for group in by_start.values():
        assert len(group) == 2
        assert group[0].link_id == group[1].link_id != 0
    assert engine.timeline.link_is_synced(by_start[10][0])


def test_matching_unlinked_clips_can_be_cut_together():
    """Same source, same offset and speed: a reliable match even without a link."""
    from meenifx.ui.audio_tools import silence_members, synchronised_matches

    engine, _clip = build_engine(frames=60, duration=60)
    engine.timeline.tracks[0].clips.clear()
    for track in engine.timeline.tracks[1:]:
        track.clips.clear()
    video, audio = _pair_for_scope(engine, video_path="same.mp4", audio_path="same.mp4")
    assert audio.link_id == video.link_id == 0

    class _Window:
        def __init__(self, engine):
            self.engine = engine

    window = _Window(engine)
    assert synchronised_matches(window, audio) == [video]
    members = silence_members(window, audio, "matching")
    assert [clip.id for clip in members] == [audio.id, video.id]

    engine.session.remove_silence(audio.id, [(10 / 30.0, 20 / 30.0)],
                                  members=[clip.id for clip in members], crossfade_frames=0)
    clips = engine.timeline.all_clips()
    assert len(clips) == 4
    assert {clip.track.type for clip in clips} == {"video", "audio"}
    by_start = {}
    for clip in clips:
        by_start.setdefault(clip.timeline_start, []).append(clip)
    for group in by_start.values():
        assert len(group) == 2 and group[0].link_id == group[1].link_id != 0


def test_matching_rejects_a_different_source_or_a_drifting_offset():
    from meenifx.ui.audio_tools import synchronised_matches

    engine, _clip = build_engine(frames=80, duration=80)
    engine.timeline.tracks[0].clips.clear()
    for track in engine.timeline.tracks[1:]:
        track.clips.clear()
    video, audio = _pair_for_scope(engine, video_path="one.mp4", audio_path="two.wav")

    class _Window:
        def __init__(self, engine):
            self.engine = engine

    window = _Window(engine)
    assert synchronised_matches(window, audio) == [], "different files are not a match"
    audio.source_path = "one.mp4"
    assert synchronised_matches(window, audio) == [video]
    audio.timeline_start = 5
    assert synchronised_matches(window, audio) == [], "a different offset is not a match"


def test_audio_only_scope_leaves_the_linked_video_alone():
    engine, _clip = build_engine(frames=60, duration=60)
    engine.timeline.tracks[0].clips.clear()
    for track in engine.timeline.tracks[1:]:
        track.clips.clear()
    video, audio = _pair_for_scope(engine)
    engine.timeline.assign_link([video.id, audio.id])
    engine.session.remove_silence(audio.id, [(10 / 30.0, 20 / 30.0)],
                                  members=[audio.id], crossfade_frames=0)
    video_now = engine.timeline.find_clip(video.id)
    assert video_now.duration == 60 and video_now.timeline_start == 0
    assert [clip.name for clip in engine.timeline.all_clips()] == ["video", "audio", "audio"]


def test_a_locked_member_blocks_the_whole_pass():
    engine, _clip = build_engine(frames=60, duration=60)
    engine.timeline.tracks[0].clips.clear()
    for track in engine.timeline.tracks[1:]:
        track.clips.clear()
    video, audio = _pair_for_scope(engine)
    engine.timeline.assign_link([video.id, audio.id])
    video.track.locked = True
    assert engine.session.remove_silence(audio.id, [(10 / 30.0, 20 / 30.0)]) == 0
    assert len(engine.timeline.all_clips()) == 2
    assert engine.timeline.find_clip(audio.id).duration == 60


def test_an_offset_pair_only_cuts_the_span_both_clips_cover():
    engine, _clip = build_engine(frames=150, duration=150)
    engine.timeline.tracks[0].clips.clear()
    for track in engine.timeline.tracks[1:]:
        track.clips.clear()
    video, audio = _pair_for_scope(engine, video_start=0, audio_start=10, duration=100)
    engine.timeline.assign_link([video.id, audio.id])
    engine.session.remove_silence(audio.id, [(1.0, 1.5)],
                                  members=[video.id, audio.id], crossfade_frames=0)
    clips = engine.timeline.all_clips()
    audio_pieces = sorted((clip for clip in clips if clip.track.type == "audio"),
                          key=lambda clip: clip.timeline_start)
    assert [clip.duration for clip in audio_pieces] == [30, 55]
    video_pieces = sorted((clip for clip in clips if clip.track.type == "video"),
                          key=lambda clip: clip.timeline_start)
    assert [clip.duration for clip in video_pieces] == [40, 45], \
        "only the 15 frames both clips cover are removed from the video"
    assert video_pieces[0].timeline_start == 0
    assert video_pieces[0].link_id == audio_pieces[0].link_id != 0
    assert video_pieces[1].link_id == audio_pieces[1].link_id != 0


def test_a_short_retained_fragment_is_swallowed_by_the_cuts():
    settings = silence.Settings(min_silence=0.05, min_speech=0.01, pad_before=0.0,
                                pad_after=0.0, keep_pause=0.0, min_retained=0.2)
    speech = [(0.0, 0.5), (0.55, 0.6), (0.8, 1.0)]
    cuts = silence.cut_plan(speech, 1.0, settings)
    assert len(cuts) == 1, cuts
    assert cuts[0][0] == pytest.approx(0.5, abs=0.01)
    assert cuts[0][1] == pytest.approx(0.8, abs=0.01)


def test_match_scope_is_not_used_for_a_clip_with_no_counterpart():
    from meenifx.ui.audio_tools import silence_members

    engine, _clip = build_engine(frames=60, duration=60)
    audio = engine.timeline.all_clips()[0]

    class _Window:
        def __init__(self, engine):
            self.engine = engine

    assert [clip.id for clip in silence_members(_Window(engine), audio, "matching")] == [audio.id]


def test_normalize_sets_gain_as_one_undo_step():
    engine, clip = build_engine(frames=30, duration=30)
    engine.session.normalize_audio([(clip.id, 7.5)])
    assert engine.timeline.find_clip(clip.id).gain == pytest.approx(7.5)
    engine.session.undo()
    assert engine.timeline.find_clip(clip.id).gain == pytest.approx(0.0)


def test_peak_db_of_a_known_signal():
    assert peak_db(tone(0.1, amplitude=0.5)) == pytest.approx(-6.02, abs=0.05)
    assert peak_db(np.zeros(10, np.float32)) < -100


def test_capture_gaps_become_silence_instead_of_being_closed():
    from meenifx.recording.audiocap import silence_gap

    assert silence_gap(0.0, 0.0, 0, 48000) == 0
    assert silence_gap(5.0, 0.0, 0, 48000) == 5 * 48000, \
        "leading silence the endpoint skipped is kept"
    assert silence_gap(5.02, 5.0, 0, 48000) == 960, \
        "a hole in the middle of the take is filled with silence"
    assert silence_gap(1.0, 1.0, 48000, 48000) == 0
    assert silence_gap(0.5, 1.0, 4800, 48000) == 0, "never pad when the writer is ahead"
    assert silence_gap(900.0, 0.0, 0, 48000) == 0, "a suspend/clock jump is not a gap"


def test_floating_recording_bar_is_off_by_default():
    from meenifx.recording.settings import RecorderSettings

    assert RecorderSettings().floating_bar is False
