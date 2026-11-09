"""Optional U^2-Net subject segmentation model.

U^2-Net is a salient-object segmentation network. It is trained to find the main
foreground subject, so it works on people and pets against ordinary, multicoloured
backgrounds without a colour key. The weights are Apache-2.0 and are downloaded on demand;
they are not part of the repository or the installer.

The model is deliberately optional. A missing model leaves the explicitly labelled Basic
GrabCut fallback available, but the UI must never present that fallback as AI subject
segmentation.
"""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from ..config import user_config_dir

MODEL_VERSION = "u2net-1"
MODEL_SIZE = 320
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str
    filename: str
    url: str
    sha256: str
    bytes: int
    license: str
    credit: str
    description: str


U2NET = ModelSpec(
    key="u2net",
    name="U^2-Net",
    filename="u2net.onnx",
    url="https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
    sha256="8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491",
    bytes=175997641,
    license="Apache-2.0",
    credit="Xuebin Qin et al., U^2-Net. ONNX weights distributed by rembg.",
    description="Automatic salient-subject segmentation for people, pets and objects.",
)
MODELS = {U2NET.key: U2NET}
DEFAULT_MODEL = U2NET


class DownloadCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelStatus:
    available: bool
    installed: bool
    path: str
    size: int
    expected_size: int
    verified: bool
    error: str
    runtime: str

    @property
    def label(self) -> str:
        if self.available:
            return f"{U2NET.name} ready"
        if self.installed:
            return "Model unavailable"
        return "AI model not installed"

    @property
    def detail(self) -> str:
        if self.error:
            return self.error
        if self.available:
            return f"{U2NET.name} {self.size / (1024 * 1024):.0f} MB, CPU inference"
        return "Download the optional AI model in Roto Brush Details."


def _disabled() -> bool:
    return os.environ.get("MEENIFX_DISABLE_ROTO_MODEL", "").strip().lower() in _TRUTHY


