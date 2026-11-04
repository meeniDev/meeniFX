"""Header-only audio detection, on hand-built containers and on real files."""

import struct

import pytest

from meenifx.media import probe
from tests.av_fixture import write_av_avi

HERE = __file__.rsplit("tests", 1)[0]


def _box(kind, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _handler(kind):
    """A minimal hdlr box: version/flags, predefined, handler_type."""
    return _box(b"hdlr", b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + kind)


def _mp4(*handlers, filler=0):
    tracks = b"".join(_box(b"trak", _box(b"mdia", _handler(name))) for name in handlers)
    data = _box(b"ftyp", b"isom\x00\x00\x02\x00isommp41")
    if filler:
        data += _box(b"mdat", b"\x00" * filler)
    return data + _box(b"moov", tracks)


def _vint_size(value):
    for length in range(1, 9):
        if value < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | value).to_bytes(length, "big")
    raise ValueError(value)


def _mkv(track_types):
    entries = b""
    for number in track_types:
        number = _box_type = b"\x83" + _vint_size(1) + bytes([number])
        entries += b"\xae" + _vint_size(len(number)) + number
    tracks = b"\x16\x54\xae\x6b" + _vint_size(len(entries)) + entries
    segment = b"\x18\x53\x80\x67" + _vint_size(len(tracks)) + tracks
    return segment


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# ------------------------------------------------------------------ ISO-BMFF
def test_mp4_with_a_sound_track_is_detected(tmp_path):
    path = _write(tmp_path, "with-audio.mp4", _mp4(b"vide", b"soun"))
    assert probe.has_audio_stream(path) is True


def test_mp4_with_only_a_video_track_is_silent(tmp_path):
    path = _write(tmp_path, "video-only.mp4", _mp4(b"vide"))
    assert probe.has_audio_stream(path) is False


def test_a_large_mdat_before_moov_does_not_confuse_the_scan(tmp_path):
    """Faststart-less files put the whole movie after the media data."""
    path = _write(tmp_path, "big-mdat.mp4", _mp4(b"vide", b"soun", filler=200_000))
    assert probe.has_audio_stream(path) is True


def test_the_real_video_only_sample_reports_no_audio():
    assert probe.has_audio_stream(HERE + "mosaic-output.mp4") is False


def test_a_truncated_mp4_is_treated_as_silent(tmp_path):
    path = _write(tmp_path, "broken.mp4", _mp4(b"vide", b"soun")[:20])
    assert probe.has_audio_stream(path) is False


def test_a_box_with_an_absurd_size_is_treated_as_silent(tmp_path):
    bogus = struct.pack(">I", 0xFFFFFFFF) + b"moov" + b"\x00" * 32
    path = _write(tmp_path, "absurd.mp4", bogus)
    assert probe.has_audio_stream(path) is False


# --------------------------------------------------------------- Matroska
def test_matroska_audio_track_is_detected(tmp_path):
    path = _write(tmp_path, "sound.mkv", _mkv([1, 2]))
    assert probe.has_audio_stream(path) is True


def test_matroska_video_only_track_list_is_silent(tmp_path):
    path = _write(tmp_path, "silent.mkv", _mkv([1]))
    assert probe.has_audio_stream(path) is False


# --------------------------------------------------------------------- AVI
def test_avi_with_pcm_sound_is_detected(tmp_path):
    path = write_av_avi(str(tmp_path / "av.avi"), seconds=1.0)
    assert probe.has_audio_stream(path) is True


def test_avi_without_a_sound_stream_is_silent(tmp_path):
    path = write_av_avi(str(tmp_path / "picture-only.avi"), seconds=1.0, audio=False)
    assert probe.has_audio_stream(path) is False


# ----------------------------------------------------------- trivial cases
@pytest.mark.parametrize("name", ["take.wav", "song.mp3", "voice.m4a", "x.flac", "y.ogg"])
def test_audio_files_always_carry_audio(tmp_path, name):
    path = _write(tmp_path, name, b"\x00" * 64)
    assert probe.has_audio_stream(path) is True


@pytest.mark.parametrize("name", ["still.png", "photo.JPG", "frame.bmp", "art.webp"])
def test_images_never_carry_audio(tmp_path, name):
    path = _write(tmp_path, name, b"\x00" * 64)
    assert probe.has_audio_stream(path) is False


def test_missing_files_and_empty_strings_are_silent(tmp_path):
    assert probe.has_audio_stream("") is False
    assert probe.has_audio_stream(str(tmp_path / "nope.mp4")) is False
    assert probe.has_audio_stream(str(tmp_path)) is False


def test_unknown_extensions_are_silent(tmp_path):
    path = _write(tmp_path, "thing.xyz", _mp4(b"vide", b"soun"))
    assert probe.has_audio_stream(path) is False


def test_the_cache_memoises_answers(tmp_path):
    path = write_av_avi(str(tmp_path / "av.avi"), seconds=0.5)
    cache = {}
    assert probe.has_audio_stream(path, cache) is True
    assert list(cache.values()) == [True]
    # Even after the file disappears the cached answer stands (imports stay consistent).
    import os
    os.remove(path)
    assert probe.has_audio_stream(path, cache) is True
