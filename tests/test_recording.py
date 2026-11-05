import os
import tempfile

from meenifx.model.timeline import CommandStack, Timeline
from meenifx.recording import disk, insert
from meenifx.recording.capture import RecordingResult, Segment, recording_name
from meenifx.recording.hotkey import MOD_CONTROL, MOD_SHIFT, parse_hotkey
from meenifx.recording.settings import RecorderSettings


def fake_probe(path):
    return (4.0, 0, 0.0) if path.endswith(".wav") else (4.0, 120, 30.0)


def make_result(start_frame=45, parts=1):
    r = RecordingResult("2026-11-02 14-30-05", "x", start_frame)
    for kind, label in (("webcam", "Webcam"), ("screen", "Screen"), ("mic", "Voice")):
        r.segments[kind] = [Segment(f"{label} s{i}.{'wav' if kind == 'mic' else 'mkv'}", i * 3.0, 3.0) for i in range(parts)]
    return r


def test_names():
    assert recording_name("webcam", "2026-11-02 14-30-05") == "Webcam 2026-11-02 14-30-05.mkv"
    assert recording_name("mic", "S") == "Voice S.wav"
    assert recording_name("screen", "S", 1, 2) == "Screen S part 2.mkv"


def test_insert_one_undo_same_start_separate_tracks():
    tl = Timeline(30)
    stack = CommandStack(tl)
    ids = insert.insert_recording(tl, stack, make_result(), fake_probe)
    assert len(ids) == 3
    clips = [tl.find_clip(i) for i in ids]
    assert {c.timeline_start for c in clips} == {45}
    assert len({id(c.track) for c in clips}) == 3
    types = sorted(c.track.type for c in clips)
    assert types == ["audio", "video", "video"]
    top = tl.tracks[0]
    assert top.clips[0].name.startswith("Webcam")          # webcam is the top video layer
    stack.undo()
    assert tl.all_clips() == [] and not tl.tracks_of_type("video")
    stack.redo()
    assert len(tl.all_clips()) == 3


def test_reuses_free_tracks_and_avoids_overlap():
    tl = Timeline(30)
    stack = CommandStack(tl)
    v1, v2 = tl.add_track("video"), tl.add_track("video")
    a1 = tl.add_track("audio")
    tl.new_clip("busy", "b.mp4", v1, 0, 400)            # occupied lane
    ids = insert.insert_recording(tl, stack, make_result(start_frame=60), fake_probe)
    clips = [tl.find_clip(i) for i in ids]
    assert len(tl.tracks_of_type("video")) == 3          # one reused (v2), one new
    assert all(c.track is not v1 for c in clips)
    assert any(c.track is v2 for c in clips) and any(c.track is a1 for c in clips)


def test_segments_trimmed_back_to_back():
    tl = Timeline(30)
    stack = CommandStack(tl)
    r = make_result(start_frame=0, parts=2)
    ids = insert.insert_recording(tl, stack, r, fake_probe)
    video = [c for c in map(tl.find_clip, ids) if c.name.startswith("Screen")]
    video.sort(key=lambda c: c.timeline_start)
    assert video[0].timeline_start == 0 and video[1].timeline_start == 90
    assert video[0].end <= video[1].timeline_start      # 4 s file trimmed to the 3 s segment


def test_hotkey_parse_and_settings_and_disk():
    assert parse_hotkey("Ctrl+Shift+R") == (MOD_CONTROL | MOD_SHIFT, ord("R"))
    assert parse_hotkey("R") is None and parse_hotkey("Ctrl+Nope") is None
    assert parse_hotkey("Ctrl+F9")[1] == 0x78
    s = RecorderSettings(screen_on=True, voice_over=True)
    assert s.sources() == ["mic"]
    assert RecorderSettings(webcam_on=False, screen_on=True, mic_on=False,
                            system_on=False).sources() == ["screen"]
    # System sound is its own source now, and voice-over stays microphone only.
    assert RecorderSettings(webcam_on=False, screen_on=False, mic_on=False).sources() == ["system"]
    assert RecorderSettings(webcam_on=True, screen_on=True, mic_on=True,
                            system_on=True, voice_over=True).sources() == ["mic"]
    assert RecorderSettings(webcam_on=True, screen_on=False, mic_on=False,
                            system_on=True).sources() == ["webcam", "system"]
    with tempfile.TemporaryDirectory() as tmp:
        assert disk.free_mb(os.path.join(tmp, "not", "there")) > 0
    assert "Settings" in disk.friendly_error("mic", "Permission denied")
    assert "in use" in disk.friendly_error("webcam", "device busy")
    assert "Disk full" in disk.friendly_error("screen", "no space left")


def test_the_record_button_is_the_in_app_status_indicator(qapp):
    from PyQt6.QtWidgets import QPushButton
    from meenifx.ui.main_window import MainWindow

    class Stub:
        pass

    stub = Stub()
    stub.record_button = QPushButton()
    MainWindow.set_recording_status(stub, 75)
    assert stub.record_button.text() == "\u25cf 01:15"
    assert "stop" in stub.record_button.toolTip().lower()
    MainWindow.set_recording_status(stub, None)
    assert stub.record_button.text() == "Record"