def _override_model_path() -> Path | None:
    raw = os.environ.get("MEENIFX_ROTO_MODEL", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_dir():
        path = path / DEFAULT_MODEL.filename
    return path


def model_root() -> Path:
    override = os.environ.get("MEENIFX_ROTO_MODEL_DIR", "").strip()
    if override:
        return Path(override)
    return Path(user_config_dir()) / "meenifx-models"


def model_path(spec: ModelSpec = DEFAULT_MODEL) -> Path:
    override = _override_model_path()
    return override if override is not None else model_root() / spec.filename


_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_STATUS_CACHE: dict[tuple[str, int, int], ModelStatus] = {}
_MODEL_LOCK = threading.RLock()
_MODELS: dict[str, "U2NetModel"] = {}


def _digest(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    if key in _HASH_CACHE:
        return _HASH_CACHE[key]
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    _HASH_CACHE[key] = value
    return value


def model_status(spec: ModelSpec = DEFAULT_MODEL, verify: bool = False) -> ModelStatus:
    """Cheap availability check. `verify` hashes the downloaded weights once."""
    if _disabled():
        return ModelStatus(False, False, "", 0, spec.bytes, False,
                           "The AI model is disabled by MEENIFX_DISABLE_ROTO_MODEL.", "off")
    path = model_path(spec)
    try:
        stat = path.stat()
    except OSError:
        return ModelStatus(False, False, str(path), 0, spec.bytes, False, "", "missing")
    key = (str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    if key in _STATUS_CACHE and not verify:
        return _STATUS_CACHE[key]
    installed = bool(stat.st_size == spec.bytes)
    verified = False
    error = ""
    if installed and verify:
        try:
            verified = _digest(path) == spec.sha256
            if not verified:
                error = "The downloaded model failed its checksum. Download it again."
        except OSError as exc:
            error = f"Could not verify the model: {exc}"
    elif not installed:
        error = (f"The model file is {stat.st_size / (1024 * 1024):.1f} MB, but "
                 f"{spec.bytes / (1024 * 1024):.1f} MB was expected. Download it again.")
    try:
        import onnxruntime  # noqa: F401
        runtime = "ONNX Runtime CPU"
    except ImportError:
        runtime = "ONNX Runtime missing"
        if installed and not error:
            error = ("The model is downloaded, but ONNX Runtime is missing from this "
                     "build. Reinstall meeniFX or install onnxruntime.")
    status = ModelStatus(installed and not error, installed, str(path), int(stat.st_size),
                         spec.bytes, verified, error, runtime)
    _STATUS_CACHE[key] = status
    return status


def model_signature() -> str:
    """A cache-key component that changes when the model is installed or removed."""
    status = model_status()
    return f"{MODEL_VERSION}:{'ready' if status.available else 'basic'}"


def download_model(spec: ModelSpec = DEFAULT_MODEL,
                   on_progress: Callable[[int, int], None] | None = None,
                   should_stop: Callable[[], bool] | None = None) -> str:
    """Download and verify the weights. The caller can cancel between 1 MiB blocks."""
    path = model_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    request = urllib.request.Request(spec.url, headers={"User-Agent": "meeniFX-Studios"})
    digest = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(temporary, "wb") as handle:
            total = int(response.headers.get("Content-Length") or spec.bytes)
            while True:
                if should_stop is not None and should_stop():
                    raise DownloadCancelled("Download cancelled")
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
                digest.update(block)
                done += len(block)
                if on_progress is not None:
                    on_progress(done, total)
        if done != spec.bytes:
            raise OSError(f"Downloaded {done} bytes, expected {spec.bytes}.")
        if digest.hexdigest() != spec.sha256:
            raise OSError("The downloaded model failed its SHA-256 check.")
        os.replace(temporary, path)
        _STATUS_CACHE.clear()
        return str(path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


class U2NetModel:
    """Thread-safe CPU inference for the downloaded U^2-Net ONNX weights."""

    def __init__(self, path: str):
        self.path = str(path)
        self._session = None
        self._input = None
        self._lock = threading.RLock()
        self.error = ""
        self._load()

    def _load(self):
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("ONNX Runtime is not available in this build.") from exc
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 1)))
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            self.path, sess_options=options, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def _run(self, blob):
        with self._lock:
            return self._session.run(None, {self._input: blob})[0]

    def predict(self, frame_bgr, quality: str = "balanced") -> np.ndarray | None:
        if frame_bgr is None or frame_bgr.size == 0 or self._session is None:
            return None
        height, width = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (MODEL_SIZE, MODEL_SIZE), interpolation=cv2.INTER_AREA)
        rgb = rgb.astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], np.float32)
        std = np.array([0.229, 0.224, 0.225], np.float32)
        blob = ((rgb - mean) / std).transpose(2, 0, 1)[None]
        output = self._run(blob)
        if str(quality) == "high":
            flipped = self._run(np.ascontiguousarray(blob[:, :, :, ::-1]))
            output = (output + flipped[:, :, :, ::-1]) * 0.5
        pred = np.squeeze(output).astype(np.float32)
        span = float(pred.max() - pred.min())
        if not np.isfinite(span) or span < 1e-6:
            return np.zeros((height, width), np.float32)
        pred = (pred - float(pred.min())) / span
        matte = cv2.resize(pred, (width, height), interpolation=cv2.INTER_LINEAR)
        return np.clip(matte, 0.0, 1.0).astype(np.float32)


def get_model(spec: ModelSpec = DEFAULT_MODEL) -> U2NetModel | None:
    """The loaded model, or None when it is missing, disabled or failed to load."""
    status = model_status(spec)
    if not status.available:
        return None
    path = str(Path(status.path).resolve())
    with _MODEL_LOCK:
        current = _MODELS.get(path)
        if current is not None:
            return current
        try:
            current = U2NetModel(path)
        except Exception as exc:                       # noqa: BLE001 - reported in the UI
            status = ModelStatus(False, True, path, status.size, spec.bytes, False,
                                 f"The model could not be loaded: {exc}", status.runtime)
            try:
                stat = Path(path).stat()
                key = (str(Path(path).resolve()), int(stat.st_size), int(stat.st_mtime_ns))
                _STATUS_CACHE[key] = status
            except OSError:
                pass
            return None
        _MODELS[path] = current
        return current
