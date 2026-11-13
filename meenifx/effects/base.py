"""Effect base classes and shared tracker color logic; no GUI toolkit imports.

Every effect publishes a `PARAMS` table of `ParamSpec`s. The properties panel builds
its controls from that table and the parameter audit test drives it, so the slider a
user drags and the value the effect reads can never drift apart again.
"""

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass

import cv2
import numpy as np

from ..constants import parse_palette_colors as _parse_palette_colors
from ..model.values import Value
from . import tracking_regions as REGIONS
from .tracking_viz import STYLE_KEYS, TrackStabilizer, draw_tracking, resolve_style


@dataclass(frozen=True)
class ParamSpec:
    """One editable parameter: range, unit and how a preview should scale it."""

    name: str
    label: str
    kind: str = "int"           # int | float | bool | choice | text | media | font
    minimum: float = 0.0
    maximum: float = 1.0
    default: object = 0
    step: float = 1.0
    unit: str = ""
    soft_min: float = None
    soft_max: float = None
    choices: tuple = ()
    # Human labels for `choices`, same order. A combo must never show a raw key like
    # ``black_white`` or ``soft_light``; when a choice has no label the key is tidied
    # (``black_white`` -> ``Black white``) so every dropdown still reads as prose.
    choice_labels: tuple = ()
    pixels: bool = False        # value is in logical-resolution pixels
    area: bool = False          # value is a pixel area (scales with quality^2)
    # Decimal places the control shows and keeps. None = the old default (0 for an int,
    # 2 for a float); a rate such as 23.976 sets 3 so typing it is not rounded to 23.98.
    decimals: int = None
    help: str = ""
    advanced: bool = False      # raw technical value: shown under the "Advanced" fold
    # Optional collapsible section inside the effect card ("Detection", "Labels"...).
    # Empty means the parameter sits directly in the card; a group is only rendered when
    # the effect really declares members for it, so no card grows empty headings.
    group: str = ""
    # Typed media parameters use the reusable asset picker.  "image" accepts still
    # pictures; "lut" accepts .cube tables.  The stored value remains the path string,
    # so rendering and old projects are unchanged.
    media_kind: str = "image"

    def choice_items(self):
        """[(key, label)] for a choice parameter, labels falling back to a tidied key."""
        labels = list(self.choice_labels)
        items = []
        for index, key in enumerate(self.choices):
            label = labels[index] if index < len(labels) and labels[index] else str(key)
            if label == str(key) and "_" in label:
                label = label.replace("_", " ").capitalize()
            items.append((key, label))
        return items

    def choice_index(self, value):
        """Index of `value` in `choices` (0 when it is unknown, so a combo always shows something)."""
        try:
            return list(self.choices).index(value)
        except ValueError:
            return 0

    def clamp(self, value):
        if self.kind == "bool":
            return bool(value)
        if self.kind == "choice":
            keys = list(self.choices)
            if not keys:
                return value
            if value in keys:
                return value
            text = str(value or "").strip().lower()
            match = next((key for key in keys if str(key).lower() == text), None)
            return match if match is not None else keys[0]
        if self.kind in ("text", "media", "font"):
            return value
        try:
            number = float(value)
        except (TypeError, ValueError):
            return self.default
        if not math.isfinite(number):
            # NaN/inf have no meaningful slider position; the default is the honest answer
            # (clamping NaN would silently land on the maximum).
            return self.default
        if not self.minimum <= number <= self.maximum:
            number = max(self.minimum, min(self.maximum, number))
        return int(round(number)) if self.kind == "int" else number


def param_number(value, frame=0):
    """A parameter's number at `frame`: a Value, an animated curve or a plain number.

    One helper for every effect module, so keyframed parameters behave the same
    everywhere and no effect accidentally reads an animated curve's base value only.
    """
    from ..model.keyframes import AnimatedValue, parameter_value_at
    if isinstance(value, AnimatedValue):
        return float(parameter_value_at(value, frame))
    if hasattr(value, "get"):
        return float(value.get())
    return float(value)


