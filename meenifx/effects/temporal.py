"""Time effects: posterize time, echo/trail and stutter.

Posterize time and stutter change *which* source frame a clip shows - that decision is
made by the engine (`EditorEngine.held_frame`), so the whole pipeline (picture,
effects, transform) freezes together and the export matches the preview exactly.
Echo samples the clip's own earlier source frames, which is deterministic no matter
what order frames are rendered in.

Posterize Time is expressed as a **Frame Rate** in fps, not as a number of frames per
hold: the hold length then follows the project's own rate instead of breaking when the
project frame rate changes. Projects saved before that control stored ``step`` (frames
per hold); `migrate_timeline` converts them on load with the project's rate, so their
picture holds for exactly the same frames it always did.
"""

from __future__ import annotations

import math

import numpy as np

import cv2

from ..model.keyframes import AnimatedValue, Keyframe, parameter_value_at
from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number

DEFAULT_FPS = 12.0


def _fps_from_step(step, project_fps):
    """Frame rate represented by one old-style Step value (frames per hold)."""
    try:
        step = float(step)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return max(0.01, float(project_fps or 30.0) / max(1.0, step))


def frame_rate_at(params, frame, project_fps=30.0):
    """The Posterize Time rate (fps) of one entry, at a clip-local frame.

    ``fps`` is the control; an entry saved with the old ``step`` is still honoured, so a
    project that has not been through `migrate_timeline` cannot render differently.
    """
    params = params or {}
    stored = params.get("fps")
    if stored is None:
        stored = params.get("step")
        if stored is None:
            return DEFAULT_FPS
        try:
            return _fps_from_step(parameter_value_at(stored, frame), project_fps)
        except (TypeError, ValueError):
            return DEFAULT_FPS
    try:
        return float(parameter_value_at(stored, frame))
    except (TypeError, ValueError):
        return DEFAULT_FPS


def posterize_hold(frame, params, project_fps=30.0):
    """The clip-local frame a Posterize Time entry samples at ``frame``.

    With a fixed rate the hold comes from the *index* of the window
    (``floor(t * fps / project)``) and the sample from that index
    (``round(index * project / fps)``), so a fractional rate never accumulates drift:
    60 fps / 12 fps holds 0..4, 5..9, 10..14, while 30 fps / 7 fps alternates four and
    five frame holds and still lands exactly on frame 30 at the end.

    A keyframed rate advances from one window start to the next by that start's own
    window length in fractional frames. The start sequence depends only on the curve and
    the offset, so it is monotonic - a rising rate can never make the picture jump
    backwards - and it never drifts. A rate at or above the project's own rate adds no
    hold, and the mapping only changes *which* picture is shown: clip time, duration and
    audio are never touched.
    """
    frame = max(0, int(frame))
    project = float(project_fps or 30.0)
    params = params or {}
    try:
        offset = int(round(parameter_value_at(params.get("offset", 0), frame)))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    if frame < offset:
        return 0
    animated = isinstance(params.get("fps", params.get("step")), AnimatedValue)
    if not animated:
        fps = frame_rate_at(params, frame, project)
        if not 0.0 < fps < project:
            return frame
        index = int(math.floor((frame - offset) * fps / project + 1e-9))
        sample = offset + int(round(index * project / fps))
        return max(0, min(sample, frame))
    boundary = float(offset)
    while True:
        rate = frame_rate_at(params, int(round(boundary)), project)
        if not 0.0 < rate < project:
            return frame
        following = boundary + project / rate
        if following > frame:
            return max(0, min(int(round(boundary)), frame))
        boundary = following


def _converted_fps(value, project_fps):
    """One stored Step value (or curve) rewritten as Frame Rate, keyframes included."""
    if isinstance(value, AnimatedValue):
        converted = AnimatedValue()
        if value.base_value is not None:
            converted.base_value = _fps_from_step(value.base_value, project_fps)
        converted.keyframes = [
            Keyframe(key.time, _fps_from_step(key.value, project_fps), key.interpolation,
                     key.in_influence, key.out_influence, key.in_value_handle,
                     key.out_value_handle)
            for key in value.keyframes
        ]
        return converted
    return _fps_from_step(value, project_fps)


def migrate_effect_params(entry, project_fps=30.0):
    """Convert one saved Posterize Time entry from Step to Frame Rate, in place.

    Nothing is discarded: the hold length is preserved through the project's own frame
    rate, and a keyframed Step curve becomes a keyframed Frame Rate curve with the same
    keyframe times. Returns True when the entry changed.
    """
    params = (entry or {}).get("params")
    if not isinstance(params, dict) or "step" not in params or "fps" in params:
        return False
    params["fps"] = _converted_fps(params.pop("step"), project_fps)
    return True


def migrate_timeline(timeline):
    """Migrate every legacy Posterize Time on a loaded timeline; returns the count."""
    fps = float(getattr(timeline, "fps", 30.0) or 30.0)
    clips = timeline.all_clips() if hasattr(timeline, "all_clips") else (
        clip for track in getattr(timeline, "tracks", ()) for clip in track.clips)
    changed = 0
    for clip in clips:
        for entry in getattr(clip, "effects", None) or ():
            if entry.get("effect_id") == "posterize_time" and migrate_effect_params(entry, fps):
                changed += 1
    return changed


class PosterizeTimeEffect(LayerEffect):
    """Hold the picture at a chosen frame rate: a lower sampling rate, not a speed change."""

    key = "posterize_time"
    name = "Posterize Time"
    category = "Time"

    PARAMS = (
        ParamSpec("fps", "Frame Rate", "float", 1.0, 60.0, DEFAULT_FPS, step=0.1,
                  unit=" fps",
                  help="Pictures per second the layer is sampled at - a sampling rate, "
                       "not playback speed"),
        ParamSpec("offset", "Offset", "int", 0, 59, 0, unit=" f",
                  help="Shift where each held picture begins on the timeline"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.fps = Value(DEFAULT_FPS)
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
