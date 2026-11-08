"""A local media server, so link imports are exercised without the internet.

The routes cover what a real import has to survive: a plain picture, an animated GIF, a
video, a sound file, an expired signed link, a refusal, a redirect, a web page, bytes that
are not media, a slow stream (cancel) and a connection that dies mid-body.
"""

import io
import os
import shutil
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

PNG = "image/png"
GIF = "image/gif"
MP4 = "video/mp4"
WAV = "audio/wav"


def png_bytes(width=32, height=24, alpha=False):
    channels = 4 if alpha else 3
    picture = np.zeros((height, width, channels), np.uint8)
    picture[:, :, 0] = 60
    picture[:, :, 1] = 120
    if alpha:
        picture[:, width // 2:, 3] = 255
    else:
        picture[:, :, 2] = 200
    ok, buffer = cv2.imencode(".png", picture)
    assert ok
    return buffer.tobytes()


def jpeg_bytes(width=32, height=24):
    ok, buffer = cv2.imencode(".jpg", np.full((height, width, 3), 120, np.uint8))
    assert ok
    return buffer.tobytes()


def noisy_png_bytes(width=300, height=300, seed=7):
    generator = np.random.default_rng(seed)
    picture = generator.integers(0, 255, (height, width, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".png", picture)
    assert ok
    return buffer.tobytes()


def gif_bytes(frames=3, size=(32, 24)):
    """A small animated GIF whose background is transparent."""
    pictures = []
    for index in range(frames):
        picture = Image.new("P", size, 0)
        palette = [0, 0, 0, 230, 80, 50, 255, 255, 255] + [0, 0, 0] * 253
        picture.putpalette(palette)
        ImageDraw.Draw(picture).rectangle([index * 6, 2, index * 6 + 8, size[1] - 2], fill=1)
        pictures.append(picture)
    buffer = io.BytesIO()
    pictures[0].save(buffer, format="GIF", save_all=True, append_images=pictures[1:],
                     duration=[80, 120, 400][:frames], loop=0, transparency=0,
                     disposal=2, optimize=False)
    return buffer.getvalue()


def mp4_bytes(folder):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "fixture.mp4")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (32, 24))
    assert writer.isOpened(), "this OpenCV build cannot write a test MP4"
    for index in range(5):
        writer.write(np.full((24, 32, 3), 40 + index * 20, np.uint8))
    writer.release()
    with open(path, "rb") as handle:
        return handle.read()


def wav_bytes(seconds=0.2, rate=8000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def build_routes(folder):
    return {
        "/cat.png": {"status": 200, "type": PNG, "body": png_bytes()},
        "/alpha.png": {"status": 200, "type": PNG, "body": png_bytes(alpha=True)},
        "/photo.jpg": {"status": 200, "type": "image/jpeg", "body": jpeg_bytes()},
        "/anim.gif": {"status": 200, "type": GIF, "body": gif_bytes()},
        "/clip.mp4": {"status": 200, "type": MP4, "body": mp4_bytes(folder)},
        "/sound.wav": {"status": 200, "type": WAV, "body": wav_bytes()},
        "/to-cat.png": {"status": 302, "type": "text/plain", "body": b"",
                        "headers": {"Location": "/cat.png"}},
        "/missing.png": {"status": 404, "type": "text/plain", "body": b"no"},
        "/private.png": {"status": 403, "type": "text/plain", "body": b"no"},
        "/expired.png": {"status": 403, "type": "application/json",
                         "body": b'{"message": "This content is no longer available.", '
                                 b'"code": 10005}'},
        "/page": {"status": 200, "type": "text/html; charset=utf-8",
                  "body": b"<html><body>not media</body></html>"},
        "/fake.png": {"status": 200, "type": PNG, "body": b"this is definitely not a png"},
        "/big.png": {"status": 200, "type": PNG, "body": noisy_png_bytes()},
        "/folder/my%20cat.png": {"status": 200, "type": PNG, "body": png_bytes()},
    }


class MediaServer:
    """A throwaway HTTP server bound to loopback; Smart Paste is pointed at its URLs."""

    def __init__(self):
        self.requests = []
        # Generated fixtures live in their own folder, never in the download destination.
        self.scratch = tempfile.mkdtemp(prefix="meenifx-links-")
        self.routes = build_routes(self.scratch)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                outer.respond(self, head=True)

            def do_GET(self):
                outer.respond(self, head=False)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    # ---- addressing ----------------------------------------------------
    def url(self, path):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def paths(self):
        return [urlsplit(raw).path for raw in self.requests]

    # ---- responses -----------------------------------------------------
    def respond(self, handler, head):
        raw = handler.path
        self.requests.append(raw)
        route = urlsplit(raw).path
        if route == "/slow.gif":
            body = gif_bytes()
            handler.send_response(200)
            handler.send_header("Content-Type", GIF)
            handler.send_header("Content-Length", str(len(body) * 12))
            handler.end_headers()
            for _ in range(12):
                try:
                    handler.wfile.write(body)
                    handler.wfile.flush()
                except OSError:
                    return
                time.sleep(0.06)
            return
        if route == "/broken.gif":
            handler.send_response(200)
            handler.send_header("Content-Type", GIF)
            handler.send_header("Content-Length", "100000")
            handler.end_headers()
            try:
                handler.wfile.write(gif_bytes()[:64])
                handler.wfile.flush()
            except OSError:
                pass
            handler.close_connection = True
            return
        spec = self.routes.get(route)
        if spec is None:
            handler.send_response(404)
            handler.send_header("Content-Type", "text/plain")
            handler.send_header("Content-Length", "7")
            handler.end_headers()
            if not head:
                handler.wfile.write(b"missing")
            return
        body = spec["body"]
        handler.send_response(spec["status"])
        handler.send_header("Content-Type", spec["type"])
        handler.send_header("Content-Length", str(len(body)))
        for name, value in spec.get("headers", {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if not head:
            handler.wfile.write(body)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.scratch, ignore_errors=True)


@pytest.fixture()
def media_server():
    server = MediaServer()
    try:
        yield server
    finally:
        server.close()
