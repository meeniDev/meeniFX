"""A local stand-in for YouTube: real media, served over loopback.

yt-dlp's generic extractor reads a direct media URL exactly as it reads one of YouTube's
stream URLs, so the download, format selection, ffmpeg trim and muxing are exercised for real
without touching the network or anyone's video.
"""

import os
import subprocess
import time
from urllib.parse import urlsplit

import pytest

from link_fixtures import MediaServer
from meenifx.media.hardware import ffmpeg_exe


def av_mp4(folder, seconds=4.0, *, audio=True, fps=25):
    """A short H.264 MP4 with (or without) an AAC tone, written by the bundled ffmpeg."""
    os.makedirs(folder, exist_ok=True)
    name = f"av-{seconds:g}-{'a' if audio else 'v'}.mp4"
    target = os.path.join(folder, name)
    if os.path.isfile(target):
        return target
    command = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={fps}:duration={seconds:g}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds:g}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    command += ["-c:a", "aac", "-b:a", "96k"] if audio else ["-an"]
    command += ["-shortest", target]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    return target


def m4a_file(folder, *, source=None, name="tone.m4a"):
    """The sound of a fixture video, extracted as an ordinary .m4a the timeline can read."""
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, name)
    if os.path.isfile(target):
        return target
    source = source or av_mp4(folder, 3.0, audio=True)
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", source,
                    "-vn", "-c:a", "copy", target], check=True, capture_output=True, timeout=60)
    return target


def _bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class YouTubeServer(MediaServer):
    """The link-import server plus a few real videos, one of which arrives very slowly."""

    def __init__(self):
        super().__init__()
        self.media = {
            "/av.mp4": _bytes(av_mp4(self.scratch, 4.0, audio=True)),
            "/silent.mp4": _bytes(av_mp4(self.scratch, 4.0, audio=False)),
            "/short.mp4": _bytes(av_mp4(self.scratch, 2.0, audio=True)),
        }
        self.media_type = {route: "video/mp4" for route in self.media}

    def respond(self, handler, head):
        route = urlsplit(handler.path).path
        slow = route == "/slow-av.mp4"
        body = self.media.get(route) or (self.media["/av.mp4"] if slow else None)
        if body is None:
            super().respond(handler, head)
            return
        self.requests.append(handler.path)
        handler.send_response(200)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if head:
            return
        if not slow:
            handler.wfile.write(body)
            return
        piece = max(1, len(body) // 8)
        try:
            for offset in range(0, len(body), piece):
                handler.wfile.write(body[offset:offset + piece])
                handler.wfile.flush()
                time.sleep(0.12)
        except OSError:
            return


@pytest.fixture()
def youtube_server():
    server = YouTubeServer()
    try:
        yield server
    finally:
        server.close()


def local_link(server, path="/av.mp4"):
    """A link shaped like a YouTube video but pointing at the local stand-in."""
    from meenifx.media import youtube

    return youtube.YouTubeLink("dQw4w9WgXcQ", server.url(path), host="127.0.0.1")
