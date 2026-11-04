"""Waveforms that show the sound a clip really plays.

Three layers are checked here, and each one is the real code that ships:

* the exact part - gain and fades are per-sample multipliers, so the drawn (min, max)
  envelope must scale by exactly the same factor the mixer applies;
* the expensive part - everything that reshapes the signal is measured by running the
  actual audio chain on decoded samples, on a worker, and caching the result;
* the painted part - the timeline canvas draws the processed envelope inside its lane
  and never larger than the lane for a boosted clip.
"""

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.audio import processed_waveform as PW  # noqa: E402
from meenifx.audio.library import SAMPLE_RATE, AudioLibrary  # noqa: E402
from meenifx.audio.waveform import compute_peaks  # noqa: E402
from meenifx.audio.waveform_service import WaveformService  # noqa: E402
from meenifx.model import channels as CH  # noqa: E402
from meenifx.model.keyframes import AnimatedValue  # noqa: E402

RATE = 16000


def pump(seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.004)


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication(sys.argv)


def tone(seconds=1.0, rate=RATE, amplitude=0.5, freq=220.0):
    frames = int(seconds * rate)
    wave = (np.sin(2 * np.pi * freq * np.arange(frames) / rate) * amplitude).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def audio_clip(engine, duration=60, path="synthetic.wav"):
    track = engine.timeline.add_track("audio")
    return engine.timeline.new_clip("sound", path, track, 0, duration)


@pytest.fixture
def engine():
    from conftest import build_engine
    return build_engine()[0]


# ------------------------------------------------------------------ gain maths
def test_db_to_linear_matches_the_mixer_curve():
    assert PW.db_to_linear(0.0) == pytest.approx(1.0, rel=1e-5)
    assert PW.db_to_linear(-6.0) == pytest.approx(0.5012, rel=1e-3)
    assert PW.db_to_linear(6.0) == pytest.approx(1.9953, rel=1e-3)
    # Silence is silence, not a tiny non-zero line on screen.
    assert float(PW.db_to_linear(-80.0)) == 0.0


def test_gain_curve_reads_the_keyframed_gain_channel(engine):
    clip = audio_clip(engine)
    CH.set_value(clip, CH.gain_channel(), 6.0)
    gains = PW.gain_curve(clip, np.array([0.0, 30.0, 60.0]))
    assert np.allclose(gains, PW.db_to_linear(6.0), rtol=1e-5)

    # A ramp from 0 dB to -12 dB must be interpolated per column, not held at the first key.
    ramp = AnimatedValue(base_value=0.0)
    ramp.set_keyframe(0, 0.0)
    ramp.set_keyframe(60, -12.0)
    clip.gain = ramp  # the channel reads the clip field, animated or not
    gains = PW.gain_curve(clip, np.array([0.0, 30.0, 60.0]))
    assert gains[0] == pytest.approx(1.0, rel=1e-3)
    assert 0.25 < float(gains[1]) < 0.75
    assert gains[2] == pytest.approx(PW.db_to_linear(-12.0), rel=1e-3)
    assert gains[0] > gains[1] > gains[2]


def test_gain_curve_is_flat_for_an_empty_selection():
    assert PW.gain_curve(None, np.zeros(0)).size == 0


# ---------------------------------------------------------------- cache keys
def test_signature_tracks_gain_fades_and_effects(engine):
    clip = audio_clip(engine)
    base = PW.clip_signature(clip)
    assert base == PW.clip_signature(clip), "an unchanged clip keeps its cache entry"

    CH.set_value(clip, CH.gain_channel(), -6.0)
    quieter = PW.clip_signature(clip)
    assert quieter != base

    clip.fade_in = 20
    assert PW.clip_signature(clip) != quieter

    before = PW.clip_signature(clip)
    clip.effects = [{"effect_id": "audio_compressor", "enabled": True,
                     "params": {"threshold": -20.0, "ratio": 4.0}}]
    assert PW.clip_signature(clip) != before


def test_shaping_effects_ignore_pure_gain_and_fades(engine):
    clip = audio_clip(engine)
    clip.effects = [{"effect_id": "audio_gain", "enabled": True, "params": {"gain_db": 6.0}},
                    {"effect_id": "audio_fade", "enabled": True, "params": {}}]
    assert not PW.has_shaping_effects(clip)

    clip.effects = [{"effect_id": "audio_compressor", "enabled": True, "params": {}}]
    assert PW.has_shaping_effects(clip)

    # A disabled compressor cannot change the sound, so it must not force a recompute.
    clip.effects = [{"effect_id": "audio_compressor", "enabled": False, "params": {}}]
    assert not PW.has_shaping_effects(clip)


