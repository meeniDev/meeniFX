"""Effect base classes and shared tracker color logic; no GUI toolkit imports.

Every effect publishes a `PARAMS` table of `ParamSpec`s. The properties panel builds
its controls from that table and the parameter audit test drives it, so the slider a
user drags and the value the effect reads can never drift apart again.
"""

import math
from dataclasses import dataclass

import numpy as np

from ..constants import parse_palette_colors as _parse_palette_colors
from ..model.values import Value


@dataclass(frozen=True)
class ParamSpec:
    """One editable parameter: range, unit and how a preview should scale it."""

    name: str
    label: str
    kind: str = "int"           # int | float | bool | choice | text
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
    help: str = ""
    advanced: bool = False      # raw technical value: shown under the "Advanced" fold

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
        if self.kind == "text":
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


class EffectBase:
    key = "effect"
    name = "Effect"
    category = "Stylize"        # group in the Effects panel
    # Track types this effect can process. Every effect declares its media types here and
    # the UI, the timeline drop target and the engine all read that one declaration, so a
    # picture effect can never run on an audio clip (or the other way round).
    MEDIA_TYPES = ("video",)
    PARAMS = ()

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
                  default="solid", help="How each tracked object is coloured"),
        ParamSpec("color_r", "Red", "int", 0, 255, 52, help="Red channel of the solid colour"),
        ParamSpec("color_g", "Green", "int", 0, 255, 188, help="Green channel of the solid colour"),
        ParamSpec("color_b", "Blue", "int", 0, 255, 255, help="Blue channel of the solid colour"),
        ParamSpec("palette_text", "Palette", "text",
                  default="#ffae00,#28c9ff,#65f4a3,#f26d6d",
                  help="Comma-separated colours used when the mode is palette"),
    )

    def _init_color_controls(self):
        self.color_mode = Value("solid")
        self.color_r = Value(52)
        self.color_g = Value(188)
        self.color_b = Value(255)
        self.palette_text = Value("#ffae00,#28c9ff,#65f4a3,#f26d6d")
        self._color_rng = np.random.default_rng(20261028)

    def _resolve_draw_color(self, object_index=0):
        mode = str(self.color_mode.get()).strip().lower()
        if mode == "solid":
            return (int(self.color_b.get()), int(self.color_g.get()), int(self.color_r.get()))
        if mode == "random":
            rgb = self._color_rng.integers(0, 256, size=3)
            return (int(rgb[2]), int(rgb[1]), int(rgb[0]))
        if mode == "palette":
            palette = _parse_palette_colors(self.palette_text.get())
            if palette:
                return palette[object_index % len(palette)]
            return (0, 255, 255)
        return (0, 255, 255)
