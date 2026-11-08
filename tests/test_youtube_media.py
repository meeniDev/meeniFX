"""YouTube imports: link recognition, metadata, planning, and the real download pipeline.

The network is never touched. Metadata is built from crafted yt-dlp ``info_dict`` shapes, and
the download/trim tests run yt-dlp's generic extractor against a loopback MP4, so the real
download, ffmpeg trim, mux and validation code is exercised end to end.
"""

import collections
import os

import pytest

from meenifx.media import youtube
from meenifx.media.youtube import YouTubeError, YouTubeLink
from youtube_fixtures import av_mp4, local_link, youtube_server  # noqa: F401

WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def fake_info(**overrides):
    """A yt-dlp info_dict with separate video and audio streams, as YouTube sends."""
    info = {
        "title": "Sunset Test",
        "channel": "Meeni Studio",
        "uploader": "Meeni Studio",
        "duration": 120.0,
        "formats": [
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none",
             "height": 1080, "width": 1920, "fps": 30, "filesize": 50_000_000},
            {"format_id": "136", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
             "height": 720, "width": 1280, "fps": 30, "filesize": 25_000_000},
            {"format_id": "135", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
             "height": 480, "width": 854, "fps": 30, "filesize": 12_000_000},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
             "abr": 128.0, "filesize": 2_000_000},
            {"format_id": "139", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
             "abr": 48.0, "filesize": 800_000},
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a.40.2",
             "height": 360, "width": 640, "fps": 30, "filesize": 9_000_000},
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"},
        ],
    }
    info.update(overrides)
    return info


def meta_for(link=None, **overrides):
    link = link or youtube.parse_url(WATCH)
    return youtube.meta_from_info(link, fake_info(**overrides))


# ------------------------------------------------------------------ 1-4 links
def test_watch_short_live_embed_and_short_links_are_recognised():
    cases = {
        WATCH: "dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc123XYZ_-": "abc123XYZ_-",
        "https://www.youtube.com/live/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share": "dQw4w9WgXcQ",
    }
    for url, expected in cases.items():
        assert youtube.parse_url(url).video_id == expected, url
    short = youtube.parse_url("https://youtu.be/dQw4w9WgXcQ?t=90")
    assert short.start == 90.0 and short.video_id == "dQw4w9WgXcQ"
    watch = youtube.parse_url(WATCH + "&t=1m30s")
    assert watch.start == 90.0, "a timestamp in the link becomes the start time"
    fragment = youtube.parse_url(WATCH + "#t=45")
    assert fragment.start == 45.0


def test_a_domain_that_only_contains_youtube_is_not_youtube():
    for text in ("https://notyoutube.com/watch?v=dQw4w9WgXcQ",
                 "https://youtube.example.com/watch?v=dQw4w9WgXcQ",
                 "https://myyoutube.com/watch?v=dQw4w9WgXcQ",
                 "https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
                 "https://example.com/youtube/watch?v=dQw4w9WgXcQ",
                 "youtube.com/watch?v=dQw4w9WgXcQ",
                 "ftp://youtube.com/watch?v=dQw4w9WgXcQ"):
        assert youtube.parse_url(text) is None, text


def test_invalid_ids_playlists_and_channels_are_explained():
    for text, fragment in (
            ("https://www.youtube.com/watch?v=abc", "invalid video ID"),
            ("https://www.youtube.com/playlist?list=PL123", "playlist or channel"),
            ("https://www.youtube.com/@someone", "playlist or channel"),
            ("https://www.youtube.com/watch", "does not name a video"),
    ):
        with pytest.raises(YouTubeError) as caught:
            youtube.parse_url(text)
        assert fragment in caught.value.message


def test_timecodes_round_trip_and_reject_nonsense():
    assert youtube.format_timecode(0) == "00:00:00.000"
    assert youtube.format_timecode(90.5) == "00:01:30.500"
    assert youtube.format_timecode(3661.25) == "01:01:01.250"
    assert youtube.parse_timecode("00:01:30.500") == 90.5
    assert youtube.parse_timecode("1:30") == 90.0
    assert youtube.parse_timecode("90") == 90.0
    assert youtube.parse_timecode("-5") == -5.0, "a negative time is parsed, then refused"
    for bad in ("", "soon", "1:2:3:4", "half past"):
        assert youtube.parse_timecode(bad) == -1.0


# ------------------------------------------------------------------ 5 metadata
def test_metadata_lists_only_the_qualities_the_video_offers():
    meta = meta_for()
    assert meta.title == "Sunset Test" and meta.channel == "Meeni Studio"
    assert meta.heights == [1080, 720, 480, 360]
    choices = meta.video_choices
    assert [c.key for c in choices] == ["best", "1080", "720", "480", "360"]
    assert choices[0].label == "Best available (1080p)"
    assert choices[1].label == "1080p"
    assert all(abs(c.height - 1440) > 1 for c in choices), "no invented 1440p option"
    assert all(f.format_id != "sb0" for f in meta.formats), "storyboards are not media"


