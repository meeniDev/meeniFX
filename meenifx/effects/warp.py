"""Warp and deformation effects: Puppet Warp, Corner Pin, Perspective, Mesh Warp,
Skew and Distortion.

They are ordinary effects in the existing pipeline: they sit in the clip's effect
list, their numerical parameters are keyframable like every other effect parameter,
and they run through the same ``apply_effects`` call that the preview and the
exporter share. They only declare one extra hook, ``apply_layer``, because they need
the layer's alpha channel (transforms can rotate a clip, and a rotated corner-pin
must not paint black corners).

Coordinates for pins, corners and mesh offsets are normalized to the frame (0..1), so
a half-quality preview deforms exactly like the full-resolution export.
"""

from __future__ import annotations

import numpy as np

from ..model.keyframes import AnimatedValue, parameter_value_at
from ..processing import warp
from .base import EffectBase, LayerEffect, ParamSpec


def _num(value, frame=0):
    """A parameter's number: an engine-loaded Value, an animated curve or a plain number."""
    if isinstance(value, AnimatedValue):
        return float(value.value_at(frame))
    if hasattr(value, "get"):
        return float(value.get())
    return float(value)


class PuppetWarpEffect(LayerEffect):
    """Pin-based mesh deformation (moving-least-squares)."""

    key = "puppet_warp"
    name = "Puppet Warp"

    PARAMS = (
        ParamSpec("mesh_density", "Mesh density", "int", 6, 64, 24,
                  help="Grid resolution of the deformation. Higher is smoother, slower"),
        ParamSpec("stiffness", "Stiffness", "float", 0.2, 4.0, 1.0, step=0.05,
                  help="How tightly each pin holds the pixels around it"),
        ParamSpec("show_mesh", "Show mesh", "bool", default=True,
                  help="Draw the deformation mesh over the preview (never exported)"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.mesh_density = _value(24)
        self.stiffness = _value(1.0)
        self.show_mesh = _value(True)
        self._cache = {}

    def pins_at(self, local_frame):
        """Pins evaluated at a clip-local frame (pins are animatable)."""
        rows = []
        for pin in self.entry_pins(self.clip_entry):
            try:
                # A disabled pin is out of the deformation entirely: it neither moves pixels
                # nor holds them, which is the difference between "off" and "locked".
                if not bool(pin.get("enabled", True)):
                    continue
                x = pin.get("x", pin["src"][0])
                y = pin.get("y", pin["src"][1])
                source = (float(pin["src"][0]), float(pin["src"][1]))
                locked = bool(pin.get("locked"))
                rows.append({"id": int(pin.get("id", 0)), "src": source,
                             # A locked pin is an anchor: it pulls the mesh toward its
                             # original position, so the rest of the picture stays put.
                             "x": source[0] if locked else float(parameter_value_at(x, local_frame)),
                             "y": source[1] if locked else float(parameter_value_at(y, local_frame)),
                             "locked": locked})
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        return rows

    def apply_layer(self, layer):
        pins = self.pins_at(self.local_frame)
        moving = [pin for pin in pins if not pin["locked"]
                  and (abs(pin["x"] - pin["src"][0]) > 1e-6 or abs(pin["y"] - pin["src"][1]) > 1e-6)]
        if not moving:
            return layer
        return warp.apply_puppet(layer, pins, float(self.stiffness.get()),
                                 int(self.mesh_density.get()), self._cache, self.layer_rect)


class CornerPinEffect(LayerEffect):
    """Four draggable corners, i.e. a real perspective transform of the layer."""

    key = "corner_pin"
    name = "Corner Pin"

    CORNERS = (("tl", "Top left", 0.0, 0.0), ("tr", "Top right", 1.0, 0.0),
               ("br", "Bottom right", 1.0, 1.0), ("bl", "Bottom left", 0.0, 1.0))
    PARAMS = tuple(
        ParamSpec(f"corner_{name}_{axis}", f"{label} {axis.upper()}", "float", -1.0, 2.0,
                  value, step=0.002, advanced=True,
                  help="Normalized position of the corner inside the frame")
        for name, label, cx, cy in CORNERS for axis, value in (("x", cx), ("y", cy))
    )

    def __init__(self, app):
        super().__init__(app)
        for name, _label, cx, cy in self.CORNERS:
            setattr(self, f"corner_{name}_x", _value(cx))
            setattr(self, f"corner_{name}_y", _value(cy))

    def corners_at(self, local_frame):
        return [(_num(getattr(self, f"corner_{name}_x"), local_frame),
                 _num(getattr(self, f"corner_{name}_y"), local_frame))
                for name, _label, _cx, _cy in self.CORNERS]

    def apply_layer(self, layer):
        return warp.apply_corner_pin(layer, self.corners_at(self.local_frame))


class PerspectiveEffect(LayerEffect):
    """Keystone/tilt of the whole layer around its centre."""

    key = "perspective"
    name = "Perspective"

    PARAMS = (
        ParamSpec("tilt_x", "Tilt X", "float", -720.0, 720.0, 0.0, step=0.5, unit="\u00b0",
                  help="Lean the top edge toward or away from the viewer"),
        ParamSpec("tilt_y", "Tilt Y", "float", -720.0, 720.0, 0.0, step=0.5, unit="\u00b0",
                  help="Lean the right edge toward or away from the viewer"),
        ParamSpec("zoom", "Zoom", "float", 50.0, 200.0, 100.0, step=0.5, unit="%"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.tilt_x = _value(0.0)
        self.tilt_y = _value(0.0)
        self.zoom = _value(100.0)

    def apply_layer(self, layer):
        in_frame = self.local_frame
        tilt_x = _num(self.tilt_x, in_frame)
        tilt_y = _num(self.tilt_y, in_frame)
        zoom = max(0.1, _num(self.zoom, in_frame) / 100.0)
        if abs(tilt_x) < 1e-3 and abs(tilt_y) < 1e-3 and abs(zoom - 1.0) < 1e-3:
            return layer
        return warp.apply_perspective(layer, tilt_x, tilt_y, zoom)


class MeshWarpEffect(LayerEffect):
    """Control-grid warp: each grid node can be pushed, interpolated smoothly."""

    key = "mesh_warp"
    name = "Mesh Warp"

    # The control net is 5x5 and every node is live at the default grid; lowering the
    # grid only makes the warp coarser (nodes outside the active grid are kept, not
    # dropped, so turning the grid back up restores the shape).
    GRID = 5
    PARAMS = (
        ParamSpec("grid", "Grid", "int", 2, 5, GRID,
                  help="Control grid resolution (2 = corners only, 5 = dense)"),
    ) + tuple(
        ParamSpec(f"node_{row}_{col}_{axis}", f"Node {row + 1},{col + 1} {axis.upper()}", "float",
                  -1.0, 1.0, 0.0, step=0.002, advanced=True,
                  help="Normalized offset of one grid node")
        for row in range(5) for col in range(5) for axis in ("x", "y")
    )

    def __init__(self, app):
        super().__init__(app)
        self.grid = _value(self.GRID)
        for row in range(5):
            for col in range(5):
                setattr(self, f"node_{row}_{col}_x", _value(0.0))
                setattr(self, f"node_{row}_{col}_y", _value(0.0))

    def offsets_at(self, local_frame):
        size = max(2, min(5, int(round(_num(self.grid, local_frame)))))
        offsets = {}
        for row in range(size):
            for col in range(size):
                dx = _num(getattr(self, f"node_{row}_{col}_x"), local_frame)
                dy = _num(getattr(self, f"node_{row}_{col}_y"), local_frame)
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    offsets[(row, col)] = (dx, dy)
        return size, offsets

    def apply_layer(self, layer):
        size, offsets = self.offsets_at(self.local_frame)
        if not offsets:
            return layer
        return warp.apply_mesh(layer, size, size, offsets)


class SkewEffect(LayerEffect):
    """Shear the layer horizontally and/or vertically."""

    key = "skew"
    name = "Skew"

    PARAMS = (
        ParamSpec("skew_x", "Shear X", "float", -80.0, 80.0, 0.0, step=0.5, unit="\u00b0",
                  help="Slant the layer sideways"),
        ParamSpec("skew_y", "Shear Y", "float", -80.0, 80.0, 0.0, step=0.5, unit="\u00b0",
                  help="Slant the layer up or down"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.skew_x = _value(0.0)
        self.skew_y = _value(0.0)

    def apply_layer(self, layer):
        return warp.apply_skew(layer,
                               _num(self.skew_x, self.local_frame),
                               _num(self.skew_y, self.local_frame))


class DistortionEffect(LayerEffect):
    """Bulge/pinch around an adjustable centre."""

    key = "distortion"
    name = "Distortion"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", -100.0, 100.0, 30.0, step=1.0, unit="%",
                  help="Positive bulges toward the viewer, negative pinches"),
        ParamSpec("center_x", "Centre X", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("center_y", "Centre Y", "float", 0.0, 1.0, 0.5, step=0.002),
        ParamSpec("radius", "Radius", "float", 0.05, 1.5, 0.6, step=0.01),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = _value(30.0)
        self.center_x = _value(0.5)
        self.center_y = _value(0.5)
        self.radius = _value(0.6)

    def apply_layer(self, layer):
        frame = self.local_frame
        return warp.apply_distortion(
            layer,
            _num(self.amount, frame),
            (_num(self.center_x, frame),
             _num(self.center_y, frame)),
            _num(self.radius, frame))


def _value(default):
    """A fresh Value for an effect parameter (imported lazily to keep the module light)."""
    from ..model.values import Value
    return Value(default)


WARP_EFFECTS = (PuppetWarpEffect, CornerPinEffect, PerspectiveEffect, MeshWarpEffect,
                SkewEffect, DistortionEffect)
