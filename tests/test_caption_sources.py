"""Auto Captions must find audio in whatever the user actually selected.

The old failure - "that range has no audio to analyse" while a perfectly audible clip
was selected - came from reading the clicked clip instead of the audio that belongs to
it. These tests pin every selection shape the dialog offers, including trimmed clips and
clips with a source offset, so a linked pair never has to be unlinked to be captioned.
"""

import pytest

from conftest import build_engine


def _audio_clip(engine, name="voice", start=15, duration=45, source_in=0, source_out=45,
                offset=0, link_to=None):
    track = engine.timeline.add_track("audio")
    clip = engine.timeline.new_clip(name, name + ".wav", track, start, duration,
                                    source_in=source_in, source_out=source_out,
                                    source_fps=30, stream="audio")
    clip.source_offset = offset
    if link_to is not None:
        engine.timeline.assign_link([link_to.id, clip.id])
    return clip


def test_a_standalone_audio_clip_is_its_own_caption_source():
    engine, _video = build_engine(frames=120, duration=120)
    audio = _audio_clip(engine)
    engine.session.set_selection({audio.id})
    region = engine.session.caption_source_range("clip")
    assert region is not None
    start, end, ids = region
    assert (start, end) == (audio.timeline_start, audio.end)
    assert ids == [audio.id]
    assert (end - start) / engine.timeline.fps > 0.5, "a real range, not a zero-length one"


def test_a_trimmed_audio_clip_analyses_only_its_visible_span():
    engine, _video = build_engine(frames=200, duration=200)
    audio = _audio_clip(engine, start=40, duration=30, source_in=60, source_out=90)
    engine.session.set_selection({audio.id})
    start, end, ids = engine.session.caption_source_range("clip")
    assert (start, end) == (40, 70)
    assert ids == [audio.id]


def test_a_source_offset_does_not_change_the_caption_range():
    engine, _video = build_engine(frames=200, duration=200)
    audio = _audio_clip(engine, start=20, duration=50, offset=12)
    engine.session.set_selection({audio.id})
    assert engine.session.caption_source_range("clip") == (20, 70, [audio.id])


def test_a_linked_pair_uses_the_audio_member_even_when_the_audio_is_shorter():
    engine, video = build_engine(frames=120, duration=120)
    audio = _audio_clip(engine, start=0, duration=40)
    engine.timeline.assign_link([video.id, audio.id])
    engine.session.set_linked_selection(True)

    for clicked in (video, audio):
        engine.session.set_selection({clicked.id})
        start, end, ids = engine.session.caption_source_range("clip")
        assert (start, end) == (audio.timeline_start, audio.end), \
            "the range follows the sound, not the longer picture"
        assert ids == [audio.id]


def test_a_video_with_embedded_audio_is_its_own_source_when_trimmed():
    engine, video = build_engine(frames=120, duration=120)
    video.stream = "both"
    video.timeline_start, video.duration = 30, 40
    video.source_out = 70
    engine.session.set_selection({video.id})
    region = engine.session.caption_source_range("clip")
    assert region is not None
    assert region[:2] == (30, 70)


def test_a_track_source_keeps_the_track_span_even_before_audio_is_decoded():
    engine, video = build_engine(frames=120, duration=120)
    engine.session.set_selection({video.id})
    region = engine.session.caption_source_range("track")
    assert region is not None and region[2] == [video.id]
    assert region[1] > region[0]


def test_the_whole_timeline_range_is_never_zero_length():
    engine, _video = build_engine(frames=90, duration=90)
    start, end, ids = engine.session.caption_source_range("timeline")
    assert start == 0 and end >= 90 and ids is None


def test_an_empty_selection_asks_rather_than_reporting_no_audio():
    """With nothing selected the dialog prompts for a clip, it does not claim silence."""
    engine, _video = build_engine(frames=60, duration=60)
    engine.session.set_selection(set())
    assert engine.session.caption_source_range("clip") is None
    assert engine.session.caption_source_range("track") is None
    # The whole-timeline choice never depends on the selection.
    assert engine.session.caption_source_range("timeline") is not None


def test_captioning_a_linked_pair_leaves_the_link_intact():
    engine, video = build_engine(frames=120, duration=120)
    audio = _audio_clip(engine, start=0, duration=60)
    engine.timeline.assign_link([video.id, audio.id])
    engine.session.set_selection({video.id})
    engine.session.generate_captions(
        [{"start": 0.0, "end": 1.0, "text": "Hello there", "words": []}],
        start_frame=0, width=320, height=180, animation="")
    assert engine.timeline.link_group(video.link_id) == [video, audio], \
        "generating captions must not touch the video/audio link"


@pytest.mark.parametrize("source", ["clip", "track", "timeline"])
def test_every_source_returns_a_usable_range(source):
    engine, video = build_engine(frames=120, duration=120)
    audio = _audio_clip(engine, start=0, duration=60)
    engine.timeline.assign_link([video.id, audio.id])
    engine.session.set_selection({video.id})
    region = engine.session.caption_source_range(source)
    assert region is not None
    start, end, _ids = region
    assert end > start
    assert (end - start) / engine.timeline.fps > 0.05
