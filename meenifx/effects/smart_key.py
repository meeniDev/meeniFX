"""Smart Key: automatic subject isolation from an ordinary picture.

Smart Key is not Chroma Key. Chroma Key removes one chosen screen colour; Smart Key asks
the optional U^2-Net model to find the main foreground subject and removes everything
else, even when the background is a normal multicoloured room, street or garden. There is
no key colour, no colour picker and no brush stroke in the default workflow.

The matte is applied as the layer's alpha channel. Composite preview and export therefore
show clips underneath through the removed background. Isolated and Matte are diagnostic
views; export is always forced back to Composite.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..model.values import Value
from ..roto import (MaskCache, finish_matte, mask_key, mask_size, model_status,
                    segment_subject)
from ..roto.segment import quality_settings
from .base import LayerEffect, ParamSpec

PREVIEWS = ("composite", "isolated", "alpha")
PREVIEW_LABELS = ("Composite", "Isolated", "Matte")
QUALITIES = ("draft", "balanced", "high")
QUALITY_LABELS = ("Fast", "Balanced", "High")
MAX_MEMO = 8


class SmartKeyEffect(LayerEffect):
    """Automatic foreground-subject isolation with the optional U^2-Net model."""

    key = "smart_key"
    name = "Smart Key"
    category = "Keying & Masking"
    NOTE = ("Automatically finds the main subject with the optional U^2-Net model. "
            "No screen colour, colour picker or brush stroke is needed. Use Remove "
            "Background in the card to analyse the whole clip.")

    PARAMS = (
        ParamSpec("subject", "Subject", "choice", default="auto",
                  choices=("auto", "choose"), choice_labels=("Auto", "Choose"),
                  help="Auto keeps the detected main subject; Choose then click a "
                       "subject in the preview"),
        ParamSpec("quality", "Quality", "choice", default="balanced",
                  choices=QUALITIES, choice_labels=QUALITY_LABELS,
                  help="Fast analyses at half size. High adds a flip-averaged model pass"),
        ParamSpec("smooth", "Edge Smoothness", "float", 0.0, 10.0, 1.0, step=0.5,
                  unit=" px", pixels=True,
                  help="Smooth the matte before its edge is shaped"),
        ParamSpec("feather", "Feather", "float", 0.0, 40.0, 1.5, step=0.5, unit=" px",
                  pixels=True, help="Soften the finished matte edge"),
        ParamSpec("choke", "Expand / Shrink", "float", -30.0, 30.0, 0.0, step=0.5,
                  unit=" px", pixels=True,
                  help="Positive expands the matte outward; negative shrinks it inward"),
        ParamSpec("matte_view", "Preview", "choice", default="composite",
                  choices=PREVIEWS, choice_labels=PREVIEW_LABELS,
                  help="Inspect the composite, the isolated subject or the raw matte"),
        ParamSpec("edge_contrast", "Edge Refinement", "float", 0.0, 100.0, 25.0,
                  step=1.0, unit=" %", advanced=True,
                  help="Steepen the decision at the boundary without moving it far"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.subject = Value("auto")
        self.quality = Value("balanced")
        self.smooth = Value(1.0)
        self.feather = Value(1.5)
        self.choke = Value(0.0)
        self.matte_view = Value("composite")
        self.edge_contrast = Value(25.0)
        self.last_note = ""
        self.last_backend = "unavailable"
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
            return max(0, int(clip.source_frame_at(
                int(getattr(clip, "timeline_start", 0)) + local)))
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
        quality = quality_settings(str(self.quality.get() or "balanced"))
        return {
            # Smart Key is model-only on purpose. Falling back to a colour key or to an
            # unseeded GrabCut pass would be the chroma behaviour this effect replaces.
            "backend": "model",
            "quality": str(self.quality.get() or "balanced"),
            "scale": float(quality["scale"]),
            "iterations": int(quality["iterations"]),
            "band": float(quality["band"]),
            "tolerance": 28.0,
            "search_radius": 24.0,
            "refine": False,
            "subject_point": self._subject_point(),
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

    def segment_frame(self, rgb_region, source_frame, prior=None):
        """Segment one frame for the background analysis worker."""
        if rgb_region is None or rgb_region.size == 0:
            return None
        if not model_status().available:
            self.last_note = ("The AI subject model is not installed. Use Remove "
                              "Background to download it.")
            return None
        height, width = rgb_region.shape[:2]
        work_w, work_h = mask_size(width, height)
        work = (cv2.resize(rgb_region, (work_w, work_h), interpolation=cv2.INTER_AREA)
                if (work_w, work_h) != (width, height) else rgb_region)
        previous = prior
        if previous is not None and previous.shape[:2] != (work_h, work_w):
            previous = cv2.resize(previous, (work_w, work_h),
                                  interpolation=cv2.INTER_LINEAR)
        matte, backend = segment_subject(
            work, None, None, None, self._analysis_settings(),
            subject_point=self._subject_point(), prior=previous)
        self.last_backend = backend
        if matte is None or backend != "u2net":
            self.last_note = ("The AI subject model found no subject on this frame.")
            return None
        if (work_w, work_h) != (width, height):
            matte = cv2.resize(matte, (width, height), interpolation=cv2.INTER_LINEAR)
        self.last_note = ""
        return np.clip(matte, 0.0, 1.0).astype(np.float32)

    def raw_matte(self, rgb_region, source_frame, key=None):
        """The unshaped matte for one frame, from the Smart Key cache or the model."""
        entry = self.clip_entry or {}
        key = key or mask_key(entry)
        cache = MaskCache(scope="smart_key")
        cached = cache.load(self.clip, source_frame, key) if self.clip is not None else None
        if cached is not None:
            cached = np.asarray(cached, np.float32)
            if rgb_region is not None and cached.shape[:2] != rgb_region.shape[:2]:
                cached = cv2.resize(cached, (rgb_region.shape[1], rgb_region.shape[0]),
                                    interpolation=cv2.INTER_LINEAR)
            return np.clip(cached, 0.0, 1.0)
        if rgb_region is None or rgb_region.size == 0:
            return None
        return self.segment_frame(rgb_region, source_frame)

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
        return view if view in PREVIEWS else "composite"

    def _compose(self, layer, matte):
        height, width = layer.shape[:2]
        view = self._view()
        out = np.empty_like(layer)
        if view == "alpha":
            grey = (np.clip(matte, 0.0, 1.0) * 255.0).astype(np.uint8)
            out[:, :, :3] = np.repeat(grey[:, :, None], 3, axis=2)
            out[:, :, 3] = 255
            return out
        if view == "isolated":
            weight = np.clip(matte, 0.0, 1.0)[:, :, None]
            colour = np.array([24.0, 26.0, 30.0], np.float32)
            out[:, :, :3] = np.clip(layer[:, :, :3] * weight + colour * (1.0 - weight),
                                    0.0, 255.0).astype(np.uint8)
            out[:, :, 3] = 255
            return out
        out[:, :, :3] = layer[:, :, :3]
        alpha = layer[:, :, 3].astype(np.float32) / 255.0
        out[:, :, 3] = np.clip(alpha * np.clip(matte, 0.0, 1.0) * 255.0,
                               0.0, 255.0).astype(np.uint8)
        return out

    def apply_layer(self, layer):
        if layer is None or layer.ndim != 3 or layer.shape[2] < 4 or layer.size == 0:
            return layer
        height, width = layer.shape[:2]
        region = self._region(width, height)
        if region is None:
            return layer
        x0, y0, x1, y1 = region
        rgb_region = layer[y0:y1, x0:x1, :3]
        raw = self.matte_for_region(rgb_region, self.source_frame())
        if raw is None:
            return layer
        shaped = finish_matte(raw, {
            "smooth": float(self.smooth.get()),
            "feather": float(self.feather.get()),
            "choke": float(self.choke.get()),
            "edge_contrast": float(self.edge_contrast.get()),
        }, scale=max(0.05, float(self.param_scale)))
        matte = np.zeros((height, width), np.float32)
        matte[y0:y1, x0:x1] = cv2.resize(shaped, (x1 - x0, y1 - y0),
                                         interpolation=cv2.INTER_LINEAR)
        return self._compose(layer, matte)
