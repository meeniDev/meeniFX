"""Time-animatable distortion tools: Turbulent Displace, Displacement Map and Lens
Distortion.  Wave Warp remains the existing sine-ripple implementation and gains the same
animation contract through its speed/phase parameters.

Fields are built on a bounded working grid and sampled back to the layer, so a preview and
an export use the same displacement shape without doing Python per-pixel work.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec
from .creative_utils import fractal_field, num, radial_coordinates, remap


class TurbulentDisplaceEffect(LayerEffect):
    key = "turbulent_displace"
    name = "Turbulent Displace"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 400.0, 40.0, step=1.0, unit=" px",
                  pixels=True, help="Maximum displacement in project pixels"),
        ParamSpec("size", "Turbulence size", "float", 2.0, 500.0, 80.0, step=1.0, unit=" px",
                  pixels=True, help="Size of the largest noise features"),
        ParamSpec("complexity", "Complexity", "float", 1.0, 8.0, 4.0, step=1.0,
                  help="Number of noise octaves; more adds fine detail"),
        ParamSpec("evolution", "Evolution", "float", 0.0, 720.0, 0.0, step=1.0, unit="\u00b0",
                  help="Animate this to make the turbulence flow over time"),
        ParamSpec("speed", "Speed", "float", -20.0, 20.0, 0.0, step=0.1, unit="\u00b0/f",
                  help="Automatic evolution per frame; keyframe Evolution for manual timing"),
        ParamSpec("angle", "Direction", "float", -180.0, 180.0, 0.0, step=1.0, unit="\u00b0"),
        ParamSpec("seed", "Seed", "int", 0, 99999, 7),
        ParamSpec("edge", "Edges", "choice", choices=("clamp", "transparent"), default="clamp",
                  choice_labels=("Repeat edge", "Transparent")),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_layer(self, layer):
        frame = self.local_frame
        amount = num(self.amount, frame) * max(0.02, float(self.param_scale))
        if amount <= 0.01:
            return layer
        height, width = layer.shape[:2]
        factor = min(1.0, 720.0 / max(1.0, max(width, height)))
        work = (max(2, int(round(width * factor))), max(2, int(round(height * factor))))
        field_size = max(2.0, num(self.size, frame) * factor)
        phase = (num(self.evolution, frame) + num(self.speed, frame) * frame) / 4.0
        seed = int(self.seed.get())
        noise_x = fractal_field(work[0], work[1], field_size,
                                int(round(num(self.complexity, frame))), seed, phase)
        noise_y = fractal_field(work[0], work[1], field_size,
                                int(round(num(self.complexity, frame))), seed + 104729,
                                phase + 37.0)
        angle = np.radians(num(self.angle, frame))
        dx = (noise_x - 0.5) * np.cos(angle) - (noise_y - 0.5) * np.sin(angle)
        dy = (noise_x - 0.5) * np.sin(angle) + (noise_y - 0.5) * np.cos(angle)
        if factor < 1.0:
            dx = cv2.resize(dx, (width, height), interpolation=cv2.INTER_LINEAR)
            dy = cv2.resize(dy, (width, height), interpolation=cv2.INTER_LINEAR)
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        border = cv2.BORDER_REPLICATE if str(self.edge.get()) == "clamp" else cv2.BORDER_CONSTANT
        return remap(layer, xx + dx * amount * 2.0, yy + dy * amount * 2.0, border=border)


class DisplacementMapEffect(LayerEffect):
    key = "displacement_map"
    name = "Displacement Map"
    category = "Glitch & Distortion"
    NOTE = ("Choose another clip or an image as the map. With no source selected the "
            "picture's own luminance drives the displacement.")

    PARAMS = (
        ParamSpec("map_source", "Map clip", "int", 0, 1000000000, 0, advanced=True,
                  help="Clip id used as the displacement map"),
        ParamSpec("map_path", "Map image", "text", default="", advanced=True,
                  help="Image file used when Map clip is 0"),
        ParamSpec("map_channel", "Map channel", "choice",
                  choices=("luma", "red", "green", "blue", "alpha"), default="luma",
                  choice_labels=("Luminance", "Red", "Green", "Blue", "Alpha")),
        ParamSpec("amount", "Amount", "float", 0.0, 400.0, 32.0, step=1.0, unit=" px",
                  pixels=True, help="Maximum movement at full white or black"),
        ParamSpec("scale_x", "Horizontal", "float", -200.0, 200.0, 100.0, step=1.0, unit="%"),
        ParamSpec("scale_y", "Vertical", "float", -200.0, 200.0, 100.0, step=1.0, unit="%"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.0, step=0.002, advanced=True),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.0, step=0.002, advanced=True),
        ParamSpec("invert", "Invert map", "bool", default=False),
        ParamSpec("edge", "Edges", "choice", choices=("clamp", "transparent"), default="clamp",
                  choice_labels=("Repeat edge", "Transparent")),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def _source_layer(self, width, height):
        source_id = int(self.map_source.get() or 0)
        path = str(self.map_path.get() or "").strip()
        if path:
            try:
                image = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            except Exception:
                image = None
            if image is None:
                return None
            if image.ndim == 2:
                image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
            elif image.shape[2] == 3:
                image = np.dstack([image, np.full(image.shape[:2], 255, np.uint8)])
            return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        if source_id <= 0 or not hasattr(self.app, "timeline"):
            return None
        source = next((clip for clip in self.app.timeline.all_clips()
                       if int(getattr(clip, "id", 0) or 0) == source_id), None)
        if source is None or source is self.clip:
            return None
        try:
            local = self.app.held_frame(source, int(self.local_frame))
            if local < 0 or local >= max(1, int(source.duration)):
                return None
            layer, rect = self.app._layer_for(source, source.timeline_start + local,
                                             max(0.02, float(self.param_scale)))
            if layer is None:
                return None
            from ..model import transform as transforms
            from ..processing import layers as layer_ops
            state = transforms.evaluate(source, local)
            layer = layer_ops.apply_transform(layer, rect, state, self.app.project_size(),
                                              max(0.02, float(self.param_scale)))
            return cv2.resize(layer, (width, height), interpolation=cv2.INTER_AREA)
        except Exception:
            return None

    def _map_values(self, layer):
        height, width = layer.shape[:2]
        source = self._source_layer(width, height)
        if source is None:
            source = layer
        channel = str(self.map_channel.get())
        if channel == "alpha":
            values = source[:, :, 3].astype(np.float32) / 255.0
        elif channel == "red":
            values = source[:, :, 2].astype(np.float32) / 255.0
        elif channel == "green":
            values = source[:, :, 1].astype(np.float32) / 255.0
        elif channel == "blue":
            values = source[:, :, 0].astype(np.float32) / 255.0
        else:
            values = (source[:, :, :3].astype(np.float32)
                      @ np.array([0.0722, 0.7152, 0.2126]) / 255.0)
        if bool(self.invert.get()):
            values = 1.0 - values
        return values

    def apply_layer(self, layer):
        frame = self.local_frame
        amount = num(self.amount, frame) * max(0.02, float(self.param_scale))
        if amount <= 0.01:
            return layer
        height, width = layer.shape[:2]
        values = self._map_values(layer)
        field = (values - 0.5) * 2.0
        scale_x = num(self.scale_x, frame) / 100.0
        scale_y = num(self.scale_y, frame) / 100.0
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        cx, cy = num(self.center_x, frame) * width, num(self.center_y, frame) * height
        dx = (xx - cx - field * amount * scale_x)
        dy = (yy - cy - field * amount * scale_y)
        border = cv2.BORDER_REPLICATE if str(self.edge.get()) == "clamp" else cv2.BORDER_CONSTANT
        return remap(layer, dx, dy, border=border)


class LensDistortionEffect(LayerEffect):
    key = "lens_distortion"
    name = "Lens Distortion"
    category = "Glitch & Distortion"

    PARAMS = (
        ParamSpec("k1", "Curvature", "float", -1.0, 1.0, 0.18, step=0.01,
                  help="First radial distortion term"),
        ParamSpec("k2", "Secondary curvature", "float", -1.0, 1.0, 0.0, step=0.01),
        ParamSpec("zoom", "Scale", "float", 0.5, 2.0, 1.0, step=0.01),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("chromatic", "Colour fringe", "float", 0.0, 24.0, 0.0, step=0.1, unit=" px",
                  pixels=True, advanced=True,
                  help="Radially offset the channels for a lens-like colour fringe"),
        ParamSpec("edge", "Edges", "choice", choices=("transparent", "clamp"),
                  choice_labels=("Transparent", "Repeat edge"), default="transparent"),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def apply_layer(self, layer):
        frame = self.local_frame
        height, width = layer.shape[:2]
        dx, dy = radial_coordinates(width, height, num(self.center_x, frame),
                                    num(self.center_y, frame))
        radius2 = dx * dx + dy * dy
        k1, k2 = num(self.k1, frame), num(self.k2, frame)
        factor = 1.0 + k1 * radius2 + k2 * radius2 * radius2
        zoom = max(0.01, num(self.zoom, frame))
        cx, cy = num(self.center_x, frame) * width, num(self.center_y, frame) * height
        half_w, half_h = width * 0.5, height * 0.5
        map_x = cx + dx * factor * half_w * zoom
        map_y = cy + dy * factor * half_h * zoom
        border = cv2.BORDER_REPLICATE if str(self.edge.get()) == "clamp" else cv2.BORDER_CONSTANT
        fringe = num(self.chromatic, frame) * max(0.02, float(self.param_scale))
        if fringe <= 0.01:
            return remap(layer, map_x, map_y, border=border)
        output = remap(layer, map_x, map_y, border=border)
        for index, sign in ((0, 1.0), (1, 0.0), (2, -1.0)):
            shifted_x = cx + (map_x - cx) * (1.0 + sign * fringe / max(1.0, half_w))
            shifted_y = cy + (map_y - cy) * (1.0 + sign * fringe / max(1.0, half_h))
            channel = remap(layer[:, :, index], shifted_x, shifted_y, border=border)
            output[:, :, index] = channel
        return output


DISTORT_EXTRA_EFFECTS = (TurbulentDisplaceEffect, DisplacementMapEffect, LensDistortionEffect)
