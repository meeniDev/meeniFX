"""Roto Brush: a stroke-guided matte that can be refined by GrabCut.

The preview tool writes normalised brush strokes into the effect entry. Foreground strokes
are green and background strokes are red; the effect seeds a colour model and, when
possible, OpenCV GrabCut with those strokes. A centred starting seed makes a newly added
effect visible and inspectable before the first brush stroke; it is replaced as soon as the
user paints.

Everything is clip-local and deterministic. The strokes are ordinary JSON data on the
effect entry, so undo, copy/paste, project save and `.mfxpf` reload preserve them.
"""

from __future__ import annotations

import hashlib

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number

MATTE_MODES = ("composite", "alpha", "black_white")
MATTE_LABELS = ("Composite", "Alpha matte", "Black / white matte")
QUALITIES = ("draft", "balanced", "high")
QUALITY_LABELS = ("Draft", "Balanced", "High")
_QUALITY = {"draft": (0.5, 2), "balanced": (1.0, 4), "high": (1.0, 6)}


def _smoothstep(edge0, edge1, value):
    width = max(1e-5, float(edge1) - float(edge0))
    t = np.clip((value - float(edge0)) / width, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


class RotoBrushEffect(LayerEffect):
    """Build a soft alpha matte from foreground/background brush strokes."""

    key = "roto_brush"
    name = "Roto Brush"
    category = "Keying & Masking"
    NOTE = ("Paint green strokes on the subject and Alt-drag red strokes on the background. "
            "The strokes are saved with the clip; no online model or extra download is needed.")

    PARAMS = (
        ParamSpec("tolerance", "Colour tolerance", "float", 1.0, 100.0, 42.0, step=1.0,
                  unit="%", help="How far the matte may follow colours sampled by the strokes"),
        ParamSpec("feather", "Edge feather", "float", 0.0, 40.0, 2.0, step=0.5, unit=" px",
                  pixels=True, help="Soften the finished matte edge"),
        ParamSpec("choke", "Erode / expand", "float", -30.0, 30.0, 0.0, step=0.5,
                  unit=" px", pixels=True,
                  help="Positive pulls the matte inward; negative grows it outward"),
        ParamSpec("cleanup", "Matte cleanup", "float", 0.0, 100.0, 12.0, step=1.0,
                  unit="%", help="Remove speckles and close small holes in the matte"),
        ParamSpec("transparency", "Transparency", "float", 0.0, 100.0, 100.0, step=1.0,
                  unit="%", help="How much of the unmasked background is removed"),
        ParamSpec("auto_seed", "Starting seed", "bool", default=True,
                  help="Use a centred starting mask before the first brush stroke"),
        ParamSpec("refine", "Edge refine", "bool", default=True,
                  help="Use GrabCut to refine the brush strokes spatially"),
        ParamSpec("matte_view", "Matte view", "choice", default="composite",
                  choices=MATTE_MODES, choice_labels=MATTE_LABELS,
                  help="Inspect the matte instead of the composite"),
        ParamSpec("quality", "Quality", "choice", default="balanced",
                  choices=QUALITIES, choice_labels=QUALITY_LABELS,
                  help="Draft solves at half resolution; High runs more refinement passes"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.tolerance = Value(42.0)
        self.feather = Value(2.0)
        self.choke = Value(0.0)
        self.cleanup = Value(12.0)
        self.transparency = Value(100.0)
        self.auto_seed = Value(True)
        self.refine = Value(True)
        self.matte_view = Value("composite")
        self.quality = Value("balanced")
        self._cache_key = None
        self._cache_output = None
        self.last_note = ""

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

    def _project_to_layer(self, point, width, height):
        x0, y0, rect_w, rect_h = self._rect(width, height)
        try:
            nx, ny = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        return int(round(x0 + nx * rect_w)), int(round(y0 + ny * rect_h))

    def _stroke_masks(self, width, height):
        """(foreground, background) masks in layer pixels, or (None, None)."""
        entry = self.clip_entry or {}
        strokes = entry.get("strokes") or ()
        fg = np.zeros((height, width), np.uint8)
        bg = np.zeros((height, width), np.uint8)
        if not strokes:
            if not bool(self.auto_seed.get()):
                self.last_note = "No brush strokes yet. Paint the subject with the Roto Brush tool."
                return None, None
            # A useful starting point: the subject is normally around the frame centre.
            cv2.ellipse(fg, (width // 2, height // 2),
                        (max(2, int(width * 0.27)), max(2, int(height * 0.32))),
                        0, 0, 360, 255, -1)
            return fg, bg
        for stroke in strokes:
            if not isinstance(stroke, dict):
                continue
            points = stroke.get("points") or ()
            if not points:
                continue
            mode = str(stroke.get("mode") or "foreground")
            target = bg if mode == "background" else fg
            radius = max(1, int(round(float(stroke.get("radius", 0.055)) * min(width, height))))
            mapped = [self._project_to_layer(point, width, height) for point in points]
            mapped = [point for point in mapped if point is not None]
            if not mapped:
                continue
            if len(mapped) == 1:
                cv2.circle(target, mapped[0], radius, 255, -1)
                continue
            for a, b in zip(mapped, mapped[1:]):
                cv2.line(target, a, b, 255, max(1, radius * 2), cv2.LINE_AA)
            for point in mapped:
                cv2.circle(target, point, radius, 255, -1)
        # A background stroke is an explicit correction. If the two strokes overlap, the
        # background wins instead of the foreground maximum painting the hole back.
        fg[bg > 0] = 0
        return fg, bg

    # ---------------------------------------------------------------------- matte
    @staticmethod
    def _color_matte(lab, fg, bg, tolerance):
        """Soft matte from colour distance to the painted foreground/background samples."""
        fg_pixels = lab[fg > 0]
        bg_pixels = lab[bg > 0]
        if fg_pixels.size == 0:
            return np.zeros(lab.shape[:2], np.float32)
        fg_mean = fg_pixels.mean(axis=0)
        if bg_pixels.size == 0:
            # The border is a safe background sample when the user has painted only the
            # subject.  Ignore it when the subject actually touches the edge (the user can
            # paint a red stroke to correct that).
            border = np.zeros(lab.shape[:2], bool)
            border[:2, :] = border[-2:, :] = True
            border[:, :2] = border[:, -2:] = True
            bg_pixels = lab[border]
        bg_mean = bg_pixels.mean(axis=0) if bg_pixels.size else np.array([128.0, 128.0, 128.0])
        d_fg = np.linalg.norm(lab - fg_mean, axis=2)
        d_bg = np.linalg.norm(lab - bg_mean, axis=2)
        margin = max(1.0, float(tolerance) * 0.55)
        return _smoothstep(-margin, margin, d_bg - d_fg)

    def _matte(self, layer, frame, quality):
        height, width = layer.shape[:2]
        scale, iterations = _QUALITY.get(str(quality), _QUALITY["balanced"])
        work_w, work_h = max(1, int(round(width * scale))), max(1, int(round(height * scale)))
        rgb = layer[:, :, :3]
        if scale < 1.0:
            rgb = cv2.resize(rgb, (work_w, work_h), interpolation=cv2.INTER_AREA)
        # Build the stroke masks at layer resolution, then resample them with the picture.
        # Brush coordinates are stored in project space, so the letterbox rect is applied
        # before the working-resolution resize.
        fg, bg = self._stroke_masks(width, height)
        if fg is None:
            return None
        if scale < 1.0:
            fg = cv2.resize(fg, (work_w, work_h), interpolation=cv2.INTER_NEAREST)
            bg = cv2.resize(bg, (work_w, work_h), interpolation=cv2.INTER_NEAREST)
        tolerance = max(1.0, param_number(self.tolerance, frame))
        lab = cv2.cvtColor(rgb, cv2.COLOR_BGR2LAB).astype(np.float32)
        color = self._color_matte(lab, fg, bg, tolerance)
        if bool(self.refine.get()):
            gc = np.full((work_h, work_w), cv2.GC_PR_BGD, np.uint8)
            gc[bg > 0] = cv2.GC_BGD
            gc[fg > 0] = cv2.GC_FGD
            # GrabCut needs at least one sure pixel of each class. If the user painted only
            # the subject, the frame border supplies a conservative background seed.
            if not np.any(gc == cv2.GC_BGD):
                gc[:2, :] = gc[-2:, :] = cv2.GC_BGD
                gc[:, :2] = gc[:, -2:] = cv2.GC_BGD
                gc[fg > 0] = cv2.GC_FGD
            if np.any(gc == cv2.GC_FGD) and np.any(gc == cv2.GC_BGD):
                bg_model = np.zeros((1, 65), np.float64)
                fg_model = np.zeros((1, 65), np.float64)
                try:
                    cv2.grabCut(rgb, gc, None, bg_model, fg_model, iterations,
                                cv2.GC_INIT_WITH_MASK)
                    grab = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 1.0, 0.0)
                    # Blend the spatial refinement with the colour model. This keeps the
                    # brush's colour intent visible and makes tolerance useful even when
                    # GrabCut is confident.
                    matte = np.clip(grab * (0.55 + 0.45 * color), 0.0, 1.0)
                    matte = np.maximum(matte, (fg > 0).astype(np.float32))
                except cv2.error:
                    matte = color
            else:
                matte = color
        else:
            matte = color

        cleanup = max(0.0, min(100.0, param_number(self.cleanup, frame)))
        if cleanup > 0.01:
            binary = (matte >= 0.5).astype(np.uint8) * 255
            size = max(1, int(round(cleanup / 100.0 * 5.0)))
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                               (size * 2 + 1, size * 2 + 1))
            binary = cv2.medianBlur(binary, size * 2 + 1)
            binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
            binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
            matte = np.maximum(matte, binary.astype(np.float32) / 255.0)

        choke = param_number(self.choke, frame) * float(self.param_scale) * scale
        amount = int(round(abs(choke)))
        if amount > 0:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                               (min(61, amount * 2 + 1),) * 2)
            u8 = (matte * 255.0).astype(np.uint8)
            u8 = cv2.erode(u8, kernel) if choke > 0 else cv2.dilate(u8, kernel)
            matte = u8.astype(np.float32) / 255.0

        feather = max(0.0, param_number(self.feather, frame) * float(self.param_scale) * scale)
        if feather > 0.01:
            matte = cv2.GaussianBlur(matte, (0, 0), sigmaX=feather / 3.0,
                                     sigmaY=feather / 3.0,
                                     borderType=cv2.BORDER_REPLICATE)
        if scale < 1.0:
            matte = cv2.resize(matte, (width, height), interpolation=cv2.INTER_LINEAR)
        return np.clip(matte, 0.0, 1.0)

    def apply_layer(self, layer):
        if layer is None or layer.ndim != 3 or layer.shape[2] < 4 or layer.size == 0:
            return layer
        frame = int(self.local_frame)
        quality = str(self.quality.get() or "balanced")
        entry = self.clip_entry or {}
        strokes = entry.get("strokes") or ()
        values = (str(quality), round(param_number(self.tolerance, frame), 3),
                  round(param_number(self.feather, frame), 3),
                  round(param_number(self.choke, frame), 3),
                  round(param_number(self.cleanup, frame), 3),
                  round(param_number(self.transparency, frame), 3),
                  bool(self.auto_seed.get()), bool(self.refine.get()),
                  str(self.matte_view.get()), tuple(strokes), layer.shape,
                  round(float(self.param_scale), 4), self.layer_rect)
        digest = hashlib.blake2b(layer.tobytes(), digest_size=12).digest()
        cache_key = (values, digest)
        if cache_key == self._cache_key and self._cache_output is not None:
            return self._cache_output

        transparency = max(0.0, min(1.0, param_number(self.transparency, frame) / 100.0))
        if transparency <= 0.0:
            self._cache_key, self._cache_output = cache_key, layer
            return layer
        matte = self._matte(layer, frame, quality)
        if matte is None:
            self._cache_key, self._cache_output = cache_key, layer
            return layer

        alpha = layer[:, :, 3].astype(np.float32) / 255.0
        view = str(self.matte_view.get())
        if view == "alpha":
            rgb = np.repeat((matte * 255.0).astype(np.uint8)[:, :, None], 3, axis=2)
            out_alpha = np.ones_like(alpha)
        elif view == "black_white":
            binary = np.where(matte >= 0.5, 255, 0).astype(np.uint8)
            rgb = np.repeat(binary[:, :, None], 3, axis=2)
            out_alpha = np.ones_like(alpha)
        else:
            rgb = layer[:, :, :3]
            out_alpha = alpha * (1.0 - transparency * (1.0 - matte))
        out = np.empty_like(layer)
        out[:, :, :3] = rgb
        out[:, :, 3] = np.clip(out_alpha * 255.0, 0.0, 255.0).astype(np.uint8)
        self._cache_key, self._cache_output = cache_key, out
        return out
