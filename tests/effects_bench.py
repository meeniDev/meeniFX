"""Measure 1080p frame times for stacks of the heavier effects.

Run it directly (it is not part of the test suite)::

    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python tests/effects_bench.py

It renders a 1920x1080 frame with each stack several times and reports the median, so
the numbers are the ones the preview actually pays - the same ``render_frame`` call the
playback timer uses. Preview scales mirror the quality slider (100%, 75% and 50%).
"""

import os
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import build_engine, synthetic_frame  # noqa: E402

STACKS = {
    "VHS/CRT + Film Grain + Bloom streaks": [
        {"effect_id": "vhs_crt", "enabled": True,
         "params": {"scanlines": 45, "noise": 25, "bleed": 12, "curvature": 30,
                    "vignette": 30, "wobble": 10}},
        {"effect_id": "film_grain", "enabled": True, "params": {"amount": 35}},
        {"effect_id": "bloom_streaks", "enabled": True,
         "params": {"threshold": 60, "intensity": 80, "length": 200}},
    ],
    "Glitch + RGB split + Light leak": [
        {"effect_id": "glitch", "enabled": True,
         "params": {"slices": 60, "block": 40, "color": 50}},
        {"effect_id": "chromatic_aberration", "enabled": True,
         "params": {"amount": 8, "falloff": 60}},
        {"effect_id": "light_leak", "enabled": True, "params": {"amount": 40}},
    ],
    "Halftone + Edge outline + Glow": [
        {"effect_id": "halftone", "enabled": True, "params": {"dot": 9}},
        {"effect_id": "edge_outline", "enabled": True, "params": {"strength": 60}},
        {"effect_id": "glow", "enabled": True, "params": {"intensity": 120, "radius": 40}},
    ],
    "LUT + Duotone + Vignette": [
        {"effect_id": "lut", "enabled": True, "params": {"intensity": 100}},
        {"effect_id": "duotone", "enabled": True, "params": {"intensity": 60}},
        {"effect_id": "vignette", "enabled": True, "params": {"amount": 40}},
    ],
}

SCALES = (1.0, 0.75, 0.5)
REPEATS = 5


def measure(engine, clip, effects, scale):
    clip.effects = effects
    engine.render_frame(0, scale)                       # warm caches, shaders, LUT tables
    timings = []
    for index in range(REPEATS):
        start = time.perf_counter()
        engine.render_frame(index, scale)
        timings.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(timings)


def main():
    engine, clip = build_engine(frame=synthetic_frame(1920, 1080), duration=30)
    baseline = {scale: measure(engine, clip, [], scale) for scale in SCALES}
    print(f"{'stack':42s} " + " ".join(f"{int(scale * 100):>7d}%" for scale in SCALES))
    print("-" * 72)
    print(f"{'(composite only)':42s} " + " ".join(f"{baseline[scale]:8.1f}" for scale in SCALES))
    for name, effects in STACKS.items():
        row = [measure(engine, clip, effects, scale) for scale in SCALES]
        print(f"{name:42s} " + " ".join(f"{value:8.1f}" for value in row))
    print("\nmilliseconds per frame (median of %d renders, 1920x1080)" % REPEATS)


if __name__ == "__main__":
    main()
