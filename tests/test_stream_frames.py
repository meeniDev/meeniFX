"""Long videos stay on disk: StreamFrames behaves like a list of frames."""
import os
import tempfile
import time

import cv2
import numpy as np

from meenifx.media import reader


def _make_video(path, frames=90, size=(160, 96), fps=30):
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    for i in range(frames):
        out.write(np.full((size[1], size[0], 3), i * 2 % 256, np.uint8))
    out.release()


def test_stream_matches_eager():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.avi")
        _make_video(path)
        _kind, eager, _fps = reader.read_media(path)
        old = reader.STREAM_BYTES
        reader.STREAM_BYTES = 1000
        try:
            _kind, lazy, fps = reader.read_media(path)
        finally:
            reader.STREAM_BYTES = old
        assert isinstance(lazy, reader.StreamFrames) and len(lazy) == len(eager) == 90
        for i in (0, 1, 2, 40, 39, 89, 5):
            assert abs(int(lazy[i].mean()) - int(eager[i].mean())) <= 3, i
        for _ in range(50):
            if lazy.thumbs:
                break
            time.sleep(0.05)
        assert lazy.thumb_for(70) is not None
        lazy.close()
        lazy._thumb_thread.join(5)
