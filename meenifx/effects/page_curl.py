"""Cylindrical Page Curl with matching CPU and GPU rendering."""
import math
import cv2
import numpy as np
from .base import ParamSpec
from .shader_effects import ShaderEffect, BACKEND
from ..processing import shaders as S


class PageCurlEffect(ShaderEffect):
    key, name = "page_curl", "Page Curl"
    PRESETS = {
        "corner_lift": ("Corner Lift", {"progress": .28, "angle": -40, "radius": 12}),
        "broad_turn": ("Broad Turn", {"progress": .6, "angle": 20, "radius": 24, "depth": 50}),
        "top_fold": ("Top Fold", {"progress": .4, "direction": "top", "angle": 0, "radius": 16}),
    }
    PARAMS = (
        ParamSpec("progress", "Turn progress", "float", 0, 1, .4, step=.01),
        ParamSpec("radius", "Curl radius", "float", 2, 50, 16., unit="%"),
        ParamSpec("angle", "Curl angle", "float", -180, 180, -25., unit="degrees"),
        ParamSpec("position_x", "Curl position X", "float", 0, 100, 50., unit="%"),
        ParamSpec("position_y", "Curl position Y", "float", 0, 100, 50., unit="%"),
        ParamSpec("direction", "Turn from", "choice", default="right",
                  choices=("right", "left", "top", "bottom")),
        ParamSpec("thickness", "Page thickness", "float", 0, 5, .3, unit="%"),
        ParamSpec("front", "Front brightness", "float", 0, 200, 100., unit="%"),
        ParamSpec("back_r", "Backside red", "int", 0, 255, 235),
        ParamSpec("back_g", "Backside green", "int", 0, 255, 230),
        ParamSpec("back_b", "Backside blue", "int", 0, 255, 220),
        ParamSpec("back_texture", "Backside", "choice", default="color",
                  choices=("color", "source", "image"),
                  choice_labels=("Solid color", "Mirrored page", "Image file")),
        ParamSpec("back_path", "Backside image", "text", default="",
                  help="Optional image path; choose Image file above"),
        ParamSpec("back", "Backside brightness", "float", 0, 200, 90., unit="%"),
        ParamSpec("shadow", "Shadow intensity", "float", 0, 100, 45., unit="%"),
        ParamSpec("softness", "Shadow softness", "float", .1, 30, 6., unit="%"),
        ParamSpec("highlight", "Highlight intensity", "float", 0, 100, 30., unit="%"),
        ParamSpec("depth", "Perspective depth", "float", 0, 100, 35., unit="%"),
        ParamSpec("antialias", "Edge antialiasing", "choice", default="smooth",
                  choices=("smooth", "draft"), choice_labels=("Smooth (4 samples)", "Draft")),
        BACKEND,
    )

    def neutral(self, p):
        return p["progress"] <= 0

    def uniforms(self, p, layer):
        h, w = layer.shape[:2]
        short = float(min(w, h))
        angle = math.radians(p["angle"] + {"right": 0, "left": 180, "top": -90, "bottom": 90}[p["direction"]])
        normal = np.array([math.cos(angle), math.sin(angle)], np.float32)
        center = np.array([w * p["position_x"] / 100, h * p["position_y"] / 100], np.float32)
        corners = np.array([[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]], np.float32)
        projections = corners @ normal
        radius = short * p["radius"] / 100 / (1 + p["depth"] * .005)
        # Position is weighted to zero at the endpoints, preserving complete turns.
        shift = float((center - [w / 2, h / 2]) @ normal) * math.sin(p["progress"] * math.pi)
        axis = float(projections.max() + radius - p["progress"] *
                     (projections.max() - projections.min() + (math.pi + 1) * radius) + shift)
        color = np.array([p["back_r"], p["back_g"], p["back_b"]], np.float32) / 255
        color = np.where(color <= .04045, color / 12.92, ((color + .055) / 1.055) ** 2.4)
        extra = ()
        mode = ("color", "source", "image").index(p["back_texture"])
        if mode == 2:
            import os
            path = str(p["back_path"] or "")
            try:
                key = (path, os.stat(path).st_mtime_ns, w, h)
            except OSError:
                key = (path, None, w, h)
            cache = getattr(self, "_back_cache", (None, None))
            picture = cache[1] if cache[0] == key else (
                cv2.imread(path, cv2.IMREAD_UNCHANGED) if path else None)
            if picture is None:
                raise ValueError("Page Curl backside image is unavailable; choose an image or Solid color")
            if picture.ndim == 2:
                picture = cv2.cvtColor(picture, cv2.COLOR_GRAY2BGRA)
            elif picture.shape[2] == 3:
                picture = cv2.cvtColor(picture, cv2.COLOR_BGR2BGRA)
            if picture.dtype == np.uint16:
                picture = (picture / 257).astype(np.uint8)
            picture = cv2.resize(picture, (w, h))
            self._back_cache = (key, picture)
            extra = (picture,)
        return dict(normal=tuple(float(v) for v in normal), axis=axis, radius=radius,
                    center=tuple(float(v) for v in center), thickness=p["thickness"] * short / 100,
                    front=p["front"] / 100, back=p["back"] / 100,
                    backColor=tuple(float(v) for v in color), backMode=mode,
                    shadow=p["shadow"] / 100, softness=p["softness"] * short / 100,
                    highlight=p["highlight"] / 100, depth=p["depth"] / 100,
                    antialias=p["antialias"] == "smooth", complete=p["progress"] >= 1), extra

    @staticmethod
    def cpu(image, u, extra=()):
        if u["complete"]:
            return np.zeros_like(image)
        y, x = np.indices(image.shape[:2], dtype=np.float32)
        n = u["normal"]
        tangent = (-n[1], n[0])
        radius = u["radius"]

        def at(x, y):
            q = x * n[0] + y * n[1] - u["axis"]
            theta = np.arcsin(np.clip(q / radius, 0, 1))
            v = (x - u["center"][0]) * tangent[0] + (y - u["center"][1]) * tangent[1]
            tx = v * u["depth"] * (1 + np.cos(theta)) * .15
            sback = radius * (np.pi - theta) + u["thickness"]
            bx = x + n[0] * (sback - q) + tangent[0] * tx
            by = y + n[1] * (sback - q) + tangent[1] * tx
            backside = S.sample(image, bx, by)
            visible_back = (bx >= 0) & (bx <= image.shape[1] - 1) & (by >= 0) & (by <= image.shape[0] - 1)
            sx = x + n[0] * (radius * theta - q)
            sy = y + n[1] * (radius * theta - q)
            curled = S.sample(image, sx, sy)
            if u["backMode"] == 2:
                texture = S.sample(extra[0], bx, by)
                backside = texture * backside[..., 3:4]
            elif u["backMode"] == 0:
                backside = np.concatenate((np.asarray(u["backColor"], np.float32) * backside[..., 3:4], backside[..., 3:4]), axis=2)
            backside[..., :3] *= u["back"] * (.35 + .65 * np.sin(theta)[..., None])
            curled[..., :3] *= u["front"] * (.45 + .55 * np.cos(theta)[..., None])
            fold = np.where(visible_back[..., None], backside, curled)
            fold[..., :3] += fold[..., 3:4] * u["highlight"] * np.sin(theta)[..., None] ** 8 * .35
            flat = S.sample(image, x, y)
            flat[..., :3] *= u["front"] * (1 - u["shadow"] * np.exp(np.minimum(q, 0) / u["softness"]))[..., None]
            return np.where((q < 0)[..., None], flat,
                            np.where((q <= radius)[..., None], fold, 0)).astype(np.float32)

        if u["antialias"]:
            return sum(at(x + dx, y + dy) for dx, dy in ((-.25, -.25), (.25, -.25), (-.25, .25), (.25, .25))) * .25
        return at(x, y)
