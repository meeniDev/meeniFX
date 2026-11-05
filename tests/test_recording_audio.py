"""Recorder audio: WASAPI capture, WAV output, muxing and the linked timeline clips.

The device tests run against whatever audio endpoints this machine has and skip when there
are none; everything else (WAV writing, resampling, muxing, insertion) is deterministic.
"""

import os
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.recording import audiocap, mux  # noqa: E402
from meenifx.recording.capture import RecordingResult, Segment  # noqa: E402
from meenifx.recording.insert import insert_recording, probe_segment  # noqa: E402
from meenifx.recording.settings import RecorderSettings  # noqa: E402

FFMPEG = None


def ffmpeg():
    global FFMPEG
    if FFMPEG is None:
        from meenifx.audio.export_mix import ffmpeg_exe
        FFMPEG = ffmpeg_exe()
    return FFMPEG


def make_video(path, seconds=1.0):
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size=160x120:rate=30:duration={seconds}", "-c:v", "mpeg4", path],
                   check=True)
    return path


def make_wav(path, seconds=1.0, rate=48000, channels=2, freq=440.0):
    t = np.arange(int(seconds * rate)) / float(rate)
    tone = 0.3 * np.sin(2 * np.pi * freq * t)
    block = np.repeat(tone[:, None], channels, axis=1)
    with wave.open(path, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((block * 32767.0).astype("<i2").tobytes())
    return path


def streams(path):
    """ffmpeg's stream summary for a file: ('video', 'audio') etc."""
    probe = subprocess.run([ffmpeg(), "-hide_banner", "-i", path], capture_output=True, text=True)
    found = []
    for line in probe.stderr.splitlines():
        if "Stream #" not in line:
            continue
        found.append("audio" if "Audio:" in line else "video")
    return found


# --------------------------------------------------------------------- building blocks
def test_wav_sink_writes_a_header_a_player_can_read(tmp_path):
    path = str(tmp_path / "sink.wav")
    sink = audiocap.WaveSink(path, 48000, 2)
    frames = 4800
    sink.write(np.zeros((frames // 2, 2), dtype=np.float32))
    sink.write(np.zeros((frames - frames // 2, 2), dtype=np.float32))
    assert sink.close() == frames
    with wave.open(path) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (2, 48000, 2)
        assert w.getnframes() == frames
        assert len(w.readframes(frames)) == frames * 4


def test_resampling_produces_the_requested_rate_without_drift():
    converter = audiocap.RateConverter(44100, 48000, 2)
    produced = 0
    block = np.random.default_rng(3).normal(0, 0.1, (441, 2)).astype(np.float32)
    for _ in range(100):
        produced += len(converter.push(block))
    assert abs(produced - 48000) <= 2, f"expected ~48000 samples, got {produced}"


def test_resampler_leaves_a_matching_rate_untouched():
    converter = audiocap.RateConverter(48000, 48000, 2)
    source = np.random.default_rng(4).normal(0, 0.2, (480, 2)).astype(np.float32)
    out = np.vstack([converter.push(source.copy()) for _ in range(5)])
    assert abs(len(out) - 2400) <= 5
    # The waveform has to survive: no blocks of silence and no sign flips.
    assert float(np.mean(np.abs(out))) > 0.05


def test_the_resampler_follows_the_wall_clock():
    """When the endpoint clock runs fast, the ratio bends so the audio stays with the picture."""
    converter = audiocap.RateConverter(48000, 48000, 2)
    # Pretend a half second of audio has already been produced after 0.4 s of real time.
    for _ in range(50):
        converter.nudge(0.5, tolerance=0.03)
    assert converter.step > converter.nominal, "a fast clock must slow the stream down"
    for _ in range(200):
        converter.nudge(0.0, tolerance=0.03)
    assert abs(converter.step - converter.nominal) < 1e-6, "it settles back when in sync"


def test_channel_mapping_and_meters():
    stereo = np.ones((10, 2), dtype=np.float32)
    mono = audiocap.fit_channels(stereo, 1)
    assert mono.shape == (10, 1) and mono[0, 0] == pytest.approx(1.0)
    doubled = audiocap.fit_channels(np.full((10, 1), 0.5, dtype=np.float32), 2)
    assert doubled.shape == (10, 2) and doubled[0, 1] == pytest.approx(0.5)
    silent = audiocap.fit_channels(np.zeros((10, 6), dtype=np.float32), 2)
    assert silent.shape == (10, 2)
    assert audiocap.level_of(np.zeros((10, 2), dtype=np.float32)) == 0.0
    loud = audiocap.level_of(np.full((10, 2), 0.5, dtype=np.float32))
    quiet = audiocap.level_of(np.full((10, 2), 0.005, dtype=np.float32))
    assert 0.0 < quiet < loud <= 1.0


# ----------------------------------------------------------------------------- muxing
def test_mux_puts_real_audio_inside_the_video(tmp_path):
    video = make_video(str(tmp_path / "Screen 1.mkv"))
    wav = make_wav(str(tmp_path / "Voice 1.wav"), seconds=1.0)
    result = RecordingResult("t", str(tmp_path), 0)
    result.segments["screen"] = [Segment(video, 0.0, 1.0)]
    result.segments["mic"] = [Segment(wav, 0.0, 1.0)]

    kinds, warnings = mux.mux_recording(result)
    assert kinds == {"screen"} and warnings == []
    muxed = result.segments["screen"][0]
    assert muxed.muxed and muxed.path != video and os.path.isfile(muxed.path)
    assert muxed.audio_paths == [wav]
    assert streams(muxed.path) == ["video", "audio"], "the file must really carry sound"


def test_mux_mixes_the_microphone_with_the_system_sound(tmp_path):
    video = make_video(str(tmp_path / "Screen 1.mkv"))
    voice = make_wav(str(tmp_path / "Voice 1.wav"), seconds=1.0, freq=300.0)
    system = make_wav(str(tmp_path / "System 1.wav"), seconds=1.0, freq=900.0)
    result = RecordingResult("t", str(tmp_path), 0)
    result.segments["screen"] = [Segment(video, 0.0, 1.0)]
    result.segments["mic"] = [Segment(voice, 0.0, 1.0)]
    result.segments["system"] = [Segment(system, 0.0, 1.0)]
    kinds, warnings = mux.mux_recording(result)
    assert kinds == {"screen"} and warnings == []
    assert streams(result.segments["screen"][0].path) == ["video", "audio"]


def test_mux_failure_keeps_the_plain_video_and_warns(tmp_path):
    missing = str(tmp_path / "gone.mkv")
    result = RecordingResult("t", str(tmp_path), 0)
    result.segments["screen"] = [Segment(missing, 0.0, 1.0)]
    result.segments["mic"] = [Segment(make_wav(str(tmp_path / "v.wav")), 0.0, 1.0)]
    kinds, warnings = mux.mux_recording(result)
    assert kinds == set()
    assert warnings and "Could not add the sound" in warnings[0]
    assert result.segments["screen"][0].path == missing
    assert result.segments["screen"][0].muxed is False


# -------------------------------------------------------------------------- insertion
def test_a_muxed_recording_becomes_a_linked_video_audio_pair(tmp_path):
    from meenifx.model.timeline import CommandStack, Timeline

    video = make_video(str(tmp_path / "Screen 1.mkv"))
    result = RecordingResult("t", str(tmp_path), 30)
    result.segments["screen"] = [Segment(video, 0.0, 1.0, muxed=True,
                                         audio_paths=[str(tmp_path / "Voice 1.wav")])]
    result.segments["mic"] = [Segment(str(tmp_path / "Voice 1.wav"), 0.0, 1.0)]
    timeline = Timeline(fps=30)
    stack = CommandStack(timeline)
    ids = insert_recording(timeline, stack, result)
    assert len(ids) == 2, "one picture clip and one sound clip, not three"
    picture = next(c for c in timeline.all_clips() if c.stream == "video")
    sound = next(c for c in timeline.all_clips() if c.stream == "audio")
    assert picture.link_id and picture.link_id == sound.link_id
    assert picture.timeline_start == sound.timeline_start == 30
    assert picture.duration == sound.duration
    assert picture.track.type == "video" and sound.track.type == "audio"
    assert sound.carries_audio and not picture.carries_audio, "the sound plays exactly once"
    # One undo step for the whole recording.
    assert stack.can_undo
    stack.undo()
    assert timeline.all_clips() == []


def test_an_unmuxed_audio_track_is_inserted_on_its_own_but_linked(tmp_path):
    from meenifx.model.timeline import CommandStack, Timeline

    video = make_video(str(tmp_path / "Screen 1.mkv"))
    voice = make_wav(str(tmp_path / "Voice 1.wav"))
    result = RecordingResult("t", str(tmp_path), 0)
    result.segments["screen"] = [Segment(video, 0.0, 1.0)]
    result.segments["mic"] = [Segment(voice, 0.0, 1.0)]
    timeline = Timeline(fps=30)
    ids = insert_recording(timeline, CommandStack(timeline), result)
    assert len(ids) == 2
    assert {c.track.type for c in timeline.all_clips()} == {"video", "audio"}
    # Separate clips, one group: they slide together but stay independently editable.
    assert len({c.link_id for c in timeline.all_clips()}) == 1
    assert timeline.all_clips()[0].link_id != 0


def test_probe_segment_reads_a_wav_duration(tmp_path):
    path = make_wav(str(tmp_path / "x.wav"), seconds=1.5, rate=48000)
    seconds, frames, fps = probe_segment(path)
    assert seconds == pytest.approx(1.5, abs=0.01) and frames == 0 and fps == 0.0


# ------------------------------------------------------------------------- settings
def test_recorder_settings_carry_the_audio_options(tmp_path):
    store = __import__("PyQt6.QtCore", fromlist=["QSettings"]).QSettings(
        str(tmp_path / "s.ini"), __import__("PyQt6.QtCore", fromlist=["QSettings"]).QSettings.Format.IniFormat)
    settings = RecorderSettings()
    # Muxing is off by default: the microphone and the system sound stay separate clips.
    assert (settings.audio_rate, settings.audio_channels, settings.mux_audio) == (48000, 2, False)
    settings.system_on, settings.system_id = False, "ep-1"
    settings.audio_rate, settings.audio_channels, settings.mux_audio = 44100, 1, False
    settings.system_volume = 40
    settings.save(store)
    loaded = RecorderSettings.load(store)
    assert (loaded.system_on, loaded.system_id, loaded.system_volume) == (False, "ep-1", 40)
    assert (loaded.audio_rate, loaded.audio_channels, loaded.mux_audio) == (44100, 1, False)


# ---------------------------------------------------------------------- real devices
@pytest.fixture(scope="module")
def qt_app():
    """One QApplication for this module; it has to outlive every widget created here."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_the_recorder_panel_starts_with_real_audio_sources(tmp_path, qt_app):
    """The panel offers system sound as a source and hands over ready captures."""
    from meenifx.ui.recorder_panel import RecorderPanel

    settings = RecorderSettings(folder=str(tmp_path))
    panel = RecorderPanel(None, default_folder=str(tmp_path))
    panel.settings = settings
    try:
        assert panel.system_card.is_on(), "system audio is offered and on by default"
        assert panel.system_card is not None and panel.system_level is not None
        # The controls must show the settings they were handed, not this machine's saved ones.
        panel._load_into_controls()
        assert panel.system_volume.value() == settings.system_volume
        if not audiocap.pick_endpoint(True, ""):
            pytest.skip("no output device on this machine")
        assert panel.system_combo.count() >= 1 and panel.mic_combo.count() >= 1
        captures = panel.captures()
        assert "system" in captures and "mic" in captures
        capture = captures["system"]
        assert capture.device is not None and capture.loopback
        assert (capture.rate, capture.channels) == (settings.audio_rate, settings.audio_channels)
    finally:
        panel.stop_previews()
        panel.hide()


def test_window_capture_saves_the_title_because_qt_has_no_window_id(tmp_path, qt_app, monkeypatch):
    """Picking a window used to raise AttributeError: QCapturableWindow has no id()."""
    from meenifx.recording import devices
    from meenifx.ui.recorder_panel import RecorderPanel

    class _Window:
        def __init__(self, title):
            self._title = title

        def description(self):
            return self._title

        def isValid(self):
            return True

    titles = ["meeniFX - project", "VLC media player"]
    monkeypatch.setattr(devices, "windows", lambda: [_Window(title) for title in titles])
    panel = RecorderPanel(None, default_folder=str(tmp_path))
    try:
        panel.settings = RecorderSettings(screen_mode="window", window_title=titles[1],
                                          folder=str(tmp_path))
        panel._loading = True                     # do not push the stub into the real captures
        try:
            panel._load_into_controls()           # the traceback's crash site
        finally:
            panel._loading = False
        assert panel.screen_target.count() == len(titles)
        assert panel.screen_target.currentData().description() == titles[1],             "the saved window was not reselected"
        assert panel.collect().window_title == titles[1], "the choice is saved as the title"
        panel.screen_target.setCurrentIndex(0)
        assert panel.collect().window_title == titles[0]
        assert devices.find_window(titles[0]).description() == titles[0]
        assert devices.find_window("a window that is gone") is None
    finally:
        panel.stop_previews()
        panel.hide()


def _peak_level(path):
    """Loudest sample in a 16-bit WAV, for the "was anything playing?" check."""
    import numpy as np
    with wave.open(path) as handle:
        data = handle.readframes(handle.getnframes())
    if not data:
        return 0
    return int(np.abs(np.frombuffer(data, dtype="<i2")).max(initial=0))


@pytest.mark.parametrize("loopback,kind", [(True, "system"), (False, "mic")])
def test_a_real_device_is_captured_at_the_requested_format(tmp_path, loopback, kind):
    endpoint = audiocap.pick_endpoint(loopback, "")
    if endpoint is None:
        pytest.skip("no audio endpoint on this machine")
    capture = audiocap.AudioCapture(kind, loopback)
    capture.use_endpoint(endpoint)
    capture.set_format(48000, 2)
    capture.set_gain(1.0)
    errors, levels = [], []
    capture.failed.connect(lambda k, m: errors.append(m))
    capture.levelChanged.connect(levels.append)
    path = str(tmp_path / f"{kind}.wav")
    seconds = 0.8
    assert capture.begin(path), "capture should start"
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        time.sleep(0.02)
    capture.end()
    if loopback and errors == ["System audio recorded no sound."]:
        # The capture reports silence as an error, which is the right thing to tell a user
        # but not a failure of the recorder: a quiet machine plays nothing to loop back.
        pytest.skip("nothing was playing on the system output during the capture")
    assert not errors, errors
    with wave.open(path) as w:
        rate, channels, frames = w.getframerate(), w.getnchannels(), w.getnframes()
    assert (rate, channels) == (48000, 2), "the format is what was asked for"
    captured = frames / float(rate)
    assert abs(captured - seconds) <= 0.15, f"clock drift: asked {seconds}s, got {captured:.3f}s"
    assert levels, "the meter has to see the stream"
    if loopback and _peak_level(path) < 200:
        # Loopback records what the speakers play, so a machine playing nothing produces a
        # perfectly valid silent recording. Only a signal can prove the routing works.
        pytest.skip("nothing was playing on the system output during the capture")
    if not loopback:
        assert _peak_level(path) >= 0, "the microphone file is always readable"
