"""Measure the real tracking paths on this machine.

Run with the application environment and the offscreen Qt platform:

    QT_QPA_PLATFORM=offscreen PYTHONPATH=".;tests" python tools/benchmark_tracking.py

The script never invents timings: every number comes from the installed MediaPipe
model, OpenCV face/blob detection, or the real overlay/region code.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import build_engine  # noqa: E402
from meenifx.effects.tracking_viz import Detection, draw_tracking  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "hands", "two_hands.jpg")
OUT = os.path.join(ROOT, "artifacts", "benchmarks")
FRAMES = int(os.environ.get("BENCH_FRAMES", "12"))


class _MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb():
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    try:
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                               ctypes.POINTER(_MemoryCounters),
                                               wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                          ctypes.byref(counters), counters.cb):
            return None
        return round(counters.WorkingSetSize / (1024 * 1024), 1)
    except Exception:
        return None


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def hardware():
    memory = _MemoryStatus()
    memory.dwLength = ctypes.sizeof(memory)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
    except Exception:
        pass
    try:
        gpu = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_VideoController).Name -join '; '"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        gpu = "unknown"
    return {
        "cpu": platform.processor() or platform.machine(),
        "logical_cores": os.cpu_count(),
        "ram_gb": round(memory.ullTotalPhys / (1024 ** 3), 1) if memory.ullTotalPhys else None,
        "gpu": gpu or "unknown",
        "cuda_devices": cv2.cuda.getCudaEnabledDeviceCount() if hasattr(cv2, "cuda") else 0,
        "python": platform.python_version(),
        "opencv": cv2.__version__,
    }


def _timed(run, frames):
    run(0)  # warm-up, excluded
    times = []
    for index in range(1, frames + 1):
        start = time.perf_counter()
        value = run(index)
        times.append((time.perf_counter() - start) * 1000.0)
    return {"avg_ms": round(statistics.mean(times), 2),
            "min_ms": round(min(times), 2), "max_ms": round(max(times), 2),
            "frames": len(times)}


def main():
    if not os.path.isfile(FIXTURE):
        raise SystemExit(f"missing real hand fixture: {FIXTURE}")
    image = cv2.imread(FIXTURE)
    results = {"hardware": hardware(), "input": {}, "benchmarks": {}, "memory": {}}

    for label, size in (("1280x720", (1280, 720)), ("1920x1080", (1920, 1080))):
        frame = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        results["input"][label] = {"resolution": f"{frame.shape[1]}x{frame.shape[0]}",
                                   "fixture": os.path.relpath(FIXTURE, ROOT)}
        engine, _clip = build_engine(frame=frame)
        hand = engine.runtime_effects["hand_tracker"]
        hand.param_scale = 1.0
        hand.max_hands.set(2)
        hand.hand_confidence.set(30)
        face = engine.runtime_effects["face_tracker"]
        face.param_scale = 1.0
        blob = engine.runtime_effects["blob_tracker"]
        blob.param_scale = 1.0

        def variant(index):
            changed = frame.copy()
            changed[:, :, 1] = np.clip(changed[:, :, 1].astype(np.int16) + index, 0, 255)
            return changed

        key = f"hand@{label}"
        tracemalloc.start()
        before = rss_mb()
        hand_start = time.perf_counter()
        loaded = hand.landmark_backend()
        load_ms = round((time.perf_counter() - hand_start) * 1000.0, 1)
        results["benchmarks"][key] = _timed(lambda i: hand._detect_landmarks(variant(i)), FRAMES)
        results["benchmarks"][key]["model_load_ms"] = load_ms
        results["benchmarks"][key]["detections"] = len(hand._detect_landmarks(frame) or [])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        results["memory"][key] = {"rss_before_mb": before, "rss_after_mb": rss_mb(),
                                  "python_peak_mb": round(peak / (1024 * 1024), 1),
                                  "backend": hand.landmark_status(),
                                  "loaded": loaded is not None}

        results["benchmarks"][f"face@{label}"] = _timed(
            lambda i: face.detect_faces(variant(i)), FRAMES)
        results["benchmarks"][f"blob@{label}"] = _timed(
            lambda i: blob._detect(variant(i)), FRAMES)

        detections = hand._detect_landmarks(frame) or []
        results["benchmarks"][f"overlay@{label}"] = _timed(
            lambda i: hand.draw_tracking(variant(i), detections, label="Hand"), FRAMES)
        region = hand
        region.region_effect_1.set("blur")
        region.region_amount_1.set(60)
        results["benchmarks"][f"region_blur@{label}"] = _timed(
            lambda i: region.apply_tracked_regions(variant(i), detections), FRAMES)

        def cached_pair(index):
            fresh = variant(index)
            start = time.perf_counter()
            hand.apply(fresh)
            first = (time.perf_counter() - start) * 1000.0
            start = time.perf_counter()
            hand.apply(fresh)
            second = (time.perf_counter() - start) * 1000.0
            return first, second

        cached_pair(0)
        pairs = [cached_pair(i) for i in range(1, FRAMES + 1)]
        results["benchmarks"][f"apply_cached@{label}"] = {
            "uncached_avg_ms": round(statistics.mean(p[0] for p in pairs), 2),
            "cached_avg_ms": round(statistics.mean(p[1] for p in pairs), 2),
            "frames": len(pairs),
        }
        hand.close()

    os.makedirs(OUT, exist_ok=True)
    json_path = os.path.join(OUT, "tracking-benchmark.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)

    lines = ["# Tracking benchmarks", "", f"Frames per case: {FRAMES}", "",
             "## Hardware", ""]
    for name, value in results["hardware"].items():
        lines.append(f"- {name}: {value}")
    lines += ["", "## Results", "", "| Case | Average ms | Min ms | Max ms |", "| --- | ---: | ---: | ---: |"]
    for name, row in results["benchmarks"].items():
        if "avg_ms" in row:
            lines.append(f"| {name} | {row['avg_ms']} | {row['min_ms']} | {row['max_ms']} |")
        else:
            lines.append(f"| {name} | uncached {row['uncached_avg_ms']} | cached {row['cached_avg_ms']} | n/a |")
    lines += ["", "## Memory", ""]
    for name, row in results["memory"].items():
        lines.append(f"- {name}: {row}")
    markdown_path = os.path.join(OUT, "tracking-benchmark.md")
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nWrote {json_path} and {markdown_path}")


if __name__ == "__main__":
    main()
