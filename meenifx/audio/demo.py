"""Generated demo audio so the starter project has a real, audible music bed.

The old demo placed three silent placeholder clips on the audio tracks (empty paths),
which is why nothing could be heard. These small WAV files are written once next to the
project (or into a temp folder when the project folder is read-only) and then imported
like any other media.

Levels are deliberately healthy (peaks around 0.5) so the clips are clearly audible and
their waveforms actually fill the audio lanes instead of looking like a flat line. The
version stamp makes an older, quieter set regenerate itself.
"""

import os
import tempfile
import wave

import numpy as np

RATE = 48000
VERSION = "3"
STAMP = ".demo-audio-version"
DEMO_FILES = {
    "bed": "music-bed.wav",
    "voice": "voice.wav",
    "hit": "sfx-hit.wav",
}


def _write(path, samples):
    data = np.clip(samples, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    with wave.open(path, "wb") as handle:
        handle.setnchannels(pcm.shape[1] if pcm.ndim > 1 else 1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())


def _stereo(signal, pan=0.0):
    left = signal * (1.0 - max(0.0, pan))
    right = signal * (1.0 + min(0.0, pan))
    return np.stack([left, right], axis=1)


def _bed(seconds=14.0):
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    chord = (0.22 * np.sin(2 * np.pi * 110.0 * t)
             + 0.15 * np.sin(2 * np.pi * 164.81 * t + 0.4)
             + 0.11 * np.sin(2 * np.pi * 220.0 * t + 0.9))
    swell = 0.55 + 0.45 * np.sin(2 * np.pi * 0.11 * t - np.pi / 2)
    pulse = (np.sin(2 * np.pi * 2.0 * t) > 0.85).astype(np.float32) * 0.08
    signal = chord * swell + pulse
    fade = min(1.0, seconds / 2.0)
    edge = np.ones_like(t)
    ramp = int(RATE * min(fade, 1.0))
    edge[:ramp] = np.linspace(0.0, 1.0, ramp, dtype=np.float32)
    edge[-ramp:] = np.linspace(1.0, 0.0, ramp, dtype=np.float32)
    return _stereo(signal * edge, pan=0.1)


def _voice(seconds=4.0):
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    vibrato = 1.0 + 0.02 * np.sin(2 * np.pi * 5.5 * t)
    signal = (0.30 * np.sin(2 * np.pi * 196.0 * t * vibrato)
              + 0.14 * np.sin(2 * np.pi * 392.0 * t * vibrato)
              + 0.07 * np.sin(2 * np.pi * 588.0 * t * vibrato))
    syllables = 0.55 + 0.45 * np.sin(2 * np.pi * 1.7 * t - np.pi / 2)
    return _stereo(signal * syllables, pan=-0.15)


def _hit(seconds=2.0):
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(t.size).astype(np.float32) * np.exp(-t * 9.0)
    thump = np.sin(2 * np.pi * (70.0 + 40.0 * np.exp(-t * 20.0)) * t) * np.exp(-t * 3.0)
    # tanh keeps the transient punchy without hard clipping the exported WAV.
    signal = np.tanh(noise * 0.30 + thump * 0.9) * 0.65
    return _stereo(signal, pan=0.0)


def _current(folder):
    try:
        with open(os.path.join(folder, STAMP), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _build(folder):
    makers = {"bed": _bed, "voice": _voice, "hit": _hit}
    paths = {}
    for key, name in DEMO_FILES.items():
        target = os.path.join(folder, name)
        if not os.path.exists(target) or os.path.getsize(target) < 1024:
            _write(target, makers[key]())
        paths[key] = target
    with open(os.path.join(folder, STAMP), "w", encoding="utf-8") as handle:
        handle.write(VERSION)
    return paths


def ensure_demo_audio(directory=""):
    """Create the demo WAVs if needed and return {key: path}. Never raises."""
    for folder in (directory, os.path.join(tempfile.gettempdir(), "meenifx-demo-audio")):
        if not folder:
            continue
        try:
            os.makedirs(folder, exist_ok=True)
            if _current(folder) != VERSION:
                # Regenerate the whole set so an older, quieter demo is replaced.
                for name in DEMO_FILES.values():
                    try:
                        os.remove(os.path.join(folder, name))
                    except OSError:
                        pass
            return _build(folder)
        except OSError:
            continue
    return {}
