"""Link import: classification, security, streamed download, validation and GIF timing."""

import os

import pytest

from link_fixtures import gif_bytes, media_server  # noqa: F401  (pytest fixture)
from meenifx.media import link_import as L
from meenifx.media.reader import read_gif_frames, read_media

SIGNED = "?ex=65a1b2c3&is=65a1b2c3&hm=deadbeef"


@pytest.fixture(autouse=True)
def loopback_links(monkeypatch):
    """The fixture server is on 127.0.0.1; real imports still refuse private addresses."""
    monkeypatch.setenv(L.ALLOW_LOCAL_ENV, "1")


# ------------------------------------------------------------------ recognition
def test_direct_media_links_are_recognised():
    info = L.classify("https://cdn.discordapp.com/attachments/1/2/cat.gif" + SIGNED)
    assert (info.kind, info.suffix, info.media_type) == ("animation", ".gif", "image")
    assert info.signed and info.expires > 0
    assert info.filename == "cat.gif", "the signed query must not reach the file name"
    expected = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
                ".mp4": "video", ".webm": "video", ".mov": "video",
                ".wav": "audio", ".mp3": "audio", ".flac": "audio", ".ogg": "audio"}
    for suffix, kind in expected.items():
        assert L.classify(f"https://example.com/media/clip{suffix}").kind == kind


def test_pages_platforms_and_unsupported_files_are_not_downloads():
    with pytest.raises(L.UnsupportedLink) as youtube:
        L.classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert "YouTube" in youtube.value.message
    with pytest.raises(L.UnsupportedLink):
        L.classify("https://example.com/an-article")
    with pytest.raises(L.UnsupportedLink):
        L.classify("https://example.com/archive.zip")
    assert L.classify("just some words") is None
    assert L.classify("ftp://example.com/cat.png") is None
    assert L.classify("") is None


def test_credentials_in_a_link_are_refused():
    with pytest.raises(L.UnsupportedLink):
        L.classify("https://user:secret@example.com/cat.png")


def test_safe_file_names_strip_paths_queries_and_reserved_words():
    assert L.safe_filename("../../etc/passwd", ".png") == "passwd.png"
    assert L.safe_filename("cat.png?ex=1&hm=2", ".png") == "cat.png"
    assert L.safe_filename("CON.png", ".png") == "_CON.png"
    assert L.safe_filename("", ".gif") == "Imported media.gif"
    assert "/" not in L.safe_filename("a/b/c.jpg", ".jpg")
    assert "\\" not in L.safe_filename("a\\b\\c.jpg", ".jpg")


# ---------------------------------------------------------------------- headers
def test_probe_reports_the_type_and_size(media_server):
    info = L.probe(media_server.url("/cat.png"))
    assert info.content_type == "image/png"
    assert info.size > 0
    assert info.kind == "image"
    assert media_server.requests == ["/cat.png"], "a probe asks with HEAD only"


def test_probe_explains_an_expired_signed_link(media_server):
    with pytest.raises(L.LinkError) as error:
        L.probe(media_server.url("/expired.png" + SIGNED))
    assert "expired" in error.value.message.lower()


def test_forbidden_and_missing_links_are_plain_messages(media_server):
    with pytest.raises(L.LinkError) as forbidden:
        L.probe(media_server.url("/private.png"))
    assert "HTTP 403" in forbidden.value.message
    with pytest.raises(L.LinkError) as missing:
        L.probe(media_server.url("/missing.png"))
    assert "HTTP 404" in missing.value.message


def test_a_web_page_is_refused_at_probe_time(media_server):
    with pytest.raises(L.LinkError) as error:
        L.probe(media_server.url("/page"))
    assert "web page" in error.value.message.lower()


# --------------------------------------------------------------------- download
def test_download_writes_a_validated_png(media_server, tmp_path):
    folder = tmp_path / "Media"
    info = L.probe(media_server.url("/cat.png"))
    result = L.download(info, str(folder))
    assert os.path.isfile(result.path) and result.path.endswith(".png")
    assert result.info.size == os.path.getsize(result.path)
    assert (result.width, result.height) == (32, 24)
    assert sorted(os.listdir(folder)) == ["cat.png"]


def test_the_signed_query_is_sent_untouched(media_server, tmp_path):
    url = media_server.url("/anim.gif" + SIGNED)
    L.download(L.classify(url), str(tmp_path))
    assert media_server.requests[-1] == "/anim.gif" + SIGNED
    assert os.listdir(tmp_path) == ["anim.gif"], "the query is never part of the file name"


def test_gif_is_saved_byte_for_byte(media_server, tmp_path):
    source = gif_bytes()
    result = L.download(L.classify(media_server.url("/anim.gif")), str(tmp_path))
    assert result.path.endswith(".gif") and result.info.kind == "animation"
    with open(result.path, "rb") as handle:
        assert handle.read() == source, "an animated GIF is never re-encoded"
    assert result.frames >= 3 and result.seconds > 0.4


def test_download_can_reencode_a_still_as_png(media_server, tmp_path):
    result = L.download(L.classify(media_server.url("/photo.jpg")), str(tmp_path),
                        preserve=False)
    assert result.path.endswith(".png")
    assert os.listdir(tmp_path) == [os.path.basename(result.path)]


