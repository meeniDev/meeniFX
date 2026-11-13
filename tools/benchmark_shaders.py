"""Measure actual CPU/GPU processing, compilation and readback costs."""
import json
import sys
import time
import tracemalloc
from pathlib import Path

import cv2
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meenifx.effects.shader_effects import LensWarpEffect
from meenifx.effects.page_curl import PageCurlEffect
from meenifx.processing import shaders as S
from meenifx.effects.shader_expansion import SHADER_EXPANSION_EFFECTS
from meenifx.effects.glitch import KaleidoscopeEffect


def source(w, h):
    y, x = np.indices((h, w))
    grid = ((x // 40 + y // 40) % 2 * 100 + 60).astype(np.uint8)
    image = np.dstack((grid, np.roll(grid, 20, axis=1), 255-grid, np.full_like(grid, 255)))
    cv2.putText(image, "meeniFX", (w//8, h//2), cv2.FONT_HERSHEY_SIMPLEX,
                w/500, (255, 255, 255, 255), max(1, w//240), cv2.LINE_AA)
    return image


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/shader-phase1")
    out.mkdir(parents=True, exist_ok=True)
    runner = S.ShaderRunner()
    rows = []
    try:
        for w, h in ((1280, 720), (1920, 1080)):
            image = source(w, h)
            for cls in ((*SHADER_EXPANSION_EFFECTS, KaleidoscopeEffect) if "--phase2" in sys.argv else (LensWarpEffect, PageCurlEffect)):
                effect = cls(None)
                effect.local_frame = 47
                u, extra = effect.uniforms(effect.values(), image)
                start = time.perf_counter()
                gpu = runner.render(effect.key, image, u, extra=extra)
                cold = (time.perf_counter() - start) * 1000
                times = []
                for _ in range(3):
                    start = time.perf_counter()
                    gpu = runner.render(effect.key, image, u, extra=extra)
                    times.append((time.perf_counter() - start) * 1000)
                transfer = dict(runner.timings)
                effect.backend.set("cpu")
                start = time.perf_counter()
                cpu = effect.apply_layer(image)
                elapsed = (time.perf_counter() - start) * 1000
                tracemalloc.start()
                effect.apply_layer(image)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                row = dict(effect=effect.key, resolution=[w, h], gpu_cold_ms=cold,
                           gpu_warm_mean_ms=float(np.mean(times)), gpu_warm_samples_ms=times, cpu_ms=elapsed,
                           cpu_peak_python_bytes=peak, gpu_details=transfer,
                           gpu_cpu_mae=float(np.abs(cpu.astype(float)-gpu.astype(float)).mean()))
                rows.append(row)
                cv2.imwrite(str(out / f"{effect.key}-{w}.png"), gpu)
                print(json.dumps(row), flush=True)
        (out / "measurements.json").write_text(json.dumps(
            {"gpu": runner.info, "measurements": rows}, indent=2), encoding="utf-8")
    finally:
        runner.close()


if __name__ == "__main__":
    main()
