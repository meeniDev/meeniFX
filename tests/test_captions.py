"""Captions: timeline clips, styles, grouping, SRT interchange and transcript edits."""

import numpy as np
import pytest

from meenifx.captions import model as CAP
from meenifx.captions import styles as STYLES
from meenifx.captions import subtitles
from meenifx.model import text as TXT
from conftest import build_engine

SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,000
Hello there.

2
00:00:03,500 --> 00:00:05,250
This is a test
of two lines.

3
00:00:07,000 --> 00:00:08,000
<i>Bye</i>
"""


def cues():
    return [
        {"start": 0.0, "end": 1.2, "text": "Hello there",
         "words": [{"start": 0.0, "end": 0.5, "word": "Hello"},
                   {"start": 0.5, "end": 1.2, "word": "there"}]},
        {"start": 1.5, "end": 2.6, "text": "Second line",
         "words": [{"start": 1.5, "end": 2.0, "word": "Second"},
                   {"start": 2.0, "end": 2.6, "word": "line"}]},
    ]


def test_generated_captions_are_text_clips_on_a_dedicated_track():
    engine, _clip = build_engine(frames=90, duration=90)
    session = engine.session
    ids = session.generate_captions(cues(), style_id="classic", start_frame=30,
                                    width=320, height=180, animation="")
    assert len(ids) == 2
    track = CAP.caption_track(engine.timeline)
    assert track is not None and track.name == "Captions"
    assert {clip.track for clip in CAP.caption_clips(engine.timeline)} == {track}
    first = engine.timeline.find_clip(ids[0])
    assert first.is_text and first.timeline_start == 30 and first.duration == 36
    assert TXT.content_at(first, 0) == "Hello there"
    info = CAP.caption_info(first)
    assert info["id"] and info["style"] == "classic" and len(info["words"]) == 2


def test_captions_render_in_the_composited_frame():
    engine, _clip = build_engine(frames=60, duration=60)
    engine.timeline.tracks[0].visible = False        # only the captions show
    blank = engine.render_frame(10, 1.0)[1]
    engine.session.generate_captions(cues(), start_frame=0, width=320, height=180,
                                     animation="", style_id="shorts")
    drawn = engine.render_frame(10, 1.0)[1]
    assert np.abs(drawn.astype(np.int16) - blank.astype(np.int16)).mean() > 1.0


def test_regenerating_a_range_replaces_only_that_range():
    engine, _clip = build_engine(frames=120, duration=120)
    session = engine.session
    session.generate_captions(cues(), start_frame=0, width=320, height=180, animation="")
    before = {clip.id: clip.timeline_start for clip in CAP.caption_clips(engine.timeline)}

    session.generate_captions([{"start": 0.0, "end": 1.0, "text": "New start", "words": []}],
                              start_frame=0, width=320, height=180, animation="",
                              replace_range=(0, 36))
    after = CAP.caption_clips(engine.timeline)
    starts = sorted(clip.timeline_start for clip in after)
    assert starts == [0, 45], "the caption outside the range survived"
    assert TXT.content_at(next(c for c in after if c.timeline_start == 0), 0) == "New start"
    assert set(before) - {c.id for c in after}, "the replaced caption is gone"


def test_srt_round_trip_keeps_times_and_text():
    rows = subtitles.parse_srt(SAMPLE_SRT)
    assert len(rows) == 3
    assert rows[0]["start"] == pytest.approx(1.0) and rows[0]["end"] == pytest.approx(3.0)
    assert rows[1]["text"] == "This is a test\nof two lines."
    assert rows[2]["text"] == "Bye", "inline tags are stripped"

    payload = subtitles.format_srt(rows)
    again = subtitles.parse_srt(payload)
    assert [row["text"] for row in again] == [row["text"] for row in rows]
    for left, right in zip(rows, again):
        assert left["start"] == pytest.approx(right["start"], abs=1e-3)
        assert left["end"] == pytest.approx(right["end"], abs=1e-3)


def test_srt_import_builds_caption_clips():
    engine, _clip = build_engine(frames=300, duration=300)
    rows = subtitles.parse_srt(SAMPLE_SRT)
    ids = engine.session.generate_captions(CAP.cues_from_srt(rows), start_frame=0,
                                           width=320, height=180, animation="",
                                           label="Import subtitles")
    assert len(ids) == 3
    first = engine.timeline.find_clip(ids[0])
    assert first.timeline_start == 30 and first.duration == 60
    info = CAP.caption_info(first)
    assert len(info["words"]) == len(TXT.content_at(first, 0).split())


def test_ass_export_mirrors_the_caption_style():
    engine, _clip = build_engine(frames=120, duration=120)
    engine.session.generate_captions(cues(), start_frame=0, width=320, height=180,
                                     animation="", style_id="minimal")
    clips = CAP.caption_clips(engine.timeline)
    text = subtitles.format_ass(CAP.export_cues(clips, 30), 320, 180, font="Segoe UI",
                                size=46.0, color="#F3F5F8")
    assert "[V4+ Styles]" in text and "PlayResX: 320" in text
    assert "Dialogue: 0,0:00:00.00,0:00:01.20,Captions" in text
    assert "Hello there" in text


def test_phrase_grouping_breaks_at_pauses_and_punctuation():
    segments = [
        {"start": 0.0, "end": 3.0, "text": "Hello there. How are you",
         "words": [{"start": 0.0, "end": 0.4, "word": "Hello"},
                   {"start": 0.4, "end": 0.9, "word": "there."},
                   {"start": 2.0, "end": 2.3, "word": "How"},
                   {"start": 2.3, "end": 2.6, "word": "are"},
                   {"start": 2.6, "end": 3.0, "word": "you"}]},
    ]
    phrases = CAP.cues(segments, "phrase", 8)
    assert [cue["text"] for cue in phrases] == ["Hello there.", "How are you"]
    words = CAP.cues(segments, "word", 8)
    assert len(words) == 5 and words[1]["text"] == "there."
    capped = CAP.cues(segments, "sentence", 2)
    assert len(capped) >= 2, "max words per caption is respected"


def test_style_presets_are_editable_text_values():
    engine, _clip = build_engine()
    width, height = 1920, 1080
    for style_id in STYLES.STYLE_ORDER:
        settings = STYLES.style_settings(style_id, width, height)
        assert settings["size"] > 0 and settings["wrap"] > 0
        assert "offset_y" in settings
    small = STYLES.style_settings("classic", 1280, 720)
    large = STYLES.style_settings("classic", 3840, 2160)
    assert large["size"] > small["size"], "sizes scale with the project height"
    clip_text = STYLES.caption_text("classic", "Hi", width, height, words=[])
    assert clip_text["text"] == "Hi" and clip_text["caption"]["style"] == "classic"


def test_transcript_edits_are_one_undo_step():
    engine, _clip = build_engine(frames=150, duration=150)
    session = engine.session
    ids = session.generate_captions(cues(), start_frame=0, width=320, height=180, animation="")
    first, second = engine.timeline.find_clip(ids[0]), engine.timeline.find_clip(ids[1])
    session.update_captions([
        {"id": first.id, "text": "Corrected", "start": 6, "end": 40},
        {"id": second.id, "text": "Also fixed"},
    ])
    assert TXT.content_at(first, 0) == "Corrected"
    assert first.timeline_start == 6 and first.duration == 34
    assert TXT.content_at(second, 0) == "Also fixed"
    session.undo()
    first, second = engine.timeline.find_clip(ids[0]), engine.timeline.find_clip(ids[1])
    assert TXT.content_at(first, 0) == "Hello there" and first.timeline_start == 0
    assert TXT.content_at(second, 0) == "Second line"


def test_restyling_keeps_words_and_timing():
    engine, _clip = build_engine(frames=90, duration=90)
    session = engine.session
    ids = session.generate_captions(cues(), start_frame=0, width=320, height=180,
                                    animation="", style_id="classic")
    before = engine.timeline.find_clip(ids[0])
    start, duration = before.timeline_start, before.duration
    words = list(CAP.caption_info(before)["words"])
    assert session.restyle_captions(ids, "gaming") == 2
    after = engine.timeline.find_clip(ids[0])
    assert CAP.caption_info(after)["style"] == "gaming"
    assert CAP.caption_info(after)["words"] == words
    assert (after.timeline_start, after.duration) == (start, duration)
    assert TXT.content_at(after, 0) == "Hello there"


def test_captions_survive_a_project_round_trip(tmp_path):
    engine, _clip = build_engine(frames=90, duration=90)
    engine.session.generate_captions(cues(), start_frame=0, width=320, height=180,
                                     animation="", style_id="pop")
    from meenifx.project.io import Project, apply, collect
    from meenifx.project.settings import ProjectSettings

    project = Project(ProjectSettings.from_preset("720p 30", "Captions"), None)
    data = collect(engine, project)
    from meenifx.project.container import read_container, write_container

    path = str(tmp_path / "captions.mfxpf")
    write_container(path, data)
    loaded = read_container(path)
    fresh_engine, _clip2 = build_engine(frames=90, duration=90)
    fresh_engine.timeline.tracks.clear()
    applied, missing = apply(fresh_engine, loaded, path)
    # The fixture's synthetic picture has no file on disk; only captions matter here.
    assert all(item.name != "Caption" for item in missing)
    restored = CAP.caption_clips(fresh_engine.timeline)
    assert len(restored) == 2
    assert CAP.caption_info(restored[0])["style"] == "pop"
    assert TXT.content_at(restored[0], 0) == "Hello there"