# ------------------------------------------------------- processed measurement
def test_processed_levels_follow_the_real_chain():
    samples = tone(seconds=1.0)
    source = compute_peaks(samples)[0]
    source_peak = float(np.abs(source).max())

    quiet = PW.build_processed_levels(
        [{"effect_id": "audio_gain", "enabled": True, "params": {"gain_db": -12.0}}],
        samples, RATE)
    assert quiet is not None
    assert float(np.abs(quiet[0]).max()) == pytest.approx(source_peak * 0.2512, rel=0.05)

    loud = PW.build_processed_levels(
        [{"effect_id": "audio_gain", "enabled": True, "params": {"gain_db": 6.0}}],
        samples, RATE)
    assert float(np.abs(loud[0]).max()) > source_peak


def test_compressor_shrinks_the_measured_envelope_not_randomly():
    """A loud tone squeezed by a compressor must measure smaller, and its crest with it."""
    samples = tone(seconds=1.0, amplitude=0.9)
    flat = PW.build_processed_levels([], samples, RATE)
    squashed = PW.build_processed_levels(
        [{"effect_id": "audio_compressor", "enabled": True,
          "params": {"threshold": -30.0, "ratio": 8.0, "attack": 1.0,
                     "release": 50.0, "knee": 6.0, "makeup": 0.0}}],
        samples, RATE)
    assert squashed is not None and flat is not None
    flat_peak = float(np.abs(flat[0]).max())
    squash_peak = float(np.abs(squashed[0]).max())
    assert squash_peak < flat_peak * 0.5, "8:1 above -30 dB brings 0.9 down near -26 dB"
    assert squash_peak > 0.01, "silence would mean the chain broke, not compressed"
    # A softer ratio must land between the two, so the drawing tracks the setting.
    gentle = PW.build_processed_levels(
        [{"effect_id": "audio_compressor", "enabled": True,
          "params": {"threshold": -30.0, "ratio": 2.0, "attack": 1.0,
                     "release": 50.0, "knee": 0.0, "makeup": 0.0}}],
        samples, RATE)
    assert squash_peak < float(np.abs(gentle[0]).max()) < flat_peak


def test_cached_processed_levels_are_reused_and_evictable():
    PW.clear()
    signature = "sig-1"
    levels = compute_peaks(tone(seconds=0.2))
    assert PW.cached("a.wav", signature, RATE) is None
    PW.store("a.wav", signature, RATE, levels)
    assert PW.cached("a.wav", signature, RATE) is levels
    assert PW.cached("a.wav", "other", RATE) is None
    PW.clear()
    assert PW.cached("a.wav", signature, RATE) is None


def test_apply_gain_keeps_the_envelope_inside_the_unit_range():
    mins = np.array([-0.5, -1.0], np.float32)
    maxs = np.array([0.5, 1.0], np.float32)
    scaled_min, scaled_max = PW.apply_gain(mins, maxs, np.array([2.0, 4.0], np.float32))
    assert np.all(np.abs(scaled_min) <= 1.0) and np.all(np.abs(scaled_max) <= 1.0)
    assert scaled_min[0] == pytest.approx(-1.0)


# --------------------------------------------------------------- worker plumbing
def test_service_computes_processed_peaks_off_the_gui_thread(app, tmp_path):
    path = str(tmp_path / "tone.wav")
    library = AudioLibrary(sample_rate=RATE)
    library.set_cached(path, tone(seconds=1.0))
    service = WaveformService(library, str(tmp_path / "cache"))
    seen = []
    service.processed_ready.connect(lambda p, s: seen.append((p, s)))
    PW.clear()

    signature = "worker-sig"
    assert service.processed(path, signature, RATE) is None
    assert service.request_processed(
        path, signature,
        [{"effect_id": "audio_equalizer", "enabled": True,
          "params": {"low": 6.0, "mid": 0.0, "high": 0.0}}],
        RATE, 30.0) is False, "first call has to compute"

    end = time.monotonic() + 8.0
    while time.monotonic() < end and not seen:
        app.processEvents()
        time.sleep(0.01)
    assert seen and seen[0][0] == path and seen[0][1] == signature
    levels = service.processed(path, signature, RATE)
    assert levels is not None and levels[0].shape[1] == 2
    # Second request for the same state is answered from the memo, not re-run.
    assert service.request_processed(path, signature, [], RATE, 30.0) is True


