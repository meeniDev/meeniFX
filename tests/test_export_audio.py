import os
import subprocess

import cv2
import numpy as np
import pytest

from meenifx.audio.export_mix import ffmpeg_exe
from meenifx.audio.mix import Segment
from test_effect_parameters import build_engine, synthetic_frame


def _tone_segment(seconds, rate=48000):
    t = np.arange(int(seconds * rate)) / rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return Segment(start_seconds=0.0, end_seconds=seconds, source=np.stack([tone, tone], axis=1), rate=rate,
                   base_seconds=0.0, speed=1.0, direction=1, loop_start=0.0, loop_length=0.0, gain=1.0)


def _audio_info(path):
    """Decode the audio track to raw PCM with ffmpeg and report (has_audio, peak, seconds)."""
    out = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", path, "-vn", "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                         capture_output=True)
    samples = np.frombuffer(out.stdout, dtype=np.int16)
    if samples.size == 0:
        return False, 0, 0.0
    return True, int(np.abs(samples).max()), samples.size / 8000.0


@pytest.mark.parametrize("fmt", ["mp4", "avi"])
def test_export_contains_audio(tmp_path, fmt):
    engine, _clip = build_engine(frame=synthetic_frame(), duration=60)
    target = tmp_path / f"out.{fmt}"
    path, count, fps, _size = engine.export({
        "output_path": str(target), "format": fmt, "use_source_fps": True, "use_source_size": True,
        "range_start": 0, "range_end": 60, "audio_segments": [_tone_segment(2.0)], "audio_rate": 48000,
    })
    has_audio, peak, seconds = _audio_info(path)
    assert has_audio and peak > 5000
    assert seconds == pytest.approx(count / fps, abs=0.25)
    assert cv2.VideoCapture(path).isOpened()
    assert not [f for f in os.listdir(tmp_path) if ".tmp" in f]


def test_export_without_audio_stays_video_only(tmp_path):
    engine, _clip = build_engine(frame=synthetic_frame(), duration=10)
    path, *_ = engine.export({"output_path": str(tmp_path / "v.mp4"), "format": "mp4", "use_source_fps": True,
                              "use_source_size": True, "range_start": 0, "range_end": 10})
    assert os.path.exists(path) and not _audio_info(path)[0]

def _decode_frame_count(path):
    cap = cv2.VideoCapture(path)
    count = 0
    while True:
        ok, _frame = cap.read()
        if not ok:
            break
        count += 1
    cap.release()
    return count


def test_export_uses_the_hardware_encoder_and_falls_back(tmp_path):
    """NVENC when it is really usable, the OpenCV writer when it is not - never a lie."""
    from meenifx.media.hardware import nvenc_available

    engine, _clip = build_engine(frame=synthetic_frame(), duration=20)
    engine.hardware_encode = "auto"
    path, count, _fps, _size = engine.export({
        "output_path": str(tmp_path / "hw.mp4"), "format": "mp4", "use_source_fps": True,
        "use_source_size": True, "range_start": 0, "range_end": 20})
    expected = "h264_nvenc" if nvenc_available() else "mp4v"
    assert engine.last_export_encoder == expected
    decoded = _decode_frame_count(path)
    assert abs(decoded - count) <= 2, (decoded, count)

    engine.hardware_encode = "off"
    path2, count2, _fps2, _size2 = engine.export({
        "output_path": str(tmp_path / "cpu.mp4"), "format": "mp4", "use_source_fps": True,
        "use_source_size": True, "range_start": 0, "range_end": 20})
    assert engine.last_export_encoder == "mp4v"
    assert abs(_decode_frame_count(path2) - count2) <= 2

