"""Toolkit-independent media, effect, frame composition and export orchestration."""

import os
import threading

import cv2
import imageio.v2 as imageio
import numpy as np

from .config import DEFAULT_TILE_PATHS
from .audio.effects import AUDIO_EFFECTS
from .effects.builtins import AsciiEffect, BlobTrackerEffect, FaceTrackerEffect, HandTrackerEffect, MosaicEffect
from .effects.blur import (BoxBlurEffect, DirectionalBlurEffect, GaussianBlurEffect,
                           MotionBlurEffect, RadialBlurEffect, ZoomBlurEffect)
from .effects.color import ColorCorrectEffect
from .effects.glitch import (BulgePinchEffect, ChromaticAberrationEffect, GlitchEffect,
                             KaleidoscopeEffect, TwirlEffect, WaveWarpEffect)
from .effects.glow import GlowEffect
from .effects.light import (BloomStreaksEffect, ExposureFlickerEffect, FlashEffect,
                            LightLeakEffect)
from .effects.looks import LookEffect
from .effects.lut import LutEffect
from .effects.matte import SimpleChokerEffect
from .effects.stylize import (DuotoneEffect, EdgeOutlineEffect, FilmGrainEffect, HalftoneEffect,
                              InvertEffect, PosterizeEffect, VhsCrtEffect, VignetteEffect)
from .effects.temporal import EchoEffect, PosterizeTimeEffect, StutterEffect, posterize_hold
from .effects.warp import (CornerPinEffect, DistortionEffect, MeshWarpEffect,
                           PerspectiveEffect, PuppetWarpEffect, SkewEffect)
from .media.cache import MediaCache, MediaEntry
from .media.reader import load_default_tiles
from .model import transform as transforms
from .model.keyframes import parameter_value_at
from .model.session import TimelineSession
from .model.timeline import Timeline
from .model.values import Value
from .model import presets as preset_model
from .model import shape as shape_model
from .model import text as text_model
from .processing import layers as layer_ops
from .processing import shapes as shape_render
from .processing import text as text_render
from .processing import text_units as text_unit_render
from .processing.mosaic import build_palette