def test_audio_choices_come_from_real_audio_streams():
    meta = meta_for()
    choices = meta.audio_choices
    assert choices[0].key == "best" and choices[0].audio == "140"
    assert [c.key for c in choices] == ["best", "audio-48"]
    assert choices[1].label == "48 kbps" and choices[1].audio == "139"


def test_a_video_with_only_a_muxed_format_still_offers_audio():
    info = fake_info(formats=[
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a",
         "height": 360, "filesize": 9_000_000}])
    meta = youtube.meta_from_info(youtube.parse_url(WATCH), info)
    assert meta.audio_formats == ()
    assert meta.audio_choices[0].audio == "18", "the muxed file is the only sound there is"


def test_metadata_refuses_drm_live_playlist_and_empty_videos():
    cases = [
        (fake_info(has_drm=True), "DRM"),
        (fake_info(is_live=True), "live"),
        (fake_info(live_status="is_upcoming"), "upcoming"),
        (fake_info(entries=[{"id": "x"}], _type="playlist"), "playlist"),
        (fake_info(formats=[]), "no picture or sound"),
    ]
    for info, fragment in cases:
        with pytest.raises(YouTubeError) as caught:
            youtube.meta_from_info(youtube.parse_url(WATCH), info)
        assert fragment in caught.value.message, fragment


def test_common_ytdlp_problems_become_plain_sentences():
    assert "private" in youtube._friendly("ERROR: Private video").lower()
    assert "members" in youtube._friendly("Join this channel to get access").lower()
    assert "age" in youtube._friendly("Sign in to confirm your age").lower()
    assert "region" in youtube._friendly("The uploader has not made this video "
                                         "available in your country").lower()
    assert "drm" in youtube._friendly("This video is DRM protected").lower()
    assert "deleted" in youtube._friendly("Video unavailable").lower()
    assert "sign-in" in youtube._friendly("Sign in to confirm you're not a bot").lower()
    assert youtube._friendly("weird failure") == "weird failure"


# ------------------------------------------------------------------ 6 planning
def test_each_mode_resolves_to_real_format_ids():
    meta = meta_for()
    plan = youtube.resolve(meta, youtube.ImportRequest(mode="video_audio"))
    assert (plan.video, plan.audio, plan.muxed) == ("137", "140", False)
    plan = youtube.resolve(meta, youtube.ImportRequest(mode="video_only"))
    assert plan.video == "137" and plan.audio == "" and plan.muxed is False
    plan = youtube.resolve(meta, youtube.ImportRequest(mode="audio_only"))
    assert plan.audio == "140" and plan.video == ""
    plan = youtube.resolve(meta, youtube.ImportRequest(mode="video_audio", quality="480"))
    assert (plan.video, plan.audio) == ("135", "140")


def test_advanced_format_choice_is_honoured_and_checked():
    meta = meta_for()
    plan = youtube.resolve(meta, youtube.ImportRequest(mode="video_audio", format_id="135"))
    assert plan.video == "135" and plan.audio == "140"
    plan = youtube.resolve(meta, youtube.ImportRequest(mode="audio_only", format_id="139"))
    assert plan.audio == "139"
    with pytest.raises(YouTubeError):
        youtube.resolve(meta, youtube.ImportRequest(format_id="does-not-exist"))


def test_a_bad_range_is_refused_before_downloading():
    meta = meta_for()
    assert youtube.check_range(-1, 10, 120).startswith("The start time")
    assert "after the start" in youtube.check_range(10, 10, 120)
    assert "too short" in youtube.check_range(10, 10.01, 120)
    assert "past the end" in youtube.check_range(10, 200, 120)
    assert youtube.check_range(10, 20, 120) == ""
    with pytest.raises(YouTubeError):
        youtube.import_media(youtube.parse_url(WATCH), meta,
                             youtube.ImportRequest(start=10, end=200), "unused")


def test_disk_space_is_checked_before_a_long_download(tmp_path, monkeypatch):
    tiny = collections.namedtuple("usage", "total used free")(10, 9, 1)
    monkeypatch.setattr(youtube.shutil, "disk_usage", lambda _path: tiny)
    with pytest.raises(YouTubeError) as caught:
        youtube._check_space(str(tmp_path), 10_000_000)
    assert "free disk space" in caught.value.message
    roomy = collections.namedtuple("usage", "total used free")(10 ** 12, 0, 10 ** 12)
    monkeypatch.setattr(youtube.shutil, "disk_usage", lambda _path: roomy)
    assert youtube._check_space(str(tmp_path), 10_000_000) is None


