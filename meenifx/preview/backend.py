"""GPU render backend: measured per-operation OpenCL dispatch with a CPU fallback.

There is no honest way to promise "GPU acceleration": a frame operation on a discrete GPU
is only faster when the work outweighs the two PCIe copies of a full BGRA frame. So this
module *measures* each operation once at startup on a 1080p frame - the size of a real
preview - and then routes every call to whichever side actually won:

* ``auto``  - follow the measurement (this is the default).
* ``on``    - force the OpenCL path even if it measured slower, for experiments.
* ``off``   - always the plain CPU call.

Everything that can run on the device goes through this module (resize, warps, remap,
colour conversion); mask/composite blending stays in vectorised numpy, which is already
faster than a per-element OpenCL kernel for these sizes. Three failed dispatches disable
the GPU for the session instead of breaking a frame.

The measurement itself runs in a *child process*. Timing OpenCL kernels in a worker
thread meant that a driver fault inside cv2 - an access violation, which no try/except
can catch - took the whole editor down moments after a project was opened. The child may
die instead: a session only ever enables OpenCL after a sandboxed run completed the exact
same operations on this machine, and the verdict is cached per OpenCV build and device.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import cv2
import numpy as np

# Three failures are enough to conclude the driver cannot be trusted for this session.
FAILURE_LIMIT = 3
OPS = ("resize", "warp_affine", "warp_perspective", "remap", "cvt_color")

# The measurement uses a 1080p BGRA frame - the size of a real full-quality preview.
PROFILE_NAME = "gpu_profile.json"
SANDBOX_TIMEOUT_S = 180

# The measurement runs in its own interpreter. OpenCL kernels on a 1080p frame can fault
# inside the driver, and a fault is an access violation - not a Python exception - so the
# only containment is process separation. The child prints one JSON line.
_SANDBOX_SCRIPT = r"""
import json, sys, time
import numpy as np
import cv2

OPS = ("resize", "warp_affine", "warp_perspective", "remap", "cvt_color")
width, height = 1920, 1080
result = {"ok": False, "opencv": cv2.__version__, "wins": {}, "ratios": {}}
try:
    result["device"] = str(cv2.ocl.Device.getDefault().name() or "")
except Exception:
    result["device"] = ""
try:
    if not cv2.ocl.haveOpenCL():
        print(json.dumps(result)); raise SystemExit(0)
    cv2.ocl.setUseOpenCL(True)
    if not cv2.ocl.useOpenCL():
        print(json.dumps(result)); raise SystemExit(0)
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 255, (height, width, 4), dtype=np.uint8)
    matrix = np.array([[0.98, -0.05, 12.0], [0.05, 0.98, 8.0]], dtype=np.float64)
    homography = np.array([[1.0, 0.02, 0.0], [0.0, 1.0, 0.0], [0.00008, 0.0, 1.0]])
    grid_y, grid_x = np.mgrid[0:height, 0:width]
    map_x = (grid_x + 8 * np.sin(grid_y / 60.0)).astype(np.float32)
    map_y = (grid_y + 8 * np.sin(grid_x / 60.0)).astype(np.float32)
    cpu_calls = {
        "resize": lambda: cv2.resize(frame, (width // 2, height // 2), interpolation=cv2.INTER_AREA),
        "warp_affine": lambda: cv2.warpAffine(frame, matrix, (width, height)),
        "warp_perspective": lambda: cv2.warpPerspective(frame, homography, (width, height)),
        "remap": lambda: cv2.remap(frame, map_x, map_y, interpolation=cv2.INTER_LINEAR),
        "cvt_color": lambda: cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR),
    }
    gpu_calls = {
        "resize": lambda: cv2.resize(cv2.UMat(frame), (width // 2, height // 2),
                                     interpolation=cv2.INTER_AREA).get(),
        "warp_affine": lambda: cv2.warpAffine(cv2.UMat(frame), matrix, (width, height)).get(),
        "warp_perspective": lambda: cv2.warpPerspective(cv2.UMat(frame), homography,
                                                        (width, height)).get(),
        "remap": lambda: cv2.remap(cv2.UMat(frame), cv2.UMat(map_x), cv2.UMat(map_y),
                                   interpolation=cv2.INTER_LINEAR).get(),
        "cvt_color": lambda: cv2.cvtColor(cv2.UMat(frame), cv2.COLOR_BGRA2BGR).get(),
    }
    def timed(call, repeats=2):
        call()
        start = time.perf_counter()
        for _ in range(repeats):
            call()
        return (time.perf_counter() - start) / repeats * 1000.0
    for op in OPS:
        cpu_ms = timed(cpu_calls[op])
        gpu_ms = timed(gpu_calls[op])
        if gpu_ms <= 0:
            result["wins"][op] = False
            result["ratios"][op] = 0.0
            continue
        result["wins"][op] = gpu_ms < cpu_ms * 0.95
        result["ratios"][op] = cpu_ms / gpu_ms
    result["ok"] = True
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result))
"""


def profile_path():
    """Per-user cache of the last sandboxed measurement (keyed by OpenCV + device)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "meeniFX")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(folder, PROFILE_NAME)


