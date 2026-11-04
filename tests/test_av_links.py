"""Linked video/audio clips at the model level: placement, sync, editing, undo, projects.

No Qt and no media files: these cover the parts that must be right regardless of the
widgets (two clips from one entry, one link id, shared undo, serialization).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.model.session import TimelineSession  # noqa: E402
from meenifx.model.timeline import Clip, CommandStack, Timeline  # noqa: E402
from meenifx.project import container as C  # noqa: E402
from meenifx.project.media import MediaLibrary  # noqa: E402
from meenifx.project.serialize import sections_to_timeline, timeline_to_sections  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402

MOVIE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "mosaic-output.mp4")


def av_entry(name="movie.mp4", duration=90, audio=True, path=MOVIE):
    return {"name": name, "path": path, "duration": duration, "type": "video",
            "audio": audio, "source_out": 100, "source_fps": 30}


def fresh_timeline():
    timeline = Timeline(30)
    timelime_video = timeline.add_track("video")
    timeline.add_track("audio")
    return timeline


def test_video_with_audio_creates_a_linked_pair_on_separate_tracks():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry()])
    assert len(ids) == 2, ids
    video, audio = (timeline.find_clip(ids[0]), timeline.find_clip(ids[1]))
    assert video.track.type == "video" and audio.track.type == "audio"
    assert video.stream == "video" and audio.stream == "audio"
    assert video.source_path == audio.source_path == MOVIE
    assert video.timeline_start == audio.timeline_start == 0
    assert (video.source_in, video.source_out) == (audio.source_in, audio.source_out)
    assert video.link_id and video.link_id == audio.link_id
    assert timeline.linked_pairs(video) == [audio]
    assert timeline.link_is_synced(video)
    # The audio lane sits directly under the video lane it belongs to.
    assert timeline.tracks.index(audio.track) == timeline.tracks.index(video.track) + 1


def test_video_without_audio_creates_only_the_video_clip():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(audio=False)])
    assert len(ids) == 1
    clip = timeline.find_clip(ids[0])
    assert clip.track.type == "video" and clip.link_id == 0


def test_audio_only_entry_creates_only_an_audio_clip():
    timeline = fresh_timeline()
    ids = timeline.place_media([{"name": "bed.wav", "path": "bed.wav", "duration": 120,
                                 "type": "audio"}])
    assert len(ids) == 1
    clip = timeline.find_clip(ids[0])
    assert clip.track.type == "audio" and clip.stream == "audio" and clip.link_id == 0


def test_missing_tracks_are_created_and_paired_underneath():
    timeline = Timeline(30)
    timeline.add_track("video")
    ids = timeline.place_media([av_entry()])
    assert len(ids) == 2
    audio = timeline.find_clip(ids[1])
    assert audio.track in timeline.tracks_of_type("audio")
    assert timeline.tracks.index(audio.track) == timeline.tracks.index(timeline.find_clip(ids[0]).track) + 1


def test_a_locked_audio_track_cancels_the_whole_drop():
    timeline = fresh_timeline()
    timeline.tracks_of_type("audio")[0].locked = True
    assert timeline.place_media([av_entry()]) == []
    assert timeline.all_clips() == [], "nothing may be left behind when the pair cannot be placed"


def test_both_clips_are_pushed_past_occupied_lanes_together():
    timeline = fresh_timeline()
    first = timeline.place_media([av_entry(duration=60)])
    assert len(first) == 2
    second = timeline.place_media([av_entry(duration=30)])
    video, audio = timeline.find_clip(second[0]), timeline.find_clip(second[1])
    assert video.timeline_start == audio.timeline_start == 60
    assert video.link_id == audio.link_id and video.link_id != timeline.find_clip(first[0]).link_id


def test_preview_plan_does_not_touch_the_timeline():
    timeline = fresh_timeline()
    placements, ok, _reason = timeline.plan_media([av_entry()], start=15, create=False)
    assert ok and len(placements) == 2
    assert timeline.all_clips() == []
    assert placements[0]["start"] == placements[1]["start"] == 15
    assert placements[0]["track"].type == "video" and placements[1]["track"].type == "audio"


def test_trimming_one_clip_trims_its_partner_too():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=90)])
    session = TimelineSession(timeline)
    snap = timeline.snapshot()
    applied = timeline.apply_trim(snap, ids[0], "left", 10, also=[ids[1]])
    assert applied == 10
    # Trimming restores the snapshot first, so the live clips are fresh objects.
    video, audio = timeline.find_clip(ids[0]), timeline.find_clip(ids[1])
    assert video.timeline_start == audio.timeline_start == 10
    assert video.duration == audio.duration == 80
    assert video.source_in == audio.source_in
    assert timeline.link_is_synced(video), timeline.link_offset(video)
    del session


def test_trim_limits_come_from_the_tightest_member():
    timeline = fresh_timeline()
    ids = timeline.place_media([{"name": "m.mp4", "path": MOVIE, "duration": 90, "type": "video",
                                 "audio": True, "source_in": 12, "source_out": 100,
                                 "source_fps": 30}], start=40)
    # Both start twelve frames into the source, but the partner's usable range starts at
    # 8, so only four frames of headroom are left for the pair (the video could give 12).
    for cid in ids:
        timeline.find_clip(cid).source_min = 0
    audio = timeline.find_clip(ids[1])
    audio.source_min = 8
    assert timeline.link_is_synced(timeline.find_clip(ids[0]))
    snap = timeline.snapshot()
    applied = timeline.apply_trim(snap, ids[0], "left", -10, also=[ids[1]])
    assert applied == -4, applied
    video, audio = timeline.find_clip(ids[0]), timeline.find_clip(ids[1])
    assert video.timeline_start == audio.timeline_start == 36
    assert timeline.link_is_synced(video)


def test_splitting_a_linked_pair_splits_both_and_keeps_one_group():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=90)])
    session = TimelineSession(timeline)
    video = timeline.find_clip(ids[0])
    session.set_selection({video.id})
    session.set_playhead(40)
    session.split_at_playhead()
    clips = timeline.all_clips()
    assert len(clips) == 4, [(c.name, c.track.type, c.timeline_start) for c in clips]
    assert len({c.link_id for c in clips}) == 1
    assert timeline.link_is_synced(clips[0]), [(c.timeline_start, c.sync_key()) for c in clips]
    left = [c for c in clips if c.timeline_start == 0]
    right = [c for c in clips if c.timeline_start == 40]
    assert len(left) == 2 and len(right) == 2
    assert {c.track.type for c in right} == {"video", "audio"}


def test_delete_removes_the_linked_partner_and_undo_restores_it():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=90)])
    session = TimelineSession(timeline)
    video = timeline.find_clip(ids[0])
    session.set_selection({video.id})
    assert session.selected == set(ids), "selecting one clip selects the group"
    session.delete_selected()
    assert timeline.all_clips() == []
    session.undo()
    assert len(timeline.all_clips()) == 2
    restored = timeline.find_clip(ids[0])
    assert restored.link_id == timeline.find_clip(ids[1]).link_id


def test_linked_selection_off_allows_editing_one_clip_only():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=90)])
    session = TimelineSession(timeline)
    video, audio = timeline.find_clip(ids[0]), timeline.find_clip(ids[1])
    session.set_linked_selection(False)
    session.select({video.id})
    assert session.selected == {video.id}
    session.delete_selected()
    alive = timeline.all_clips()
    assert [c.id for c in alive] == [audio.id]
    assert audio.link_id != 0, "the link is kept, it is only the selection that stops following it"


def test_unlink_then_move_then_relink_preserves_positions():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=90)])
    session = TimelineSession(timeline)
    video, audio = timeline.find_clip(ids[0]), timeline.find_clip(ids[1])
    session.set_selection(set(ids))
    assert session.unlink_selected()
    assert video.link_id == audio.link_id == 0
    session.move_selection_to([audio.id], 12, 0, "snap")
    assert audio.timeline_start == 12 and video.timeline_start == 0
    session.select({video.id, audio.id})
    assert session.link_selected()
    assert video.link_id == audio.link_id != 0
    assert (video.timeline_start, audio.timeline_start) == (0, 12), "relinking never moves clips"
    assert not timeline.link_is_synced(video)
    assert round(timeline.link_offset(audio)) == 12


def test_one_undo_step_per_link_and_unlink():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=90)])
    session = TimelineSession(timeline)
    stack = CommandStack.__new__(CommandStack)   # only the label bookkeeping matters here
    video, audio = timeline.find_clip(ids[0]), timeline.find_clip(ids[1])
    session.set_selection(set(ids))
    session.unlink_selected()
    session.undo()
    assert timeline.find_clip(video.id).link_id == timeline.find_clip(audio.id).link_id != 0
    del stack


def test_moving_a_linked_pair_keeps_them_together():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=60)])
    session = TimelineSession(timeline)
    session.set_selection({ids[0]})
    assert session.selected == set(ids)
    assert session.move_selection_to(list(session.selected), 25, 1, "snap")
    video, audio = timeline.find_clip(ids[0]), timeline.find_clip(ids[1])
    assert video.timeline_start == audio.timeline_start == 25
    assert video.track.type == "video" and audio.track.type == "audio"


def test_copy_paste_keeps_the_pairing_but_with_a_new_link():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=60)])
    session = TimelineSession(timeline)
    session.set_selection(set(ids))
    assert session.copy_selected()
    session.set_playhead(120)
    session.set_selection(set())
    session.paste()
    copies = [c for c in timeline.all_clips() if c.id not in ids]
    assert len(copies) == 2
    assert len({c.link_id for c in copies}) == 1
    assert copies[0].link_id not in {timeline.find_clip(i).link_id for i in ids}


def test_yes_project_roundtrip_keeps_streams_and_links():
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=60)])
    library = MediaLibrary()
    sections = timeline_to_sections(timeline, lambda p: library.add(p).id)
    data = C.ProjectData()
    settings = ProjectSettings()
    data["settings"] = settings.to_dict()
    for key, value in sections.items():
        data[key] = value
    data["media"] = library.to_dict()
    data["meta"] = C.make_meta("T", settings, timeline.duration, 0)
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "linked.mfxpf")
    C.write_container(path, data)
    loaded = C.read_container(path)
    lib2 = MediaLibrary.from_dict(loaded["media"])
    rebuilt = sections_to_timeline(loaded.sections, 30, lambda mid: lib2.items[mid].path)
    clips = rebuilt.all_clips()
    assert len(clips) == 2
    assert {c.stream for c in clips} == {"video", "audio"}
    assert clips[0].link_id == clips[1].link_id != 0
    assert rebuilt.link_is_synced(clips[0])
    assert rebuilt.new_link_id() not in {clips[0].link_id}


def test_yes_old_projects_keep_playing_their_own_audio():
    """Format 1 clips had no stream column: they carry both streams and must stay audible."""
    sections = {
        "tracks": {"tracks": [{"id": 1, "type": "video", "name": "Video 1"}], "counters": {}, "max_clip": 1},
        "clips": {"id": [1], "track": [0], "media": [""], "name": ["old.mp4"], "start": [0], "dur": [30],
                  "src_in": [0], "src_out": [30], "src_min": [0], "src_max": [30], "src_fps": [30.0],
                  "tl_fps": [30.0], "speed": [1.0], "reverse": [False], "loop": [False], "freeze": [False],
                  "offset": [0.0], "volume": [1.0], "fade_in": [0], "fade_out": [0],
                  "fade_in_curve": [0.0], "fade_out_curve": [0.0]},
        "effects": {"clip": [], "effect_id": [], "enabled": [], "params": []},
        "keyframes": {"owners": [], "owner": [], "time": [], "value": [], "interp": [], "interp_names": [],
                      "in_influence": [], "out_influence": [], "in_value_handle": [], "out_value_handle": []},
        "markers": {"markers": [], "loop_in": None, "loop_out": None, "loop_enabled": False,
                    "loop_at_end": True},
    }
    migrated = C.migrate(sections, 1)
    timeline = sections_to_timeline(migrated, 30, lambda _mid: "")
    clip = timeline.all_clips()[0]
    assert clip.stream == "both" and clip.link_id == 0
    assert clip.carries_audio is True


def test_clip_defaults_keep_old_call_sites_working():
    track = Timeline(30).add_track("audio")
    clip = Clip(1, "a", "a.wav", track, 0, 10)
    assert clip.stream == "audio" and clip.link_id == 0 and clip.carries_audio
    video_track = Timeline(30).add_track("video")
    assert Clip(2, "v", "v.mp4", video_track, 0, 10).stream == "video"


# --------------------------------------------------------------- audio mixing
def _samples(seconds=4.0, rate=48000):
    import numpy as np
    import math
    time_axis = np.arange(int(rate * seconds), dtype=np.float32) / rate
    tone = 0.5 * np.sin(2 * math.pi * 220.0 * time_axis).astype(np.float32)
    return np.stack([tone, tone], axis=1)


def test_the_pair_produces_exactly_one_mixer_segment():
    from meenifx.audio.plan import build_segments
    timeline = fresh_timeline()
    ids = timeline.place_media([av_entry(duration=60)])
    library = {MOVIE: _samples()}
    segments = build_segments(timeline, lambda path: library.get(path), 48000)
    assert len(segments) == 1, "the video clip must not play the same sound as the audio clip"
    assert segments[0].start_seconds == 0.0
    assert segments[0].end_seconds == pytest_approx(60 / 30.0)
    assert ids


def pytest_approx(value, tolerance=1e-6):
    return _Approx(value, tolerance)


class _Approx:
    def __init__(self, value, tolerance):
        self.value, self.tolerance = value, tolerance

    def __eq__(self, other):
        return abs(float(other) - self.value) <= self.tolerance

    def __repr__(self):
        return f"~{self.value}"


def test_muting_the_audio_lane_silences_only_the_sound():
    from meenifx.audio.plan import build_segments
    timeline = fresh_timeline()
    timeline.place_media([av_entry(duration=60)])
    audio_lane = timeline.tracks_of_type("audio")[0]
    audio_lane.muted = True
    assert build_segments(timeline, lambda path: _samples(), 48000) == []
    video_lane = timeline.tracks_of_type("video")[0]
    assert video_lane.muted is False and video_lane.visible is True


def test_hiding_the_video_lane_keeps_the_linked_audio_playing():
    from meenifx.audio.plan import build_segments
    timeline = fresh_timeline()
    timeline.place_media([av_entry(duration=60)])
    timeline.tracks_of_type("video")[0].visible = False
    segments = build_segments(timeline, lambda path: _samples(), 48000)
    assert len(segments) == 1, "the audio clip is a separate lane: hiding video must not mute it"


def test_trimmed_audio_clip_uses_its_source_timestamps():
    from meenifx.audio.plan import build_segments
    timeline = fresh_timeline()
    ids = timeline.place_media([{"name": "m.mp4", "path": MOVIE, "duration": 90, "type": "video",
                                 "audio": True, "source_in": 30, "source_out": 100,
                                 "source_fps": 30}], start=60)
    audio_clip = timeline.find_clip(ids[1])
    assert audio_clip.source_in == 30
    segments = build_segments(timeline, lambda path: _samples(), 48000)
    assert len(segments) == 1
    segment = segments[0]
    assert segment.start_seconds == pytest_approx(60 / 30.0)
    assert segment.base_seconds == pytest_approx(30 / 30.0), "trimmed in-point must be honoured"
    assert segment.rate == 48000
    # Playback from the clip start must read source second 1.0, not 0.0.
    assert segment.source_seconds(segment.start_seconds) == pytest_approx(1.0)


def test_the_mixed_buffer_carries_the_sound_and_not_double_of_it():
    """What the device would actually be handed at the clip start."""
    from meenifx.audio.mix import render
    from meenifx.audio.plan import build_segments
    timeline = fresh_timeline()
    timeline.place_media([av_entry(duration=60)])
    segments = build_segments(timeline, lambda path: _samples(), 48000)
    block = render(segments, 0.0, 4800, 48000, 2)
    peak = float(abs(block).max())
    loudest = float(abs(_samples()[:4800]).max())
    assert peak > 0.1, "the linked audio clip must reach the output device"
    assert peak <= loudest + 1e-6, "the pair must not be summed twice"
    # A little later the same clip still plays; past its end the buffer is silent.
    assert float(abs(render(segments, 1.5, 2400, 48000, 2)).max()) > 0.1
    assert float(abs(render(segments, 60 / 30.0 + 0.2, 2400, 48000, 2)).max()) == 0.0