def test_service_defers_until_the_source_is_decoded(app, tmp_path):
    path = str(tmp_path / "missing.wav")
    library = AudioLibrary(sample_rate=RATE)
    service = WaveformService(library, str(tmp_path / "cache2"))
    PW.clear()
    requested = []
    service.request = lambda p: requested.append(p)
    assert service.request_processed(path, "sig", [], RATE) is False
    assert requested == [path], "the decode must be kicked off, not skipped"


# ------------------------------------------------------------------ painting
def test_canvas_draws_a_taller_waveform_for_more_gain(app):
    """What the painter draws, not just the gain: attenuation shrinks, boost grows."""
    from meenifx.ui.timeline_widget import TimelineCanvas, shaped_envelope
    from conftest import build_engine

    engine = build_engine()[0]
    clip = audio_clip(engine, duration=60, path="tone.wav")
    canvas = TimelineCanvas(engine, None)
    canvas.resize(900, 300)
    canvas.timeline = engine.timeline
    # A half-scale source, exactly like a normal recording.
    source_min = np.full(40, -0.5, np.float32)
    source_max = np.full(40, 0.5, np.float32)

    def drawn(gain_db):
        CH.set_value(clip, CH.gain_channel(), gain_db)
        gains = canvas._wave_gain(clip, canvas.HEADER_W, 40)
        low, high = shaped_envelope(source_min, source_max, gains)
        return float(np.mean(high - low))

    unity = drawn(0.0)
    assert unity == pytest.approx(1.0, rel=1e-3), "0 dB draws the source as it is"
    assert drawn(-6.0) == pytest.approx(0.5, rel=0.02)
    assert 0.0 < drawn(-24.0) < 0.1
    assert drawn(6.0) == pytest.approx(2.0, rel=0.02), "a boost really draws larger"
    assert drawn(24.0) == pytest.approx(2.0, rel=0.02), "and stops at the lane bound"


def test_canvas_gain_follows_fades(engine):
    from meenifx.ui.timeline_widget import TimelineCanvas

    clip = audio_clip(engine, duration=60, path="tone.wav")
    canvas = TimelineCanvas(engine, None)
    canvas.timeline = engine.timeline
    canvas.ppf = 2.0            # 120 columns = the clip's 60 frames, at 2 px per frame
    CH.set_value(clip, CH.gain_channel(), 0.0)
    clip.fade_in = 30
    gains = canvas._wave_gain(clip, canvas.x_at(0), 120)
    assert gains[0] < 0.15 and gains[-1] > 0.9
    assert np.all(np.diff(gains) >= -1e-4), "fade in must rise monotonically"
    clip.fade_in = 0
    clip.fade_out = 30
    gains = canvas._wave_gain(clip, canvas.x_at(0), 120)
    assert gains[0] > 0.9 and gains[-1] < 0.15
    assert np.all(np.diff(gains) <= 1e-4), "fade out must fall monotonically"


def test_painted_waveform_stays_inside_its_lane(engine, app):
    """A boosted clip is bounded by the amplitude, never by freezing the gain at 1.0."""
    from meenifx.ui.timeline_widget import TimelineCanvas, shaped_envelope

    clip = audio_clip(engine, duration=60, path="tone.wav")
    canvas = TimelineCanvas(engine, None)
    canvas.timeline = engine.timeline
    canvas.resize(900, 300)
    CH.set_value(clip, CH.gain_channel(), 24.0)
    gains = canvas._wave_gain(clip, canvas.HEADER_W, 60)
    assert gains.max() > 1.0, "the boost has to survive to the envelope for this to mean anything"
    low, high = shaped_envelope(np.full(60, -0.9, np.float32), np.full(60, 0.9, np.float32), gains)
    assert np.all(np.abs(low) <= 1.0) and np.all(np.abs(high) <= 1.0)
    assert high.min() == pytest.approx(1.0), "a hot clip fills the lane and stops there"


def test_source_mode_is_opt_in(app):
    from meenifx.ui.timeline_widget import TimelineCanvas
    from conftest import build_engine

    engine = build_engine()[0]
    canvas = TimelineCanvas(engine, None)
    assert canvas.waveform_mode() == "Processed", "processing is the default"
    canvas.waveform_mode_getter = lambda: "Source"
    assert canvas.waveform_mode() == "Source"
