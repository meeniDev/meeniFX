"""Light effects: flash/strobe, exposure flicker, procedural light leaks, bloom streaks.

They are additive light, so they work on the BGRA layer and only raise colour (and,
for a leak, coverage is kept). The time-based brightness of every one is a pure
function of the frame number and the effect's own seed, so scrubbing, playback and the
exporter all show the same pulse.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number


def _seed(*parts):
    seed = 0
    for part in parts:
        seed = (seed * 1000003 + int(part)) % (2 ** 32)
    return np.random.default_rng(seed)


def _blend(base, light, mode):
    """Blend float32 BGR light onto base in the 0..255 domain."""
    if mode == "add":
        return base + light
    if mode == "overlay":
        b = np.clip(base, 0.0, 255.0) / 255.0
        g = np.clip(light, 0.0, 255.0) / 255.0
        out = np.where(b <= 0.5, 2.0 * b * g, 1.0 - 2.0 * (1.0 - b) * (1.0 - g))
        return out * 255.0
    return 255.0 - (255.0 - np.clip(base, 0.0, 255.0)) * (255.0 - np.clip(light, 0.0, 255.0)) / 255.0


class FlashEffect(LayerEffect):
    """A timed white/coloured flash with a fade, hold or pulse envelope."""

    key = "flash"
    name = "Flash / Strobe"
    category = "Light"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 65.0, step=1.0, unit="%"),
        ParamSpec("duration", "Duration", "int", 1, 30, 4, unit=" f",
                  help="How long one flash lasts"),
        ParamSpec("interval", "Interval", "int", 2, 240, 30, unit=" f", advanced=True,
                  help="Frames between flashes"),
        ParamSpec("envelope", "Envelope", "choice", choices=("flash", "fade", "pulse"),
                  default="fade", choice_labels=("Flash", "Fade", "Pulse"),
                  help="Shape of each flash"),
        ParamSpec("offset", "Offset", "int", 0, 120, 0, unit=" f", advanced=True),
        ParamSpec("color_r", "Red", "int", 0, 255, 255, advanced=True),
        ParamSpec("color_g", "Green", "int", 0, 255, 255, advanced=True),
        ParamSpec("color_b", "Blue", "int", 0, 255, 255, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(65.0)
        self.duration = Value(4)
        self.interval = Value(30)
        self.envelope = Value("fade")
        self.offset = Value(0)
        self.color_r = Value(255)
        self.color_g = Value(255)
        self.color_b = Value(255)

    def _envelope(self, frame):
        duration = max(1, int(self.duration.get()))
        interval = max(duration, int(self.interval.get()))
        shifted = int(frame) - int(self.offset.get())
        if shifted < 0:
            return 0.0          # before the first flash; an offset delays the strobe
        phase = shifted % interval
        if phase >= duration:
            return 0.0
        x = phase / float(duration)
        style = str(self.envelope.get())
        if style == "flash":
            return 1.0
        if style == "pulse":
            return math.sin(math.pi * x) ** 2
        return 1.0 - x

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        level = param_number(self.amount, frame) / 100.0 * self._envelope(frame)
        if level <= 0.001:
            return layer
        colour = np.array([float(self.color_b.get()), float(self.color_g.get()),
                           float(self.color_r.get())], dtype=np.float32)
        out = layer.astype(np.float32)
        out[:, :, :3] += colour * level
        return np.clip(out, 0.0, 255.0).astype(np.uint8)


class ExposureFlickerEffect(LayerEffect):
    """A seeded exposure wobble, smooth like a failing bulb or stepped like a strobe."""

    key = "exposure_flicker"
    name = "Exposure Flicker"
    category = "Light"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 35.0, step=1.0, unit="%"),
        ParamSpec("speed", "Speed", "float", 0.1, 30.0, 4.0, step=0.1, unit=" fps",
                  help="How fast the exposure wanders"),
        ParamSpec("seed", "Seed", "int", 0, 9999, 5),
        ParamSpec("style", "Style", "choice", choices=("smooth", "strobe"), default="smooth",
                  choice_labels=("Smooth", "Strobe"), advanced=True),
        ParamSpec("color", "Warmth", "float", -100.0, 100.0, 0.0, step=1.0, unit="%",
                  advanced=True, help="Tint the flicker warm or cool"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(35.0)
        self.speed = Value(4.0)
        self.seed = Value(5)
        self.style = Value("smooth")
        self.color = Value(0.0)

    def _level(self, frame):
        speed = max(0.05, param_number(self.speed, frame))
        rng = _seed(self.seed.get(), int(round(speed * 1000.0)), 0)
        if str(self.style.get()) == "strobe":
            idx = int(math.floor(frame * speed / 30.0))
            values = rng.normal(0.0, 1.0, size=max(2, idx + 1))
            return float(np.clip(values[idx], -1.0, 1.0))
        # The seed picks where on the wave the clip starts, so two seeds never flicker
        # in lockstep; the speed only changes how fast it wanders (a time-based value).
        start = _seed(self.seed.get(), 97).uniform(0.0, 2.0 * math.pi)
        phase = frame * speed / 30.0
        wave = math.sin(2.0 * math.pi * phase + start) * 0.65 +             math.sin(2.0 * math.pi * phase * 0.37 + start * 1.7) * 0.35
        return float(np.clip(wave, -1.0, 1.0))

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        amount = param_number(self.amount, frame) / 100.0
        if amount <= 0.001:
            return layer
        level = self._level(frame) * amount
        warmth = param_number(self.color, frame)
        gain = np.array([1.0 + level, 1.0 + level * (1.0 + warmth * 0.003),
                         1.0 + level * (1.0 - warmth * 0.003)], dtype=np.float32)
        out = layer.astype(np.float32)
        out[:, :, :3] *= gain
        return np.clip(out, 0.0, 255.0).astype(np.uint8)


class LightLeakEffect(LayerEffect):
    """Procedural coloured leaks that drift across the frame, composited as light."""

    key = "light_leak"
    name = "Light Leak"
    category = "Light"

    PARAMS = (
        ParamSpec("amount", "Amount", "float", 0.0, 100.0, 45.0, step=1.0, unit="%"),
        ParamSpec("seed", "Seed", "int", 0, 999, 12),
        ParamSpec("blend", "Blend", "choice", choices=("screen", "add", "overlay"),
                  default="screen", choice_labels=("Screen", "Add", "Overlay")),
        ParamSpec("position", "Position", "float", -100.0, 100.0, 0.0, step=1.0, unit="%",
                  advanced=True),
        ParamSpec("spread", "Spread", "float", 10.0, 100.0, 60.0, step=1.0, unit="%",
                  advanced=True),
        ParamSpec("hue", "Hue", "float", -90.0, 90.0, 0.0, step=1.0, unit="\u00b0",
                  advanced=True),
        ParamSpec("speed", "Speed", "float", 0.0, 4.0, 0.6, step=0.05, advanced=True),
    )

    def __init__(self, app):
        super().__init__(app)
        self.amount = Value(45.0)
        self.seed = Value(12)
        self.blend = Value("screen")
        self.position = Value(0.0)
        self.spread = Value(60.0)
        self.hue = Value(0.0)
        self.speed = Value(0.6)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        amount = param_number(self.amount, frame) / 100.0
        if amount <= 0.001:
            return layer
        height, width = layer.shape[:2]
        speed = max(0.01, param_number(self.speed, frame))
        rng = _seed(self.seed.get(), int(round(speed * 100.0)), 41)
        spread = max(0.05, param_number(self.spread, frame) / 100.0)
        shift = param_number(self.position, frame) / 100.0
        time = frame * speed / 30.0
        # A leak is a smooth gradient: build it on a small grid and scale it up.
        low_w, low_h, _factor = self.work_size(width, height, 480)
        yy, xx = np.mgrid[0:low_h, 0:low_w].astype(np.float32)
        nx = xx / max(1.0, low_w)
        ny = yy / max(1.0, low_h)
        light = np.zeros((low_h, low_w, 3), np.float32)
        for _ in range(3):
            cx = float(rng.uniform(-0.2, 1.2)) + shift + 0.15 * math.sin(time + rng.uniform(0, 6))
            cy = float(rng.uniform(-0.2, 1.2)) + 0.10 * math.cos(time * 0.7 + rng.uniform(0, 6))
            radius = spread * float(rng.uniform(0.45, 0.95))
            hue = (float(rng.uniform(0, 360)) + param_number(self.hue, frame)) % 360.0
            strength = float(rng.uniform(0.4, 1.0))
            hsv = np.uint8([[[int(round(hue / 2.0)) % 180, 200, 255]]])
            colour = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0].astype(np.float32)
            dx = (nx - cx) / radius
            dy = (ny - cy) / (radius * low_h / max(1.0, low_w))
            falloff = np.exp(-(dx * dx + dy * dy) * 3.0) * strength
            light += falloff[:, :, None] * colour
        light = self.resize_to(light, width, height)
        base = layer[:, :, :3].astype(np.float32)
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(_blend(base, light * amount, str(self.blend.get())),
                                0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


class BloomStreaksEffect(LayerEffect):
    """Bloom with horizontal (or angled) anamorphic streaks."""

    key = "bloom_streaks"
    name = "Bloom Streaks"
    category = "Light"

    PARAMS = (
        ParamSpec("threshold", "Threshold", "float", 0.0, 100.0, 68.0, step=1.0, unit="%",
                  help="Brightness a pixel needs to bloom"),
        ParamSpec("intensity", "Intensity", "float", 0.0, 200.0, 70.0, step=1.0, unit="%"),
        ParamSpec("length", "Streak length", "float", 0.0, 500.0, 120.0, step=1.0, unit=" px",
                  pixels=True, help="How far the light streaks"),
        ParamSpec("angle", "Angle", "float", 0.0, 90.0, 0.0, step=1.0, unit="\u00b0",
                  advanced=True, help="Streak direction: 0 is horizontal, 90 vertical"),
        ParamSpec("radius", "Bloom radius", "float", 0.0, 300.0, 26.0, step=1.0, unit=" px",
                  pixels=True, advanced=True),
        ParamSpec("samples", "Samples", "int", 3, 24, 9, advanced=True,
                  help="Streak copies; more is smoother and slower"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.threshold = Value(68.0)
        self.intensity = Value(70.0)
        self.length = Value(120.0)
        self.radius = Value(26.0)
        self.angle = Value(0.0)
        self.samples = Value(9)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        intensity = param_number(self.intensity, frame) / 100.0
        length = param_number(self.length, frame) * max(0.02, float(self.param_scale))
        if intensity <= 0.001 or length <= 0.5:
            return layer
        radius = max(0.3, param_number(self.radius, frame) * max(0.02, float(self.param_scale)))
        height, width = layer.shape[:2]
        # Bloom is light: computing it on a bounded grid and scaling the glow back is
        # visually identical (it is a blur) and roughly ten times cheaper at 1080p.
        low_w, low_h, factor = self.work_size(width, height, 480)
        small = layer if (low_w, low_h) == (width, height) else cv2.resize(
            layer, (low_w, low_h), interpolation=cv2.INTER_AREA)
        data = small.astype(np.float32)
        alpha = data[:, :, 3:4] / 255.0
        rgb = data[:, :, :3] * alpha
        luma = np.clip(rgb @ np.array([0.0722, 0.7152, 0.2126], np.float32) / 255.0, 0.0, 1.0)
        threshold = max(0.0, min(1.0, param_number(self.threshold, frame) / 100.0))
        mask = np.clip((luma - threshold) / 0.12, 0.0, 1.0)
        bright = rgb * mask[:, :, None]
        if not bright.any():
            return layer
        angle = math.radians(param_number(self.angle, frame))
        dx, dy = math.cos(angle), math.sin(angle)
        count = max(2, int(self.samples.get()))
        streaks = np.zeros_like(bright)
        small_length = max(0.5, length * factor)
        for index in range(count):
            offset = (index / float(count - 1) - 0.5) * small_length
            matrix = np.array([[1.0, 0.0, dx * offset], [0.0, 1.0, dy * offset]], np.float32)
            streaks += cv2.warpAffine(bright, matrix, (low_w, low_h), flags=cv2.INTER_LINEAR,
                                      borderMode=cv2.BORDER_REPLICATE)
        streaks /= float(count)
        small_radius = max(0.3, radius * factor)
        bloom = cv2.GaussianBlur(bright, (0, 0), sigmaX=small_radius / 2.5,
                                 sigmaY=small_radius / 2.5, borderType=cv2.BORDER_REPLICATE)
        light = (streaks * 0.75 + bloom * 0.6) * intensity
        if (low_w, low_h) != (width, height):
            light = self.resize_to(light, width, height)
        full = layer.astype(np.float32)
        full_alpha = full[:, :, 3:4] / 255.0
        rgb = full[:, :, :3] * full_alpha
        alpha = full_alpha
        out_rgb = 255.0 - (255.0 - np.clip(rgb, 0.0, 255.0)) * \
            (255.0 - np.clip(light, 0.0, 255.0)) / 255.0
        out = np.empty_like(layer)
        safe = np.where(alpha > 1e-3, np.clip(alpha, 0.0, 1.0), 1.0)
        out[:, :, :3] = np.clip(out_rgb / safe, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


LIGHT_EFFECTS = (FlashEffect, ExposureFlickerEffect, LightLeakEffect, BloomStreaksEffect)
