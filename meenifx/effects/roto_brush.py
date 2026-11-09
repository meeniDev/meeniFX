"""Roto Brush: a guided subject matte with an optional learned segmenter.

The preferred backend is U^2-Net (Apache-2.0, downloaded on demand). It predicts the main
foreground subject from an ordinary picture, not from a screen colour. Green strokes mark
parts that must stay, Alt-red strokes mark parts that must go, and the Refine Edge brush
pushes the matte onto a difficult boundary. GrabCut remains available under an explicitly
labelled Basic mode; it is never described as the AI segmenter.

The mask is applied as the layer's alpha channel. Composite preview and export therefore
show the clip underneath through the removed background; the diagnostic views
(Isolated, Alpha Matte, Checkerboard) are previews only and are forced back to Composite
for export.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..model.values import Value
from ..roto import (MaskCache, basic_fallback_note, decontaminate, finish_matte,
                    mask_key, model_status, rasterise, segment_subject,
                    strokes_for_frame)
from ..roto.segment import quality_settings
from .base import LayerEffect, ParamSpec, param_number

MATTE_VIEWS = ("original", "outline", "isolated", "alpha", "checkerboard",
               "solid", "composite")
MATTE_LABELS = ("Original", "Outline", "Isolated Subject", "Alpha Matte",
                "Transparency Checkerboard", "Foreground on Colour", "Composite")
VISIBLE_VIEWS = ("original", "outline", "isolated", "alpha", "checkerboard", "composite")
VISIBLE_LABELS = ("Original", "Outline", "Isolated Subject", "Alpha Matte",
                  "Transparency Checkerboard", "Composite")
QUALITIES = ("draft", "balanced", "high")
QUALITY_LABELS = ("Fast", "Balanced", "High")
BACKENDS = ("auto", "model", "basic")
BACKEND_LABELS = ("Automatic", "AI Model (U^2-Net)", "Basic (GrabCut)")
MAX_MEMO = 8


def _colour(value, fallback=0):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return max(0, min(255, number))


class RotoBrushEffect(LayerEffect):
    """A brush-guided subject matte with real segmentation and per-frame propagation."""

    key = "roto_brush"
    name = "Roto Brush"
    category = "Keying & Masking"
    NOTE = ("Uses the optional U^2-Net subject model. Green strokes keep, Alt-red strokes "
            "remove. The Basic mode is GrabCut and is not equivalent to the AI segmenter.")

    PARAMS = (
        ParamSpec("subject", "Subject", "choice", default="auto",
                  choices=("auto", "choose"), choice_labels=("Auto", "Choose"),
                  help="Auto keeps the main subject. Choose then click a subject in the preview"),
        ParamSpec("quality", "Quality", "choice", default="balanced",
                  choices=QUALITIES, choice_labels=QUALITY_LABELS,
                  help="Fast analyses at half size. High adds a flip-averaged model pass"),
        ParamSpec("smooth", "Edge Smoothness", "float", 0.0, 10.0, 1.0, step=0.5,
                  unit=" px", pixels=True, group="Matte",
                  help="Smooth the matte before its edge is shaped"),
        ParamSpec("feather", "Feather", "float", 0.0, 40.0, 1.5, step=0.5, unit=" px",
                  pixels=True, group="Matte", help="Soften the finished matte edge"),
        ParamSpec("choke", "Expand / Shrink", "float", -30.0, 30.0, 0.0, step=0.5,
                  unit=" px", pixels=True, group="Matte",
                  help="Positive expands the matte outward; negative shrinks it inward"),
        ParamSpec("edge_contrast", "Edge Refinement", "float", 0.0, 100.0, 35.0, step=1.0,
                  unit=" %", group="Matte",
                  help="Steepen the decision at the boundary without moving it far"),
        ParamSpec("decontaminate", "Edge Colour Cleanup", "float", 0.0, 100.0, 0.0,
                  step=1.0, unit=" %", advanced=True, group="Details",
                  help="Pull the old background colour out of the soft edge"),
        ParamSpec("search_radius", "Search Radius", "float", 4.0, 120.0, 24.0, step=1.0,
                  unit=" px", pixels=True, advanced=True, group="Details",
                  help="How far a correction stroke may search for a boundary"),
        ParamSpec("tolerance", "Colour Tolerance", "float", 1.0, 100.0, 28.0, step=1.0,
                  unit=" %", advanced=True, group="Details",
                  help="Colour range used by the Basic fallback and correction fitting"),
        ParamSpec("refine", "Refine Corrections", "bool", default=True, advanced=True,
                  group="Details", help="Fit painted corrections to the local pixels"),
        ParamSpec("backend", "Segmenter", "choice", default="auto",
                  choices=BACKENDS, choice_labels=BACKEND_LABELS, advanced=True,
                  group="Details",
                  help="Automatic uses U^2-Net when installed and Basic otherwise"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.subject = Value("auto")
        self.quality = Value("balanced")
        self.smooth = Value(1.0)
        self.feather = Value(1.5)
        self.choke = Value(0.0)
        self.edge_contrast = Value(35.0)
        self.decontaminate = Value(0.0)
        self.search_radius = Value(24.0)
        self.tolerance = Value(28.0)
        self.refine = Value(True)
        self.backend = Value("auto")
        # Kept as hidden values so older projects and the existing tests can still set
        # them. They are not workflow controls; preview mode lives on the context toolbar.
        self.transparency = Value(100.0)
        self.matte_view = Value("composite")
        self.view_r = Value(24)
        self.view_g = Value(26)
        self.view_b = Value(30)
        self.auto_seed = Value(False)
        self.last_note = ""
        self.last_backend = "basic"
        self._memo = {}
        self._memo_order = []

    # ------------------------------------------------------------------ geometry
    def _rect(self, width, height):
        rect = self.layer_rect
        if not rect:
            return 0.0, 0.0, float(width), float(height)
        try:
            x, y, w, h = (float(value) for value in rect)
        except (TypeError, ValueError):
            return 0.0, 0.0, float(width), float(height)
        return x, y, max(1.0, w), max(1.0, h)

    def _region(self, width, height):
        x, y, rect_w, rect_h = self._rect(width, height)
        x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
        x1, y1 = min(width, int(round(x + rect_w))), min(height, int(round(y + rect_h)))
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def source_frame(self):
        clip = self.clip
        local = int(self.local_frame)
        if clip is None or not hasattr(clip, "source_frame_at"):
            return max(0, local)
        try:
            return max(0, int(clip.source_frame_at(int(getattr(clip, "timeline_start", 0)) + local)))
        except Exception:                                # noqa: BLE001 - keep rendering
            return max(0, local)

    # ------------------------------------------------------------------ analysis
    def _subject_point(self):
        entry = self.clip_entry or {}
        point = entry.get("subject_point")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            return None
        return (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))

    def _analysis_settings(self):
        quality = quality_settings(str(self.quality.get()))
        try:
            point = self._subject_point()
        except Exception:                                # noqa: BLE001 - live editing
            point = None
        return {
            "backend": str(self.backend.get() or "auto"),
            "refine": bool(self.refine.get()),
            "search_radius": float(self.search_radius.get()),
            "iterations": int(quality["iterations"]),
            "band": float(quality["band"]),
            "scale": float(quality["scale"]),
            "tolerance": max(1.0, float(self.tolerance.get())),
            "quality": str(self.quality.get() or "balanced"),
            "subject_point": point,
        }

    def _memo_get(self, key):
        value = self._memo.get(key)
        if value is not None:
            self._memo_order.remove(key)
            self._memo_order.append(key)
        return value

    def _memo_put(self, key, matte):
        self._memo[key] = matte
        self._memo_order.append(key)
        while len(self._memo_order) > MAX_MEMO:
            self._memo.pop(self._memo_order.pop(0), None)
        return matte

    def _work_region(self, rgb_region, settings):
        scale = float(settings.get("scale", 1.0))
        height, width = rgb_region.shape[:2]
        work_w = max(8, int(round(width * scale)))
        work_h = max(8, int(round(height * scale)))
        work = cv2.resize(rgb_region, (work_w, work_h), interpolation=cv2.INTER_AREA) \
            if (work_w, work_h) != (width, height) else rgb_region
        return work

    def _segment_region(self, rgb_region, source_frame, settings, prior=None):
        height, width = rgb_region.shape[:2]
        strokes = strokes_for_frame((self.clip_entry or {}).get("strokes") or (),
                                    source_frame,
                                    (self.clip_entry or {}).get("base_frame", 0))
        if not strokes and not bool(self.auto_seed.get()):
            backend = str(settings.get("backend") or "auto")
            if backend == "basic" or not model_status().available:
                return None, "basic"
        work = self._work_region(rgb_region, settings)
        work_h, work_w = work.shape[:2]
        foreground, background, refine_mask = rasterise(
            strokes, work_w, work_h, (0.0, 0.0, float(work_w), float(work_h)))
        if not strokes and bool(self.auto_seed.get()):
            # Hidden compatibility/audit seed. The real workflow always starts from a
            # painted stroke; this gives old projects without strokes a real matte.
            foreground = np.zeros((work_h, work_w), np.uint8)
            cv2.ellipse(foreground, (work_w // 2, work_h // 2),
                        (max(4, int(work_w * 0.28)), max(4, int(work_h * 0.34))),
                        0, 0, 360, 255, -1)
            background = np.zeros((work_h, work_w), np.uint8)
            background[:max(1, work_h // 20), :] = 255
            background[-max(1, work_h // 20):, :] = 255
            background[:, :max(1, work_w // 20)] = 255
            background[:, -max(1, work_w // 20):] = 255
        previous = prior
        if previous is not None and previous.shape[:2] != (work_h, work_w):
            previous = cv2.resize(previous, (work_w, work_h), interpolation=cv2.INTER_LINEAR)
        matte, backend = segment_subject(
            work, foreground, background, refine_mask, settings,
            subject_point=self._subject_point(), prior=previous)
        if matte is None:
            return None, backend
        if (work_w, work_h) != (width, height):
            matte = cv2.resize(matte, (width, height), interpolation=cv2.INTER_LINEAR)
        return np.clip(matte, 0.0, 1.0).astype(np.float32), backend

    def segment_frame(self, rgb_region, source_frame, prior=None):
        """Segment one frame for the background propagation worker."""
        return self._segment_region(rgb_region, int(source_frame),
                                    self._analysis_settings(), prior=prior)[0]

    def raw_matte(self, rgb_region, source_frame, key=None):
        """The unshaped matte for one source frame, from cache or segmentation."""
        entry = self.clip_entry or {}
        key = key or mask_key(entry)
        cache = MaskCache()
        clip = self.clip
        cached = cache.load(clip, source_frame, key) if clip is not None else None
        if cached is not None:
            cached = np.asarray(cached, np.float32)
            if rgb_region is not None and cached.shape[:2] != rgb_region.shape[:2]:
                cached = cv2.resize(cached, (rgb_region.shape[1], rgb_region.shape[0]),
                                    interpolation=cv2.INTER_LINEAR)
            return np.clip(cached, 0.0, 1.0)

        if rgb_region is None or rgb_region.size == 0:
            return None
        settings = self._analysis_settings()
        matte, backend = self._segment_region(rgb_region, source_frame, settings)
        self.last_backend = backend
        if matte is None:
            if backend == "basic":
                self.last_note = ("No mask on this frame yet. Paint the subject, then use "
                                  "Propagate Forward or Analyze Clip.")
            else:
                self.last_note = "The segmenter did not find a subject on this frame."
            return None
        note = basic_fallback_note(settings)
        if backend == "u2net":
            self.last_note = ""
        elif note:
            self.last_note = note
        else:
            self.last_note = ""
        return np.clip(matte, 0.0, 1.0).astype(np.float32)

    def matte_for_region(self, rgb_region, source_frame):
        entry = self.clip_entry or {}
        key = mask_key(entry)
        memo_key = (int(source_frame), key,
                    rgb_region.shape[1] if rgb_region is not None else 0,
                    rgb_region.shape[0] if rgb_region is not None else 0)
        cached = self._memo_get(memo_key)
        if cached is not None:
            return cached
        matte = self.raw_matte(rgb_region, source_frame, key)
        if matte is None:
            return None
        return self._memo_put(memo_key, matte)

    # ------------------------------------------------------------------ rendering
    def _view(self):
        if bool(getattr(self.app, "exporting", False)):
            return "composite"
        view = str(self.matte_view.get() or "composite")
        return view if view in VISIBLE_VIEWS + ("solid",) else "composite"

    def _checkerboard(self, height, width):
        tile = max(4, int(round(16 * float(self.param_scale))))
        yy, xx = np.mgrid[0:height, 0:width]
        board = (((yy // tile) + (xx // tile)) % 2).astype(np.float32)
        light = np.array([58.0, 58.0, 58.0], np.float32)
        dark = np.array([40.0, 40.0, 40.0], np.float32)
        return (light[None, None, :] * board[:, :, None]
                + dark[None, None, :] * (1.0 - board[:, :, None]))

    def _compose(self, layer, matte, frame):
        height, width = layer.shape[:2]
        alpha = layer[:, :, 3].astype(np.float32) / 255.0
        transparency = max(0.0, min(1.0, param_number(self.transparency, frame) / 100.0))
        view = self._view()
        out = np.empty_like(layer)
        if view in ("original", "outline"):
            return layer
        if view == "alpha":
            grey = (np.clip(matte, 0.0, 1.0) * 255.0).astype(np.uint8)
            out[:, :, :3] = np.repeat(grey[:, :, None], 3, axis=2)
            out[:, :, 3] = 255
            return out
        if view in ("solid", "checkerboard", "isolated"):
            if view == "solid":
                colour = np.array([_colour(self.view_b.get()), _colour(self.view_g.get()),
                                   _colour(self.view_r.get())], np.float32)
            elif view == "checkerboard":
                colour = self._checkerboard(height, width)
            else:
                colour = np.array([24.0, 26.0, 30.0], np.float32)
            weight = np.clip(matte, 0.0, 1.0)[:, :, None]
            out[:, :, :3] = np.clip(layer[:, :, :3] * weight + colour * (1.0 - weight),
                                    0.0, 255.0).astype(np.uint8)
            out[:, :, 3] = 255
            return out
        colour = layer[:, :, :3]
        cleanup = float(param_number(self.decontaminate, frame))
        if cleanup > 0.01:
            colour = decontaminate(colour, matte, cleanup)
        out[:, :, :3] = colour
        if transparency > 0.0:
            out[:, :, 3] = np.clip(alpha * (1.0 - transparency * (1.0 - matte)), 0.0, 1.0)\
                .__mul__(255.0).astype(np.uint8)
        else:
            out[:, :, 3] = layer[:, :, 3]
        return out

    def apply_layer(self, layer):
        if layer is None or layer.ndim != 3 or layer.shape[2] < 4 or layer.size == 0:
            return layer
        view = self._view()
        transparency = max(0.0, min(1.0, param_number(self.transparency, self.local_frame)
                                    / 100.0))
        if view in ("original", "outline"):
            return layer
        if transparency <= 0.0 and view == "composite":
            return layer
        height, width = layer.shape[:2]
        region = self._region(width, height)
        if region is None:
            return layer
        x0, y0, x1, y1 = region
        rgb_region = layer[y0:y1, x0:x1, :3]
        source_frame = self.source_frame()
        raw = self.matte_for_region(rgb_region, source_frame)
        if raw is None:
            return layer
        settings = {
            "smooth": float(param_number(self.smooth, self.local_frame)),
            "feather": float(param_number(self.feather, self.local_frame)),
            "choke": float(param_number(self.choke, self.local_frame)),
            "edge_contrast": float(param_number(self.edge_contrast, self.local_frame)),
        }
        shaped = finish_matte(raw, settings, scale=max(0.05, float(self.param_scale)))
        matte = np.zeros((height, width), np.float32)
        matte[y0:y1, x0:x1] = cv2.resize(shaped, (x1 - x0, y1 - y0),
                                         interpolation=cv2.INTER_LINEAR)
        return self._compose(layer, matte, self.local_frame)
