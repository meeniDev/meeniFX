"""Waveforms for the sound a clip actually plays, not just the file on disk.

Two stages, because they cost very different amounts:

* **Gain and fades are per-sample multipliers**, so their effect on a peak envelope is
  exact: every bucket's (min, max) is simply multiplied by the column's gain factor. That
  is a few dozen microseconds and can run on every mouse-move of a gain slider.
* **Every other audio effect changes the signal's shape** - a compressor, a filter, a
  reverb - so the only honest answer is to run the real chain on the decoded samples and
  measure the result. That happens on the waveform worker, is cached per chain signature,
  and is never computed on the GUI thread.

`clip_signature()` is what makes the cache correct: it describes everything that can change
the drawn envelope (gain curve, fades, speed, the effect stack), so a stale drawing cannot
survive a change, and an unchanged clip never pays for work it already has.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

import numpy as np

from ..model import animation as ANIM
from ..model import channels as CH
from ..model.keyframes import AnimatedValue
from . import dsp
from .effects import AUDIO_EFFECTS, apply_chain, chain_signature, has_audio_effects
from .waveform import compute_peaks

# Only the blocks the timeline can actually draw are needed; a 30-minute file at 256
# samples per bucket is ~340k buckets per level, so four levels stay small.
_MAX_ENTRIES = 6
_BUILT = OrderedDict()
_LOCK = threading.Lock()


def _gain_at(clip, frame):
    """The clip's gain channel in dB at a clip-local frame (0.0 when unset)."""
    stored = CH.stored(clip, CH.gain_channel())
    if isinstance(stored, AnimatedValue):
        value = stored.value_at(frame)
    else:
        value = stored
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def db_to_linear(db):
    """Vectorised dB -> amplitude with the same silence floor the mixer uses."""
    values = np.asarray(db, np.float32)
    gain = np.power(10.0, values / 20.0).astype(np.float32)
    return np.where(values <= ANIM.GAIN_FLOOR_DB, 0.0, gain).astype(np.float32)


def gain_curve(clip, frames):
    """Per-column linear gain from the keyframed dB gain channel (fades are separate).

    `frames` are clip-local frame positions, one per drawn column.
    """
    frames = np.asarray(frames, np.float64).reshape(-1)
    if not frames.size:
        return np.ones(0, np.float32)
    stored = CH.stored(clip, CH.gain_channel())
    if not isinstance(stored, AnimatedValue):
        return np.full(frames.size, float(db_to_linear(_gain_at(clip, 0))), np.float32)
    values = np.array([_gain_at(clip, float(frame)) for frame in frames], np.float32)
    return db_to_linear(values)


def clip_signature(clip, resolver=None, fps=30.0):
    """Hashable description of everything that changes a clip's drawn envelope."""
    entry = []
    stored = CH.stored(clip, CH.gain_channel())
    if isinstance(stored, AnimatedValue):
        entry.append(("gain", tuple((key.time, _num(key.value), key.interpolation)
                                    for key in stored.keyframes)))
    else:
        entry.append(("gain", _num(stored)))
    entry.append(("fades", int(getattr(clip, "fade_in", 0)), int(getattr(clip, "fade_out", 0)),
                  round(float(getattr(clip, "fade_in_curve", 0.0) or 0.0), 4),
                  round(float(getattr(clip, "fade_out_curve", 0.0) or 0.0), 4)))
    entry.append(("speed", round(float(getattr(clip, "speed", 1.0) or 1.0), 6)))
    entry.append(("reverse", bool(getattr(clip, "reverse", False))))
    effects = getattr(clip, "effects", None) or ()
    entry.append(("chain", chain_signature(effects)))
    entry.append(("rate", round(float(fps), 4)))
    raw = repr(tuple(entry))
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=12).hexdigest()


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def has_shaping_effects(clip, resolver=None):
    """True when the clip's chain does more than gain/fades (so peaks must be computed)."""
    entries = [item for item in (getattr(clip, "effects", None) or ())
               if item.get("effect_id") not in ("audio_gain", "audio_fade")]
    return has_audio_effects(entries, resolver or resolver_for_peaks())


_RESOLVER = None


def resolver_for_peaks():
    """Effect instances used only to ask questions (media types), never to process."""
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = {cls.key: cls(None) for cls in AUDIO_EFFECTS}
    return _RESOLVER


def build_processed_levels(entries, samples, rate, fps=30.0):
    """Run the real chain over decoded samples and peak the result.

    Called on the waveform worker, so it builds its *own* effect instances instead of
    touching the ones the preview mixer is using: two threads must never share the state
    of an effect object.
    """
    if samples is None or not len(samples):
        return None
    resolver = {cls.key: cls(None) for cls in AUDIO_EFFECTS}
    processed = apply_chain(samples, rate, entries or (), resolver, fps=fps, start_frame=0)
    if processed is None or not len(processed):
        return None
    # The mixer soft-clips a hot chain before it reaches the encoder; the drawing has to
    # show the same ceiling or a boosted clip would look louder than it sounds.
    if processed.size and dsp.peak_db(processed) > 0.0:
        processed = dsp.soft_clip(processed)
    return compute_peaks(processed)


def cache_key(path, signature, rate):
    return (str(path), str(signature), int(rate))


def cached(path, signature, rate):
    """Peaks that were already computed for exactly this clip state."""
    key = cache_key(path, signature, rate)
    with _LOCK:
        entry = _BUILT.get(key)
        if entry is None:
            return None
        _BUILT.move_to_end(key)
        return entry


def store(path, signature, rate, levels):
    key = cache_key(path, signature, rate)
    with _LOCK:
        _BUILT[key] = levels
        while len(_BUILT) > _MAX_ENTRIES:
            _BUILT.popitem(last=False)


def clear():
    """Drop everything (project close, or a test that reuses a path)."""
    with _LOCK:
        _BUILT.clear()


def apply_gain(minimums, maximums, gain):
    """Scale an envelope by per-column linear gain, keeping it inside the unit range."""
    data_min = np.asarray(minimums, np.float32) * np.asarray(gain, np.float32)
    data_max = np.asarray(maximums, np.float32) * np.asarray(gain, np.float32)
    np.clip(data_min, -1.0, 1.0, out=data_min)
    np.clip(data_max, -1.0, 1.0, out=data_max)
    return data_min, data_max
