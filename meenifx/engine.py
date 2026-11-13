"""Toolkit-independent media, effect, frame composition and export orchestration."""

import math
import os
import threading

import cv2
import numpy as np

from .config import DEFAULT_TILE_PATHS
from .audio.effects import AUDIO_EFFECTS
from .effects.builtins import AsciiEffect, BlobTrackerEffect, FaceTrackerEffect, HandTrackerEffect, MosaicEffect
from .effects.blur import (BoxBlurEffect, DirectionalBlurEffect, GaussianBlurEffect,
                           MotionBlurEffect, RadialBlurEffect, ZoomBlurEffect)
from .effects.color import ColorCorrectEffect
from .effects.color_extra import (ColorWheelsEffect, CurvesEffect, HueSaturationEffect,
                                  LevelsEffect, TintEffect, TritoneEffect)
from .effects.distort_extra import (DisplacementMapEffect, LensDistortionEffect,
                                    TurbulentDisplaceEffect)
from .effects.glitch import (BulgePinchEffect, ChromaticAberrationEffect, GlitchEffect,
                             KaleidoscopeEffect, TwirlEffect, WaveWarpEffect)
from .effects.shader_effects import LensWarpEffect
from .effects.page_curl import PageCurlEffect
from .effects.chroma import ChromaKeyEffect
from .effects.glow import GlowEffect
from .effects.glow_extra import (DirectionalGlowEffect, LightWrapEffect, MultiGlowEffect,
                                 NeonGlowEffect, SoftBloomEffect)
from .effects.generate import (CheckerboardEffect, FractalNoiseEffect, GradientRampEffect,
                               GridEffect)
from .effects.light import (BloomStreaksEffect, ExposureFlickerEffect, FlashEffect,
                            LightLeakEffect)
from .effects.looks import LookEffect
from .effects.lut import LutEffect
from .effects.matte import SimpleChokerEffect
from .effects.matte_extra import MatteChokerEffect
from .effects.roto_brush import RotoBrushEffect
from .effects.smart_key import SmartKeyEffect
from .effects.stylize import (DuotoneEffect, EdgeOutlineEffect, FilmGrainEffect, HalftoneEffect,
                              InvertEffect, PosterizeEffect, VhsCrtEffect, VignetteEffect)
from .effects.temporal import EchoEffect, PosterizeTimeEffect, StutterEffect, posterize_hold
from .effects.warp import (CornerPinEffect, DistortionEffect, MeshWarpEffect,
                           PerspectiveEffect, PuppetWarpEffect, SkewEffect)
from .media.cache import MediaCache, MediaEntry
from .media.reader import load_default_tiles
from .model import transform as transforms
from .model.composition import CompositionLibrary
from .model.keyframes import parameter_value_at
from .model.session import TimelineSession
from .model.timeline import Timeline
from .model.values import Value
from .model import presets as preset_model
from .model import mask as mask_model
from .model import matte as matte_model
from .model import shape as shape_model
from .model import text as text_model
from .processing import gif_export
from .processing import layers as layer_ops
from .processing import masks as mask_ops
from .processing import shapes as shape_render
from .processing import text as text_render
from .processing import text_units as text_unit_render
from .processing.mosaic import build_palette


