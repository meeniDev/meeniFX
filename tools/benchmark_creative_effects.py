"""Measured CPU benchmark for the Creative Tools Update heavy effects.

Run: python tools/benchmark_creative_effects.py
Writes artifacts/benchmarks/creative-part3.{json,md}. Timings are real measured wall
clock values; nothing here invents a result when an effect cannot run.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
import tracemalloc
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "artifacts" / "benchmarks"
FRAMES = 6
CASES = (
    ("multi_glow", {"intensity": 140.0, "radius": 42.0}),
    ("neon_glow", {"intensity": 180.0, "radius": 28.0}),
    ("soft_bloom", {"intensity": 130.0, "radius": 50.0}),
    ("light_wrap", {"intensity": 150.0, "radius": 28.0, "wrap": 24.0}),
    ("turbulent_displace", {"amount": 35.0, "complexity": 4.0}),
    ("displacement_map", {"amount": 40.0}),
    ("lens_distortion", {"k1": 0.22}),
    ("fractal_noise", {"complexity": 6.0}),
    ("matte_choker", {"choke": 6.0, "feather": 3.0}),
)


def make_layer(width, height):
    yy, xx = np.mgrid[0:height, 0:width]
    rgb = np.dstack([(30 + xx * 0.10), (50 + yy * 0.12), (80 + xx * 0.05)])
    layer = np.dstack([rgb, np.full((height, width), 255)]).clip(0, 255).astype(np.uint8)
    cv2.ellipse(layer, (width // 2, height // 2), (width // 4, height // 3), 0, 0, 360,
                (245, 240, 220, 255), -1)
    cv2.ellipse(layer, (width // 2, height // 2), (width // 5, height // 4), 0, 0, 360,
                (255, 255, 255, 255), -1)
    alpha = np.zeros((height, width), np.uint8)
    cv2.ellipse(alpha, (width // 2, height // 2), (width // 3, height // 2 - 8),
                0, 0, 360, 255, -1)
    layer[:, :, 3] = cv2.GaussianBlur(alpha, (0, 0), 2.5)
    return layer


def main():
    from meenifx.engine import EditorEngine
    OUT.mkdir(parents=True, exist_ok=True)
    engine = EditorEngine(30)
    rows = []
    for width, height in ((1280, 720), (1920, 1080)):
        layer = make_layer(width, height)
        for key, params in CASES:
            runtime = engine.runtime_effects[key]
            runtime.param_scale = 1.0
            runtime.load_params({**runtime.defaults, **params})
            times = []
            for frame in range(FRAMES):
                runtime.local_frame = frame
                tracemalloc.start()
                started = time.perf_counter()
                runtime.apply_layer(layer.copy())
                times.append((time.perf_counter() - started) * 1000.0)
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            rows.append({"effect": key, "width": width, "height": height,
                         "frames": FRAMES, "average_ms": round(float(np.mean(times)), 3),
                         "min_ms": round(float(np.min(times)), 3),
                         "max_ms": round(float(np.max(times)), 3),
                         "peak_python_mb": round(peak / 1e6, 2),
                         "params": params})
            print(f"{key:22s} {width}x{height} {np.mean(times):8.2f} ms")
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "python": platform.python_version(), "platform": platform.platform(),
              "processor": platform.processor(), "cpu_count": os.cpu_count(),
              "opencv": cv2.__version__, "rows": rows}
    (OUT / "creative-part3.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Creative Tools Part 3 benchmarks", "",
             f"- Generated: {report['generated_at']}",
             f"- CPU: {report['processor']} ({report['cpu_count']} logical cores)",
             f"- Python {report['python']}, OpenCV {report['opencv']}",
             f"- {FRAMES} frames per case; direct effect pass, CPU only.", "",
             "| Effect | Resolution | Average | Min | Max | Python peak |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['effect']} | {row['width']}x{row['height']} | "
                     f"{row['average_ms']:.2f} ms | {row['min_ms']:.2f} ms | "
                     f"{row['max_ms']:.2f} ms | {row['peak_python_mb']:.1f} MB |")
    (OUT / "creative-part3.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT / 'creative-part3.json'}")


if __name__ == "__main__":
    main()
