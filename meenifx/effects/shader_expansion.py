"""Phase 2: deterministic refraction, radial waves, slices and light shafts."""
import math
import numpy as np
from .base import ParamSpec
from .shader_effects import ShaderEffect, BACKEND
from ..processing import shaders as S

EDGES = ParamSpec("edge", "Edges", "choice", default="clamp",
                  choices=("transparent", "clamp", "mirror"))


def phase(effect, p):
    fps = max(.001, float(getattr(effect.app, "project_fps", 30)))
    return float((effect.local_frame / fps * p["speed"] * math.tau
                  + math.radians(p["evolution"])) % math.tau)


class HeatHazeEffect(ShaderEffect):
    key, name = "heat_haze", "Heat Haze"
    PRESETS = {
        "road_heat": ("Road Heat", {"amount": 18, "wavelength": 85, "height_bias": 70, "speed": .8}),
        "soft_refraction": ("Soft Refraction", {"amount": 6, "detail": 35, "height_bias": 0}),
        "thermal_drift": ("Thermal Drift", {"amount": 28, "angle": 35, "speed": .4, "detail": 85}),
    }
    PARAMS = (
        ParamSpec("amount", "Refraction", "float", 0, 100, 18., unit=" px", pixels=True),
        ParamSpec("wavelength", "Wave size", "float", 8, 600, 85., unit=" px", pixels=True),
        ParamSpec("angle", "Direction", "float", -180, 180, 0., unit="degrees"),
        ParamSpec("detail", "Detail", "float", 0, 100, 60., unit="%"),
        ParamSpec("height_bias", "Height bias", "float", 0, 100, 70., unit="%",
                  help="Concentrate the refraction near the bottom of the frame"),
        ParamSpec("speed", "Speed", "float", -4, 4, .8, step=.05, unit=" cycles/s"),
        ParamSpec("evolution", "Evolution", "float", 0, 360, 0., unit="degrees"),
        ParamSpec("seed", "Seed", "int", 0, 65535, 7), EDGES, BACKEND,
    )

    def neutral(self, p):
        return p["amount"] <= 0

    def uniforms(self, p, layer):
        angle = math.radians(p["angle"])
        return dict(amount=p["amount"] * self.param_scale,
                    wavelength=max(1., p["wavelength"] * self.param_scale),
                    direction=(math.cos(angle), math.sin(angle)),
                    detail=p["detail"] / 100, heightBias=p["height_bias"] * .04,
                    phase=phase(self, p), seedPhase=float(math.sin(p["seed"] * .731) * math.tau),
                    edge=("transparent", "clamp", "mirror").index(p["edge"])), ()

    @staticmethod
    def cpu(image, u, extra=()):
        y, x = np.indices(image.shape[:2], dtype=np.float32)
        nx, ny = u["direction"]
        s = (x * nx + y * ny) / u["wavelength"]
        t = (-x * ny + y * nx) / u["wavelength"]
        wave = np.sin(s * math.tau + u["phase"]) + u["detail"] * .65 * np.sin(
            t * 3.1 - u["phase"] * 2. + np.sin(s * 1.7 + u["seedPhase"]))
        mask = np.maximum(y / max(1, image.shape[0]-1), 1e-8) ** u["heightBias"]
        distance = wave * u["amount"] * mask
        return S.sample(image, x + nx * distance, y + ny * distance, u["edge"])


class RippleEffect(ShaderEffect):
    key, name = "ripple", "Ripple"
    PRESETS = {
        "water_rings": ("Water Rings", {"amount": 12, "wavelength": 40, "speed": 1., "falloff": 70}),
        "slow_pulse": ("Slow Pulse", {"amount": 20, "wavelength": 90, "speed": .25}),
        "tight_waves": ("Tight Waves", {"amount": 6, "wavelength": 18, "falloff": 35}),
    }
    PARAMS = (
        ParamSpec("center_x", "Center X", "float", 0, 100, 50., unit="%"),
        ParamSpec("center_y", "Center Y", "float", 0, 100, 50., unit="%"),
        ParamSpec("radius", "Radius", "float", 1, 150, 65., unit="%"),
        ParamSpec("amount", "Amplitude", "float", -50, 50, 12., unit=" px", pixels=True),
        ParamSpec("wavelength", "Wavelength", "float", 5, 500, 40., unit=" px", pixels=True),
        ParamSpec("falloff", "Falloff", "float", 0, 100, 70., unit="%"),
        ParamSpec("speed", "Speed", "float", -4, 4, 1., step=.05, unit=" cycles/s"),
        ParamSpec("evolution", "Evolution", "float", 0, 360, 0., unit="degrees"), EDGES, BACKEND,
    )

    def neutral(self, p):
        return abs(p["amount"]) < 1e-8

    def uniforms(self, p, layer):
        h, w = layer.shape[:2]
        return dict(center=(p["center_x"] * (w-1) / 100, p["center_y"] * (h-1) / 100),
                    radius=p["radius"] * min(w, h) / 100,
                    amount=p["amount"] * self.param_scale,
                    wavelength=max(1., p["wavelength"] * self.param_scale),
                    falloff=.5 + p["falloff"] * .035, phase=phase(self, p),
                    edge=("transparent", "clamp", "mirror").index(p["edge"])), ()

    @staticmethod
    def cpu(image, u, extra=()):
        y, x = np.indices(image.shape[:2], dtype=np.float32)
        dx, dy = x-u["center"][0], y-u["center"][1]
        r = np.sqrt(dx*dx + dy*dy)
        inner = np.clip(r / (u["wavelength"] * .5), 0, 1)
        inner = inner*inner*(3-2*inner)
        outer = np.maximum(0, 1-r/u["radius"]) ** u["falloff"]
        offset = np.sin(r/u["wavelength"] * math.tau-u["phase"]) * u["amount"] * inner * outer
        return S.sample(image, x+dx/np.maximum(r, 1e-8)*offset,
                        y+dy/np.maximum(r, 1e-8)*offset, u["edge"])