class EffectBase:
    key = "effect"
    name = "Effect"
    category = "Stylize"        # group in the Effects panel
    # Track types this effect can process. Every effect declares its media types here and
    # the UI, the timeline drop target and the engine all read that one declaration, so a
    # picture effect can never run on an audio clip (or the other way round).
    MEDIA_TYPES = ("video",)
    PARAMS = ()
    # A sentence the properties card shows under the controls when the effect only applies
    # to some sources (an alpha edge, a face, a matte). Empty means no note.
    NOTE = ""

    def __init__(self, app):
        self.app = app
        self.defaults = {}
        # 1.0 renders the logical frame; a lower preview quality sets the render
        # scale, and pixel-based parameters are multiplied by it so the picture
        # looks the same as it will in the full-resolution export.
        self.param_scale = 1.0
        # Set per call by the engine: the clip's effect entry (puppet pins and other
        # structured data live there) and the clip-local frame being rendered.
        self.clip_entry = None
        # The clip itself, so motion-aware effects can sample its animation.
        self.clip = None
        self.local_frame = 0
        # Where the picture sits inside the layer, in layer pixels. A letterboxed picture
        # leaves transparent margins; warps use this so they deform the picture only.
        self.layer_rect = None

    def apply(self, frame):
        return frame

    def on_registered(self):
        pass

    @classmethod
    def param_specs(cls):
        return list(cls.PARAMS)

    @classmethod
    def media_types(cls):
        return tuple(cls.MEDIA_TYPES)

    @classmethod
    def supports_track_type(cls, track_type):
        return (track_type or "video") in cls.MEDIA_TYPES

    @classmethod
    def media_label(cls):
        """Human wording for the tooltip: "video", "audio", "video and audio"."""
        names = {"video": "video", "audio": "audio"}
        return " and ".join(names.get(kind, kind) for kind in cls.MEDIA_TYPES)

    def spec(self, name):
        return next((item for item in self.PARAMS if item.name == name), None)

    def param_vars(self):
        """Return effect parameter value objects (UI is built by the Qt editor)."""
        return {name: value for name, value in vars(self).items()
                if isinstance(value, Value) and not name.startswith("_")}

    def capture_params(self):
        return {name: value.get() for name, value in self.param_vars().items()}

    def load_params(self, params):
        """Apply values captured for a particular clip to the processing instance."""
        for name, value in self.param_vars().items():
            if name in params:
                value.set(params[name])

    # ------------------------------------------------------------ pixel scaling
    def px(self, value):
        """A logical-pixel parameter at the current render scale (never below 1)."""
        return max(1, int(round(float(value) * self.param_scale)))

    def area(self, value):
        """A logical-pixel area at the current render scale."""
        return max(1, int(round(float(value) * self.param_scale * self.param_scale)))

    @staticmethod
    def scaled(value, param_scale):
        return max(1, int(round(float(value) * param_scale)))

    # ------------------------------------------------------- cheap smooth fields
    @staticmethod
    def work_size(width, height, max_edge=720):
        """(w, h, factor) for computing a smooth field at a bounded resolution.

        Glow, bloom, leaks, vignettes and grain are low-frequency: computing them on a
        smaller grid and scaling the result back looks the same and costs a fraction of
        a full-resolution pass. The size depends only on the render size, so preview and
        export stay deterministic.
        """
        factor = min(1.0, float(max_edge) / max(1.0, float(max(width, height))))
        return max(1, int(round(width * factor))), max(1, int(round(height * factor))), factor

    @staticmethod
    def resize_to(field, width, height):
        """Scale a 2-D/3-D float field back to the layer's size (no-op when equal)."""
        if field.shape[1] == width and field.shape[0] == height:
            return field
        resized = cv2.resize(field, (int(width), int(height)), interpolation=cv2.INTER_LINEAR)
        if field.ndim == 3 and resized.ndim == 2:
            # OpenCV drops a single-channel axis; callers rely on the last axis to
            # broadcast a field across BGR.
            resized = resized[:, :, None]
        return resized