# ------------------------------------------------------- 7 real local pipeline
def test_metadata_can_be_read_from_a_real_source_without_downloading(youtube_server):
    info = youtube._extract_info(youtube_server.url("/av.mp4"))
    meta = youtube.meta_from_info(local_link(youtube_server), info)
    assert meta.title and meta.formats, "the generic source still yields a usable format"
    assert youtube_server.requests, "reading details is a request, not a file download"


def test_video_and_audio_are_trimmed_to_the_selected_range(youtube_server, tmp_path):
    meta = meta_for()  # formats are not needed here: the local source supplies its own
    meta = youtube.meta_from_info(local_link(youtube_server),
                                  youtube._extract_info(youtube_server.url("/av.mp4")))
    out = youtube.import_media(local_link(youtube_server), meta,
                               youtube.ImportRequest(mode="video_audio", start=1.0, end=2.2,
                                                     partial=False), str(tmp_path))
    assert os.path.isfile(out.path) and out.path.lower().endswith(".mp4")
    assert 1.0 <= out.seconds <= 1.45, f"expected about 1.2 s, got {out.seconds}"
    assert (out.width, out.height) == (320, 240)
    assert os.listdir(tmp_path) == [os.path.basename(out.path)], "no temporary files left"
    assert youtube._audio_ok(out.path), "the picture kept its sound"


def test_video_only_really_has_no_sound(youtube_server, tmp_path):
    meta = youtube.meta_from_info(local_link(youtube_server),
                                  youtube._extract_info(youtube_server.url("/av.mp4")))
    out = youtube.import_media(local_link(youtube_server), meta,
                               youtube.ImportRequest(mode="video_only", start=0.5, end=1.5,
                                                     partial=False), str(tmp_path))
    assert 0.8 <= out.seconds <= 1.2
    assert not youtube._audio_ok(out.path)


def test_audio_only_is_an_m4a_with_sound_and_no_picture(youtube_server, tmp_path):
    meta = youtube.meta_from_info(local_link(youtube_server),
                                  youtube._extract_info(youtube_server.url("/short.mp4")))
    out = youtube.import_media(local_link(youtube_server), meta,
                               youtube.ImportRequest(mode="audio_only", start=0.0, end=1.5,
                                                     partial=False), str(tmp_path))
    assert out.path.lower().endswith(".m4a")
    assert out.width == 0 and out.height == 0
    assert youtube._audio_ok(out.path)
    assert 1.2 <= out.seconds <= 1.8


def test_a_full_import_is_not_re_encoded(youtube_server, tmp_path):
    meta = youtube.meta_from_info(local_link(youtube_server),
                                  youtube._extract_info(youtube_server.url("/av.mp4")))
    out = youtube.import_media(local_link(youtube_server), meta,
                               youtube.ImportRequest(mode="video_audio", start=0.0, end=0.0,
                                                     partial=False), str(tmp_path))
    assert out.partial is False
    assert os.path.getsize(out.path) == len(youtube_server.media["/av.mp4"])


def test_progress_reports_downloading_then_trimming(youtube_server, tmp_path):
    stages = []
    meta = youtube.meta_from_info(local_link(youtube_server),
                                  youtube._extract_info(youtube_server.url("/av.mp4")))
    youtube.import_media(local_link(youtube_server), meta,
                         youtube.ImportRequest(mode="video_audio", start=0.5, end=1.5,
                                               partial=False), str(tmp_path),
                         progress=lambda stage, done, total: stages.append(stage))
    assert "Downloading" in stages
    assert "Trimming" in stages or "Preparing" in stages


def test_cancel_during_download_leaves_nothing(youtube_server, tmp_path):
    meta = youtube.meta_from_info(local_link(youtube_server),
                                  youtube._extract_info(youtube_server.url("/slow-av.mp4")))
    state = {"cancel": False}

    def progress(stage, done, total):
        if stage == "Downloading" and done > 0:
            state["cancel"] = True

    with pytest.raises(youtube.YouTubeCancelled):
        youtube.import_media(local_link(youtube_server, "/slow-av.mp4"), meta,
                             youtube.ImportRequest(mode="video_audio", partial=False),
                             str(tmp_path), progress=progress,
                             cancelled=lambda: state["cancel"])
    assert os.listdir(tmp_path) == [], "a cancelled import leaves no file or temp folder"


def test_a_failed_download_leaves_nothing(youtube_server, tmp_path):
    meta = youtube.meta_from_info(local_link(youtube_server),
                                  youtube._extract_info(youtube_server.url("/av.mp4")))
    with pytest.raises(YouTubeError):
        youtube.import_media(local_link(youtube_server, "/not-there.mp4"), meta,
                             youtube.ImportRequest(mode="video_audio", partial=False),
                             str(tmp_path))
    assert os.listdir(tmp_path) == []
