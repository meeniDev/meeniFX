"""Original geometric effects, sharing linear-premultiplied GPU/CPU algorithms."""
import logging
import math
import threading

import cv2
import numpy as np

from .base import LayerEffect, ParamSpec
from ..model.values import Value
from ..processing import shaders as S

BACKEND = ParamSpec("backend", "Renderer", "choice", default="auto",
                    choices=("auto", "gpu", "cpu"), choice_labels=("Automatic", "GPU", "CPU"),
                    advanced=True, help="Automatic uses a matching CPU renderer if GPU shaders are unavailable")


class ShaderEffect(LayerEffect):
    NOTE = "GPU acceleration with a matching CPU fallback. Forced GPU reports unsupported hardware."
    category = "Glitch & Distortion"

    def __init__(self, app):
        super().__init__(app)
        self.status = "Not rendered yet; Automatic supports GPU or matching CPU"
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def values(self):
        result = {}
        for spec in self.PARAMS:
            value = getattr(self, spec.name).get()
            try:
                if spec.kind in ("int", "float") and not math.isfinite(float(value)):
                    value = spec.default
                value = spec.clamp(value)
            except (TypeError, ValueError, OverflowError):
                value = spec.default
            result[spec.name] = value
        return result

    def apply_layer(self, layer):
        p = self.values()
        if self.neutral(p):
            return layer.copy()
        u, extra = self.uniforms(p, layer)
        fps = max(.001, float(getattr(self.app, "project_fps", 30)))
        u.update(iFrame=int(self.local_frame), iTime=float(self.local_frame) / fps)
        use_gpu = p["backend"] == "gpu" or (p["backend"] == "auto" and
                    threading.current_thread() is not threading.main_thread())
        if use_gpu:
            try:
                result = S.runner().render(self.key, layer, u, extra=extra)
                self.status = "GPU renderer ready"
                return result
            except S.ShaderUnavailable as exc:
                self.status = "Matching CPU fallback: " + str(exc)
                if p["backend"] == "gpu":
                    raise
                logging.getLogger(__name__).debug(self.status)
        else:
            self.status = "CPU renderer (GPU compilation stays off the UI thread)"
        return S.finish(self.cpu(S.prepare(layer), u, tuple(S.prepare(item) for item in extra)))

    def close(self):
        # Shared context destruction remains on its owning worker.
        backend = S._RUNNER
        if backend is not None and not backend.closed:
            backend.close()


class LensWarpEffect(ShaderEffect):
    key, name = "lens_warp", "Lens Warp"
    PRESETS = {
        "soft_glass": ("Soft Glass", {"amount": 35, "radius": 45, "mode": "bulge", "chromatic": 0}),
        "wide_view": ("Wide View", {"amount": 80, "radius": 110, "mode": "pinch", "edge": "mirror"}),
        "prism_lens": ("Prism Lens", {"amount": 60, "radius": 55, "chromatic": 12}),
    }
    PARAMS = (
        ParamSpec("center_x", "Center X", "float", 0, 100, 50., unit="%"),
        ParamSpec("center_y", "Center Y", "float", 0, 100, 50., unit="%"),
        ParamSpec("radius", "Radius", "float", 1, 150, 45., unit="%",
                  help="Radius relative to the shorter frame edge"),
        ParamSpec("amount", "Distortion", "float", -100, 100, 60., unit="%"),
        ParamSpec("mode", "Shape", "choice", default="bulge", choices=("bulge", "pinch")),
        ParamSpec("aspect", "Aspect ratio", "float", .25, 4, 1., step=.01),
        ParamSpec("edge", "Edges", "choice", default="transparent",
                  choices=("transparent", "clamp", "mirror")),
        ParamSpec("chromatic", "Chromatic aberration", "float", 0, 20, 0., unit="%"),
        ParamSpec("interpolation", "Interpolation", "choice", default="linear",
                  choices=("linear", "nearest"), choice_labels=("Smooth", "Nearest pixel")),
        BACKEND,
    )

    def neutral(self, p):
        return abs(p["amount"]) < 1e-7 and p["chromatic"] < 1e-7

    def uniforms(self, p, layer):
        h, w = layer.shape[:2]
        return dict(center=(p["center_x"] / 100 * (w - 1), p["center_y"] / 100 * (h - 1)),
                    radius=p["radius"] / 100 * min(w, h), aspect=p["aspect"],
                    amount=p["amount"] * .008 * (-1 if p["mode"] == "pinch" else 1),
                    chromatic=p["chromatic"] * .002,
                    edge=("transparent", "clamp", "mirror").index(p["edge"]),
                    nearest=p["interpolation"] == "nearest"), ()

    @staticmethod
    def cpu(image, u, extra=()):
        y, x = np.indices(image.shape[:2], dtype=np.float32)
        dx, dy = x - u["center"][0], y - u["center"][1]
        d2 = (dx / (u["radius"] * u["aspect"])) ** 2 + (dy / u["radius"]) ** 2
        falloff = np.maximum(0, 1 - d2) ** 2
        scale = 1 - u["amount"] * falloff
        r = S.sample(image, x + dx * (scale - 1 - u["chromatic"] * falloff),
                     y + dy * (scale - 1 - u["chromatic"] * falloff), u["edge"], u["nearest"])
        g = S.sample(image, x + dx * (scale - 1), y + dy * (scale - 1), u["edge"], u["nearest"])
        b = S.sample(image, x + dx * (scale - 1 + u["chromatic"] * falloff),
                     y + dy * (scale - 1 + u["chromatic"] * falloff), u["edge"], u["nearest"])
        return np.stack((r[..., 0], g[..., 1], b[..., 2],
                         np.maximum(np.maximum(r[..., 3], g[..., 3]), b[..., 3])), axis=2)


