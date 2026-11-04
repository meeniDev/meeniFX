"""Colour correction: one grade, real colour maths, alpha preserved.

The effect runs on the BGRA layer, so a graded PNG character, a rotated clip or a
text layer keeps its transparency instead of gaining a black border. The pipeline is
fixed and documented so preview and export can never drift apart:

``exposure -> white balance (temperature/tint) -> RGB gains -> brightness ->
contrast -> shadows/highlights -> gamma -> saturation -> hue -> preset grade``

Highlights and shadows are tone-curve weights rather than global brightness, the
saturation mixer works on Rec.709 luma, and hue rotates in YIQ space, so no step
invents channel values the way an arbitrary per-pixel tweak would.

Presets are applied after the manual controls, on purpose: a preset is a starting
grade, and the sliders then refine it. Everything numeric can be keyframed like any
other effect parameter.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec

PRESETS = (
    ("none", "None"),
    ("black_white", "Black and White"),
    ("warm", "Warm"),
    ("cool", "Cool"),
    ("high_contrast", "High Contrast"),
    ("vintage", "Vintage"),
    ("desaturated", "Desaturated"),
    ("cinematic", "Cinematic Contrast"),
)
PRESET_KEYS = tuple(key for key, _label in PRESETS)

# Rec.709 luma weights: the same numbers every other grade in the app uses.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Hue rotation happens in HSV: the hue angle moves and value/saturation stay, which
# keeps the picture's brightness instead of clipping channels the way an RGB matrix does.

def _rotate_hue(rgb, degrees):
    """Rotate the hue of a float BGR array by `degrees`, keeping value and saturation."""
    data = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    hsv = cv2.cvtColor(data, cv2.COLOR_BGR2HSV)          # 8-bit H runs 0..179
    shift = int(round(float(degrees) / 2.0)) % 180
    if shift:
        hsv[:, :, 0] = (hsv[:, :, 0].astype(np.int16) + shift) % 180
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).astype(np.float32)


def _num(value, frame=0):
    """A parameter's number: engine Value, animated curve or plain number."""
    from ..model.keyframes import AnimatedValue, parameter_value_at
    if isinstance(value, AnimatedValue):
        return float(parameter_value_at(value, frame))
    if hasattr(value, "get"):
        return float(value.get())
    return float(value)


def _clamp01(value, low, high):
    return max(low, min(high, float(value)))


def preset_grade(rgb, preset):
    """Apply a named grade to a float array in place-safe fashion (returns new array)."""
    key = str(preset or "none").strip().lower()
    if key in ("", "none") or key not in PRESET_KEYS:
        return rgb
    out = rgb
    if key == "black_white":
        luma = (out @ _LUMA)[..., None]
        out = np.repeat(luma, 3, axis=2)
    elif key == "warm":
        out = out * np.array([1.10, 1.02, 0.88], dtype=np.float32) + np.array([4.0, 0.0, 0.0], dtype=np.float32)
    elif key == "cool":
        out = out * np.array([0.88, 0.99, 1.12], dtype=np.float32) + np.array([0.0, 0.0, 6.0], dtype=np.float32)
    elif key == "high_contrast":
        out = (out - 128.0) * 1.35 + 128.0
    elif key == "vintage":
        # Faded blacks, warm cast, lifted shadows and a gentle desaturation.
        luma = (out @ _LUMA)[..., None]
        out = luma + (out - luma) * 0.55
        out = out * np.array([1.06, 0.98, 0.86], dtype=np.float32) + np.array([14.0, 8.0, 2.0], dtype=np.float32)
        out = 255.0 * np.power(np.clip(out, 0.0, 255.0) / 255.0, 0.92)
    elif key == "desaturated":
        luma = (out @ _LUMA)[..., None]
        out = luma + (out - luma) * 0.35
    elif key == "cinematic":
        # Teal shadows, warm highlights, a mild S-curve: the classic blockbuster look.
        luma = np.clip((out @ _LUMA) / 255.0, 0.0, 1.0)[..., None]
        shadow = (1.0 - luma) ** 2
        high = luma ** 2
        out = out + shadow * np.array([-8.0, 2.0, 10.0], dtype=np.float32)
        out = out + high * np.array([8.0, 2.0, -6.0], dtype=np.float32)
        out = (out - 128.0) * 1.15 + 128.0
        luma = (out @ _LUMA)[..., None]
        out = luma + (out - luma) * 0.92
    return out


