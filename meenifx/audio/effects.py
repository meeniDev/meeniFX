"""Audio effects: gain, fades, EQ, filters, dynamics, reverb, delay and pan.

These are ordinary entries in the clip's effect list - the same structure the video
effects use, with ``MEDIA_TYPES = ("audio",)``. That means the Effects panel, the
Inspector, keyframes, ordering, enable/disable and the project file all work on them
without a second mechanism.

They are applied to the decoded samples in :mod:`meenifx.audio.plan`, so playback, the
waveform, the mixdown and the exported audio all contain the effect. Gain, pan and the
compressor follow keyframes sample-by-sample (gain/pan) or in ten chunks a second
(dynamics); the filter, reverb and delay settings are rebuilt from the value on the
clip's first frame, because an impulse response cannot be morphed per sample.

Levels are protected: filters are normalised so they never change the level on their
own, the limiter has a hard ceiling with a lookahead, and the chain ends in a tanh
safety net so a stack of effects can never hand a clipped signal to the encoder.
"""

from __future__ import annotations

import numpy as np

from ..effects.base import EffectBase, ParamSpec
from ..model.keyframes import AnimatedValue, parameter_value_at
from ..model.values import Value
from . import dsp


def _num(params, name, default=0.0):
    try:
        return float(params.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _automation(ctx, name, frames, rate):
    """Per-sample values of an animated parameter, or None when it is static."""
    animated = (ctx.get("automation") or {}).get(name)
    if not isinstance(animated, AnimatedValue) or frames <= 0:
        return None
    fps = max(1.0, float(ctx.get("fps") or 30.0))
    start = float(ctx.get("start_frame") or 0.0)
    seconds = frames / float(rate)
    duration_frames = max(1.0, seconds * fps)
    # One automation sample every 1/10 s is far finer than a keyframe needs and keeps
    # the interpolation linear between values a user can actually see on the curve.
    step = max(1, int(round(fps / 10.0)))
    grid = np.arange(0.0, duration_frames + step, step) + start
    values = np.array([float(parameter_value_at(animated, frame)) for frame in grid],
                      dtype=np.float64)
    sample_frames = start + np.arange(frames, dtype=np.float64) * fps / float(rate)
    return np.interp(sample_frames, grid, values)


def _params_at(params, ctx, frame):
    """Parameter values at one clip-local frame (animated values included)."""
    resolved = {}
    for name, value in params.items():
        resolved[name] = (float(parameter_value_at(value, frame))
                          if isinstance(value, AnimatedValue) else value)
    return resolved


class AudioEffectBase(EffectBase):
    """Base for effects that process decoded audio samples."""

    MEDIA_TYPES = ("audio",)
    category = "Audio Effects"

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_audio(self, signal, rate, params, ctx):
        return signal


_KERNEL_CACHE = {}


def _cached_kernel(key, builder):
    kernel = _KERNEL_CACHE.get(key)
    if kernel is None:
        kernel = builder()
        if len(_KERNEL_CACHE) > 48:
            _KERNEL_CACHE.clear()
        _KERNEL_CACHE[key] = kernel
    return kernel


def _wet_dry(dry, wet, mix):
    mix = float(np.clip(mix, 0.0, 1.0))
    if mix <= 0.0:
        return dry
    if wet.shape[0] < dry.shape[0]:
        wet = np.vstack([wet, np.zeros((dry.shape[0] - wet.shape[0], wet.shape[1]),
                                       dtype=wet.dtype)])
    return (dry * (1.0 - mix) + wet * mix).astype(np.float32)


class AudioGainEffect(AudioEffectBase):
    key = "audio_gain"
    name = "Gain"

    PARAMS = (
        ParamSpec("gain_db", "Gain", "float", -60.0, 24.0, 0.0, step=0.5, unit=" dB",
                  help="Level of the clip, before the rest of the chain"),
    )

    def apply_audio(self, signal, rate, params, ctx):
        automated = _automation(ctx, "gain_db", signal.shape[0], rate)
        if automated is None:
            return dsp.apply_gain(signal, _num(params, "gain_db"))
        return (signal * (10.0 ** (automated / 20.0))[:, None]).astype(np.float32)


class AudioFadeEffect(AudioEffectBase):
    key = "audio_fade"
    name = "Fade In / Out"

    PARAMS = (
        ParamSpec("fade_in", "Fade in", "float", 0.0, 10.0, 0.5, step=0.01, unit=" s",
                  help="Seconds of fade up from silence"),
        ParamSpec("fade_out", "Fade out", "float", 0.0, 10.0, 0.5, step=0.01, unit=" s",
                  help="Seconds of fade down to silence"),
    )

    def apply_audio(self, signal, rate, params, ctx):
        return dsp.apply_fades(signal, rate, _num(params, "fade_in"), _num(params, "fade_out"))


class AudioEqualizerEffect(AudioEffectBase):
    key = "audio_eq"
    name = "Equalizer"

    PARAMS = (
        ParamSpec("low_freq", "Low corner", "float", 40.0, 500.0, 120.0, step=1.0, unit=" Hz",
                  help="Corner of the low shelf"),
        ParamSpec("low_gain", "Low gain", "float", -18.0, 18.0, 0.0, step=0.5, unit=" dB"),
        ParamSpec("mid_freq", "Mid centre", "float", 200.0, 8000.0, 1000.0, step=10.0, unit=" Hz",
                  help="Centre of the mid bell"),
        ParamSpec("mid_gain", "Mid gain", "float", -18.0, 18.0, 0.0, step=0.5, unit=" dB"),
        ParamSpec("mid_q", "Mid width", "float", 0.2, 8.0, 1.0, step=0.05,
                  help="Narrow (high) or broad (low) mid bell"),
        ParamSpec("high_freq", "High corner", "float", 2000.0, 16000.0, 6000.0, step=10.0, unit=" Hz",
                  help="Corner of the high shelf"),
        ParamSpec("high_gain", "High gain", "float", -18.0, 18.0, 0.0, step=0.5, unit=" dB"),
    )

    def apply_audio(self, signal, rate, params, ctx):
        gains = [_num(params, name) for name in ("low_gain", "mid_gain", "high_gain")]
        if all(abs(gain) < 1e-3 for gain in gains):
            return signal
        key = ("eq", int(rate), round(_num(params, "low_freq"), 1), round(gains[0], 2),
               round(_num(params, "mid_freq"), 1), round(gains[1], 2),
               round(_num(params, "mid_q"), 2), round(_num(params, "high_freq"), 1),
               round(gains[2], 2))
        kernel = _cached_kernel(key, lambda: dsp.eq_kernel(
            rate, _num(params, "low_freq"), gains[0], _num(params, "mid_freq"), gains[1],
            _num(params, "mid_q"), _num(params, "high_freq"), gains[2]))
        return dsp.fft_convolve(signal, kernel)


class _FilterEffect(AudioEffectBase):
    high = False

    def apply_audio(self, signal, rate, params, ctx):
        cutoff = _num(params, "cutoff", 1000.0)
        order = max(1, int(round(_num(params, "order", 4))))
        key = (self.key, int(rate), round(cutoff, 1), order)
        builder = dsp.highpass_kernel if self.high else dsp.lowpass_kernel
        kernel = _cached_kernel(key, lambda: builder(rate, cutoff, order))
        return dsp.fft_convolve(signal, kernel)


class AudioLowPassEffect(_FilterEffect):
    key = "audio_lowpass"
    name = "Low-Pass Filter"

    PARAMS = (
        ParamSpec("cutoff", "Cutoff", "float", 60.0, 20000.0, 8000.0, step=10.0, unit=" Hz",
                  help="Everything above this is rolled off"),
        ParamSpec("order", "Slope", "int", 1, 8, 4,
                  help="Filter order: higher is a steeper, more surgical slope"),
    )


class AudioHighPassEffect(_FilterEffect):
    key = "audio_highpass"
    name = "High-Pass Filter"
    high = True

    PARAMS = (
        ParamSpec("cutoff", "Cutoff", "float", 20.0, 8000.0, 120.0, step=10.0, unit=" Hz",
                  help="Everything below this is rolled off (rumble, wind, hum)"),
        ParamSpec("order", "Slope", "int", 1, 8, 4,
                  help="Filter order: higher is a steeper, more surgical slope"),
    )


class AudioCompressorEffect(AudioEffectBase):
    key = "audio_compressor"
    name = "Compressor"

    PARAMS = (
        ParamSpec("threshold", "Threshold", "float", -60.0, 0.0, -18.0, step=0.5, unit=" dB",
                  help="Level where compression starts"),
        ParamSpec("ratio", "Ratio", "float", 1.0, 20.0, 3.0, step=0.1, unit=":1",
                  help="How hard the signal above the threshold is squashed"),
        ParamSpec("attack", "Attack", "float", 0.1, 200.0, 12.0, step=0.1, unit=" ms",
                  help="How quickly the gain reduction reacts"),
        ParamSpec("release", "Release", "float", 5.0, 1000.0, 150.0, step=1.0, unit=" ms",
                  help="How quickly the gain comes back"),
        ParamSpec("knee", "Knee", "float", 0.0, 24.0, 6.0, step=0.5, unit=" dB",
                  help="Soft transition around the threshold"),
        ParamSpec("makeup", "Makeup gain", "float", -12.0, 24.0, 0.0, step=0.5, unit=" dB",
                  help="Level added back after compression"),
    )

    CHUNK_SECONDS = 0.1

    def apply_audio(self, signal, rate, params, ctx):
        frames = signal.shape[0]
        chunk = max(16, int(rate * self.CHUNK_SECONDS))
        out = np.empty_like(signal)
        state = {}
        base = _params_at(params, ctx, ctx.get("start_frame", 0))
        if not (ctx.get("automation") or {}):
            return dsp.compress(signal, rate, _num(base, "threshold", -18.0),
                                _num(base, "ratio", 3.0), _num(base, "attack", 12.0),
                                _num(base, "release", 150.0), _num(base, "makeup"),
                                _num(base, "knee", 6.0))
        fps = max(1.0, float(ctx.get("fps") or 30.0))
        start = float(ctx.get("start_frame") or 0.0)
        for begin in range(0, frames, chunk):
            end = min(frames, begin + chunk)
            frame = start + (begin / float(rate)) * fps
            values = _params_at(params, ctx, frame)
            out[begin:end] = dsp.compress(
                signal[begin:end], rate, _num(values, "threshold", -18.0),
                _num(values, "ratio", 3.0), _num(values, "attack", 12.0),
                _num(values, "release", 150.0), _num(values, "makeup"),
                _num(values, "knee", 6.0), state=state)
        return out


class AudioLimiterEffect(AudioEffectBase):
    key = "audio_limiter"
    name = "Limiter"

    PARAMS = (
        ParamSpec("ceiling", "Ceiling", "float", -24.0, 0.0, -1.0, step=0.5, unit=" dB",
                  help="Nothing leaves the effect above this level"),
        ParamSpec("release", "Release", "float", 5.0, 500.0, 80.0, step=1.0, unit=" ms"),
        ParamSpec("lookahead", "Lookahead", "float", 0.0, 20.0, 3.0, step=0.5, unit=" ms",
                  help="Delay the detection so the first transient is caught too"),
    )

    def apply_audio(self, signal, rate, params, ctx):
        return dsp.limit(signal, rate, _num(params, "ceiling", -1.0),
                         _num(params, "release", 80.0), _num(params, "lookahead", 3.0))


class AudioReverbEffect(AudioEffectBase):
    key = "audio_reverb"
    name = "Reverb"

    PARAMS = (
        ParamSpec("room_size", "Room size", "float", 0.0, 1.0, 0.5, step=0.01,
                  help="Length of the space"),
        ParamSpec("decay", "Decay", "float", 0.1, 5.0, 1.6, step=0.05, unit=" s",
                  help="How long the tail rings"),
        ParamSpec("damping", "Damping", "float", 0.0, 0.95, 0.4, step=0.01,
                  help="How quickly the tail loses its brightness"),
        ParamSpec("wet", "Wet / dry", "float", 0.0, 100.0, 30.0, step=1.0, unit="%",
                  help="Amount of reverb in the mix"),
    )

    def apply_audio(self, signal, rate, params, ctx):
        wet = _num(params, "wet", 30.0) / 100.0
        if wet <= 0.0:
            return signal
        key = ("reverb", int(rate), round(_num(params, "room_size"), 2),
               round(_num(params, "decay"), 2), round(_num(params, "damping"), 2))
        kernel = _cached_kernel(key, lambda: dsp.reverb_kernel(
            rate, _num(params, "room_size"), _num(params, "decay", 1.6),
            _num(params, "damping", 0.4)))
        return _wet_dry(signal, dsp.fft_convolve(signal, kernel), wet)


class AudioDelayEffect(AudioEffectBase):
    key = "audio_delay"
    name = "Delay / Echo"

    PARAMS = (
        ParamSpec("time_ms", "Delay time", "float", 10.0, 2000.0, 320.0, step=5.0, unit=" ms",
                  help="Gap between echoes"),
        ParamSpec("feedback", "Feedback", "float", 0.0, 95.0, 35.0, step=1.0, unit="%",
                  help="How much of each echo feeds the next one"),
        ParamSpec("repeats", "Repeats", "int", 1, 16, 6, help="Echoes before it dies out"),
        ParamSpec("wet", "Wet / dry", "float", 0.0, 100.0, 30.0, step=1.0, unit="%"),
    )

    def apply_audio(self, signal, rate, params, ctx):
        wet = _num(params, "wet", 30.0) / 100.0
        if wet <= 0.0:
            return signal
        key = ("delay", int(rate), round(_num(params, "time_ms"), 1),
               round(_num(params, "feedback"), 1), int(_num(params, "repeats", 6)))
        kernel = _cached_kernel(key, lambda: dsp.delay_kernel(
            rate, _num(params, "time_ms"), _num(params, "feedback") / 100.0,
            _num(params, "repeats", 6)))
        return _wet_dry(signal, dsp.fft_convolve(signal, kernel), wet)


class AudioPanEffect(AudioEffectBase):
    key = "audio_pan"
    name = "Stereo Pan"

    PARAMS = (
        ParamSpec("pan", "Pan", "float", -100.0, 100.0, 0.0, step=1.0, unit="%",
                  help="-100 is hard left, +100 hard right"),
        ParamSpec("width", "Stereo width", "float", 0.0, 200.0, 100.0, step=1.0, unit="%",
                  help="0 collapses to mono, 200 exaggerates the sides"),
    )

    def apply_audio(self, signal, rate, params, ctx):
        automated = _automation(ctx, "pan", signal.shape[0], rate)
        width = _num(params, "width", 100.0) / 100.0
        if automated is None:
            return dsp.apply_pan(signal, _num(params, "pan") / 100.0, width)
        # Per-sample constant-power pan: no zipper noise on a moving pan.
        angles = (np.clip(automated, -100.0, 100.0) / 100.0 + 1.0) * np.pi / 4.0
        left, right = np.cos(angles), np.sin(angles)
        if signal.shape[1] == 1:
            mono = signal[:, 0]
            return np.stack([mono * left, mono * right], axis=1).astype(np.float32)
        mid = (signal[:, 0] + signal[:, 1]) * 0.5
        side = (signal[:, 0] - signal[:, 1]) * 0.5 * width
        out = np.empty_like(signal)
        out[:, 0] = (mid + side) * left
        out[:, 1] = (mid - side) * right
        return out.astype(np.float32)


AUDIO_EFFECTS = (AudioGainEffect, AudioFadeEffect, AudioEqualizerEffect, AudioLowPassEffect,
                 AudioHighPassEffect, AudioCompressorEffect, AudioLimiterEffect,
                 AudioReverbEffect, AudioDelayEffect, AudioPanEffect)

AUDIO_EFFECT_KEYS = tuple(cls.key for cls in AUDIO_EFFECTS)


def has_audio_effects(entries, resolver):
    """True when a clip carries at least one enabled audio effect."""
    for entry in entries or ():
        if not entry.get("enabled", True):
            continue
        effect = resolver.get(entry.get("effect_id"))
        if effect is not None and "audio" in effect.media_types():
            return True
    return False


def chain_signature(entries):
    """A hashable description of the audio effect chain, for the sample cache."""
    rows = []
    for entry in entries or ():
        if not entry.get("enabled", True):
            continue
        params = []
        for name, value in sorted((entry.get("params") or {}).items()):
            if isinstance(value, AnimatedValue):
                params.append((name, "kf", tuple((key.time, key.value, key.interpolation)
                                                 for key in value.keyframes)))
            else:
                params.append((name, value))
        rows.append((entry.get("effect_id"), tuple(params)))
    return tuple(rows)


def apply_chain(signal, rate, entries, resolver, fps=30.0, start_frame=0):
    """Run the clip's audio effects in order over decoded samples."""
    data = dsp.as_stereo(signal)
    for entry in entries or ():
        if not entry.get("enabled", True):
            continue
        effect = resolver.get(entry.get("effect_id"))
        if effect is None or "audio" not in effect.media_types():
            continue
        params = dict(getattr(effect, "defaults", {}) or {})
        params.update(entry.get("params") or {})
        ctx = {"fps": float(fps), "start_frame": float(start_frame), "rate": int(rate),
               "automation": {name: value for name, value in (entry.get("params") or {}).items()
                              if isinstance(value, AnimatedValue)}}
        data = effect.apply_audio(data, rate, params, ctx)
    return data
