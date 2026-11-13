"""Bounded, shipped-shader rendering. Every GL object belongs to one worker."""
from concurrent.futures import Future, TimeoutError
from pathlib import Path
import atexit
import os
import sys
import threading
import time
import queue

import numpy as np

BUILTINS = {"identity", "lens_warp", "page_curl"}
MAX_PIXELS = 16_777_216
MAX_BYTES = 512 * 1024 * 1024


class ShaderUnavailable(RuntimeError):
    pass


class _Worker:
    """Daemon owned here, so cleanup runs before Python's executor shutdown hooks."""
    def __init__(self):
        self.jobs = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._loop, name="meeniFX-GL", daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            future, function, args = job
            try:
                future.set_result(function(*args))
            except BaseException as exc:
                future.set_exception(exc)

    def submit(self, function, *args):
        future = Future()
        self.jobs.put((future, function, args), timeout=1)
        return future

    def shutdown(self, wait=True):
        self.jobs.put(None, timeout=1)
        if wait:
            self.thread.join(timeout=30)


def resource(name):
    if name not in BUILTINS and name not in {"fullscreen", "sampling"}:
        raise ValueError("Only shipped shader resources can be loaded")
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    suffix = ".vert" if name == "fullscreen" else ".glsl"
    return (root / "assets" / "shaders" / (name + suffix)).read_text(encoding="utf-8")


def prepare(layer):
    rgba = np.ascontiguousarray(layer[:, :, [2, 1, 0, 3]], dtype=np.float32) / 255.0
    rgb = rgba[:, :, :3]
    rgb[:] = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    rgb *= rgba[:, :, 3:4]
    return rgba


def finish(rgba):
    rgba = np.nan_to_num(rgba, nan=0., posinf=1., neginf=0.)
    alpha = np.clip(rgba[:, :, 3:4], 0., 1.)
    rgb = np.clip(rgba[:, :, :3] / np.maximum(alpha, 1e-8), 0., 1.)
    rgb = np.where(rgb <= .0031308, rgb * 12.92, 1.055 * rgb ** (1 / 2.4) - .055)
    rgb = np.where(alpha > 1e-8, rgb, 0.)
    return np.ascontiguousarray(np.rint(np.concatenate((rgb[:, :, ::-1], alpha), axis=2) * 255).astype(np.uint8))


def sample(image, x, y, edge=0, nearest=False):
    """The exact same pixel-center sampler as sampling.glsl, in premultiplied light."""
    h, w = image.shape[:2]

    def fetch(ix, iy):
        valid = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
        if edge == 2:
            def mirror(i, size):
                period = max(1, 2 * (size - 1))
                q = np.mod(i, period)
                return np.minimum(q, period - q)
            ix, iy = mirror(ix, w), mirror(iy, h)
        out = image[np.clip(iy, 0, h - 1), np.clip(ix, 0, w - 1)]
        return out if edge else out * valid[..., None]

    if nearest:
        return fetch(np.floor(x + .5).astype(np.int32), np.floor(y + .5).astype(np.int32))
    ix, iy = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
    fx, fy = (x - ix)[..., None], (y - iy)[..., None]
    return ((fetch(ix, iy) * (1 - fx) + fetch(ix + 1, iy) * fx) * (1 - fy)
            + (fetch(ix, iy + 1) * (1 - fx) + fetch(ix + 1, iy + 1) * fx) * fy).astype(np.float32)