class EditorEngine:
    """Owns project data and processing state; never constructs GUI objects."""

    EFFECT_TYPES = (MosaicEffect, AsciiEffect, FaceTrackerEffect, HandTrackerEffect, BlobTrackerEffect,
                    ColorCorrectEffect, GaussianBlurEffect, BoxBlurEffect, DirectionalBlurEffect,
                    RadialBlurEffect, ZoomBlurEffect, MotionBlurEffect, GlowEffect,
                    PuppetWarpEffect, CornerPinEffect, PerspectiveEffect, MeshWarpEffect, SkewEffect,
                    DistortionEffect,
                    ChromaticAberrationEffect, GlitchEffect, WaveWarpEffect, BulgePinchEffect,
                    TwirlEffect, KaleidoscopeEffect,
                    VhsCrtEffect, FilmGrainEffect, HalftoneEffect, PosterizeEffect,
                    EdgeOutlineEffect, DuotoneEffect, InvertEffect, VignetteEffect,
                    FlashEffect, ExposureFlickerEffect, LightLeakEffect, BloomStreaksEffect,
                    PosterizeTimeEffect, StutterEffect, EchoEffect, LutEffect, LookEffect,
                    SimpleChokerEffect) + AUDIO_EFFECTS

    def __init__(self, fps=30):
        self.timeline = Timeline(fps=fps)
        self.media = MediaCache()
        # Preview-only rendering settings (proxy media, hardware decode, GPU compositing).
        self.proxy_auto = False
        self.proxy_width = 1280
        self.hardware_encode = "auto"
        self.last_export_encoder = "mp4v"
        self.session = TimelineSession(self.timeline, self.media)
        self.tile_images = load_default_tiles()
        self.palette_cache = {}
        self.effects = [kind(self) for kind in self.EFFECT_TYPES]
        self.runtime_effects = {effect.key: effect for effect in self.effects}
        for effect in self.effects:
            effect.on_registered()
            effect.defaults = effect.capture_params()
        self.source_width = 0
        self.source_height = 0
        self.source_fps = float(fps)
        self.source_path = ""
        self.current_frame = None
        self.current_processed = None
        self.current_source_index = 0
        self.current_fx = []
        self.current_media_path = ""
        self.current_clip_local_frame = 0
        self.preview_quality = Value(75)
        self.playback_fps = Value(30)
        self.speed_percent = Value(100)
        self.reverse_timeline = Value(False)
        self.source_mode = Value("file")
        self.camera_index = Value(0)
        self.render_use_source_fps = Value(True)
        self.render_fps = Value(30)
        self.render_use_source_size = Value(True)
        self.render_width = Value(1280)
        self.render_height = Value(720)
        self.render_output_path = Value("")
        self.render_format = Value("mp4")
        self.frame_use_source_size = Value(True)
        self.frame_width = Value(1280)
        self.frame_height = Value(720)
        self.frame_output_path = Value("")
        self.frame_format = Value("png")
        self.input_path = Value("")
        self._process_lock = threading.RLock()
        self.project_settings = None
        self.project_fps = float(fps)
        self.background_bgr = (0, 0, 0)
        self.missing_paths = set()
        # Effects switched off by the last load because their media type did not match.
        self.last_incompatible = []

    def apply_project_settings(self, settings, output_dir=None):
        """Make the project resolution, frame rate and background the canvas for preview, timeline and export.

        The timeline counts whole frames, so 29.97 runs as 30 there; export keeps the exact rate.
        """
        self.project_settings = settings
        # The project folder owns the disposable Cache (preview frames, proxies).
        if output_dir:
            self.project_folder = str(output_dir)
        # Frame-space edits (Fill Frame) read the resolution from the session, which has
        # no engine reference of its own. This is the one place the size is published.
        self.session.project_size = (int(settings.width), int(settings.height))
        nominal = settings.nominal_fps
        self.timeline.fps = nominal
        self.timeline.min_duration = nominal * 30
        self.project_fps = float(settings.fps)
        self.source_width, self.source_height = settings.width, settings.height
        self.source_fps = float(settings.fps)
        r, g, b = settings.background_rgb()
        self.background_bgr = (b, g, r)
        self.playback_fps.set(nominal)
        self.render_fps.set(nominal)
        self.render_width.set(settings.width)
        self.render_height.set(settings.height)
        self.frame_width.set(settings.width)
        self.frame_height.set(settings.height)
        if output_dir:
            base = os.path.join(output_dir, settings.name)
            self.render_output_path.set(base + ".mp4")
            self.frame_output_path.set(base + "_frame.png")

    # ------------------------------------------------------- effect compatibility
    def track_type_of(self, clip):
        """The media type an effect would run on: "video" (pictures and text) or "audio"."""
        track = getattr(clip, "track", None)
        return getattr(track, "type", "video") or "video"

    def effect_supports(self, effect_key, track_type):
        """True when the registered effect declares this track type in MEDIA_TYPES."""
        effect = self.runtime_effects.get(effect_key)
        return effect is not None and effect.supports_track_type(track_type)

    def clip_accepts_effect(self, clip, effect_key):
        return clip is not None and self.effect_supports(effect_key, self.track_type_of(clip))

    def incompatible_effects(self, clip):
        """Entries on a clip whose effect cannot process that clip's media type."""
        return [entry for entry in (getattr(clip, "effects", None) or ())
                if not self.clip_accepts_effect(clip, entry.get("effect_id"))]

    def sanitize_effects(self):
        """Disable (never delete) incompatible effects, e.g. after loading an older project.

        The entries keep their parameters so nothing is lost, but they stay disabled and
        carry an ``incompatible`` flag the properties panel explains. Returns
        [(clip_id, effect_id)] for the ones that were switched off.
        """
        disabled = []
        self.last_incompatible = disabled
        for clip in self.timeline.all_clips():
            for entry in getattr(clip, "effects", None) or ():
                if self.clip_accepts_effect(clip, entry.get("effect_id")):
                    entry.pop("incompatible", None)
                    continue
                entry["enabled"] = False
                entry["incompatible"] = True
                disabled.append((clip.id, entry.get("effect_id")))
        return disabled

    def sanitize_animation(self):
        """Make every stored animation plottable. Returns the repairs it made.

        A project written by an older build (or edited by hand) can hold an animated
        *non-numeric* parameter - a colour preset, a blend mode - or a numeric parameter
        whose curve holds text. Those values are valid data for the renderer but they are
        not curves, and the Graph Editor must never be handed one. The first keyframe's
        value is kept as a plain value, so the picture does not change, and the rest of
        the curve is dropped. Returns [(clip_id, channel, action)] for the report.
        """
        from .model import channels as CH
        from .model.keyframes import AnimatedValue

        repairs = []
        for clip in self.timeline.all_clips():
            entries = list(getattr(clip, "effects", None) or ())
            for entry in entries:
                params = entry.get("params")
                if not isinstance(params, dict):
                    entry["params"] = params = {}
                effect_id = entry.get("effect_id")
                for name, raw in list(params.items()):
                    if not isinstance(raw, AnimatedValue):
                        continue
                    channel = CH.effect_channel(effect_id, name)
                    if CH.numeric_channel(clip, channel, self.runtime_effects):
                        continue
                    value = raw.value_at(0)
                    params[name] = True if isinstance(value, bool) else (
                        "" if value is None else value)
                    repairs.append((clip.id, channel, "flattened"))
                for pin in entry.get("pins") or ():
                    for axis in ("x", "y"):
                        raw = pin.get(axis)
                        if not isinstance(raw, AnimatedValue):
                            continue
                        channel = CH.pin_channel(effect_id, pin.get("id"), axis)
                        if CH.numeric_channel(clip, channel, self.runtime_effects):
                            continue
                        pin[axis] = CH.as_number(raw.value_at(0)) or 0.0
                        repairs.append((clip.id, channel, "flattened"))
            transform = getattr(clip, "transform", None)
            if isinstance(transform, dict):
                for name, raw in list(transform.items()):
                    if not isinstance(raw, AnimatedValue):
                        continue
                    channel = CH.transform_channel(name)
                    if CH.numeric_channel(clip, channel, self.runtime_effects):
                        continue
                    transform[name] = CH.as_number(raw.value_at(0)) or 0.0
                    repairs.append((clip.id, channel, "flattened"))
        return repairs

    def reset_media(self):
        """Forget decoded media (new or closed project) so memory is released."""
        self.media._entries.clear()
        self.media._thumbs.clear()
        self.missing_paths = set()
        self.current_frame = self.current_processed = None
        self.current_media_path = ""
        self.input_path.set("")

    def blank_frame(self):
        frame = np.empty((max(1, self.source_height), max(1, self.source_width), 3), dtype=np.uint8)
        frame[:] = self.background_bgr
        return frame

    def project_size(self):
        """The logical (project) frame size; preview quality never changes it.

        The session keeps its own copy because it has no engine reference; publishing it
        here keeps Bake to Keyframes sampling at the same size the preview uses, whatever
        order the project was opened in.
        """
        size = (self.source_width, self.source_height)
        if size[0] and size[1] and self.session.project_size != size:
            self.session.project_size = size
        return size

    def prepare_mosaic_resources(self, tile_w, tile_h):
        """(tiles, palette averages) for one tile size, cached per size."""
        key = (tile_w, tile_h)
        if key not in self.palette_cache:
            self.palette_cache[key] = build_palette(self.tile_images, tile_w, tile_h)
        return self.palette_cache[key]

    def add_decoded_media(self, path, kind, frames, fps, at_frame=None, track=None, find_free=False,
                          reveal=True, audio=None):
        """Install worker-decoded frames and add a media clip to the model.

        `audio` says whether the file carries a sound track; when it does, the timeline adds
        the linked audio clip on the lane below in the same undo step.
        """
        if not frames:
            raise ValueError("No frames found in source.")
        entry = self.media.entry(path)
        if entry is None:
            entry = MediaEntry(kind, len(frames), fps)
            self.media._entries[path] = entry
        entry.kind, entry.frame_count, entry.fps, entry.frames = kind, len(frames), float(fps), frames
        self.input_path.set(path)
        self.source_path = path
        if self.source_width == 0:
            self.source_height, self.source_width = frames[0].shape[:2]
            self.source_fps = float(fps or self.timeline.fps)
            self.render_fps.set(self.timeline.fps)
            self.render_width.set(self.source_width)
            self.render_height.set(self.source_height)
            self.frame_width.set(self.source_width)
            self.frame_height.set(self.source_height)
            root, _ext = os.path.splitext(path)
            self.render_output_path.set(root + "_mosaic.mp4")
            self.frame_output_path.set(root + "_frame.png")
        duration = self.media.length_in_timeline_frames(path, self.timeline.fps) or self.timeline.fps * 5
        record = {"name": os.path.basename(path), "path": path, "duration": duration,
                  "type": "video", "source_out": len(frames), "source_fps": float(fps or self.timeline.fps),
                  "audio": bool(audio)}
        self.session.add_media([record], at_frame, track, find_free, reveal)
        return entry

    def register_decoded(self, path, kind, frames, fps):
        """Install worker-decoded frames as this path's media cache entry (no timeline edit)."""
        entry = self.media.entry(path)
        if entry is None:
            entry = MediaEntry(kind, len(frames), fps)
        entry.kind, entry.frame_count, entry.fps, entry.frames = kind, len(frames), float(fps), frames
        self.media._entries[path] = entry
        return entry

    def add_decoded_image(self, path, kind, frames, fps, at_frame=None, track=None, find_free=False, reveal=True,
                          duration=None, select=True, fit=False, label="Import media"):
        """Install stills without changing the project canvas established by the first clip.

        `duration` overrides the three-second default (Smart Paste has its own preference),
        and `select`/`fit` are applied inside the same undo step as the insert.
        """
        self.register_decoded(path, kind, frames, fps)
        length = max(1, int(duration)) if duration else self.timeline.fps * 3
        return self.session.add_media([{"name": os.path.basename(path), "path": path, "duration": length,
                                       "type": "video", "source_out": len(frames), "source_fps": fps}],
                                      at_frame, track, find_free, reveal,
                                      select=select, fit=fit, label=label)

    def import_records(self, paths, at_frame=None, track=None, find_free=False, reveal=True):
        """Place already probed media entries (used for audio placeholders/stills)."""
        return self.session.add_media(paths, at_frame, track, find_free, reveal)

    def _clip_image(self, clip, frame_no):
        """One clip's frame at project size, or None when it cannot be decoded."""
        try:
            src = self.media.frame_for_clip(clip, frame_no, self.timeline.fps)
        except Exception:
            src = None
        return None if src is None else self.fit_to_project(src)

    def _video_stack(self, frame_no):
        """Visible video clips under the playhead, bottom layer first.

        Track order is top-first in the model, so the reverse is the compositing order.
        """
        stack = []
        for track in reversed(self.timeline.tracks):
            if track.type != "video" or not track.visible:
                continue
            for clip in track.sorted_clips():
                # Adjustment layers carry no picture but occupy their track: they are part
                # of the stack so their effects can see everything composited below them.
                if ((clip.source_path or clip.is_text or clip.is_shape
                        or getattr(clip, "adjustment", False))
                        and clip.timeline_start <= frame_no < clip.end):
                    stack.append(clip)
                    break
        return stack

    def _layer_for(self, clip, frame_no, scale, unit_poses=None, spec=None):
        """(BGRA layer, picture rect) for one clip, or (None, None) when it cannot decode.

        `unit_poses` is the preset layer's per-text-unit animation; `spec` lets the caller
        (which already needed the text layout) pass the resolved text in.
        """
        if clip.is_shape:
            # A shape is generated, like text, straight into a full-frame transparent
            # layer, so the transform moves/scales it exactly like any other picture. The
            # geometry is drawn from parameters at the render resolution, so a 35 %
            # preview and the 100 % export are the same picture and nothing is upscaled.
            width, height = layer_ops.render_size(self.project_size(), scale)
            spec = shape_model.resolve(clip, frame_no - clip.timeline_start)
            layer = shape_render.render(spec, width, height, scale)
            if layer is None:
                return None, None
            return layer, (0.0, 0.0, float(self.project_size()[0]),
                           float(self.project_size()[1]))
        if clip.is_text:
            # Text lives in project space, so its layer is the whole frame: the transform
            # then moves/scales it exactly like a fitted picture.
            width, height = layer_ops.render_size(self.project_size(), scale)
            if spec is None:
                spec = text_model.resolve(clip, frame_no - clip.timeline_start)
            layer = text_render.render(spec, width, height, scale, poses=unit_poses)
            if layer is None:
                return None, None
            return layer, (0.0, 0.0, float(self.project_size()[0]), float(self.project_size()[1]))
        try:
            src = self.media.frame_for_clip(clip, frame_no, self.timeline.fps)
        except Exception:
            src = None
        if src is None:
            return None, None
        return layer_ops.fit_layer(src, self.project_size(), scale)

    def compose_frame(self, frame_no, scale=1.0):
        """Composite the visible video layers into one frame at `frame_no`.

        Each clip is built as a transparent layer: the source is fitted to the frame,
        its effects run in order (warp effects included), its transform warps it, and
        the result is alpha-composited bottom-up. This is the only compositor in the
        app - the preview and the exporter both call it, which is what guarantees the
        exported picture matches what the user sees.

        A single untransformed, fully opaque clip takes a shortcut that returns exactly
        the old single-layer result, so ordinary playback costs what it always did.
        """
        self.current_fx = []
        stack = self._video_stack(frame_no)
        if not stack:
            return None
        top = stack[-1]
        self.current_fx = top.effects
        self.current_media_path = top.source_path
        self.current_clip_local_frame = frame_no - top.timeline_start
        width, height = layer_ops.render_size(self.project_size(), scale)
        canvas = np.empty((height, width, 3), dtype=np.float32)
        canvas[:] = self.background_bgr
        painted = False
        for clip in stack:
            local = self.held_frame(clip, frame_no - clip.timeline_start)
            if getattr(clip, "adjustment", False):
                canvas = self.compose_adjustment(canvas, clip, frame_no, local, scale)
                painted = True
                continue
            # The preset layer is evaluated before the picture is built: a per-unit text
            # animation needs the glyph layout the renderer is about to draw.
            effect = preset_model.Effect()
            spec = None
            if getattr(clip, "presets", None):
                units = None
                if clip.is_text:
                    width, height = layer_ops.render_size(self.project_size(), scale)
                    spec = text_model.resolve(clip, local)
                    units = text_unit_render.layout_units(spec, width, height, scale)
                effect = preset_model.evaluate(clip, local, units=units,
                                               project_size=self.project_size())
            # Sample the *held* moment, so posterize time and stutter really do freeze
            # the picture instead of only freezing the effect parameters.
            layer, rect = self._layer_for(clip, clip.timeline_start + local, scale,
                                          effect.unit_poses if clip.is_text else None, spec)
            if layer is None:
                continue
            layer = self.apply_effects_layer(layer, clip.effects, local, scale,
                                             self.track_type_of(clip), clip=clip, rect=rect)
            state = transforms.evaluate(clip, local)
            pose = effect.pose
            if not pose.is_identity():
                # Additive/multiplicative: the preset never replaces the clip's own
                # transform, it is applied on top of it.
                state = transforms.TransformState(
                    position_x=state.position_x + pose.dx,
                    position_y=state.position_y + pose.dy,
                    scale_x=state.scale_x * pose.scale_x,
                    scale_y=state.scale_y * pose.scale_y,
                    rotation=state.rotation + pose.rotation,
                    anchor_x=state.anchor_x, anchor_y=state.anchor_y,
                    opacity=state.opacity, flip_h=state.flip_h, flip_v=state.flip_v)
            layer = layer_ops.apply_transform(layer, rect, state, self.project_size(), scale)
            if not pose.is_identity():
                layer = layer_ops.preset_look(layer, pose.blur * scale, pose.hue, pose.light)
            opacity = transforms.opacity_at(clip, local) * clip.fade_gain_at(frame_no) * pose.opacity
            if opacity <= 0.001:
                continue
            if (clip is top and len(stack) == 1 and opacity >= 0.999 and pose.is_identity()
                    and state.is_identity() and layer[:, :, 3].min() == 255
                    and clip.blend == "normal" and not getattr(clip, "adjustment", False)):
                return np.ascontiguousarray(layer[:, :, :3])
            layer_ops.composite(canvas, layer, opacity, getattr(clip, "blend", "normal"))
            painted = True
        if not painted:
            return None
        return layer_ops.flatten(canvas)

    def compose_adjustment(self, canvas, clip, frame_no, local, scale):
        """Run an adjustment layer's effects over everything composited below it.

        The canvas so far (opaque float BGR) becomes the layer, so every effect sees a
        real picture - a warp, a glow or a colour grade works exactly as it does on a
        clip. Opacity, fades and the clip's blend mode then mix the graded result back,
        which is what makes adjustment layers trim, fade and keyframe like any clip.
        """
        height, width = canvas.shape[:2]
        layer = np.empty((height, width, 4), dtype=np.uint8)
        layer[:, :, :3] = np.clip(canvas, 0.0, 255.0).astype(np.uint8)
        layer[:, :, 3] = 255
        layer = self.apply_effects_layer(layer, clip.effects, local, scale, "video",
                                         clip=clip, rect=(0.0, 0.0, float(width), float(height)))
        opacity = transforms.opacity_at(clip, local) * clip.fade_gain_at(frame_no)
        if opacity <= 0.001:
            return canvas
        return layer_ops.composite(canvas, layer, opacity, getattr(clip, "blend", "normal"))

    def held_frame(self, clip, local):
        """The clip-local frame its picture is sampled at, after temporal effects.

        Posterize time and stutter are edits to the *clip's* frame rate, not to a few
        pixels, so the engine holds the sampled moment for the whole pipeline. That keeps
        the choppy look identical in preview and export, and keyframes stay on the
        timeline's own clock.
        """
        frame = max(0, int(local))
        for entry in getattr(clip, "effects", None) or ():
            if not entry.get("enabled", True):
                continue
            key = entry.get("effect_id")
            params = entry.get("params") or {}
            if key == "posterize_time":
                # Frame Rate in fps. A project saved with the old Step control has been
                # converted on load; `posterize_hold` still honours Step as a fallback.
                frame = posterize_hold(frame, params, self.timeline.fps)
            elif key == "stutter":
                interval = max(1, int(round(parameter_value_at(params.get("interval", 8), frame))))
                hold = max(1, min(interval, int(round(parameter_value_at(params.get("hold", 3), frame)))))
                offset = max(0, int(round(parameter_value_at(params.get("offset", 0), frame))))
                position = (frame - offset) % interval
                if position < hold:
                    # Freeze at the first frame of this cycle, then play the rest.
                    frame = max(0, frame - position)
        return max(0, frame)

    @staticmethod
    def look_strengths(entries, local_frame):
        """{look id: 0..1} from the Look group cards on a clip (empty without any)."""
        strengths = {}
        for entry in entries or ():
            if entry.get("effect_id") != "look":
                continue
            params = entry.get("params") or {}
            key = entry.get("look_id") or params.get("look")
            if not entry.get("enabled", True):
                # A disabled group is 0, not missing: otherwise its members would fall
                # back to full strength and switching a Style off would do nothing.
                strengths[key] = 0.0
                continue
            value = parameter_value_at(params.get("strength", 100), local_frame)
            try:
                strengths[key] = max(0.0, min(1.0, float(value) / 100.0))
            except (TypeError, ValueError):
                strengths[key] = 1.0
        return strengths

    @classmethod
    def entry_mix(cls, entry, local_frame, looks=None):
        """An effect entry's Mix (0..1), scaled by its Look group's Strength."""
        params = entry.get("params") or {}
        try:
            mix = float(parameter_value_at(params.get("mix", 100), local_frame)) / 100.0
        except (TypeError, ValueError):
            mix = 1.0
        group = entry.get("look_id")
        if looks and group in looks:
            mix *= looks[group]
        return max(0.0, min(1.0, mix))

    def apply_effects_layer(self, layer, entries, local_frame, scale, track_type="video", clip=None,
                            rect=None):
        """Run a clip's effects over a BGRA layer, preserving its alpha.

        Effects whose declaration does not cover `track_type` are skipped here as well,
        so a stale or hand-edited project can never run a picture effect on something else.
        `rect` is where the picture sits inside the layer; a warp uses it so it never
        deforms the transparent letterbox margin around the picture.
        """
        from .processing import blend as blend_ops

        entries = list(entries or ())
        looks = self.look_strengths(entries, local_frame)
        output = layer
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            runtime = self.runtime_effects.get(entry.get("effect_id"))
            if runtime is None or not runtime.supports_track_type(track_type):
                continue
            mix = self.entry_mix(entry, local_frame, looks)
            if mix <= 0.001:
                continue
            original = output
            # Start from the effect's own defaults so one clip's settings can never
            # leak into the next clip that uses the same effect object.
            params = dict(getattr(runtime, "defaults", {}) or {})
            params.update({name: parameter_value_at(value, local_frame)
                           for name, value in entry.get("params", {}).items()})
            runtime.param_scale = float(scale)
            runtime.clip_entry = entry
            runtime.layer_rect = rect
            # Motion blur and other motion-aware effects need the clip's own animation.
            runtime.clip = clip
            runtime.local_frame = int(local_frame)
            runtime.load_params(params)
            if hasattr(runtime, "apply_layer"):
                result = runtime.apply_layer(output)
            else:
                colour = runtime.apply(np.ascontiguousarray(output[:, :, :3]))
                if colour.shape[:2] != output.shape[:2]:
                    colour = cv2.resize(colour, (output.shape[1], output.shape[0]),
                                        interpolation=cv2.INTER_AREA)
                result = np.dstack([colour, output[:, :, 3]])
            output = blend_ops.mix_layers(original, result, mix)
        return output

    def fit_to_project(self, src):
        pw, ph = self.source_width, self.source_height
        h, w = src.shape[:2]
        if not pw or not ph or (w, h) == (pw, ph):
            return src
        ratio = min(pw / w, ph / h)
        nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
        resized = cv2.resize(src, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.empty((ph, pw, 3), dtype=np.uint8)
        canvas[:] = self.background_bgr
        x0, y0 = (pw - nw) // 2, (ph - nh) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
        return canvas

    def apply_effects(self, frame, entries=None, local_frame=None, scale=1.0):
        """Run the clip's effects in order, evaluating every keyframe at `local_frame`.

        `scale` is the render scale of `frame` relative to the logical project size;
        effects use it for their pixel-based parameters so a preview matches export.
        """
        from .processing import blend as blend_ops

        output = frame
        local_frame = self.current_clip_local_frame if local_frame is None else local_frame
        entries = list(self.current_fx if entries is None else entries)
        looks = self.look_strengths(entries, local_frame)
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            runtime = self.runtime_effects.get(entry["effect_id"])
            if runtime is None:
                continue
            mix = self.entry_mix(entry, local_frame, looks)
            if mix <= 0.001:
                continue
            # Start from the effect's own defaults so one clip's settings can never
            # leak into the next clip that uses the same effect object.
            params = dict(getattr(runtime, "defaults", {}) or {})
            params.update({name: parameter_value_at(value, local_frame)
                           for name, value in entry.get("params", {}).items()})
            runtime.param_scale = float(scale)
            runtime.load_params(params)
            result = runtime.apply(output)
            if mix < 0.999:
                flat = np.full(output.shape[:2], 255, np.uint8)
                result = blend_ops.mix_layers(np.dstack([output, flat]),
                                              np.dstack([result, flat]), mix)[:, :, :3]
            output = result
        return output

    def render_size(self, scale=1.0):
        """Render size for a preview quality scale (logical size x scale)."""
        scale = max(0.02, min(1.0, float(scale)))
        width = max(1, int(round(max(1, self.source_width) * scale)))
        height = max(1, int(round(max(1, self.source_height) * scale)))
        return width, height

    def render_frame(self, frame_no, scale=1.0):
        """Compose, transform and process one timeline frame at `scale`.

        The same call feeds the preview (scale < 1 for speed) and the exporter
        (scale = 1). Scaling only changes how many pixels are produced; framing,
        transform values and animation are identical at every scale.
        """
        with self._process_lock:
            scale = max(0.02, min(1.0, float(scale)))
            frame = self.compose_frame(frame_no, scale)
            if frame is None:
                width, height = layer_ops.render_size(self.project_size(), scale)
                frame = np.empty((height, width, 3), dtype=np.uint8)
                frame[:] = self.background_bgr
            return frame, frame

    def has_media(self):
        clips = self.timeline.all_clips()
        return (any(c.source_path for c in clips) or any(c.is_text or c.is_shape for c in clips)) and self.source_width > 0

    @staticmethod
    def normalize_even_size(width, height):
        width, height = max(2, int(width)), max(2, int(height))
        return width + width % 2, height + height % 2

    def render_dimensions(self, use_source_size, width, height):
        if use_source_size or not self.source_width or not self.source_height:
            width, height = self.source_width, self.source_height
        return self.normalize_even_size(width, height)

    def build_render_source_indices(self, start=0, end=None):
        """Timeline frames to render: `start` to `end` (defaults to the whole project).

        The first rendered frame is `start`, so the output starts at 0 even when the
        exported range does not begin at the project start.
        """
        project_end = max(0, self.timeline.duration)
        lo = max(0, int(start))
        hi = project_end if end is None else max(lo, min(int(end), project_end))
        if hi <= lo:
            return []
        speed = max(25, int(self.speed_percent.get())) / 100.0
        result, pos = [], 0.0
        while pos < hi - lo:
            result.append(lo + int(pos))
            pos += speed
        if self.reverse_timeline.get():
            result.reverse()
        return result

    def set_proxy_settings(self, auto, width, hardware_decode="auto"):
        """Preview-only proxy playback and the decoder's hardware request.

        Proxies and decoding shortcuts never change an export: ``export`` suspends the
        proxy switch below, so the final file is always rendered from the real sources.
        """
        self.proxy_auto = bool(auto)
        self.proxy_width = max(320, int(width or 1280))
        self.media.set_proxy(self.proxy_auto, self.proxy_width, self.proxy_folder)
        from .media import reader as reader_module
        reader_module.set_hardware_decode(hardware_decode)

    @property
    def proxy_folder(self):
        folder = getattr(self, "project_folder", "") or ""
        return os.path.join(str(folder), "Cache", "proxy") if folder else ""

    def _open_video_writer(self, path, fmt, fps, size):
        """NVENC when it is available and allowed, otherwise the proven OpenCV writer."""
        width, height = size
        if fmt == "mp4" and self.hardware_encode != "off":
            try:
                from .media.hardware import FFmpegVideoWriter, nvenc_available
                if nvenc_available():
                    writer = FFmpegVideoWriter(path, width, height, fps)
                    if writer.isOpened():
                        self.last_export_encoder = "h264_nvenc"
                        return writer
                    writer.release()
            except Exception:
                pass
        fourcc = cv2.VideoWriter_fourcc(*("mp4v" if fmt == "mp4" else "XVID"))
        writer = cv2.VideoWriter(path, fourcc, float(fps), (width, height))
        if not writer.isOpened():
            raise RuntimeError("Failed to create video writer.")
        self.last_export_encoder = "mp4v" if fmt == "mp4" else "XVID"
        return writer

    def export(self, settings, progress=None, cancelled=None):
        # A reduced-resolution proxy must never reach the final file.
        self.media.suspend_proxy(True)
        try:
            return self._export(settings, progress, cancelled)
        finally:
            self.media.suspend_proxy(False)

    def _export(self, settings, progress=None, cancelled=None):
        if self.source_mode.get() == "camera":
            raise RuntimeError("Switch to File source to render full timeline.")
        if not self.has_media():
            raise RuntimeError("Nothing to export: the timeline has no clips yet. "
                               "Import media or add clips to a track, then export again.")
        path = settings.get("output_path", "").strip()
        fmt = settings.get("format", "mp4").lower()
        if not path:
            raise ValueError("Please choose an output path.")
        path = os.path.splitext(path)[0] + "." + fmt
        indices = self.build_render_source_indices(settings.get("range_start", 0),
                                                   settings.get("range_end"))
        if not indices:
            raise ValueError("The selected export range has no frames.")
        fps = self.project_fps if settings.get("use_source_fps", True) else max(1, int(settings.get("fps", 30)))
        width, height = self.render_dimensions(settings.get("use_source_size", True),
                                               settings.get("width", self.source_width),
                                               settings.get("height", self.source_height))
        writer = None
        writer_type = "image"
        segments = settings.get("audio_segments") or ()
        with_audio = fmt in {"mp4", "avi"} and bool(segments)
        video_path = path + ".video.tmp" + os.path.splitext(path)[1] if with_audio else path
        try:
            if fmt in {"mp4", "avi"}:
                writer = self._open_video_writer(video_path, fmt, float(fps), (width, height))
                writer_type = "video"
            elif fmt == "gif":
                writer = imageio.get_writer(path, mode="I", duration=1.0 / max(1.0, float(fps)), loop=0)
                writer_type = "gif"
            final = None
            for index, frame_no in enumerate(indices):
                if cancelled and cancelled():
                    raise InterruptedError("Export cancelled")
                raw, processed = self.render_frame(frame_no)
                if processed.shape[1] != width or processed.shape[0] != height:
                    processed = cv2.resize(processed, (width, height), interpolation=cv2.INTER_AREA)
                final = processed
                if writer_type == "video":
                    writer.write(processed)
                elif writer_type == "gif":
                    writer.append_data(cv2.cvtColor(processed, cv2.COLOR_BGR2RGB))
                if progress:
                    progress(index + 1, len(indices), final)
            if writer_type == "image":
                if final is None or not cv2.imwrite(path, final):
                    raise OSError("Could not write image output.")
            if with_audio:
                from .audio.export_mix import mux, render_wav
                wav_path = path + ".audio.tmp.wav"
                try:
                    writer.release()
                    writer = None
                    render_wav(segments, int(settings.get("range_start", 0)) / float(self.timeline.fps),
                               len(indices) / float(fps), settings.get("audio_rate", 48000), wav_path,
                               master=settings.get("audio_master", 1.0), cancelled=cancelled)
                    mux(video_path, wav_path, path, fmt)
                finally:
                    for temp in (wav_path,):
                        if os.path.exists(temp):
                            os.remove(temp)
        finally:
            if writer_type == "video" and writer is not None:
                writer.release()
            elif writer_type == "gif" and writer is not None:
                writer.close()
            if with_audio and os.path.exists(video_path):
                try:
                    os.remove(video_path)
                except OSError:
                    pass
        return path, len(indices), float(fps), (width, height)