class PixelStretchEffect(ShaderEffect):
    key, name = "pixel_stretch", "Pixel Stretch"
    PRESETS = {
        "vertical_ribbon": ("Vertical Ribbon", {"angle": 0, "width": 3, "amount": 100}),
        "diagonal_slice": ("Diagonal Slice", {"angle": 45, "width": 9, "amount": 100}),
        "wide_stretch": ("Wide Stretch", {"angle": 0, "width": 35, "position": 25}),
    }
    PARAMS = (
        ParamSpec("position", "Slice position", "float", 0, 100, 50., unit="%"),
        ParamSpec("width", "Slice width", "float", .1, 100, 3., unit="%"),
        ParamSpec("amount", "Stretch", "float", 0, 100, 100., unit="%"),
        ParamSpec("angle", "Direction", "float", -180, 180, 0., unit="degrees"), EDGES, BACKEND,
    )

    def neutral(self, p):
        return p["amount"] <= 0 or p["width"] >= 100

    def uniforms(self, p, layer):
        angle = math.radians(p["angle"])
        nx, ny = math.cos(angle), math.sin(angle)
        h, w = layer.shape[:2]
        return dict(direction=(nx, ny), position=(p["position"]/100-.5)*(abs(nx)*(w-1)+abs(ny)*(h-1)),
                    compression=1-p["amount"]*.01*(1-p["width"]*.01),
                    edge=("transparent", "clamp", "mirror").index(p["edge"])), ()

    @staticmethod
    def cpu(image, u, extra=()):
        h, w = image.shape[:2]
        y, x = np.indices((h, w), dtype=np.float32)
        nx, ny = u["direction"]
        q = (x-(w-1)*.5)*nx + (y-(h-1)*.5)*ny
        delta = (u["position"]-q)*(1-u["compression"])
        return S.sample(image, x+nx*delta, y+ny*delta, u["edge"])


class LightRaysEffect(ShaderEffect):
    key, name = "light_rays", "Light Rays"
    category = "Glow & Light"
    PRESETS = {
        "window_shafts": ("Window Shafts", {"strength": 150, "reach": 70, "threshold": 65}),
        "soft_radiance": ("Soft Radiance", {"strength": 80, "reach": 40, "threshold": 35}),
        "long_beams": ("Long Beams", {"strength": 220, "reach": 120, "falloff": 40}),
    }
    PARAMS = (
        ParamSpec("center_x", "Origin X", "float", 0, 100, 50., unit="%"),
        ParamSpec("center_y", "Origin Y", "float", 0, 100, 50., unit="%"),
        ParamSpec("strength", "Intensity", "float", 0, 400, 150., unit="%"),
        ParamSpec("reach", "Ray length", "float", 0, 150, 70., unit="%"),
        ParamSpec("threshold", "Threshold", "float", 0, 100, 65., unit="%"),
        ParamSpec("falloff", "Falloff", "float", 0, 100, 65., unit="%"),
        ParamSpec("samples", "Samples", "int", 8, 64, 24, advanced=True),
        ParamSpec("ray_r", "Ray red", "int", 0, 255, 255),
        ParamSpec("ray_g", "Ray green", "int", 0, 255, 255),
        ParamSpec("ray_b", "Ray blue", "int", 0, 255, 255),
        ParamSpec("blend", "Blend", "choice", default="screen", choices=("screen", "add")), BACKEND,
    )

    def neutral(self, p):
        return p["strength"] <= 0 or p["threshold"] >= 100

    def uniforms(self, p, layer):
        h, w = layer.shape[:2]
        color = S._DECODE[np.array([p["ray_r"], p["ray_g"], p["ray_b"]], np.uint8)]
        return dict(center=(p["center_x"]*(w-1)/100, p["center_y"]*(h-1)/100),
                    strength=p["strength"]/100, reach=p["reach"]/100,
                    threshold=p["threshold"]/100, falloff=p["falloff"]*.05,
                    samples=p["samples"], rayColor=tuple(float(c) for c in color),
                    additive=p["blend"] == "add"), ()

    @staticmethod
    def cpu(image, u, extra=()):
        y, x = np.indices(image.shape[:2], dtype=np.float32)
        acc = np.zeros_like(image)
        total = 0.
        for index in range(u["samples"]):
            step = (index+1)/u["samples"]
            sample = S.sample(image, x+(u["center"][0]-x)*u["reach"]*step,
                              y+(u["center"][1]-y)*u["reach"]*step)
            straight = sample[..., :3]/np.maximum(sample[..., 3:4], 1e-8)
            brightness = straight @ np.array([.2126, .7152, .0722], np.float32)
            mask = np.clip((brightness-u["threshold"])/max(1e-5, 1-u["threshold"]), 0, 1)
            weight = math.exp(-u["falloff"]*step)
            acc += sample * (mask*weight)[..., None]
            total += weight
        rays = acc * (u["strength"]/max(total, 1e-8))
        rays[..., :3] *= np.asarray(u["rayColor"], np.float32)
        light = np.clip(rays[..., :3], 0, 1)
        rgb = image[..., :3]+light if u["additive"] else 1-(1-image[..., :3])*(1-light)
        alpha = 1-(1-image[..., 3])*(1-np.clip(rays[..., 3], 0, 1))
        return np.concatenate((rgb, alpha[..., None]), axis=2)


SHADER_EXPANSION_EFFECTS = (HeatHazeEffect, RippleEffect, PixelStretchEffect, LightRaysEffect)