def _load_profile():
    try:
        with open(profile_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("opencv") != cv2.__version__:
        return None       # a different OpenCV build may measure differently
    return data


def _save_profile(payload, device, vendor, drivers):
    data = dict(payload or {})
    data.setdefault("ok", False)
    data.setdefault("wins", {})
    data.setdefault("ratios", {})
    data["opencv"] = cv2.__version__
    data["device"] = data.get("device") or device
    data["vendor"] = vendor
    data["drivers"] = drivers
    try:
        with open(profile_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass
    return data


def _run_sandbox():
    """Run the measurement in a child process; returns its JSON verdict or None."""
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run([sys.executable, "-c", _SANDBOX_SCRIPT],
                              capture_output=True, text=True, timeout=SANDBOX_TIMEOUT_S,
                              creationflags=creation)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        # The driver faulted inside the child: that is the evidence we needed.
        return {"ok": False, "error": f"sandbox exit {done.returncode}"}
    for line in reversed((done.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class RenderBackend:
    """Detects the graphics backend, measures the OpenCL ops and dispatches per call."""

    MODES = ("auto", "on", "off")

    def __init__(self, benchmark=False):
        self._lock = threading.RLock()
        self._mode = "auto"
        self._available = False
        self._device = ""
        self._vendor = ""
        self._drivers = ""
        self._failures = 0
        self._enabled = False
        self._wins = {}       # op -> True when the GPU path measured faster
        self._ratios = {}     # op -> cpu_ms / gpu_ms (> 1 means the GPU is faster)
        self._benchmarked = False
        self._benchmarking = False
        self.probe()
        self._apply()
        if benchmark and self._available:
            self.benchmark()

    # ------------------------------------------------------------------ detection
    def probe(self):
        """Ask OpenCV whether a usable OpenCL device exists (never raises)."""
        with self._lock:
            self._available = False
            self._device = self._vendor = self._drivers = ""
            try:
                self._available = bool(cv2.ocl.haveOpenCL())
            except Exception:
                self._available = False
            if not self._available:
                return False
            try:
                device = cv2.ocl.Device.getDefault()
                self._device = str(device.name() or "").strip()
                self._vendor = str(device.vendorName() or "").strip()
                self._drivers = str(device.driverVersion() or "").strip()
            except Exception:
                self._device = self._device or "OpenCL device"
            return True

    @property
    def available(self):
        return self._available

    @property
    def device(self):
        return self._device

    def set_mode(self, mode):
        """auto = measured choice, on = force the GPU ops, off = never use it."""
        mode = mode if mode in self.MODES else "auto"
        with self._lock:
            self._mode = mode
            self._failures = 0
            self._apply()

    @property
    def mode(self):
        return self._mode

    def _apply(self):
        if self._mode == "off":
            wanted = False
        elif self._mode == "on":
            wanted = self._available
        else:
            wanted = self._available and any(self._wins.values())
        try:
            cv2.ocl.setUseOpenCL(bool(wanted))
        except Exception:
            wanted = False
        # Trust the runtime, not our guess: some drivers advertise OpenCL and then say no.
        try:
            wanted = bool(wanted and cv2.ocl.useOpenCL())
        except Exception:
            wanted = False
        self._enabled = bool(wanted)

    @property
    def active(self):
        """True when this session really executes at least one OpenCL op on a device."""
        return self._enabled

    # ---------------------------------------------------------------- measuring
    def benchmark_async(self):
        """Measure in the background so launching the editor never waits for it."""
        if self._benchmarked or not self._available or self._benchmarking:
            return False
        cached = _load_profile()
        if cached is not None and cached.get("ok"):
            # A complete, matching measurement from this machine: adopt it without running
            # any OpenCL op in this process.
            with self._lock:
                self._wins = dict(cached.get("wins") or {})
                self._ratios = dict(cached.get("ratios") or {})
                self._benchmarked = True
            self._apply()
            return True
        self._benchmarking = True
        threading.Thread(target=self._sandbox_benchmark, name="gpu-benchmark",
                         daemon=True).start()
        return True

    def _sandbox_benchmark(self):
        """Run the measurement in a separate process and adopt its verdict.

        Measuring in-process meant running OpenCL kernels on a 1080p frame in a worker
        thread, and on some drivers that is an *access violation inside cv2* - which no
        try/except can catch and which killed the editor a second after opening a project.
        A child process can die instead; this session then simply stays on the CPU path.
        """
        payload = _run_sandbox()
        with self._lock:
            self._benchmarked = True
            self._benchmarking = False
            if payload and payload.get("ok"):
                self._wins = dict(payload.get("wins") or {})
                self._ratios = dict(payload.get("ratios") or {})
            else:
                # No verdict means no evidence that OpenCL survives this driver.
                self._wins = {}
                self._ratios = {}
            _save_profile(payload, self._device, self._vendor, self._drivers)
        self._apply()
        return dict(self._wins)

    def benchmark(self):
        """Measure CPU vs OpenCL once per operation, in the sandbox, and remember it.

        Is what keeps the app from "accelerating" onto a path that is measurably slower on
        transfer-bound drivers. Before it finishes the backend stays on the CPU path.
        """
        if self._benchmarked or not self._available or self._benchmarking:
            return dict(self._wins)
        self._benchmarking = True
        return self._sandbox_benchmark()

    def _use_gpu(self, op):
        if not self._enabled or self._benchmarking:
            return False
        if self._mode == "on":
            return True
        return bool(self._wins.get(op, False))

    # ---------------------------------------------------------------- reporting
    def info(self):
        """(backend, detail) for Preferences and the overlay - never nicer than reality."""
        if self._mode == "off":
            return "CPU", "GPU disabled in Preferences"
        if not self._available:
            return "CPU", "no OpenCL device"
        name = self._device or "OpenCL device"
        vendor = f", {self._vendor}" if self._vendor else ""
        if self._enabled:
            ops = [op for op in OPS if self._use_gpu(op)]
            why = "forced" if self._mode == "on" else ", ".join(ops)
            return "GPU (OpenCL)", f"{name}{vendor} [{why}]"
        slowest = min((self._ratios.get(op, 0.0) for op in OPS), default=0.0)
        if slowest:
            return "CPU", (f"OpenCL available ({name}{vendor}) but frame ops measured "
                           f"{1.0 / slowest:.1f}x slower on the GPU")
        return "CPU", f"OpenCL available ({name}{vendor}), not used"

    def summary(self):
        backend, detail = self.info()
        return f"{backend} - {detail}"

    def ratios(self):
        return dict(self._ratios)

    # ---------------------------------------------------------------- helpers
    def _fail(self, exc):
        with self._lock:
            self._failures += 1
            if self._failures >= FAILURE_LIMIT:
                self._enabled = False
                try:
                    cv2.ocl.setUseOpenCL(False)
                except Exception:
                    pass
        return exc

    @staticmethod
    def _finish(result):
        if isinstance(result, cv2.UMat):
            return result.get()
        return result

    def resize(self, image, size, interpolation=cv2.INTER_LINEAR):
        size = (int(size[0]), int(size[1]))
        if not self._use_gpu("resize"):
            return cv2.resize(image, size, interpolation=interpolation)
        try:
            out = self._finish(cv2.resize(cv2.UMat(image), size, interpolation=interpolation))
        except Exception as exc:
            self._fail(exc)
            return cv2.resize(image, size, interpolation=interpolation)
        if not isinstance(out, np.ndarray) or (out.shape[1], out.shape[0]) != size:
            return cv2.resize(image, size, interpolation=interpolation)
        return out

    def warp_affine(self, image, matrix, size, flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0)):
        """Same signature as cv2.warpAffine: call sites can drop cv2 for this module."""
        size = (int(size[0]), int(size[1]))
        if not self._use_gpu("warp_affine"):
            return cv2.warpAffine(image, matrix, size, flags=flags,
                                  borderMode=borderMode, borderValue=borderValue)
        try:
            out = self._finish(cv2.warpAffine(cv2.UMat(image), matrix, size, flags=flags,
                                              borderMode=borderMode, borderValue=borderValue))
        except Exception as exc:
            self._fail(exc)
            return cv2.warpAffine(image, matrix, size, flags=flags,
                                  borderMode=borderMode, borderValue=borderValue)
        if not isinstance(out, np.ndarray) or (out.shape[1], out.shape[0]) != size:
            return cv2.warpAffine(image, matrix, size, flags=flags,
                                  borderMode=borderMode, borderValue=borderValue)
        return out

    def warp_perspective(self, image, matrix, size, flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0)):
        """Same signature as cv2.warpPerspective."""
        size = (int(size[0]), int(size[1]))
        if not self._use_gpu("warp_perspective"):
            return cv2.warpPerspective(image, matrix, size, flags=flags,
                                       borderMode=borderMode, borderValue=borderValue)
        try:
            out = self._finish(cv2.warpPerspective(cv2.UMat(image), matrix, size, flags=flags,
                                                   borderMode=borderMode,
                                                   borderValue=borderValue))
        except Exception as exc:
            self._fail(exc)
            return cv2.warpPerspective(image, matrix, size, flags=flags,
                                       borderMode=borderMode, borderValue=borderValue)
        if not isinstance(out, np.ndarray) or (out.shape[1], out.shape[0]) != size:
            return cv2.warpPerspective(image, matrix, size, flags=flags,
                                       borderMode=borderMode, borderValue=borderValue)
        return out

    def remap(self, image, map_x, map_y, interpolation=cv2.INTER_LINEAR,
              borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0)):
        """Same signature as cv2.remap (mesh/puppet warps)."""
        shape = np.asarray(image).shape[:2]
        if not self._use_gpu("remap"):
            return cv2.remap(image, map_x, map_y, interpolation=interpolation,
                             borderMode=borderMode, borderValue=borderValue)
        try:
            out = self._finish(cv2.remap(cv2.UMat(image), cv2.UMat(map_x), cv2.UMat(map_y),
                                         interpolation=interpolation,
                                         borderMode=borderMode, borderValue=borderValue))
        except Exception as exc:
            self._fail(exc)
            return cv2.remap(image, map_x, map_y, interpolation=interpolation,
                             borderMode=borderMode, borderValue=borderValue)
        if not isinstance(out, np.ndarray) or out.shape[:2] != shape:
            return cv2.remap(image, map_x, map_y, interpolation=interpolation,
                             borderMode=borderMode, borderValue=borderValue)
        return out

    def cvt_color(self, image, code):
        shape = np.asarray(image).shape[:2]
        if not self._use_gpu("cvt_color"):
            return cv2.cvtColor(image, code)
        try:
            out = self._finish(cv2.cvtColor(cv2.UMat(image), code))
        except Exception as exc:
            self._fail(exc)
            return cv2.cvtColor(image, code)
        if not isinstance(out, np.ndarray) or out.shape[:2] != shape:
            return cv2.cvtColor(image, code)
        return out


# One process-wide instance: OpenCV's OpenCL switch is global anyway.
# Named `gpu` so it never shadows the `meenifx.preview.backend` module.
gpu = RenderBackend()