def test_a_video_link_is_saved_and_measured(media_server, tmp_path):
    result = L.download(L.classify(media_server.url("/clip.mp4")), str(tmp_path))
    assert result.path.endswith(".mp4")
    assert (result.width, result.height) == (32, 24)
    assert result.frames == 5


def test_a_sound_link_is_saved_and_timed(media_server, tmp_path):
    result = L.download(L.classify(media_server.url("/sound.wav")), str(tmp_path))
    assert result.path.endswith(".wav")
    assert 0.1 <= result.seconds <= 0.4


def test_redirects_are_followed(media_server, tmp_path):
    result = L.download(L.classify(media_server.url("/to-cat.png")), str(tmp_path))
    assert os.path.basename(result.path) == "cat.png"


def test_bytes_that_are_not_media_are_refused(media_server, tmp_path):
    with pytest.raises(L.LinkError) as error:
        L.download(L.classify(media_server.url("/fake.png")), str(tmp_path))
    assert "not media" in error.value.message
    assert os.listdir(tmp_path) == []


def test_an_html_body_with_a_picture_name_is_refused(media_server, tmp_path):
    with pytest.raises(L.LinkError):
        L.download(L.classify(media_server.url("/page")), str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_oversized_files_are_refused_before_saving(media_server, tmp_path):
    with pytest.raises(L.LinkError) as error:
        L.download(L.classify(media_server.url("/big.png")), str(tmp_path), limit=4096)
    assert "larger" in error.value.message.lower()
    assert os.listdir(tmp_path) == []


def test_interrupted_download_leaves_nothing(media_server, tmp_path):
    with pytest.raises(L.LinkError):
        L.download(L.classify(media_server.url("/broken.gif")), str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_cancel_stops_the_download_and_cleans_up(media_server, tmp_path):
    with pytest.raises(L.Cancelled):
        L.download(L.classify(media_server.url("/slow.gif")), str(tmp_path),
                   cancelled=lambda: True)
    assert os.listdir(tmp_path) == []


def test_duplicate_names_get_a_suffix(media_server, tmp_path):
    info = L.classify(media_server.url("/cat.png"))
    first = L.download(info, str(tmp_path))
    second = L.download(info, str(tmp_path))
    assert os.path.basename(first.path) == "cat.png"
    assert os.path.basename(second.path) == "cat (2).png"


def test_encoded_file_names_are_decoded_without_the_path(media_server, tmp_path):
    result = L.download(L.classify(media_server.url("/folder/my%20cat.png")), str(tmp_path))
    assert os.path.basename(result.path) == "my cat.png"


def test_progress_is_reported_until_the_end(media_server, tmp_path):
    seen = []
    info = L.probe(media_server.url("/anim.gif"))
    result = L.download(info, str(tmp_path), progress=lambda done, total: seen.append(done))
    assert seen and seen[-1] == os.path.getsize(result.path)


# --------------------------------------------------------------------- security
def test_loopback_is_refused_without_the_test_flag(media_server, tmp_path, monkeypatch):
    monkeypatch.delenv(L.ALLOW_LOCAL_ENV, raising=False)
    info = L.classify(media_server.url("/cat.png"))
    with pytest.raises(L.BlockedLink):
        L.download(info, str(tmp_path))
    assert media_server.requests == [], "a refused link never reaches the network"
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/cat.png", "http://localhost/cat.png",
                                 "http://10.1.2.3/a.png", "http://192.168.0.5/a.png",
                                 "http://169.254.1.1/a.png", "http://[::1]/a.png",
                                 "http://0.0.0.0/a.png"])
def test_private_and_local_addresses_are_blocked(url):
    with pytest.raises(L.BlockedLink):
        L.check_url(url, allow_local=False)


def test_a_link_that_is_not_downloadable_is_refused_before_resolving():
    with pytest.raises(L.UnsupportedLink):
        L.check_url("file:///C:/Windows/win.ini", allow_local=False)


# ------------------------------------------------------------------------ GIFs
def _gif(tmp_path):
    path = tmp_path / "anim.gif"
    path.write_bytes(gif_bytes())
    return str(path)


def test_animated_gif_keeps_frames_timing_and_transparency(tmp_path):
    frames, fps = read_gif_frames(_gif(tmp_path))
    assert len(frames) > 3, "the delays must be resampled, not one slot per frame"
    assert fps > 0
    assert frames[0].shape[2] == 4, "transparency becomes alpha, not a flat background"
    assert int(frames[0][:, :, 3].min()) == 0
    total = len(frames) / fps
    assert 0.5 <= total <= 1.2, f"the source lasts 0.6 s, decoded {total:.2f} s"


def test_read_media_treats_a_gif_as_an_animated_clip(tmp_path):
    kind, frames, fps = read_media(_gif(tmp_path))
    assert kind == "video" and len(frames) > 1 and fps > 0


def test_a_transparent_gif_layer_keeps_its_alpha(tmp_path):
    from meenifx.processing.layers import fit_layer

    frames, _fps = read_gif_frames(_gif(tmp_path))
    layer, rect = fit_layer(frames[0], (32, 24), 1.0)
    assert layer.shape[2] == 4
    assert int(layer[:, :, 3].min()) == 0
    assert rect[2] > 0 and rect[3] > 0
