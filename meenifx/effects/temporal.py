"""Time effects: posterize time, echo/trail and stutter.

Posterize time and stutter change *which* source frame a clip shows - that decision is
made by the engine (`EditorEngine.held_frame`), so the whole pipeline (picture,
effects, transform) freezes together and the export matches the preview exactly.
Echo samples the clip's own earlier source frames, which is deterministic no matter
what order frames are rendered in.
"""

from __future__ import annotations

import numpy as np

import cv2

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number


class PosterizeTimeEffect(LayerEffect):
    """Hold the clip's picture for N frames: a lower effective frame rate."""

    key = "posterize_time"
    name = "Posterize Time"
    category = "Time"

    PARAMS = (
        ParamSpec("step", "Step", "int", 1, 12, 3, unit=" f",
                  help="Show each source frame for this many frames"),
        ParamSpec("offset", "Offset", "int", 0, 11, 0, unit=" f",
                  help="Shift where the held frames fall on the timeline"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.step = Value(3)
        self.offset = Value(0)

    def apply_layer(self, layer):
        # The engine samples the held frame; nothing to do to the pixels themselves.
        return layer


class StutterEffect(LayerEffect):
    """Freeze, then jump forward: a stuttering playback with an adjustable hold."""

    key = "stutter"
    name = "Stutter"
    category = "Time"

    PARAMS = (
        ParamSpec("interval", "Interval", "int", 2, 40, 12, unit=" f",
                  help="Length of one stutter cycle"),
        ParamSpec("hold", "Hold", "int", 1, 40, 4, unit=" f",
                  help="Frames held still inside each cycle"),
        ParamSpec("offset", "Offset", "int", 0, 39, 0, unit=" f", advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.interval = Value(12)
        self.hold = Value(4)
        self.offset = Value(0)

    def apply_layer(self, layer):
        return layer


class EchoEffect(LayerEffect):
    """Frame-blended trail built from the clip's own earlier frames."""

    key = "echo"
    name = "Echo / Trail"
    category = "Time"

    PARAMS = (
        ParamSpec("count", "Echoes", "int", 1, 8, 3, help="How many earlier frames are blended in"),
        ParamSpec("step", "Spacing", "int", 1, 12, 2, unit=" f", help="Frames between echoes"),
        ParamSpec("decay", "Decay", "float", 0.0, 100.0, 55.0, step=1.0, unit="%",
                  help="How quickly each older echo fades"),
        ParamSpec("offset_x", "Drift X", "float", -80.0, 80.0, 0.0, step=0.5, unit=" px",
                  pixels=True, advanced=True),
        ParamSpec("offset_y", "Drift Y", "float", -80.0, 80.0, 0.0, step=0.5, unit=" px",
                  pixels=True, advanced=True),
        ParamSpec("blend", "Blend", "choice", choices=("screen", "add"), default="screen",
                  choice_labels=("Screen", "Add"), advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.count = Value(3)
        self.step = Value(2)
        self.decay = Value(55.0)
        self.offset_x = Value(0.0)
        self.offset_y = Value(0.0)
        self.blend = Value("screen")

    def _sample(self, local_frame, size):
        """The clip's own picture at an earlier local frame, fitted to the layer."""
        clip = getattr(self, "clip", None)
        if clip is None or clip.is_text or not clip.source_path or not hasattr(self.app, "media"):
            return None
        absolute = int(clip.timeline_start) + int(local_frame)
        try:
            source = self.app.media.frame_for_clip(clip, absolute, self.app.timeline.fps)
        except Exception:
            source = None
        if source is None:
            return None
        fitted = self.app.fit_to_project(source)
        if fitted.shape[:2] != (size[1], size[0]):
            fitted = cv2.resize(fitted, size, interpolation=cv2.INTER_AREA)
        return fitted

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        count = max(1, int(self.count.get()))
        step = max(1, int(self.step.get()))
        decay = max(0.0, param_number(self.decay, frame)) / 100.0
        height, width = layer.shape[:2]
        offset_x = param_number(self.offset_x, frame) * max(0.02, float(self.param_scale))
        offset_y = param_number(self.offset_y, frame) * max(0.02, float(self.param_scale))
        light = np.zeros((height, width, 3), np.float32)
        found = False
        for index in range(1, count + 1):
            earlier = frame - index * step
            if earlier < 0:
                continue
            sample = self._sample(earlier, (width, height))
            if sample is None:
                continue
            found = True
            weight = decay ** index
            if weight <= 0.004:
                continue
            shift_x = int(round(offset_x * index))
            shift_y = int(round(offset_y * index))
            if shift_x or shift_y:
                matrix = np.array([[1.0, 0.0, shift_x], [0.0, 1.0, shift_y]], np.float32)
                sample = cv2.warpAffine(sample, matrix, (width, height), flags=cv2.INTER_LINEAR,
                                        borderMode=cv2.BORDER_REPLICATE)
            light += sample.astype(np.float32) * weight
        if not found or not light.any():
            return layer
        base = layer[:, :, :3].astype(np.float32)
        if str(self.blend.get()) == "add":
            mixed = base + light
        else:
            mixed = 255.0 - (255.0 - np.clip(base, 0.0, 255.0)) * \
                (255.0 - np.clip(light, 0.0, 255.0)) / 255.0
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(mixed, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


TEMPORAL_EFFECTS = (PosterizeTimeEffect, StutterEffect, EchoEffect)