def grade_array(rgb, params):
    """Apply the documented colour pipeline to a float32 (H, W, 3) array."""
    out = rgb
    exposure = params.get("exposure", 0.0)
    if abs(exposure) > 1e-4:
        out = out * float(2.0 ** _clamp01(exposure, -8.0, 8.0))
    temperature = params.get("temperature", 0.0)
    tint = params.get("tint", 0.0)
    if abs(temperature) > 1e-4 or abs(tint) > 1e-4:
        gains = np.array([1.0 + temperature * 0.004,
                          1.0 - tint * 0.002,
                          1.0 - temperature * 0.004], dtype=np.float32)
        out = out * gains
    red, green, blue = params.get("red_gain", 100.0), params.get("green_gain", 100.0), params.get("blue_gain", 100.0)
    if abs(red - 100.0) > 1e-4 or abs(green - 100.0) > 1e-4 or abs(blue - 100.0) > 1e-4:
        out = out * np.array([red, green, blue], dtype=np.float32) / 100.0
    brightness = params.get("brightness", 0.0)
    if abs(brightness) > 1e-4:
        out = out + brightness * 2.55
    contrast = params.get("contrast", 0.0)
    if abs(contrast) > 1e-4:
        c = _clamp01(contrast, -99.0, 99.0) * 2.55
        factor = (259.0 * (c + 255.0)) / (255.0 * (259.0 - c))
        out = (out - 128.0) * factor + 128.0
    shadows = params.get("shadows", 0.0)
    highlights = params.get("highlights", 0.0)
    if abs(shadows) > 1e-4 or abs(highlights) > 1e-4:
        luma = np.clip((out @ _LUMA) / 255.0, 0.0, 1.0)[..., None]
        if abs(shadows) > 1e-4:
            out = out + (shadows * 0.45) * ((1.0 - luma) ** 2)
        if abs(highlights) > 1e-4:
            out = out + (highlights * 0.45) * (luma ** 2)
    gamma = params.get("gamma", 1.0)
    if abs(gamma - 1.0) > 1e-4:
        gamma = _clamp01(gamma, 0.1, 4.0)
        out = 255.0 * np.power(np.clip(out, 0.0, 255.0) / 255.0, 1.0 / gamma)
    saturation = params.get("saturation", 0.0)
    if abs(saturation) > 1e-4:
        amount = 1.0 + _clamp01(saturation, -100.0, 200.0) / 100.0
        luma = (out @ _LUMA)[..., None]
        out = luma + (out - luma) * amount
    hue = params.get("hue", 0.0)
    if abs(hue) > 1e-4:
        out = _rotate_hue(out, hue)
    return preset_grade(out, params.get("preset", "none"))


class ColorCorrectEffect(LayerEffect):
    """Brightness/contrast/saturation/exposure/highlights/shadows/temp/tint/gamma/hue/RGB."""

    key = "color_correct"
    name = "Color Correction"
    category = "Color Correction"

    PARAMS = (
        ParamSpec("preset", "Preset", "choice", default="none",
                  choices=PRESET_KEYS, choice_labels=tuple(label for _key, label in PRESETS),
                  help="A grade for the whole clip, applied after the sliders. It is not a "
                       "curve: choosing one never creates keyframes."),
        ParamSpec("exposure", "Exposure", "float", -5.0, 5.0, 0.0, step=0.05, unit=" EV",
                  help="Stops of exposure, like a camera: +1 EV doubles the light"),
        ParamSpec("brightness", "Brightness", "float", -100.0, 100.0, 0.0, step=1.0,
                  help="Additive brightness, in percent of full scale"),
        ParamSpec("contrast", "Contrast", "float", -100.0, 100.0, 0.0, step=1.0,
                  help="Spread around mid grey; negative flattens the picture"),
        ParamSpec("saturation", "Saturation", "float", -100.0, 200.0, 0.0, step=1.0,
                  help="-100 is black and white, 100 doubles the colour"),
        ParamSpec("highlights", "Highlights", "float", -100.0, 100.0, 0.0, step=1.0,
                  help="Lift or pull the bright parts without touching the shadows"),
        ParamSpec("shadows", "Shadows", "float", -100.0, 100.0, 0.0, step=1.0,
                  help="Lift or crush the dark parts without touching the highlights"),
        ParamSpec("temperature", "Temperature", "float", -100.0, 100.0, 0.0, step=1.0,
                  help="Positive is warmer (orange), negative is cooler (blue)"),
        ParamSpec("tint", "Tint", "float", -100.0, 100.0, 0.0, step=1.0,
                  help="Positive is magenta, negative is green"),
        ParamSpec("gamma", "Gamma", "float", 0.1, 4.0, 1.0, step=0.01,
                  help="Midtone curve: above 1 lifts mids, below 1 deepens them"),
        ParamSpec("hue", "Hue", "float", -180.0, 180.0, 0.0, step=0.5, unit="\u00b0",
                  help="Rotate every hue around the colour wheel"),
        ParamSpec("red_gain", "Red gain", "float", 0.0, 200.0, 100.0, step=0.5, unit="%",
                  advanced=True, help="Per-channel gain for colour balance"),
        ParamSpec("green_gain", "Green gain", "float", 0.0, 200.0, 100.0, step=0.5, unit="%",
                  advanced=True, help="Per-channel gain for colour balance"),
        ParamSpec("blue_gain", "Blue gain", "float", 0.0, 200.0, 100.0, step=0.5, unit="%",
                  advanced=True, help="Per-channel gain for colour balance"),
    )

    def __init__(self, app):
        super().__init__(app)
        for spec in self.PARAMS:
            setattr(self, spec.name, Value(spec.default))

    def params_at(self, local_frame):
        return {spec.name: (_num(getattr(self, spec.name), local_frame)
                            if spec.kind == "float" else getattr(self, spec.name).get())
                for spec in self.PARAMS}

    def is_identity(self, params):
        return (str(params.get("preset", "none")).lower() in ("", "none")
                and all(abs(float(params.get(spec.name, spec.default))) <= 1e-4
                        for spec in self.PARAMS
                        if spec.kind == "float" and spec.name != "gamma")
                and abs(float(params.get("gamma", 1.0)) - 1.0) <= 1e-4
                and all(abs(float(params.get(name, 100.0)) - 100.0) <= 1e-4
                        for name in ("red_gain", "green_gain", "blue_gain")))

    def apply_layer(self, layer):
        params = self.params_at(self.local_frame)
        if self.is_identity(params):
            return layer
        rgb = layer[:, :, :3].astype(np.float32)
        graded = np.clip(grade_array(rgb, params), 0.0, 255.0)
        out = np.empty_like(layer)
        out[:, :, :3] = graded.astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out
