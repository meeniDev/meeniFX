"""Shared fixtures: an offscreen Qt app and a synthetic project for effect tests."""

import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The preview and panel tests build real widgets; offscreen keeps them headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WIDTH, HEIGHT = 320, 180


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def synthetic_frame(width=WIDTH, height=HEIGHT, blobs=True, skin=True):
    """A test picture with gradients, bright blobs and skin-coloured patches."""
    import cv2
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:] = (36, 34, 32)
    # a smooth gradient so mosaic/ASCII have something to quantise
    for y in range(height):
        frame[y, :, 0] = np.clip(30 + (y * 140 // max(1, height)), 0, 255)
        frame[y, :, 1] = np.clip(40 + (y * 90 // max(1, height)), 0, 255)
    if blobs:
        for cx, cy, radius in ((40, 40, 10), (110, 50, 18), (200, 60, 26), (270, 130, 34)):
            cv2.circle(frame, (cx, cy), radius, (255, 255, 255), -1)
    if skin:
        frame[95:150, 15:75] = (120, 150, 210)
        frame[85:155, 215:300] = (110, 140, 200)
    cv2.rectangle(frame, (2, 2), (width - 3, height - 3), (255, 255, 255), 1)
    return frame


def build_engine(frame=None, fps=30, frames=60, start=0, duration=60):
    """An EditorEngine with one video clip backed by synthetic frames (no files)."""
    from meenifx.engine import EditorEngine
    from meenifx.media.cache import MediaEntry

    picture = synthetic_frame() if frame is None else frame
    height, width = picture.shape[:2]
    engine = EditorEngine(fps)
    engine.source_width, engine.source_height = width, height
    path = "synthetic-effect-test.png"
    entry = MediaEntry("video", frames, fps)
    entry.frames = [picture.copy() for _ in range(frames)]
    engine.media._entries[path] = entry
    track = engine.timeline.add_track("video")
    clip = engine.timeline.new_clip("test", path, track, start, duration,
                                    source_out=frames, source_fps=fps)
    return engine, clip


def render(engine, clip, effect_id, params, frame_no=0, scale=1.0):
    clip.effects = [{"effect_id": effect_id, "enabled": True, "params": dict(params)}]
    _raw, processed = engine.render_frame(frame_no, scale)
    return processed


def frame_diff(a, b):
    import numpy as np
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())
