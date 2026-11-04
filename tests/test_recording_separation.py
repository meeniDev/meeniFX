"""Recorded microphone and system audio stay separate, linked and independently editable.

The recorder writes one file per source today, so these tests drive the real insertion path
with real (short) media files: what matters is that the timeline ends up with one picture clip
and two sound clips that share a link group, keep their own tracks, and do not drag the
picture around when one of them is moved or muted.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model.timeline import CommandStack, Timeline  # noqa: E402
from meenifx.recording.capture import RecordingResult, Segment  # noqa: E402
from meenifx.recording.insert import insert_recording  # noqa: E402
from meenifx.recording.settings import RecorderSettings  # noqa: E402


def make_wav(path, seconds=2.0, rate=16000, amplitude=0.4):
    import wave
    frames = int(seconds * rate)
    tone = (np.sin(2 * np.pi * 220.0 * np.arange(frames) / rate) * amplitude)
    pcm = (tone * 32767.0).astype("<i2")
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.repeat(pcm, 2).tobytes())
    return path


def make_video(path, seconds=2.0, fps=30.0):
    """A tiny real Matroska file: the insertion path probes it, so it must be readable."""
    import cv2
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"FFV1"), fps, (64, 48))
    if writer.isOpened():
        for index in range(int(seconds * fps)):
            frame = np.full((48, 64, 3), index % 255, np.uint8)
            writer.write(frame)
        writer.release()
        if os.path.getsize(path) > 0:
            return path
    raise RuntimeError("could not write a test video")


def three_source_result(tmp_path, start_frame=30):
    video = make_video(str(tmp_path / "Screen 1.mkv"))
    mic = make_wav(str(tmp_path / "Voice 1.wav"))
    system = make_wav(str(tmp_path / "System sound 1.wav"))
    result = RecordingResult("2026-11-03 10-00-00", str(tmp_path), start_frame)
    result.segments["screen"] = [Segment(video, 0.0, 2.0)]
    result.segments["mic"] = [Segment(mic, 0.0, 2.0)]
    result.segments["system"] = [Segment(system, 0.0, 2.0)]
    return result


def by_source(timeline):
    return {os.path.basename(c.source_path): c for c in timeline.all_clips()}


# ----------------------------------------------------------------- structure
def test_screen_with_both_audio_sources_makes_three_linked_clips(tmp_path):
    timeline = Timeline(fps=30)
    stack = CommandStack(timeline)
    ids = insert_recording(timeline, stack, three_source_result(tmp_path))
    assert len(ids) == 3

    clips = by_source(timeline)
    assert len(clips) == 3
    video = clips["Screen 1.mkv"]
    mic = clips["Voice 1.wav"]
    system = clips["System sound 1.wav"]

    assert video.track.type == "video"
    assert mic.track.type == system.track.type == "audio"
    assert mic.track is not system.track, "the two sources get their own lanes"
    assert len({video.link_id, mic.link_id, system.link_id}) == 1
    assert video.link_id != 0
    # Everything starts on the same frame, which is what keeps the take in sync.
    assert {c.timeline_start for c in (video, mic, system)} == {30}
    assert timeline.link_is_synced(mic) and timeline.link_is_synced(video)


def test_one_undo_step_removes_the_whole_take(tmp_path):
    timeline = Timeline(fps=30)
    stack = CommandStack(timeline)
    insert_recording(timeline, stack, three_source_result(tmp_path))
    assert len(timeline.all_clips()) == 3
    stack.undo()
    assert timeline.all_clips() == []
    stack.redo()
    assert len(timeline.all_clips()) == 3


def test_mic_only_recording_needs_no_video_track(tmp_path):
    timeline = Timeline(fps=30)
    stack = CommandStack(timeline)
    result = RecordingResult("t", str(tmp_path), 0)
    result.segments["mic"] = [Segment(make_wav(str(tmp_path / "Voice 1.wav")), 0.0, 2.0)]
    ids = insert_recording(timeline, stack, result)
    assert len(ids) == 1
    clip = timeline.find_clip(ids[0])
    assert clip.track.type == "audio"
    assert timeline.tracks_of_type("video") == []
    assert clip.link_id == 0, "a lone clip has nothing to link to"


# ------------------------------------------------- editing independence
def test_gain_and_mute_are_per_source(tmp_path):
    timeline = Timeline(fps=30)
    insert_recording(timeline, CommandStack(timeline), three_source_result(tmp_path))
    clips = by_source(timeline)
    mic, system = clips["Voice 1.wav"], clips["System sound 1.wav"]

    CH.set_value(mic, CH.gain_channel(), -6.0)
    assert float(CH.value_at(mic, CH.gain_channel(), 0)) == pytest.approx(-6.0)
    assert float(CH.value_at(system, CH.gain_channel(), 0)) == pytest.approx(0.0)

    system.track.muted = True
    assert not mic.track.muted and not clips["Screen 1.mkv"].track.muted


def test_effects_are_per_source(tmp_path):
    timeline = Timeline(fps=30)
    insert_recording(timeline, CommandStack(timeline), three_source_result(tmp_path))
    clips = by_source(timeline)
    mic = clips["Voice 1.wav"]
    mic.effects = [{"effect_id": "audio_compressor", "enabled": True, "params": {}}]
    assert mic.effects
    assert not clips["System sound 1.wav"].effects


def test_unlinking_leaves_every_clip_in_place(tmp_path):
    timeline = Timeline(fps=30)
    insert_recording(timeline, CommandStack(timeline), three_source_result(tmp_path))
    clips = by_source(timeline)
    starts = {c.id: c.timeline_start for c in timeline.all_clips()}
    timeline.clear_link([c.id for c in timeline.all_clips()])
    assert all(c.link_id == 0 for c in timeline.all_clips())
    assert {c.id: c.timeline_start for c in timeline.all_clips()} == starts


def test_moving_one_audio_clip_keeps_the_picture_where_it_was(tmp_path):
    """A lane change on a sound clip must not move the video vertically."""
    timeline = Timeline(fps=30)
    insert_recording(timeline, CommandStack(timeline), three_source_result(tmp_path))
    clips = by_source(timeline)
    video = clips["Screen 1.mkv"]
    mic = clips["Voice 1.wav"]
    video_track = video.track
    other_lane = timeline.add_track("audio")
    mic.track.clips.remove(mic)
    other_lane.clips.append(mic)
    mic.track = other_lane
    assert video.track is video_track
    assert video_track.type == "video"
    assert mic.track is other_lane and other_lane.type == "audio"


def test_horizontal_move_of_the_group_keeps_sync(tmp_path):
    """Sliding the linked set in time must move every member by the same amount."""
    timeline = Timeline(fps=30)
    insert_recording(timeline, CommandStack(timeline), three_source_result(tmp_path))
    clips = by_source(timeline)
    offset = 45
    for clip in (clips["Screen 1.mkv"], clips["Voice 1.wav"], clips["System sound 1.wav"]):
        clip.timeline_start += offset
    assert timeline.link_is_synced(clips["Voice 1.wav"])
    assert {c.timeline_start for c in timeline.all_clips()} == {30 + offset}


def test_linked_trimming_keeps_the_take_together(tmp_path):
    timeline = Timeline(fps=30)
    insert_recording(timeline, CommandStack(timeline), three_source_result(tmp_path))
    clips = by_source(timeline)
    video = clips["Screen 1.mkv"]
    partners = [c.id for c in timeline.linked_pairs(video)]
    assert len(partners) == 2
    snapshot = timeline.snapshot()
    timeline.apply_stretch(snapshot, video.id, "right", video.timeline_start + 20, also=partners)
    for clip in timeline.all_clips():
        assert clip.duration == 20
    assert timeline.link_is_synced(video)


# ------------------------------------------------------------------ settings
def test_both_audio_sources_are_offered_together():
    settings = RecorderSettings(webcam_on=False, screen_on=True, mic_on=True, system_on=True)
    assert settings.sources() == ["screen", "mic", "system"]
    assert RecorderSettings(webcam_on=False, screen_on=False, mic_on=True,
                            system_on=False).sources() == ["mic"]
    assert RecorderSettings(webcam_on=False, screen_on=False, mic_on=False,
                            system_on=True).sources() == ["system"]
    assert RecorderSettings(webcam_on=True, screen_on=False, mic_on=False,
                            system_on=False).sources() == ["webcam"]


def test_muxing_is_off_so_the_default_keeps_sources_apart():
    assert RecorderSettings().mux_audio is False


# ------------------------------------------------------------- live feedback
@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def test_recording_bar_shows_every_captured_source(app):
    from PyQt6.QtGui import QImage, QPainter

    from meenifx.ui.recorder_bar import FloatingBar

    bar = FloatingBar()
    bar.set_sources(["screen", "mic", "system"])
    assert "Screen" in bar.sources.text() and "Mic" in bar.sources.text()
    assert "System" in bar.sources.text()
    assert bar.meter.isVisibleTo(bar) and bar.system_meter.isVisibleTo(bar)
    assert len(bar.history._lanes) == 2, "one history lane per audio source"

    for level in (0.2, 0.9, 0.5):
        bar.push_level("mic", level)
        bar.push_level("system", level / 2)
    bar.history._sample()
    assert bar.meter.value > 0 and bar.system_meter.value > 0
    assert len(bar.history._lanes[0][3]) == 1

    # The bar must paint without a device or a video source present.
    bar.set_elapsed(75)
    assert bar.time.text() == "01:15"
    image = QImage(360, 90, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        bar.history.resize(150, 30)
        bar.history.render(painter)
    finally:
        painter.end()


def test_a_microphone_only_take_shows_one_lane_and_no_video(app):
    from meenifx.ui.recorder_bar import FloatingBar

    bar = FloatingBar()
    bar.set_sources(["mic"])
    assert bar.sources.text() == "Mic"
    assert bar.meter.isVisibleTo(bar)
    assert not bar.system_meter.isVisibleTo(bar)
    assert len(bar.history._lanes) == 1, "no empty lane where no source is recording"
    bar.set_sources([])
    assert len(bar.history._lanes) == 0
