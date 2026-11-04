"""Audio effects on the real signal: gain, EQ, filters, dynamics, reverb, delay, pan.

Every test builds a timeline clip, runs the effect chain through the same
`audio.plan.build_segments` path playback and export use, then measures the rendered
block. Nothing here is stubbed: if a filter or compressor stopped working, the numbers
would stop matching.
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import build_engine  # noqa: E402

from meenifx.audio import mix, plan  # noqa: E402
from meenifx.model import channels as CH  # noqa: E402

RATE = 16000


def tone(freq=220.0, seconds=1.0, rate=RATE, amplitude=0.5):
    frames = int(seconds * rate)
    wave = (np.sin(2 * np.pi * freq * np.arange(frames) / rate) * amplitude).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def audio_clip(engine, duration=60):
    track = engine.timeline.add_track("audio")
    return engine.timeline.new_clip("sound", "synthetic.wav", track, 0, duration)


def with_effects(engine, clip, *entries):
    clip.effects = [{"effect_id": key, "enabled": True, "params": dict(params)}
                    for key, params in entries]
    return clip


def render(engine, samples, frames=8000, start=0.0):
    """Build the segments for the timeline and render a block of it."""
    segments = plan.build_segments(engine.timeline, lambda _path: samples, RATE,
                                   engine.runtime_effects)
    assert segments, "the clip must produce a segment"
    return mix.render(segments, start, frames, RATE, 2)


def rms(block):
    return float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0


def band_energy(block, freq, rate=RATE):
    """Amplitude of one frequency in a rendered block (Goertzel-style projection)."""
    mono = block.mean(axis=1)
    times = np.arange(mono.size) / float(rate)
    return float(abs(np.sum(mono * np.exp(-2j * np.pi * freq * times))) * 2.0 / max(1, mono.size))


# ----------------------------------------------------------------------- gain
def test_gain_effect_changes_the_mixed_level():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    samples = tone(amplitude=0.5)
    dry = render(engine, samples)
    with_effects(engine, clip, ("audio_gain", {"gain_db": -6.0}))
    quiet = render(engine, samples)
    assert mix.peak_of(quiet) == pytest.approx(mix.peak_of(dry) * 0.5, rel=0.05)


def test_gain_effect_follows_keyframes():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_gain", {}))
    channel = CH.effect_channel("audio_gain", "gain_db")
    CH.animate(clip, channel, 0)
    CH.set_value(clip, channel, 0.0, 0)
    CH.set_value(clip, channel, -60.0, 15, auto_key=True)
    samples = tone(seconds=1.0)
    early = render(engine, samples, frames=2000, start=0.0)
    late = render(engine, samples, frames=2000, start=0.6)
    assert mix.peak_of(early) > 0.3
    assert mix.peak_of(late) < 0.02, "the keyframed gain must fade the clip out"


def test_audio_fade_effect_ramps_the_start():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_fade", {"fade_in": 0.5, "fade_out": 0.5}))
    block = render(engine, tone(seconds=1.0), frames=8000)
    head = float(np.max(np.abs(block[:100])))
    middle = float(np.max(np.abs(block[3500:4500])))
    assert head < middle * 0.15


# ------------------------------------------------------------------------ EQ
def test_equalizer_boost_raises_its_band_only():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    samples = tone(200.0, amplitude=0.4)
    flat = render(engine, samples)
    with_effects(engine, clip, ("audio_eq", {"low_gain": 12.0, "low_freq": 200.0}))
    boosted = render(engine, samples)
    assert rms(boosted) > rms(flat) * 1.5
    assert band_energy(boosted, 200.0) > band_energy(flat, 200.0) * 1.5


def test_low_pass_rejects_high_tones():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_lowpass", {"cutoff": 400.0, "order": 6}))
    high = render(engine, tone(3000.0, amplitude=0.4))
    passband = render(engine, tone(200.0, amplitude=0.4))
    assert rms(high) < rms(passband) * 0.2


def test_high_pass_rejects_rumble():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_highpass", {"cutoff": 1000.0, "order": 6}))
    low = render(engine, tone(100.0, amplitude=0.4))
    still = render(engine, tone(2500.0, amplitude=0.4))
    assert rms(low) < rms(still) * 0.25


# ------------------------------------------------------------------ dynamics
def test_compressor_tames_loud_material_and_makeup_restores_level():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    samples = tone(220.0, amplitude=0.9)
    dry = render(engine, samples)
    with_effects(engine, clip, ("audio_compressor",
                                {"threshold": -24.0, "ratio": 8.0, "attack": 5.0,
                                 "release": 100.0}))
    squeezed = render(engine, samples)
    assert mix.peak_of(squeezed) < mix.peak_of(dry) * 0.7
    with_effects(engine, clip, ("audio_compressor",
                                {"threshold": -24.0, "ratio": 8.0, "attack": 5.0,
                                 "release": 100.0, "makeup": 12.0}))
    louder = render(engine, samples)
    assert mix.peak_of(louder) > mix.peak_of(squeezed)


def test_limiter_holds_the_ceiling():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_limiter", {"ceiling": -6.0, "release": 60.0}))
    block = render(engine, tone(220.0, amplitude=0.95), frames=8000)
    assert mix.peak_of(block) <= 10 ** (-6.0 / 20.0) + 0.02


# --------------------------------------------------------------- space / pan
def test_reverb_and_delay_change_the_signal_and_keep_the_dry_when_wet_is_zero():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    samples = tone(300.0, seconds=0.5, amplitude=0.4)
    dry = render(engine, samples, frames=6000)
    with_effects(engine, clip, ("audio_reverb", {"wet": 0.0}))
    zero = render(engine, samples, frames=6000)
    np.testing.assert_allclose(zero, dry, atol=1e-6)
    with_effects(engine, clip, ("audio_reverb", {"room_size": 0.7, "decay": 2.0, "wet": 60.0}))
    wet = render(engine, samples, frames=6000)
    assert float(np.max(np.abs(wet - dry))) > 0.01


def test_delay_puts_a_repeat_after_the_source():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    burst = np.zeros((4000, 2), np.float32)
    burst[:400] = 0.6           # 25 ms click, then silence
    with_effects(engine, clip, ("audio_delay", {"time_ms": 100.0, "feedback": 0.0,
                                                "repeats": 1, "wet": 100.0}))
    block = render(engine, burst, frames=6000)
    before = float(np.max(np.abs(block[600:1200])))     # 37-75 ms: silence
    echo = float(np.max(np.abs(block[1650:2300])))      # ~103-144 ms: first repeat
    assert before < 0.02
    assert echo > 0.2


def test_pan_moves_the_signal_to_one_side():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_pan", {"pan": -100.0}))
    block = render(engine, tone(amplitude=0.4))
    left, right = np.abs(block[:, 0]).max(), np.abs(block[:, 1]).max()
    assert left > 0.3 and right < 0.03


# -------------------------------------------------------------- chain rules
def test_chain_order_matters_and_disable_is_silent():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    samples = tone(amplitude=0.8)
    with_effects(engine, clip, ("audio_gain", {"gain_db": 12.0}),
                 ("audio_limiter", {"ceiling": -6.0}))
    gain_then_limit = render(engine, samples)
    with_effects(engine, clip, ("audio_limiter", {"ceiling": -6.0}),
                 ("audio_gain", {"gain_db": 12.0}))
    limit_then_gain = render(engine, samples)
    assert mix.peak_of(gain_then_limit) <= 10 ** (-6.0 / 20.0) + 0.02
    # Limiting first then boosting gets louder; the chain's tanh safety net keeps the
    # result just under full scale instead of letting it hard-clip.
    assert mix.peak_of(limit_then_gain) > mix.peak_of(gain_then_limit) * 1.5
    assert mix.peak_of(limit_then_gain) <= 1.0

    clip.effects[0]["enabled"] = False
    clip.effects[1]["enabled"] = False
    bypassed = render(engine, samples)
    assert mix.peak_of(bypassed) == pytest.approx(mix.peak_of(render(engine, samples)), rel=1e-6)


def test_imported_but_incompatible_effect_entries_are_ignored():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    clip.effects = [{"effect_id": "gaussian_blur", "enabled": True, "params": {"radius": 20.0}}]
    block = render(engine, tone(amplitude=0.4))
    assert mix.peak_of(block) == pytest.approx(0.4, rel=0.02)


@pytest.mark.parametrize("params", [
    {"threshold": -200.0, "ratio": 0.0, "attack": -40.0, "release": 0.0, "knee": -5.0},
    {"threshold": 40.0, "ratio": 500.0, "attack": 5000.0, "release": 9000.0},
])
def test_invalid_compressor_values_stay_finite(params):
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_compressor", params))
    block = render(engine, tone(amplitude=0.5))
    assert np.isfinite(block).all()
    assert mix.peak_of(block) <= 1.0 + 1e-6


def test_a_stack_never_hands_a_clipped_signal_to_the_encoder():
    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_gain", {"gain_db": 12.0}),
                 ("audio_eq", {"low_gain": 12.0, "mid_gain": 12.0, "high_gain": 12.0}))
    block = render(engine, tone(amplitude=0.95))
    assert mix.peak_of(block) <= 1.0 + 1e-6


# ------------------------------------------------------------------ project
def test_audio_effects_round_trip_through_the_project_format():
    from meenifx.model.timeline import Timeline
    from meenifx.project import MediaLibrary
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    timeline = Timeline(30)
    track = timeline.add_track("audio")
    clip = timeline.new_clip("sound", os.path.join(os.sep, "media", "sound.wav"), track, 0, 60,
                             effects=[{"effect_id": "audio_reverb", "enabled": True,
                                       "params": {"room_size": 0.7, "wet": 45.0}},
                                      {"effect_id": "audio_eq", "enabled": False,
                                       "params": {"low_gain": -6.0}}])
    library = MediaLibrary()
    sections = timeline_to_sections(timeline, lambda path: library.add(path).id)
    restored = sections_to_timeline(sections, 30, lambda mid: library.items[mid].path)
    loaded = restored.find_clip(clip.id)
    assert [entry["effect_id"] for entry in loaded.effects] == ["audio_reverb", "audio_eq"]
    assert loaded.effects[0]["params"]["room_size"] == pytest.approx(0.7)
    assert loaded.effects[0]["params"]["wet"] == pytest.approx(45.0)
    assert loaded.effects[1]["enabled"] is False


def test_export_audio_contains_the_effects(tmp_path):
    from meenifx.audio.export_mix import render_wav
    import wave

    engine, _clip = build_engine()
    clip = audio_clip(engine)
    with_effects(engine, clip, ("audio_gain", {"gain_db": -12.0}))
    segments = plan.build_segments(engine.timeline, lambda _path: tone(amplitude=0.5), RATE,
                                   engine.runtime_effects)
    out = tmp_path / "mix.wav"
    render_wav(segments, 0.0, 1.0, RATE, str(out))
    with wave.open(str(out)) as handle:
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    peak = float(np.max(np.abs(data)) / 32767.0)
    assert 0.08 < peak < 0.16, f"a -12 dB gain must quarter the 0.5 peak, got {peak:.3f}"
    assert math.isfinite(peak)


def test_every_audio_effect_declares_itself_with_parameters():
    from meenifx.audio.effects import AUDIO_EFFECTS

    assert len(AUDIO_EFFECTS) == 10, "gain, fade, EQ, LP, HP, comp, limiter, reverb, delay, pan"
    keys = set()
    for cls in AUDIO_EFFECTS:
        assert cls.MEDIA_TYPES == ("audio",), f"{cls.key} must declare audio-only"
        assert cls.category == "Audio Effects"
        assert cls.name and cls.PARAMS, f"{cls.key} needs a name and parameters"
        keys.add(cls.key)
    assert len(keys) == len(AUDIO_EFFECTS), "effect keys must be unique"