class ShaderRunner:
    def __init__(self):
        self._pool = _Worker()
        self._lock = threading.RLock()
        self._ctx = None
        self._programs = {}
        self._textures = []
        self._target = self._fbo = None
        self._size = None
        self.closed = False
        self.error = ""
        self.info = {}
        self.timings = {}
        atexit.register(self.close)

    def _context(self):
        if self._ctx is None:
            if os.environ.get("MEENIFX_DISABLE_SHADERS") == "1":
                raise ShaderUnavailable("GPU shaders disabled by configuration")
            started = time.perf_counter()
            import moderngl
            self._ctx = moderngl.create_standalone_context(require=330)
            self.info = {key: self._ctx.info.get(key) for key in
                         ("GL_VENDOR", "GL_RENDERER", "GL_VERSION")}
            self.timings["context_ms"] = (time.perf_counter() - started) * 1000
        return self._ctx

    def render(self, name, layer, uniforms=None, extra=(), cancelled=None):
        if name not in BUILTINS:
            raise ValueError("Unknown shipped shader")
        if layer.ndim != 3 or layer.shape[2] != 4 or layer.dtype != np.uint8:
            raise ValueError("Shaders require uint8 straight BGRA")
        h, w = layer.shape[:2]
        if not (0 < w <= 8192 and 0 < h <= 8192) or w * h > MAX_PIXELS:
            raise ValueError("Shader frame exceeds the pixel budget")
        if len(extra) > 3 or (len(extra) + 2) * w * h * 16 > MAX_BYTES:
            raise ValueError("Shader texture budget exceeded")
        if any(item.shape != layer.shape or item.dtype != np.uint8 for item in extra):
            raise ValueError("Additional shader textures must match the input frame")
        values = dict(uniforms or {})
        for value in values.values():
            data = np.asarray(value)
            if data.size > 4 or not np.isfinite(data.astype(float)).all():
                raise ValueError("Uniforms must be finite scalars or vectors of up to four components")
        with self._lock:
            if self.closed:
                raise ShaderUnavailable("Shader runner is closed")
            if cancelled and cancelled():
                raise InterruptedError("Shader render cancelled")
            future = self._pool.submit(self._render, name, layer, values, tuple(extra), cancelled)
            try:
                return future.result(timeout=30)
            except TimeoutError as exc:
                self.error = "GPU driver did not finish within 30 seconds"
                raise ShaderUnavailable(self.error) from exc
            except InterruptedError:
                raise
            except Exception as exc:
                self.error = str(exc)
                raise ShaderUnavailable("GPU shaders unavailable: " + str(exc)) from exc

    def _render(self, name, layer, uniforms, extra, cancelled):
        if cancelled and cancelled():
            raise InterruptedError("Shader render cancelled")
        ctx = self._context()
        if name not in self._programs:
            started = time.perf_counter()
            fragment = resource(name).replace("// SAMPLING", resource("sampling"))
            program = ctx.program(vertex_shader=resource("fullscreen"), fragment_shader=fragment)
            vao = ctx.vertex_array(program, [])
            self._programs[name] = (program, vao)
            self.timings[name + "_compile_ms"] = (time.perf_counter() - started) * 1000
        program, vao = self._programs[name]
        size = (layer.shape[1], layer.shape[0])
        if size != self._size or len(self._textures) != len(extra) + 1:
            self._release_buffers()
            self._size = size
            self._textures = [ctx.texture(size, 4, dtype="f4") for _ in range(len(extra) + 1)]
            self._target = ctx.texture(size, 4, dtype="f4")
            self._fbo = ctx.framebuffer([self._target])
        started = time.perf_counter()
        for index, image in enumerate((layer, *extra)):
            self._textures[index].write(prepare(image).tobytes())
            self._textures[index].use(index)
            if "iChannel" + str(index) in program:
                program["iChannel" + str(index)].value = index
        self.timings["upload_ms"] = (time.perf_counter() - started) * 1000
        uniforms = {"iResolution": (*size, 1.), **uniforms}
        for key, value in uniforms.items():
            if key in program:
                program[key].value = value
        self._fbo.use()
        ctx.viewport = (0, 0, *size)
        ctx.disable(0x0BE2)  # GL_BLEND: shader returns a complete premultiplied layer.
        started = time.perf_counter()
        vao.render(vertices=3)
        data = self._fbo.read(components=4, dtype="f4", alignment=1)
        self.timings["draw_readback_ms"] = (time.perf_counter() - started) * 1000
        if cancelled and cancelled():
            raise InterruptedError("Shader render cancelled")
        return finish(np.frombuffer(data, np.float32).reshape(layer.shape))

    def _release_buffers(self):
        for item in (self._fbo, self._target, *self._textures):
            if item is not None:
                item.release()
        self._textures = []
        self._fbo = self._target = None
        self._size = None

    def _release(self):
        self._release_buffers()
        for program, vao in self._programs.values():
            vao.release()
            program.release()
        self._programs.clear()
        if self._ctx is not None:
            self._ctx.release()
            self._ctx = None

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._pool.submit(self._release).result(timeout=30)
            self._pool.shutdown(wait=True)


_RUNNER = None
_LOCK = threading.Lock()


def runner():
    global _RUNNER
    with _LOCK:
        if _RUNNER is None or _RUNNER.closed:
            _RUNNER = ShaderRunner()
        return _RUNNER