class LayerEffect(EffectBase):
    """An effect that works on BGRA layers instead of flat frames.

    Layer effects get the alpha channel, so a blur, glow or colour grade can keep the
    picture's transparency (rotated clips, text, PNG characters) instead of painting
    black into the corners. `apply_effects_layer` in the engine prefers `apply_layer`
    whenever it exists and always feeds it the current BGRA layer.
    """

    category = "Warp"

    def apply(self, frame):
        # Keep the effect usable from code that only has a BGR frame.
        layer = np.dstack([frame, np.full(frame.shape[:2], 255, dtype=np.uint8)])
        return self.apply_layer(layer)[:, :, :3]

    def apply_layer(self, layer):
        return layer

    def entry_pins(self, entry):
        return (entry or {}).get("pins") or []


class TrackerColorMixin:
    COLOR_PARAMS = (
        ParamSpec("color_mode", "Color mode", "choice", choices=("solid", "random", "palette"),
                  default="solid", help="How each tracked object is coloured",
                  group="Visualization"),
        ParamSpec("color_r", "Red", "int", 0, 255, 52, help="Red channel of the solid colour",
                  group="Visualization"),
        ParamSpec("color_g", "Green", "int", 0, 255, 188, help="Green channel of the solid colour",
                  group="Visualization"),
        ParamSpec("color_b", "Blue", "int", 0, 255, 255, help="Blue channel of the solid colour",
                  group="Visualization"),
        ParamSpec("palette_text", "Palette", "text",
                  default="#ffae00,#28c9ff,#65f4a3,#f26d6d",
                  help="Comma-separated colours used when the mode is palette",
                  group="Visualization"),
    )

    # One set of visibility controls shared by every tracker.  The visual language must
    # not change just because the detector is looking for faces instead of blobs.
    VIS_PARAMS = (
        ParamSpec("viz_preset", "Look", "choice",
                  choices=("custom", "clean", "minimal", "numbered", "neon", "hud", "points"),
                  choice_labels=("Custom", "Clean detection", "Minimal boxes",
                                 "Numbered tracking", "Neon connectors", "Technical HUD",
                                 "Motion points"),
                  default="custom",
                  help="A coherent starting style; Custom uses the controls below"),
        ParamSpec("show_box", "Box", "bool", default=True,
                  help="Draw a box, corner brackets or an ellipse around each detection",
                  group="Visualization"),
        ParamSpec("box_style", "Box style", "choice",
                  choices=("rectangle", "corners", "ellipse"),
                  choice_labels=("Rectangle", "Corner brackets", "Ellipse"),
                  default="rectangle", group="Visualization"),
        ParamSpec("show_contour", "Outline", "bool", default=False,
                  help="Draw the detected contour or hull when the detector provides one",
                  group="Visualization"),
        ParamSpec("show_circle", "Circle", "bool", default=False,
                  help="Draw a circle enclosing each detection", group="Visualization"),
        ParamSpec("show_points", "Points", "bool", default=False,
                  help="Draw sampled contour points (box corners when there is no contour)",
                  group="Visualization"),
        ParamSpec("show_labels", "Labels", "bool", default=False,
                  help="Name each detection; tracking numbers are a separate control",
                  group="Labels"),
        ParamSpec("show_numbers", "Numbers", "bool", default=False,
                  help="Draw a separate tracking number", group="Labels"),
        ParamSpec("label_text", "Label text", "text", default="",
                  help="Text drawn by Labels. Variables: {id} {x} {y} {width} {height} "
                       "{speed} {confidence}", group="Labels"),
        ParamSpec("label_font", "Font", "text", default="Segoe UI",
                  help="Installed font family, for example Segoe UI or Arial",
                  group="Labels"),
        ParamSpec("label_weight", "Weight", "choice", choices=("regular", "bold"),
                  choice_labels=("Regular", "Bold"), default="regular", group="Labels"),
        ParamSpec("label_size", "Font size", "int", 8, 96, 22, unit=" px", pixels=True,
                  group="Labels"),
        ParamSpec("label_color", "Text colour", "text", default="#FFFFFF",
                  group="Labels"),
        ParamSpec("label_outline_color", "Outline colour", "text", default="#000000",
                  group="Labels"),
        ParamSpec("label_outline", "Outline", "int", 0, 8, 0, unit=" px", pixels=True,
                  group="Labels"),
        ParamSpec("label_background", "Background", "text", default="#000000",
                  group="Labels"),
        ParamSpec("label_background_opacity", "Background opacity", "int", 0, 100, 70,
                  unit=" %", group="Labels"),
        ParamSpec("label_padding", "Padding", "int", 0, 32, 6, unit=" px", pixels=True,
                  group="Labels"),
        ParamSpec("label_radius", "Corner radius", "int", 0, 32, 6, unit=" px",
                  pixels=True, group="Labels"),
        ParamSpec("label_position", "Position", "choice",
                  choices=("above", "below", "inside", "left", "right"),
                  choice_labels=("Above", "Below", "Inside", "Left", "Right"),
                  default="above", group="Labels"),
        ParamSpec("label_anchor", "Anchor", "choice",
                  choices=("top_left", "top_center", "top_right", "middle_left",
                           "center", "middle_right", "bottom_left", "bottom_center",
                           "bottom_right"),
                  choice_labels=("Top left", "Top centre", "Top right", "Middle left",
                                 "Centre", "Middle right", "Bottom left",
                                 "Bottom centre", "Bottom right"),
                  default="top_left", group="Labels"),
        ParamSpec("label_offset_x", "Offset X", "int", -300, 300, 0, unit=" px",
                  pixels=True, group="Labels"),
        ParamSpec("label_offset_y", "Offset Y", "int", -300, 300, 0, unit=" px",
                  pixels=True, group="Labels"),
        ParamSpec("label_align", "Alignment", "choice", choices=("left", "center", "right"),
                  choice_labels=("Left", "Centre", "Right"), default="left",
                  group="Labels"),
        ParamSpec("connector_mode", "Connectors", "choice",
                  choices=("none", "nearest", "distance", "sequential", "center", "custom"),
                  choice_labels=("None", "Nearest neighbour", "All within range",
                                 "Join in track order", "Connect to centre",
                                 "Custom links"),
                  default="none", help="Lines between detections in the same frame",
                  group="Connectors"),
        ParamSpec("custom_links", "Custom links", "text", default="",
                  help="For Custom links: track pairs such as 1-2; 2-3", group="Connectors"),
        ParamSpec("connector_color", "Line colour", "text", default="",
                  help="Empty uses each object's colour; otherwise #RRGGBB",
                  group="Connectors"),
        ParamSpec("connector_thickness", "Line thickness", "int", 1, 16, 2, unit=" px",
                  pixels=True, group="Connectors"),
        ParamSpec("connector_opacity", "Line opacity", "int", 5, 100, 100, unit=" %",
                  group="Connectors"),
        ParamSpec("connector_glow", "Line glow", "bool", default=False,
                  help="Soft halo under the connector lines", group="Connectors"),
        ParamSpec("connector_curve", "Curve", "int", 0, 100, 0, unit=" %",
                  help="0 draws straight lines; higher values bow them out",
                  group="Connectors"),
        ParamSpec("connector_pulses", "Pulses", "bool", default=False,
                  help="Animated dots travelling along each connector",
                  group="Connectors"),
        ParamSpec("connector_pulse_speed", "Pulse speed", "int", 1, 20, 4,
                  help="How fast pulses travel", group="Connectors"),
        ParamSpec("connector_labels", "Distance labels", "bool", default=False,
                  help="Show the pixel distance beside each connector",
                  group="Connectors"),
        ParamSpec("stable_ids", "Stable IDs", "bool", default=True,
                  help="Match objects across frames so connectors do not jump",
                  group="Detection"),
        ParamSpec("id_match_distance", "ID match range", "int", 5, 100, 40, unit=" %",
                  help="How far an object may move between frames and keep its ID",
                  group="Detection"),
        ParamSpec("region_effect_1", "Region effect 1", "choice", choices=REGIONS.REGION_EFFECTS,
                  choice_labels=REGIONS.REGION_LABELS, default="none",
                  help="An effect applied only inside each detected region",
                  group="Tracked region"),
        ParamSpec("region_amount_1", "Amount 1", "int", 1, 100, 50, unit=" %",
                  group="Tracked region"),
        ParamSpec("region_effect_2", "Region effect 2", "choice", choices=REGIONS.REGION_EFFECTS,
                  choice_labels=REGIONS.REGION_LABELS, default="none",
                  help="A second effect inside the same region", group="Tracked region"),
        ParamSpec("region_amount_2", "Amount 2", "int", 1, 100, 50, unit=" %",
                  group="Tracked region"),
        ParamSpec("region_effect_3", "Region effect 3", "choice", choices=REGIONS.REGION_EFFECTS,
                  choice_labels=REGIONS.REGION_LABELS, default="none",
                  help="A third effect inside the same region", group="Tracked region"),
        ParamSpec("region_amount_3", "Amount 3", "int", 1, 100, 50, unit=" %",
                  group="Tracked region"),
        ParamSpec("region_expand", "Expand", "int", -50, 50, 0, unit=" px", pixels=True,
                  help="Grow or shrink the tracked region", group="Tracked region"),
        ParamSpec("region_feather", "Feather", "int", 0, 80, 8, unit=" px", pixels=True,
                  help="Soften the region edge", group="Tracked region"),
        ParamSpec("region_smoothing", "Smoothing", "int", 0, 95, 0, unit=" %",
                  help="Follow the region more slowly to reduce jitter",
                  group="Tracked region"),
        ParamSpec("connector_distance", "Connector range", "int", 5, 100, 25, unit=" %",
                  help="Longest connector, as a percentage of the longer frame edge",
                  group="Connectors"),
        ParamSpec("max_connections", "Max links", "int", 1, 6, 2,
                  help="How many neighbours each detection may link to",
                  group="Connectors"),
        ParamSpec("glow", "Glow", "bool", default=False,
                  help="Add a soft glow under the tracking lines", group="Visualization"),
        ParamSpec("opacity", "Opacity", "int", 20, 100, 100, unit=" %",
                  help="Opacity of the tracking overlay", group="Visualization"),
        ParamSpec("tracking_quality", "Detection quality", "choice",
                  choices=("fast", "balanced", "precise"),
                  choice_labels=("Fast", "Balanced", "Precise"),
                  default="balanced",
                  help="Detect on a smaller working image for speed; boxes are scaled back",
                  group="Detection"),
    )

    TRACKING_LABEL = "Object"

    def _init_color_controls(self):
        self.color_mode = Value("solid")
        self.color_r = Value(52)
        self.color_g = Value(188)
        self.color_b = Value(255)
        self.palette_text = Value("#ffae00,#28c9ff,#65f4a3,#f26d6d")
        self.viz_preset = Value("custom")
        self.show_box = Value(True)
        self.box_style = Value("rectangle")
        self.show_contour = Value(False)
        self.show_circle = Value(False)
        self.show_points = Value(False)
        self.show_labels = Value(False)
        self.show_numbers = Value(False)
        # New effects get a useful default label, but old projects that stored an empty
        # label_text stay empty.  Empty now means "draw no text" rather than silently
        # falling back to "Face" and making the Labels control impossible to remove.
        self.label_text = Value(str(getattr(self, "TRACKING_LABEL", "") or ""))
        self.label_font = Value("Segoe UI")
        self.label_weight = Value("regular")
        self.label_size = Value(22)
        self.label_color = Value("#FFFFFF")
        self.label_outline_color = Value("#000000")
        self.label_outline = Value(0)
        self.label_background = Value("#000000")
        self.label_background_opacity = Value(70)
        self.label_padding = Value(6)
        self.label_radius = Value(6)
        self.label_position = Value("above")
        self.label_anchor = Value("top_left")
        self.label_offset_x = Value(0)
        self.label_offset_y = Value(0)
        self.label_align = Value("left")
        self.connector_mode = Value("none")
        self.connector_distance = Value(25)
        self.max_connections = Value(2)
        self.custom_links = Value("")
        self.connector_color = Value("")
        self.connector_thickness = Value(2)
        self.connector_opacity = Value(100)
        self.connector_glow = Value(False)
        self.connector_curve = Value(0)
        self.connector_pulses = Value(False)
        self.connector_pulse_speed = Value(4)
        self.connector_labels = Value(False)
        self.stable_ids = Value(True)
        self.id_match_distance = Value(40)
        self.region_effect_1 = Value("none")
        self.region_amount_1 = Value(50)
        self.region_effect_2 = Value("none")
        self.region_amount_2 = Value(50)
        self.region_effect_3 = Value("none")
        self.region_amount_3 = Value(50)
        self.region_expand = Value(0)
        self.region_feather = Value(8)
        self.region_smoothing = Value(0)
        self.glow = Value(False)
        self.opacity = Value(100)
        self.tracking_quality = Value("balanced")
        self._tracking_cache = OrderedDict()
        self._tracking_cache_limit = 8
        self._stabilizer = TrackStabilizer()
        self._region_masks = {}

    def load_params(self, params):
        """Apply parameters, including the tracker controls used before this version.

        Projects saved before the shared tracking panel stored ``draw_box``/``draw_hull``,
        ``draw_mode`` and ``label_faces``.  Mapping them here keeps those projects looking
        the same without keeping duplicate controls in the properties panel.
        """
        mapped = dict(params or {})
        if "label_faces" in mapped:
            mapped["show_labels"] = bool(mapped["label_faces"])
        if "draw_hull" in mapped:
            mapped["show_contour"] = bool(mapped["draw_hull"])
        if "draw_box" in mapped:
            mapped["show_box"] = bool(mapped["draw_box"])
        mode = str(mapped.pop("draw_mode", "") or "").strip().lower()
        if mode:
            mapped["show_box"] = mode == "box"
            mapped["show_contour"] = mode == "contour"
            mapped["show_circle"] = mode == "circle"
        # Projects saved before label_text existed must not inherit the new "Face"
        # default along with an already-enabled label.  An absent legacy value means
        # no text; the user can type one or turn Labels back on explicitly.
        if "label_text" not in mapped:
            mapped["label_text"] = ""
        super().load_params(mapped)

    def _resolve_draw_color(self, object_index=0):
        mode = str(self.color_mode.get()).strip().lower()
        if mode == "solid":
            return (int(self.color_b.get()), int(self.color_g.get()), int(self.color_r.get()))
        if mode == "random":
            # Derive the colour from the object's index instead of advancing an RNG: a
            # random colour that changes every frame looks like a broken overlay.
            digest = hashlib.blake2b(str(int(object_index)).encode(), digest_size=3).digest()
            return (int(digest[2]), int(digest[1]), int(digest[0]))
        if mode == "palette":
            palette = _parse_palette_colors(self.palette_text.get())
            if palette:
                return palette[object_index % len(palette)]
            return (0, 255, 255)
        return (0, 255, 255)

    # --------------------------------------------------------------- shared visuals
    def _tracking_style(self):
        manual = {key: getattr(self, key).get() for key in STYLE_KEYS}
        manual["opacity"] = int(manual.get("opacity", 100) or 100)
        defaults = {spec.name: spec.default for spec in self.VIS_PARAMS}
        style = resolve_style(self.viz_preset.get(), manual, defaults)
        # The Labels checkbox is authoritative even when an old project still names a
        # preset whose defaults include labels.  Otherwise false looked identical to
        # "unset" and the checkbox appeared to do nothing.
        if not bool(self.show_labels.get()):
            style["show_labels"] = False
        return style

    def _label_style(self):
        scale = max(0.2, float(self.param_scale or 1.0))
        return {
            "label_font": self.label_font.get(),
            "label_weight": self.label_weight.get(),
            "label_size": float(self.label_size.get()),
            "label_scale": scale,
            "label_color": self.label_color.get(),
            "label_outline_color": self.label_outline_color.get(),
            "label_outline": float(self.px(self.label_outline.get())),
            "label_background": self.label_background.get(),
            "label_background_opacity": int(self.label_background_opacity.get()),
            "label_padding": float(self.px(self.label_padding.get())),
            "label_radius": float(self.px(self.label_radius.get())),
            "label_position": self.label_position.get(),
            "label_anchor": self.label_anchor.get(),
            "label_offset_x": float(self.px(self.label_offset_x.get()))
            if self.label_offset_x.get() >= 0 else -float(self.px(-self.label_offset_x.get())),
            "label_offset_y": float(self.px(self.label_offset_y.get()))
            if self.label_offset_y.get() >= 0 else -float(self.px(-self.label_offset_y.get())),
            "label_align": self.label_align.get(),
        }

    def _region_stack(self):
        stack = []
        for index in (1, 2, 3):
            effect = getattr(self, f"region_effect_{index}").get()
            if str(effect or "none").lower() != "none":
                stack.append((effect, int(getattr(self, f"region_amount_{index}").get())))
        return stack

    def apply_tracked_regions(self, output, detections):
        """Apply this tracker's region effects inside each real detection."""
        stack = self._region_stack()
        if not stack or not detections:
            return output
        expand = self.px(self.region_expand.get())
        if self.region_expand.get() < 0:
            expand = -self.px(-self.region_expand.get())
        feather = self.px(self.region_feather.get())
        smoothing = max(0.0, min(0.95, float(self.region_smoothing.get()) / 100.0))
        for index, detection in enumerate(detections):
            key = int(detection.track_id or index + 1)
            mask = REGIONS.region_mask(output.shape, detection.box, detection.contour,
                                       expand=expand, feather=feather)
            if smoothing > 0.0:
                previous = self._region_masks.get(key)
                if previous is not None and previous.shape == mask.shape:
                    mask = cv2.addWeighted(previous, smoothing, mask, 1.0 - smoothing, 0)
                self._region_masks[key] = mask
            output = REGIONS.apply_stack(output, mask, stack, scale=max(0.2, self.param_scale))
        return output

    def apply_layer(self, layer):
        """Run the tracker on the layer's picture and keep its transparency intact."""
        if layer.ndim == 3 and layer.shape[2] == 4:
            picture = np.ascontiguousarray(layer[:, :, :3])
            output = self.apply(picture)
            return np.dstack([output, layer[:, :, 3]])
        return self.apply(layer)

    def draw_tracking(self, frame, detections, label=None, thickness=None):
        """Draw detections with this tracker's shared style and colour scheme."""
        style = self._tracking_style()
        detections = list(detections or ())
        if bool(self.stable_ids.get()):
            detections = self._stabilizer.update(
                detections, frame=int(getattr(self, "local_frame", 0) or 0),
                max_distance=float(self.id_match_distance.get()) / 100.0
                * max(frame.shape[:2]))
        colors = [self._resolve_draw_color(index) for index in range(len(detections))]
        return draw_tracking(
            frame, detections,
            color=self._resolve_draw_color(0),
            colors=colors,
            thickness=max(1, self.px(thickness if thickness is not None else self.thickness.get())),
            opacity=style.get("opacity", 100),
            show_box=bool(style.get("show_box", True)),
            show_contour=bool(style.get("show_contour", False)),
            show_points=bool(style.get("show_points", False)),
            show_circle=bool(style.get("show_circle", False)),
            show_labels=bool(style.get("show_labels", False)),
            show_numbers=bool(style.get("show_numbers", False)),
            # An empty label field means no text.  Do not silently fall back to
            # "Face"/"Hand"/"Blob": that made the Labels control impossible to remove.
            label_text=str(self.label_text.get() or "").strip(),
            box_style=str(style.get("box_style", "rectangle")),
            connector_mode=str(style.get("connector_mode", "none")),
            connector_distance=int(self.connector_distance.get()),
            max_connections=int(self.max_connections.get()),
            glow=bool(style.get("glow", False)),
            label_scale=0.55 * max(0.4, self.param_scale),
            label_style=self._label_style(),
            connector_color=self.connector_color.get(),
            connector_thickness=self.px(self.connector_thickness.get()),
            connector_opacity=int(self.connector_opacity.get()),
            connector_glow=bool(self.connector_glow.get()),
            connector_curve=int(self.connector_curve.get()),
            connector_pulses=bool(self.connector_pulses.get()),
            connector_pulse_speed=int(self.connector_pulse_speed.get()),
            connector_labels=bool(self.connector_labels.get()),
            custom_links=self.custom_links.get(),
            frame_index=int(getattr(self, "local_frame", 0) or 0),
            skeleton=bool(getattr(self, "show_skeleton", Value(False)).get()),
            skeleton_connections=getattr(self, "SKELETON_CONNECTIONS", ()),
            point_size=self.px(getattr(self, "point_size", Value(3)).get()),
            line_width=self.px(getattr(self, "line_width", Value(2)).get()),
            skeleton_color=str(getattr(self, "skeleton_color", Value("")).get() or ""),
        )

    # ------------------------------------------------------------ detection support
    def _detection_scale(self, frame, fast_edge=480, balanced_edge=720, precise_edge=None):
        """(working frame, factor) for the chosen detection quality.

        ``None`` for ``precise_edge`` means full resolution; the default Balanced cap is
        720 px and Fast is 480 px.  Boxes and contours are always scaled back to the
        caller's frame, so the chosen working size never changes the overlay's geometry.
        """
        quality = str(self.tracking_quality.get()).strip().lower()
        height, width = frame.shape[:2]
        if quality == "precise":
            max_edge = precise_edge
        elif quality == "fast":
            max_edge = fast_edge
        else:
            max_edge = balanced_edge
        longest = max(width, height)
        if not max_edge or longest <= max_edge:
            return frame, 1.0
        factor = max_edge / float(longest)
        size = (max(1, int(round(width * factor))), max(1, int(round(height * factor))))
        return cv2.resize(frame, size, interpolation=cv2.INTER_AREA), factor

    @staticmethod
    def _frame_signature(frame):
        """A cheap perceptual key for the cache; exact pixel equality is not required."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
        return hashlib.blake2b(small.tobytes(), digest_size=12).digest()

    def _cached_detections(self, frame, signature, compute):
        """Memoise one detector result for repeated preview/export passes.

        The key includes a small frame hash and the detector parameters.  Eight entries
        cover alternating preview/export scales without letting a long project grow a
        second media cache in memory.
        """
        key = (frame.shape[:2], self._frame_signature(frame), tuple(signature))
        cached = self._tracking_cache.get(key)
        if cached is not None:
            self._tracking_cache.move_to_end(key)
            return cached
        value = compute()
        self._tracking_cache[key] = value
        self._tracking_cache.move_to_end(key)
        while len(self._tracking_cache) > self._tracking_cache_limit:
            self._tracking_cache.popitem(last=False)
        return value

    def clear_tracking_cache(self):
        self._tracking_cache.clear()
        self._stabilizer.reset()
        self._region_masks.clear()
