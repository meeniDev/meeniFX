"""Roto Brush: stroke-guided rotoscoping with analysis, propagation and freeze.

How the tool works
------------------
The preview paints green (subject) and Alt-red (background) strokes into the effect entry;
those strokes are *hints*, never pixels. The brush backend segments the base frame from
them, the outline shows the real matte boundary, and propagation carries the mask across
the clip with optical flow so the picture is not cut out with one frozen shape.

Coordinates are normalised inside the picture rectangle the engine hands the effect, which
is what makes the matte survive moving, scaling or rotating the clip. Masks are keyed by
*source* frame, so trims, source offsets, speed and reverse playback stay correct.

Masks live in the per-clip disk cache (see `meenifx/roto/cache.py`), never in the project
JSON; the entry keeps only strokes, the base frame, analysis settings and small cache
metadata. A reopened project without its cache reports which frames need analysis instead
of silently showing a stale cutout.

Backend: the shipped segmenter is the classical GrabCut + guided-filter path from
`meenifx/roto/segment.py`. It is labelled as such in the UI and docs; it is not a learned
video-segmentation model.
"""

from __future__ import annotations

import hashlib

import cv2
import numpy as np

from ..model.values import Value
from ..roto import (MaskCache, decontaminate, finish_matte, mask_key, mask_size,
                    rasterise, segment_frame, strokes_for_frame)
from ..roto.segment import quality_settings
from .base import LayerEffect, ParamSpec, param_number

MATTE_VIEWS = ("composite", "checkerboard", "alpha", "solid")
MATTE_LABELS = ("Composite", "Transparent checkerboard", "Alpha matte", "Foreground on colour")
QUALITIES = ("draft", "balanced", "high")
QUALITY_LABELS = ("Draft (fast)", "Balanced", "High")
ANALYSIS_PARAMS = ("refine", "quality", "search_radius", "tolerance")
MAX_MEMO = 8


def _colour(value, fallback=0):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return max(0, min(255, number))