class EditorEngine:
    """Owns project data and processing state; never constructs GUI objects."""

    EFFECT_TYPES = (MosaicEffect, AsciiEffect, FaceTrackerEffect, HandTrackerEffect, BlobTrackerEffect,
                    ColorCorrectEffect, GaussianBlurEffect, BoxBlurEffect, DirectionalBlurEffect,
                    RadialBlurEffect, ZoomBlurEffect, MotionBlurEffect, GlowEffect, ChromaKeyEffect,
                    PuppetWarpEffect, CornerPinEffect, PerspectiveEffect, MeshWarpEffect, SkewEffect,
                    DistortionEffect,
                    ChromaticAberrationEffect, GlitchEffect, WaveWarpEffect, BulgePinchEffect,
                    TwirlEffect, KaleidoscopeEffect,
                    VhsCrtEffect, FilmGrainEffect, HalftoneEffect, PosterizeEffect,
                    EdgeOutlineEffect, DuotoneEffect, InvertEffect, VignetteEffect,
                    FlashEffect, ExposureFlickerEffect, LightLeakEffect, BloomStreaksEffect,
                    PosterizeTimeEffect, StutterEffect, EchoEffect, LutEffect, LookEffect,
                    SimpleChokerEffect, SmartKeyEffect, RotoBrushEffect,
                    MultiGlowEffect, NeonGlowEffect, SoftBloomEffect, LightWrapEffect,
                    DirectionalGlowEffect, CurvesEffect, HueSaturationEffect, TintEffect,
                    TritoneEffect, LevelsEffect, ColorWheelsEffect,
                    TurbulentDisplaceEffect, DisplacementMapEffect, LensDistortionEffect,
                    GradientRampEffect, FractalNoiseEffect, CheckerboardEffect, GridEffect,
                    MatteChokerEffect, LensWarpEffect, PageCurlEffect) + AUDIO_EFFECTS

    def __init__(self, fps=30):
        self.timeline = Timeline(fps=fps)
        self.root_timeline = self.timeline
        # Rendering a nested composition swaps the engine's active timeline. Preview
        # workers and the exporter can run at the same time, so the swap and its
        # restore must be serialised or one chain restores another chain's context
        # (which left the window pointed at a composition after a preview).
        from threading import RLock
        self.render_lock = RLock()
        self.compositions = CompositionLibrary()
        self.timeline.compositions = self.compositions
        self.media = MediaCache()
        # Preview-only rendering settings (proxy media, hardware decode, GPU compositing).
        self.proxy_auto = False
        self.proxy_width = 1280
        self.hardware_encode = "auto"
        self.last_export_encoder = "mp4v"
        self.session = TimelineSession(self.timeline, self.media)
        # The library belongs to the project, not to whichever timeline is being edited.
        self.session.compositions = self.compositions
        self.session.root_timeline = self.root_timeline
        self.tile_images = load_default_tiles()
        self.palette_cache = {}
        self.effects = [kind(self) for kind in self.EFFECT_TYPES]
        self.runtime_effects = {effect.key: effect for effect in self.effects}
        self.exporting = False
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
        self.render_gif_loop = Value("forever")
        self.render_gif_loops = Value(2)
        self.render_gif_colors = Value(256)
        self.render_gif_dither = Value("floyd")
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
        """Visible video layers under the playhead, bottom layer first.

        Each entry is (clip, weight, group). A transition pair shares a group and carries
        complementary weights, so the compositor blends two fully rendered pictures
        instead of choosing one of them. Ordinary clips carry weight 1 and no group.
        Track order is top-first in the model, so the reverse is the compositing order.
        """
        stack = []
        for track in reversed(self.timeline.tracks):
            if track.type != "video" or not track.visible:
                continue
            stack.extend(self.timeline.video_layers_at(track, frame_no))
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
        if clip.is_composition:
            comp = self.compositions.get(clip.composition_id)
            if comp is None:
                return None, None
            comp_frame = clip.source_frame_at(frame_no)
            sub = self._render_composition_frame(comp, comp_frame, scale)
            if sub is None:
                return None, None
            # A transparent composition arrives BGRA; fit_layer preserves that alpha.
            # An opaque one arrives BGR and gets the usual full-coverage alpha.
            return layer_ops.fit_layer(sub, self.project_size(), scale)
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

    def _render_generated(self, clip, local, scale, state, effect, spec=None):
        """Rasterise a shape/text at the density its transform will occupy.

        Generated layers used to be drawn on a full frame at the preview scale and then
        enlarged by ``apply_transform``. A 24 px title at 1000 % was therefore a 24 px
        bitmap stretched to 240 px. This path keeps the geometry vector and renders only
        the project-space window that can reach the output, at (up to) the destination
        density, before the affine transform places it.
        """
        pw, ph = self.project_size()
        render_w, render_h = layer_ops.render_size((pw, ph), scale)
        if spec is None:
            spec = (shape_model.resolve(clip, local) if clip.is_shape
                    else text_model.resolve(clip, local))
        if clip.is_shape:
            box = shape_render.bounds(spec, pw, ph)
            margin = max(4.0, float(spec.get("stroke_width", 0.0) or 0.0) * 2.0 + 4.0)
        else:
            box = text_render.bounds(spec, render_w, render_h, scale)
            if box is None:
                return None
            box = (box[0] / scale, box[1] / scale, box[2] / scale, box[3] / scale)
            margin = max(8.0, (abs(float(spec.get("shadow_offset_x", 0.0) or 0.0))
                               + abs(float(spec.get("shadow_offset_y", 0.0) or 0.0))
                               + float(spec.get("shadow_blur", 0.0) or 0.0) * 3.0
                               + float(spec.get("blur", 0.0) or 0.0) * 3.0))
        if box is None:
            return None
        x0 = max(0.0, float(box[0]) - margin)
        y0 = max(0.0, float(box[1]) - margin)
        x1 = min(float(pw), float(box[0]) + max(1.0, float(box[2])) + margin)
        y1 = min(float(ph), float(box[1]) + max(1.0, float(box[3])) + margin)
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            return None

        # A rotation can move a shape's corners outside its untransformed box. Work
        # backwards from the output frame instead of assuming the source box is enough:
        # the source pixels that can reach the frame are the inverse transform of its
        # four corners. Intersecting with the shape's own box keeps the crop small while
        # preserving every corner that is still visible.
        try:
            theta = math.radians(float(state.rotation))
            cos_t, sin_t = math.cos(theta), math.sin(theta)
            sx_state = float(state.scale_x) / 100.0 * (-1.0 if state.flip_h else 1.0)
            sy_state = float(state.scale_y) / 100.0 * (-1.0 if state.flip_v else 1.0)
            if abs(sx_state) < 1e-4:
                sx_state = 1e-4 if sx_state >= 0 else -1e-4
            if abs(sy_state) < 1e-4:
                sy_state = 1e-4 if sy_state >= 0 else -1e-4
            centre = np.array([(pw - 1.0) / 2.0, (ph - 1.0) / 2.0])
            target = centre + np.array([float(state.position_x), float(state.position_y)])
            anchor = centre + np.array([float(state.anchor_x), float(state.anchor_y)])
            linear = np.array([[cos_t * sx_state, -sin_t * sy_state],
                               [sin_t * sx_state, cos_t * sy_state]], dtype=np.float64)
            offset = target - linear @ anchor
            inverse = np.linalg.inv(np.array([[linear[0, 0], linear[0, 1], offset[0]],
                                              [linear[1, 0], linear[1, 1], offset[1]],
                                              [0.0, 0.0, 1.0]], dtype=np.float64))
            corners = np.array([[0.0, 0.0, 1.0], [pw, 0.0, 1.0],
                                [pw, ph, 1.0], [0.0, ph, 1.0]], dtype=np.float64)
            source_corners = corners @ inverse.T
            x0 = max(x0, float(source_corners[:, 0].min()))
            y0 = max(y0, float(source_corners[:, 1].min()))
            x1 = min(x1, float(source_corners[:, 0].max()))
            y1 = min(y1, float(source_corners[:, 1].max()))
        except (ValueError, np.linalg.LinAlgError):
            pass
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            return None

        # A transform can enlarge the generated picture by 10x or more. Render at that
        # density, but cap the temporary crop so an off-screen title cannot allocate a
        # multi-gigabyte bitmap; the cap is still at or above output resolution.
        sx = abs(float(state.scale_x)) / 100.0
        sy = abs(float(state.scale_y)) / 100.0
        detail = max(scale, scale * max(1.0, sx, sy))
        span_w, span_h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        side_cap = max(render_w, render_h) * 2.0
        detail = min(detail, side_cap / max(span_w, span_h))
        detail = min(detail, math.sqrt(32_000_000.0 / (span_w * span_h)))
        detail = max(0.05, detail)

        virtual_w = max(1, int(round(pw * detail)))
        virtual_h = max(1, int(round(ph * detail)))
        crop = (max(0, int(round(x0 * detail))), max(0, int(round(y0 * detail))),
                max(1, int(round((x1 - x0) * detail))),
                max(1, int(round((y1 - y0) * detail))))
        if clip.is_shape:
            layer = shape_render.render(spec, virtual_w, virtual_h, detail, crop=crop)
        else:
            layer = text_render.render(spec, virtual_w, virtual_h, detail,
                                       poses=effect.unit_poses if clip.is_text else None,
                                       crop=crop)
        if layer is None or not layer.any():
            return None
        return layer_ops.apply_transform_detail(
            layer, (crop[0] / detail, crop[1] / detail), detail, state,
            (pw, ph), scale, (render_w, render_h))

    def _apply_matte(self, clip, layer, frame_no, scale):
        """Multiply a finished layer by another clip's alpha or luma coverage.

        The source is rendered through the same compositor used for the visible stack,
        including its transform and masks, but never through its own matte.  That stops
        two clips pointing at each other from recursing forever while still letting a
        matte source be a shape, text clip, image, video or pre-composition.
        """
        if layer is None or not matte_model.is_active(clip):
            return layer
        settings = matte_model.ensure(clip)
        source = None
        for candidate in self.timeline.all_clips():
            if int(getattr(candidate, "id", 0) or 0) == int(settings.get("source_id", 0)):
                source = candidate
                break
        if source is None or source is clip:
            return layer
        local = self.held_frame(source, frame_no - source.timeline_start)
        if local < 0 or local >= max(1, int(source.duration)):
            return layer
        src_layer = None
        try:
            state = transforms.evaluate(source, local)
            effect = preset_model.Effect()
            spec = None
            if source.is_shape or source.is_text:
                src_layer = self._render_generated(source, local, scale, state, effect)
                if src_layer is not None and source.effects:
                    src_layer = self.apply_effects_layer(
                        src_layer, source.effects, local, scale,
                        self.track_type_of(source), clip=source,
                        rect=(0, 0, src_layer.shape[1], src_layer.shape[0]))
            else:
                src_layer, rect = self._layer_for(source, source.timeline_start + local, scale)
                if src_layer is not None:
                    if source.effects:
                        src_layer = self.apply_effects_layer(
                            src_layer, source.effects, local, scale,
                            self.track_type_of(source), clip=source, rect=rect)
                    src_layer = layer_ops.apply_transform(
                        src_layer, rect, state, self.project_size(), scale)
            if src_layer is None:
                return layer
            src_layer = mask_ops.apply(src_layer, mask_model.at(source, local), scale)
            height, width = layer.shape[:2]
            if src_layer.shape[0] != height or src_layer.shape[1] != width:
                src_layer = cv2.resize(src_layer, (width, height), interpolation=cv2.INTER_AREA)
            source_alpha = src_layer[:, :, 3].astype(np.float32) / 255.0
            mode = str(settings.get("mode") or "alpha")
            if mode.startswith("luma"):
                b, g, r = (src_layer[:, :, index].astype(np.float32) for index in range(3))
                coverage = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0 * source_alpha
            else:
                coverage = source_alpha
            if mode.endswith("inverted") or settings.get("invert"):
                coverage = 1.0 - coverage
            alpha = layer[:, :, 3].astype(np.float32) / 255.0
            layer[:, :, 3] = np.clip(np.rint(alpha * coverage * 255.0), 0, 255).astype(np.uint8)
        except Exception:
            # A matte is an alpha refinement, not a reason to drop the whole clip. The
            # ordinary clip still renders if the source is temporarily unavailable.
            return layer
        return layer

    def _render_composition_frame(self, comp, frame_no, scale=1.0):
        """Render one composition definition at its own native size.

        The engine has one active timeline; composing a nested composition temporarily
        swaps that context (and restores it in `finally`). This is deliberate: every
        effect, preset, text layout and transform then runs through the exact same code
        as the parent timeline, with no second compositor to drift out of sync.
        """
        lock = getattr(self, "render_lock", None)
        if lock is None:
            from threading import RLock
            lock = self.render_lock = RLock()
        lock.acquire()
        stack = getattr(self, "_composition_render_stack", None)
        if stack is None:
            stack = self._composition_render_stack = set()
        if comp.id in stack:
            lock.release()
            return None
        stack.add(comp.id)
        old = (self.timeline, self.source_width, self.source_height, self.source_fps,
               self.background_bgr, getattr(self.session, "project_size", None))
        try:
            self.timeline = comp.timeline
            self.source_width, self.source_height = comp.width, comp.height
            self.source_fps = float(comp.fps)
            self.background_bgr = tuple(comp.background)
            self.session.project_size = (comp.width, comp.height)
            transparent = bool(getattr(comp, "transparent", False))
            frame = self.compose_frame(int(frame_no), scale, with_alpha=transparent)
            if frame is None:
                if transparent:
                    width, height = layer_ops.render_size((comp.width, comp.height), scale)
                    frame = np.zeros((height, width, 4), np.uint8)
                else:
                    frame = self.blank_frame()
            return frame
        finally:
            (self.timeline, self.source_width, self.source_height, self.source_fps,
             self.background_bgr, project_size) = old
            self.session.project_size = project_size
            stack.discard(comp.id)
            lock.release()

    TEMPORAL_EFFECTS = frozenset({"posterize_time", "stutter"})
    MAX_TEMPORAL_DEPTH = 4

    @classmethod
    def has_temporal_effects(cls, clip):
        """True when a clip's stack changes *when* the picture is sampled."""
        return any(entry.get("enabled", True)
                   and entry.get("effect_id") in cls.TEMPORAL_EFFECTS
                   for entry in (getattr(clip, "effects", None) or ()))

    def compose_frame(self, frame_no, scale=1.0, with_alpha=False):
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
        top = stack[-1][0]
        self.current_fx = top.effects
        self.current_media_path = top.source_path
        self.current_clip_local_frame = frame_no - top.timeline_start
        canvas, painted = self._compose_stack(stack, frame_no, scale, 0,
                                              alpha=bool(with_alpha))
        if not painted:
            return None
        if with_alpha:
            rgba = np.empty((canvas.shape[0], canvas.shape[1], 4), np.uint8)
            rgba[:, :, :3] = np.clip(canvas[:, :, :3], 0.0, 255.0).astype(np.uint8)
            rgba[:, :, 3] = np.clip(canvas[:, :, 3] * 255.0, 0.0, 255.0).astype(np.uint8)
            return rgba
        if canvas.dtype == np.uint8:
            # The one-clip fast path already produced the exact BGR frame; do not copy
            # it through a float buffer on every playback tick.
            return canvas
        return layer_ops.flatten(canvas)

    def _compose_stack(self, stack, frame_no, scale, depth=0, alpha=False):
        """Composite `stack` (bottom layer first) and return (float canvas, painted).

        Splitting this from `compose_frame` lets an adjustment layer re-render only the
        layers below it, at an earlier sampled frame, without re-running itself. With
        `alpha`, the canvas is float BGRA with straight colour and 0..1 coverage; without
        it the historical float BGR canvas is unchanged.
        """
        from .processing import blend as blend_ops

        top = stack[-1][0]
        width, height = layer_ops.render_size(self.project_size(), scale)
        canvas = np.empty((height, width, 4 if alpha else 3), dtype=np.float32)
        canvas[:, :, :3] = self.background_bgr
        if alpha:
            canvas[:, :, 3] = 0.0
        painted = False

        def composite_layer(layer, opacity=1.0, mode="normal"):
            if alpha:
                return blend_ops.composite_rgba(canvas, layer, opacity, mode)
            return layer_ops.composite(canvas, layer, opacity, mode)
        group_key = None
        group = []

        def flush_group():
            """Composite the buffered transition pair as one alpha-weighted picture.

            Both clips were rendered (effects, transform, animation) independently; mixing
            their premultiplied colours by the transition weights is a real dissolve and
            keeps full coverage instead of fading through the background.
            """
            nonlocal canvas, painted, group_key
            if not group:
                group_key = None
                return
            if len(group) == 1:
                layer, coverage, mode = group[0]
                composite_layer(layer, coverage, mode)
                painted = True
            else:
                total = np.zeros((height, width), dtype=np.float32)
                colour = np.zeros((height, width, 3), dtype=np.float32)
                mode = "normal"
                for layer, alpha, blend_mode in group:
                    coverage = layer[:, :, 3].astype(np.float32) / 255.0 * float(alpha)
                    colour += layer[:, :, :3].astype(np.float32) * coverage[:, :, None]
                    total += coverage
                    mode = blend_mode
                covered = total > 1e-4
                if covered.any():
                    rgb = colour / np.maximum(total, 1e-6)[:, :, None]
                    combined = np.dstack([rgb, np.clip(total, 0.0, 1.0) * 255.0]).astype(np.uint8)
                    composite_layer(combined, 1.0, mode)
                    painted = True
            group.clear()
            group_key = None

        for index, (clip, weight, key) in enumerate(stack):
            local = self.held_frame(clip, frame_no - clip.timeline_start)
            if getattr(clip, "adjustment", False):
                flush_group()
                if (depth < self.MAX_TEMPORAL_DEPTH and index > 0
                        and self.has_temporal_effects(clip)):
                    # A temporal effect on an adjustment layer holds the *composite*
                    # below it, not just its own parameters: rebuild those layers at
                    # the sampled frame before grading them.
                    sampled, _painted = self._compose_stack(
                        stack[:index], int(clip.timeline_start + local), scale, depth + 1,
                        alpha=alpha)
                    canvas = (sampled if sampled.dtype == np.float32
                              else sampled.astype(np.float32))
                canvas = self.compose_adjustment(canvas, clip, frame_no, local, scale,
                                                 alpha=alpha)
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
            # Generated layers can be rasterised at their final density. Effects need a
            # full-frame layer, so a generated clip with an active effect keeps the
            # established (effect first, transform second) path.
            if clip.is_shape or clip.is_text:
                layer = self._render_generated(clip, local, scale, state, effect, spec)
                if layer is None:
                    continue
                # Generated layers are already transformed, so their effects belong in
                # output space: a blur is the same number of pixels in the preview and
                # the export, and it cannot enlarge a low-resolution bitmap again. A
                # media clip keeps the historical effect-then-transform order because
                # its source pixels come from the frame itself.
                if clip.effects:
                    layer = self.apply_effects_layer(
                        layer, clip.effects, local, scale, self.track_type_of(clip),
                        clip=clip, rect=(0, 0, layer.shape[1], layer.shape[0]))
            else:
                # Sample the *held* moment, so posterize time and stutter really do
                # freeze the picture instead of only freezing the effect parameters.
                layer, rect = self._layer_for(clip, clip.timeline_start + local, scale,
                                              effect.unit_poses if clip.is_text else None,
                                              spec)
                if layer is None:
                    continue
                layer = self.apply_effects_layer(layer, clip.effects, local, scale,
                                                 self.track_type_of(clip), clip=clip,
                                                 rect=rect)
                layer = layer_ops.apply_transform(layer, rect, state,
                                                  self.project_size(), scale)
            if not pose.is_identity():
                layer = layer_ops.preset_look(layer, pose.blur * scale, pose.hue, pose.light)
            # Masks and mattes are alpha operations in project space, after the picture
            # has been placed. The preview and export both reach this one point.
            if not getattr(clip, "adjustment", False):
                layer = mask_ops.apply(layer,
                                       mask_model.at(clip, local, self.project_size()), scale)
                layer = self._apply_matte(clip, layer, frame_no, scale)
            opacity = transforms.opacity_at(clip, local) * clip.fade_gain_at(frame_no) * pose.opacity
            opacity *= weight
            if opacity <= 0.001:
                continue
            if (not alpha and key is None and clip is top and len(stack) == 1
                    and opacity >= 0.999 and pose.is_identity() and state.is_identity()
                    and layer[:, :, 3].min() == 255 and clip.blend == "normal"
                    and not getattr(clip, "adjustment", False)):
                return np.ascontiguousarray(layer[:, :, :3]), True
            if key is None:
                flush_group()
                composite_layer(layer, opacity, getattr(clip, "blend", "normal"))
                painted = True
            else:
                if key != group_key:
                    flush_group()
                    group_key = key
                group.append((layer, opacity, getattr(clip, "blend", "normal")))
        flush_group()
        return canvas, painted

    def compose_adjustment(self, canvas, clip, frame_no, local, scale, alpha=False):
        """Run an adjustment layer's effects over everything composited below it.

        The canvas so far becomes the layer, so every effect sees a real picture - a
        warp, a glow or a colour grade works exactly as it does on a clip. Opacity, fades
        and the clip's blend mode then mix the graded result back, which is what makes
        adjustment layers trim, fade and keyframe like any clip. On an alpha canvas the
        existing coverage is passed through the grade instead of becoming opaque.
        """
        from .processing import blend as blend_ops

        height, width = canvas.shape[:2]
        layer = np.empty((height, width, 4), dtype=np.uint8)
        layer[:, :, :3] = np.clip(canvas[:, :, :3], 0.0, 255.0).astype(np.uint8)
        if alpha:
            layer[:, :, 3] = np.clip(canvas[:, :, 3] * 255.0, 0.0, 255.0).astype(np.uint8)
        else:
            layer[:, :, 3] = 255
        layer = self.apply_effects_layer(layer, clip.effects, local, scale, "video",
                                         clip=clip, rect=(0.0, 0.0, float(width), float(height)))
        opacity = transforms.opacity_at(clip, local) * clip.fade_gain_at(frame_no)
        if opacity <= 0.001:
            return canvas
        if alpha:
            return blend_ops.composite_rgba(canvas, layer, opacity,
                                            getattr(clip, "blend", "normal"))
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

    def has_media(self, timeline=None):
        clips = (timeline if timeline is not None else self.timeline).all_clips()
        return (any(c.source_path or c.is_text or c.is_shape or c.is_composition
                    for c in clips)) and self.source_width > 0

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
        # Diagnostic effect previews (alpha, checkerboard, isolated) are screen-only.
        # The exporter always renders the real composite frame.
        self.exporting = True
        try:
            return self._export(settings, progress, cancelled)
        finally:
            self.exporting = False
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
        requested_fps = (self.project_fps if settings.get("use_source_fps", True)
                         else max(1, int(settings.get("fps", 30))))
        fps = requested_fps
        render_indices = indices
        gif_timing = None
        gif_frames = []
        if fmt == "gif":
            # GIF timing is derived from the timeline range, never from rendering time.
            # A lower export rate resamples the timeline instead of stretching every
            # source frame to a longer delay.
            try:
                gif_fps = float(self.project_fps if settings.get("use_source_fps", True)
                                else settings.get("fps", 30))
            except (TypeError, ValueError):
                gif_fps = 30.0
            gif_fps = max(0.1, gif_fps)
            timeline_fps = max(1e-6, float(self.project_fps or self.timeline.fps or 30.0))
            gif_timing = gif_export.plan_timing(len(indices) / timeline_fps, gif_fps)
            render_indices = gif_export.sample_source_indices(indices, gif_timing.frame_count)
            fps = gif_timing.effective_fps
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
                writer_type = "gif"
            final = None
            for index, frame_no in enumerate(render_indices):
                if cancelled and cancelled():
                    raise InterruptedError("Export cancelled")
                raw, processed = self.render_frame(frame_no)
                if processed.shape[1] != width or processed.shape[0] != height:
                    processed = cv2.resize(processed, (width, height), interpolation=cv2.INTER_AREA)
                final = processed
                if writer_type == "video":
                    writer.write(processed)
                elif writer_type == "gif":
                    gif_frames.append(processed.copy())
                if progress:
                    progress(index + 1, len(render_indices), final)
            if writer_type == "image":
                if final is None or not cv2.imwrite(path, final):
                    raise OSError("Could not write image output.")
            elif writer_type == "gif":
                gif_export.write_gif(
                    path, gif_frames, gif_timing.delays_cs,
                    loop=gif_export.loop_parameter(settings.get("gif_loop", "forever"),
                                                    settings.get("gif_loops", 2)),
                    colors=settings.get("gif_colors", 256),
                    dither=settings.get("gif_dither", "floyd"))
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
            if with_audio and os.path.exists(video_path):
                try:
                    os.remove(video_path)
                except OSError:
                    pass
        return path, len(render_indices), float(fps), (width, height)