class RotoBrushEffect(LayerEffect):
    """A stroke-guided subject matte with per-frame analysis and propagation."""

    key = "roto_brush"
    name = "Roto Brush"
    category = "Keying & Masking"
    NOTE = ("Green strokes mark the subject, Alt-red strokes mark what to remove, and the "
            "Refine Edge brush helps difficult boundaries. Segmenter: classical GrabCut "
            "with guided-filter edge refinement (no model download).")

    PARAMS = (
        ParamSpec("smooth", "Smooth", "float", 0.0, 10.0, 1.0, step=0.5, unit=" px",
                  pixels=True, group="Matte",
                  help="Median smoothing of the matte before the edge is shaped"),
        ParamSpec("feather", "Feather", "float", 0.0, 40.0, 1.5, step=0.5, unit=" px",
                  pixels=True, group="Matte", help="Soften the finished matte edge"),
        ParamSpec("choke", "Choke / expand", "float", -30.0, 30.0, 0.0, step=0.5,
                  unit=" px", pixels=True, group="Matte",
                  help="Positive pulls the matte inward; negative grows it outward"),
        ParamSpec("edge_contrast", "Edge contrast", "float", 0.0, 100.0, 35.0, step=1.0,
                  unit=" %", group="Matte",
                  help="Make the alpha ramp steeper without moving the boundary much"),
        ParamSpec("decontaminate", "Edge colour cleanup", "float", 0.0, 100.0, 0.0,
                  step=1.0, unit=" %", group="Matte",
                  help="Pull the background colour out of the soft edge (composite view)"),
        ParamSpec("search_radius", "Search radius", "float", 4.0, 120.0, 24.0, step=1.0,
                  unit=" px", pixels=True, group="Analysis",
                  help="How far GrabCut may look around the strokes and the carried mask"),
        ParamSpec("tolerance", "Colour tolerance", "float", 1.0, 100.0, 28.0, step=1.0,
                  unit=" %", group="Analysis",
                  help="How far the colour model may follow the colours under the strokes"),
        ParamSpec("refine", "Refine with GrabCut", "bool", default=True, group="Analysis",
                  help="Fit the strokes (and each propagated mask) to the real pixels"),
        ParamSpec("quality", "Quality", "choice", default="balanced",
                  choices=QUALITIES, choice_labels=QUALITY_LABELS, group="Analysis",
                  help="Draft analyses at half size; High runs more refinement passes"),
        ParamSpec("transparency", "Transparency", "float", 0.0, 100.0, 100.0, step=1.0,
                  unit=" %", help="How much of the unmasked background is removed"),
        ParamSpec("matte_view", "Preview", "choice", default="composite",
                  choices=MATTE_VIEWS, choice_labels=MATTE_LABELS,
                  help="Inspect the cutout, its alpha, or the subject on a solid colour"),
        ParamSpec("view_r", "Background red", "int", 0, 255, 24, group="Preview",
                  help="Solid colour behind the subject in the solid preview"),
        ParamSpec("view_g", "Background green", "int", 0, 255, 26, group="Preview",
                  help="Solid colour behind the subject in the solid preview"),
        ParamSpec("view_b", "Background blue", "int", 0, 255, 30, group="Preview",
                  help="Solid colour behind the subject in the solid preview"),
        ParamSpec("auto_seed", "Auto seed", "bool", default=False, advanced=True,
                  help="Segment a centred region when no stroke has been painted yet"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.smooth = Value(1.0)
        self.feather = Value(1.5)
        self.choke = Value(0.0)
        self.edge_contrast = Value(35.0)
        self.decontaminate = Value(0.0)
        self.search_radius = Value(24.0)
        self.tolerance = Value(28.0)
        self.refine = Value(True)
        self.quality = Value("balanced")
        self.transparency = Value(100.0)
        self.matte_view = Value("composite")
        self.view_r = Value(24)
        self.view_g = Value(26)
        self.view_b = Value(30)
        self.auto_seed = Value(False)
        self.last_note = ""
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
        """The picture's pixels inside the layer, or None when it is off screen."""
        x, y, rect_w, rect_h = self._rect(width, height)
        x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
        x1, y1 = min(width, int(round(x + rect_w))), min(height, int(round(y + rect_h)))
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def source_frame(self):
        """The clip's *source* frame for the frame being rendered.

        `local_frame` is on the clip's own clock; `source_frame_at` is what maps trims,
        source offsets, speed and reverse playback onto the media. Masks are stored by
        this index, never by the timeline frame.
        """
        clip = self.clip
        local = int(self.local_frame)
        if clip is None or not hasattr(clip, "source_frame_at"):
            return max(0, local)
        try:
            return max(0, int(clip.source_frame_at(int(getattr(clip, "timeline_start", 0)) + local)))
        except Exception:                                # noqa: BLE001 - keep rendering
            return max(0, local)

    # ------------------------------------------------------------------ analysis
    def _analysis_settings(self):
        quality = quality_settings(str(self.quality.get()))
        return {
            "refine": bool(self.refine.get()),
            "search_radius": float(self.search_radius.get()),
            "iterations": int(quality["iterations"]),
            "band": float(quality["band"]),
            "scale": float(quality["scale"]),
            "tolerance": max(1.0, float(self.tolerance.get())),
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

    def raw_matte(self, rgb_region, source_frame, key=None):
        """The unshaped matte for one source frame, from the cache or from the strokes.

        Returns a float32 0..1 array at the region's mask size, or None when there is
        nothing to analyse yet. Never invents a mask for an unanalysed frame: a static
        shape copied over the whole clip would look like a cutout and be a lie.
        """
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
        strokes = strokes_for_frame(entry.get("strokes") or (), source_frame,
                                    entry.get("base_frame", 0))
        if not strokes and bool(self.auto_seed.get()):
            # An honest fallback for an effect dropped in with no strokes at all: a
            # centred seed, replaced the moment the user paints. Off by default.
            strokes = [{"frame": source_frame, "mode": "foreground", "radius": 0.28,
                        "points": [[0.5, 0.45], [0.5, 0.55]]}]
        if not strokes:
            self.last_note = ("No mask on this frame yet. Paint the base frame, then use "
                              "Propagate Forward or Analyze Entire Clip.")
            return None
        settings = self._analysis_settings()
        scale = float(settings["scale"])
        height, width = rgb_region.shape[:2]
        work_w = max(8, int(round(width * scale)))
        work_h = max(8, int(round(height * scale)))
        work = rgb_region if (work_w, work_h) == (width, height) else cv2.resize(
            rgb_region, (work_w, work_h), interpolation=cv2.INTER_AREA)
        foreground, background, refine_mask = rasterise(strokes, work_w, work_h,
                                                        (0.0, 0.0, float(work_w), float(work_h)))
        matte = segment_frame(work, foreground, background, refine_mask, settings)
        if matte is None:
            self.last_note = "The strokes did not produce a matte; paint inside the subject."
            return None
        self.last_note = ""
        if (work_w, work_h) != (width, height):
            matte = cv2.resize(matte, (width, height), interpolation=cv2.INTER_LINEAR)
        return np.clip(matte, 0.0, 1.0).astype(np.float32)

    def matte_for_region(self, rgb_region, source_frame):
        """The raw matte plus the memoised lookup used by preview and export."""
        entry = self.clip_entry or {}
        key = mask_key(entry)
        memo_key = (int(source_frame), key, rgb_region.shape[1] if rgb_region is not None else 0,
                    rgb_region.shape[0] if rgb_region is not None else 0)
        cached = self._memo_get(memo_key)
        if cached is not None:
            return cached
        matte = self.raw_matte(rgb_region, source_frame, key)
        if matte is None:
            return None
        return self._memo_put(memo_key, matte)

    # ------------------------------------------------------------------ rendering
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
        view = str(self.matte_view.get() or "composite")
        out = np.empty_like(layer)
        if view == "alpha":
            grey = (np.clip(matte, 0.0, 1.0) * 255.0).astype(np.uint8)
            out[:, :, :3] = np.repeat(grey[:, :, None], 3, axis=2)
            out[:, :, 3] = 255
            return out
        if view == "solid":
            colour = np.array([_colour(self.view_b.get()), _colour(self.view_g.get()),
                               _colour(self.view_r.get())], np.float32)
            weight = np.clip(matte, 0.0, 1.0)[:, :, None]
            out[:, :, :3] = np.clip(layer[:, :, :3] * weight + colour * (1.0 - weight),
                                    0.0, 255.0).astype(np.uint8)
            out[:, :, 3] = 255
            return out
        if view == "checkerboard":
            board = self._checkerboard(height, width)
            weight = np.clip(matte, 0.0, 1.0)[:, :, None]
            out[:, :, :3] = np.clip(layer[:, :, :3] * weight + board * (1.0 - weight),
                                    0.0, 255.0).astype(np.uint8)
            out[:, :, 3] = 255
            return out
        colour = layer[:, :, :3]
        cleanup = float(param_number(self.decontaminate, frame))
        if cleanup > 0.01:
            colour = decontaminate(colour, matte, cleanup)
        out[:, :, :3] = colour
        if transparency > 0.0:
            out[:, :, 3] = np.clip(alpha * (1.0 - transparency * (1.0 - matte)), 0.0, 1.0) \
                .__mul__(255.0).astype(np.uint8)
        else:
            out[:, :, 3] = layer[:, :, 3]
        return out

    def apply_layer(self, layer):
        if layer is None or layer.ndim != 3 or layer.shape[2] < 4 or layer.size == 0:
            return layer
        transparency = max(0.0, min(1.0, param_number(self.transparency, self.local_frame) / 100.0))
        view = str(self.matte_view.get() or "composite")
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
        # Pixel-based shaping must shrink with the render scale, or a half-quality preview
        # would feather/choke twice as much as the full-quality export.
        shaped = finish_matte(raw, settings, scale=max(0.05, float(self.param_scale)))
        matte = np.zeros((height, width), np.float32)
        matte[y0:y1, x0:x1] = cv2.resize(shaped, (x1 - x0, y1 - y0),
                                         interpolation=cv2.INTER_LINEAR)
        return self._compose(layer, matte, self.local_frame)
