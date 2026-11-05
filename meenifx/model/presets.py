"""Animation presets: a layer on top of a clip's own transform and keyframes.

A preset is an *instance* stored on the clip - which preset, which category (In, Out or
Loop), its window in clip-local frames and a few knobs. Nothing here writes keyframes:
the compositor evaluates the instances every frame and adds/multiplies their result onto
the clip's own transform, so a hand-animated position and a shake can live on the same
clip without either knowing about the other.

Two more properties matter:

* **determinism** - every noise-based program reads a hash of the instance seed, never a
  clock or a global RNG, so the preview, the render cache and the export produce the same
  frames; the instances are part of the cache digest;
* **bakeability** - :func:`bake` samples the evaluated pose into ordinary keyframes and
  drops the instance, after which the Graph Editor owns the animation like any other.
"""

from __future__ import annotations

import copy
import math

from . import easing as E

CATEGORIES = ("in", "out", "loop")
CATEGORY_LABELS = {"in": "In", "out": "Out", "loop": "Loop"}
UNITS = ("letter", "word", "line", "whole")
UNIT_LABELS = {"letter": "Letter", "word": "Word", "line": "Line", "whole": "Whole"}
DIRECTIONS = ("left", "right", "center", "edges", "random")
DIRECTION_LABELS = {"left": "Left to right", "right": "Right to left",
                    "center": "From center", "edges": "Edges in", "random": "Random"}
# The knob offers the same curve names the keyframe easing menu uses, so a preset and a
# hand-made curve can say the same thing.
EASINGS = ("Linear", "Smooth", "Ease In", "Ease Out", "Back Out", "Bounce Out", "Elastic Out")
DEFAULT_EASING = "Smooth"

DEFAULT_KNOBS = {"intensity": 1.0, "easing": DEFAULT_EASING, "direction": "left",
                 "stagger": 0.35, "unit": "word", "seed": 7}

# label, minimum, maximum, step, decimals, unit (the Animation card builds SliderFields).
KNOB_SPECS = {
    "intensity": ("Intensity", 0.0, 200.0, 1.0, 0, "%"),
    "stagger": ("Stagger", 0.0, 100.0, 1.0, 0, "%"),
    "seed": ("Seed", 0.0, 9999.0, 1.0, 0, ""),
    "duration": ("Duration", 1.0, 100000.0, 1.0, 0, "f"),
}

# Programs that only make sense per unit: the Animation card hides Unit for the rest.
UNIT_ONLY = ("tracking", "scramble", "kinetic", "wipe", "stagger_fade")


# ----------------------------------------------------------- deterministic noise
def _hash(seed, channel, index):
    """A stable 0..1 value: same seed, channel and index -> same number, forever."""
    h = (int(seed) * 0x9E3779B1 + int(channel) * 0x85EBCA6B + int(index) * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2545F491) & 0xFFFFFFFF
    h ^= h >> 13
    return h / 0xFFFFFFFF


def noise(seed, channel, x):
    """Smooth value noise in -1..1, a pure function of the seed and the position."""
    x = float(x)
    base = math.floor(x)
    fraction = x - base
    smooth = fraction * fraction * (3.0 - 2.0 * fraction)
    left = _hash(seed, channel, base) * 2.0 - 1.0
    right = _hash(seed, channel, base + 1) * 2.0 - 1.0
    return left + (right - left) * smooth


_CURVES = {}


def ease(x, name=DEFAULT_EASING):
    """Ease 0..1 through the keyframe curve of the same name (one shared definition)."""
    x = 0.0 if x <= 0.0 else (1.0 if x >= 1.0 else float(x))
    table = _CURVES.get(name)
    if table is None:
        # preset_curve() returns [(ratio, y)] for the curve drawings; keep the y values.
        points = E.preset_curve(name, 32)
        table = [float(point[1]) for point in points] if points else []
        _CURVES[name] = table
    if len(table) < 2:
        return x
    position = x * (len(table) - 1)
    index = min(len(table) - 2, int(position))
    fraction = position - index
    return table[index] + (table[index + 1] - table[index]) * fraction


def _mix(start, end, t):
    return start + (end - start) * t


def _span(knobs):
    """(slide x, slide y) in project pixels for this project's frame size."""
    width, height = (knobs.get("span") or (1920, 1080))
    return max(60.0, float(width) * 0.45), max(40.0, float(height) * 0.35)


# ------------------------------------------------------------------- programs
# Every program is an *arrival*: t = 0 is the pose before the animation, t = 1 is the
# clip as it should look at rest. An Out preset evaluates 1 - t, which is what makes the
# matching exits exact mirrors of the entrances instead of a second implementation.
# Each returns channel deltas: dx, dy (project px), scale (multiplier), rotation (deg),
# opacity (multiplier), blur (px), hue (degrees), light (-1..1 brightness), text (string).
def _fade(t, k, u, a):
    return {"opacity": t}


def _slide(t, k, u, a):
    dx, dy = _span(k)
    direction = a.get("dir", "left")
    offset = {"left": (-dx, 0.0), "right": (dx, 0.0),
              "up": (0.0, dy), "down": (0.0, -dy)}.get(direction, (-dx, 0.0))
    return {"dx": offset[0] * (1.0 - t), "dy": offset[1] * (1.0 - t)}


def _pop(t, k, u, a):
    start = float(a.get("from", 0.25))
    return {"scale": _mix(start, 1.0, t), "opacity": min(1.0, t * 1.4)}


def _scale(t, k, u, a):
    start = float(a.get("from", 0.6))
    return {"scale": _mix(start, 1.0, t), "opacity": min(1.0, t * 1.6)}


def _blur(t, k, u, a):
    amount = float(a.get("px", 18.0))
    return {"blur": amount * (1.0 - t), "opacity": min(1.0, t * 1.5)}


def _rotate(t, k, u, a):
    amount = float(a.get("deg", 30.0))
    return {"rotation": -amount * (1.0 - t), "opacity": min(1.0, t * 1.5)}


def _bounce(t, k, u, a):
    dy = _span(k)[1]
    height = float(a.get("px", 1.0))
    return {"dy": -dy * height * (1.0 - t), "opacity": min(1.0, t * 2.0)}


def _flip(t, k, u, a):
    axis = a.get("axis", "x")
    amount = max(0.02, abs(math.sin(math.pi * t / 2.0)))
    key = "scale_x" if axis == "x" else "scale_y"
    value = amount if t >= 0.999 else amount
    return {key: value, "opacity": min(1.0, t * 1.6)}


def _spiral(t, k, u, a):
    dx, dy = _span(k)
    radius = float(a.get("px", 0.35)) * max(dx, dy)
    angle = (1.0 - t) * math.radians(float(a.get("deg", 220.0)))
    return {"dx": math.cos(angle) * radius * (1.0 - t),
            "dy": math.sin(angle) * radius * (1.0 - t),
            "rotation": -math.degrees(angle) * 0.35,
            "scale": _mix(0.4, 1.0, t), "opacity": min(1.0, t * 1.5)}


def _whip(t, k, u, a):
    dx, _dy = _span(k)
    sign = 1.0 if a.get("dir", "left") == "left" else -1.0
    return {"dx": sign * dx * (1.0 - t) * 1.4, "blur": 16.0 * (1.0 - t),
            "scale": _mix(1.15, 1.0, t)}


def _zoom(t, k, u, a):
    start = float(a.get("from", 0.55))
    return {"scale": _mix(start, 1.0, t), "opacity": min(1.0, t * 1.8)}


def _spin(t, k, u, a):
    deg = float(a.get("deg", 180.0))
    return {"rotation": -deg * (1.0 - t), "scale": _mix(0.5, 1.0, t),
            "opacity": min(1.0, t * 1.6)}


def _drop(t, k, u, a):
    dy = _span(k)[1]
    return {"dy": -dy * float(a.get("px", 1.4)) * (1.0 - t),
            "scale": _mix(1.25, 1.0, t), "opacity": min(1.0, t * 2.0)}


def _flash(t, k, u, a):
    return {"opacity": min(1.0, t * 2.2), "light": 0.55 * (1.0 - t)}


def _tracking(t, k, u, a):
    """Letter spacing expands into place: each unit slides out from the block centre."""
    spread = float(a.get("px", 0.35)) * _span(k)[0]
    place = u.get("place", 0.0)
    return {"dx": place * spread * (1.0 - t), "opacity": min(1.0, t * 1.6)}


def _scramble(t, k, u, a):
    """Decode reveal: characters change until their own slot resolves."""
    text = u.get("text")
    pose = {"opacity": min(1.0, t * 2.0)}
    if not text:
        return pose
    if t >= 0.999:
        return pose
    seed = int(k.get("seed", 0))
    pool = str(a.get("pool") or "ABCDEFGHKLMNPRSTUVWXYZ#%&*+-")
    index = int(u.get("index", 0))
    held = int(t * 6.0) + seed + index
    if _hash(seed, 21 + index, held) < t:
        return pose
    glyphs = []
    for position, char in enumerate(str(text)):
        if char.isspace():
            glyphs.append(char)
        else:
            pick = int(_hash(seed, 40 + position, held + position) * len(pool))
            glyphs.append(pool[pick])
    pose["text"] = "".join(glyphs)
    pose["opacity"] = max(0.35, t)
    return pose


def _kinetic(t, k, u, a):
    """Hard-cut scale pops, Japanese motion-graphics style: quantised, never smooth."""
    steps = max(2, int(a.get("steps", 4)))
    step = math.floor(t * steps) / float(steps)
    scale = 1.0 + float(a.get("punch", 0.45)) * (1.0 - step) * (1.0 - t)
    return {"scale": scale, "opacity": 1.0 if step > 0 or t > 0.02 else 0.0}


def _wipe(t, k, u, a):
    """Mask wipe: the units resolve in reading order behind a moving edge."""
    place = u.get("place", 0.0)
    soft = max(0.05, float(a.get("soft", 0.25)))
    local = (t - place * (1.0 - soft)) / soft
    local = 0.0 if local < 0.0 else (1.0 if local > 1.0 else local)
    return {"dx": -40.0 * (1.0 - local), "opacity": local}


def _stagger_fade(t, k, u, a):
    """Per-unit fade: the plain building block behind Typewriter and the wipe reveals."""
    return {"opacity": t}


def _glitch_reveal(t, k, u, a):
    """Per-unit flicker with a horizontal jitter and a colour split while resolving."""
    seed = int(k.get("seed", 0)) + int(u.get("index", 0)) * 7
    if t >= 0.999:
        return {}
    jitter = noise(seed, 5, t * 14.0) * 36.0 * (1.0 - t)
    gate = _hash(seed, 9, int(t * 12.0))
    return {"dx": jitter, "opacity": 0.0 if gate < 0.28 * (1.0 - t) else min(1.0, t * 2.2),
            "hue": 40.0 * (1.0 - t) * (1.0 if gate > 0.5 else -1.0)}


def _wave_in(t, k, u, a):
    """Arrival wave: units swing up into place and settle."""
    amp = float(a.get("amp", 0.03)) * _span(k)[1]
    place = u.get("place", 0.0)
    settle = 1.0 - t
    return {"dy": math.sin(2.0 * math.pi * place * 0.75) * amp * settle,
            "rotation": math.sin(2.0 * math.pi * place * 0.75) * 4.0 * settle,
            "opacity": min(1.0, t * 1.6)}


def _wave(t, k, u, a):
    amp = float(a.get("amp", 0.06)) * _span(k)[1]
    cycles = float(a.get("cycles", 1.0))
    phase = u.get("phase", 0.0) + u.get("place", 0.0) * float(a.get("spread", 0.18))
    return {"dy": math.sin(2.0 * math.pi * (phase * cycles + u.get("place", 0.0) * 0.0)) * amp}


def _float(t, k, u, a):
    amp = float(a.get("amp", 0.035)) * _span(k)[1]
    return {"dy": math.sin(2.0 * math.pi * u.get("phase", 0.0)) * amp,
            "rotation": math.sin(2.0 * math.pi * u.get("phase", 0.0)) * 1.5}


def _pulse(t, k, u, a):
    amp = float(a.get("amp", 0.06))
    value = math.sin(2.0 * math.pi * u.get("phase", 0.0))
    return {"scale": 1.0 + amp * value}


def _heartbeat(t, k, u, a):
    """Two quick beats then rest, like a pulse."""
    phase = u.get("phase", 0.0) % 1.0
    beat = math.exp(-((phase - 0.08) ** 2) / 0.004) + 0.7 * math.exp(-((phase - 0.24) ** 2) / 0.006)
    return {"scale": 1.0 + float(a.get("amp", 0.08)) * beat}


def _shake(t, k, u, a):
    seed = int(k.get("seed", 0))
    amp = float(a.get("amp", 0.012)) * _span(k)[0]
    phase = u.get("phase", 0.0) * float(a.get("speed", 9.0))
    return {"dx": noise(seed, 1, phase) * amp, "dy": noise(seed, 2, phase + 17.0) * amp * 0.8,
            "rotation": noise(seed, 3, phase + 5.0) * 0.6 * float(a.get("sway", 1.0))}


def _camera_shake(t, k, u, a):
    """Handheld camera: slow drift plus a fast tremor, no rotation on the horizon."""
    seed = int(k.get("seed", 0))
    amp = float(a.get("amp", 0.008)) * _span(k)[0]
    phase = u.get("phase", 0.0) * 6.0
    slow = noise(seed, 11, phase * 0.25) * 1.6
    fast = noise(seed, 12, phase * 2.2) * 0.5
    return {"dx": (slow + fast) * amp, "dy": (noise(seed, 13, phase + 9.0) * 1.4) * amp,
            "scale": 1.0 + float(a.get("zoom", 0.012))}


def _jitter(t, k, u, a):
    seed = int(k.get("seed", 0))
    amp = float(a.get("amp", 0.006)) * _span(k)[0]
    phase = u.get("phase", 0.0) * 22.0
    return {"dx": noise(seed, 4, phase) * amp, "dy": noise(seed, 5, phase) * amp,
            "rotation": noise(seed, 6, phase) * 1.2}


def _wiggle(t, k, u, a):
    seed = int(k.get("seed", 0))
    amp = float(a.get("amp", 0.01)) * _span(k)[0]
    phase = u.get("phase", 0.0) * 3.5
    return {"dx": noise(seed, 7, phase) * amp, "dy": noise(seed, 8, phase) * amp,
            "rotation": noise(seed, 9, phase) * 2.5, "scale": 1.0 + noise(seed, 10, phase) * 0.01}


def _flicker(t, k, u, a):
    seed = int(k.get("seed", 0))
    amount = float(a.get("amount", 0.55))
    step = int(u.get("phase", 0.0) * 18.0)
    if _hash(seed, 14, step) < amount * 0.35:
        return {"opacity": 1.0 - amount * 0.8}
    return {}


def _strobe(t, k, u, a):
    phase = u.get("phase", 0.0)
    return {"opacity": 0.0 if phase % max(0.05, float(a.get("period", 0.25))) < 0.05 else 1.0,
            "light": float(a.get("light", 0.0))}


def _glitch_stutter(t, k, u, a):
    seed = int(k.get("seed", 0))
    phase = u.get("phase", 0.0)
    step = int(phase * float(a.get("rate", 10.0)))
    if _hash(seed, 15, step) > float(a.get("chance", 0.45)):
        return {}
    return {"dx": (_hash(seed, 16, step) - 0.5) * 60.0,
            "hue": (_hash(seed, 17, step) - 0.5) * 90.0,
            "opacity": 0.75 + 0.25 * _hash(seed, 18, step)}


def _color_cycle(t, k, u, a):
    return {"hue": u.get("phase", 0.0) * 360.0}


def _swing(t, k, u, a):
    amp = float(a.get("deg", 6.0))
    return {"rotation": math.sin(2.0 * math.pi * u.get("phase", 0.0)) * amp}


def _sway(t, k, u, a):
    amp = float(a.get("amp", 0.02)) * _span(k)[0]
    return {"dx": math.sin(2.0 * math.pi * u.get("phase", 0.0)) * amp,
            "rotation": math.sin(2.0 * math.pi * u.get("phase", 0.0)) * 1.2}


def _zoom_breath(t, k, u, a):
    amp = float(a.get("amp", 0.045))
    return {"scale": 1.0 + amp * math.sin(2.0 * math.pi * u.get("phase", 0.0))}


def _punch_zoom(t, k, u, a):
    phase = u.get("phase", 0.0) % 1.0
    if phase > float(a.get("hold", 0.35)):
        return {}
    decay = 1.0 - phase / max(0.05, float(a.get("hold", 0.35)))
    return {"scale": 1.0 + float(a.get("amp", 0.12)) * decay * decay}


def _scatter(t, k, u, a):
    """Exit: units fly apart in seeded directions while fading out."""
    seed = int(k.get("seed", 0)) + int(u.get("index", 0)) * 13
    dx, dy = _span(k)
    amount = 1.0 - t
    return {"dx": (_hash(seed, 30, 0) - 0.5) * dx * 1.6 * amount,
            "dy": (_hash(seed, 31, 0) - 0.5) * dy * 1.6 * amount,
            "rotation": (_hash(seed, 32, 0) - 0.5) * 120.0 * amount,
            "scale": _mix(0.2, 1.0, t), "opacity": t}


PROGRAMS = {
    "fade": _fade, "slide": _slide, "pop": _pop, "scale": _scale, "blur": _blur,
    "rotate": _rotate, "bounce": _bounce, "flip": _flip, "spiral": _spiral, "whip": _whip,
    "zoom": _zoom, "spin": _spin, "drop": _drop, "flash": _flash, "tracking": _tracking,
    "scramble": _scramble, "kinetic": _kinetic, "wipe": _wipe, "stagger_fade": _stagger_fade,
    "glitch_reveal": _glitch_reveal, "wave": _wave, "wave_in": _wave_in, "float": _float,
    "pulse": _pulse,
    "heartbeat": _heartbeat, "shake": _shake, "camera_shake": _camera_shake,
    "jitter": _jitter, "wiggle": _wiggle, "flicker": _flicker, "strobe": _strobe,
    "glitch_stutter": _glitch_stutter, "color_cycle": _color_cycle, "swing": _swing,
    "sway": _sway, "zoom_breath": _zoom_breath, "punch_zoom": _punch_zoom,
    "scatter": _scatter,
}


# ------------------------------------------------------------------- the pose
class Pose:
    """What one frame of preset maths does to a picture.

    Position, rotation, blur, colour and light add; scale and opacity multiply. That is
    the composition rule for stacking In + Loop + Out on the same clip.
    """

    __slots__ = ("dx", "dy", "scale_x", "scale_y", "rotation", "opacity", "blur", "hue",
                 "light", "text")

    def __init__(self):
        self.dx = self.dy = 0.0
        self.scale_x = self.scale_y = 1.0
        self.rotation = self.blur = self.hue = self.light = 0.0
        self.opacity = 1.0
        self.text = None

    def add(self, deltas):
        if not deltas:
            return self
        self.dx += float(deltas.get("dx", 0.0))
        self.dy += float(deltas.get("dy", 0.0))
        self.rotation += float(deltas.get("rotation", 0.0))
        self.blur += float(deltas.get("blur", 0.0))
        self.hue += float(deltas.get("hue", 0.0))
        self.light += float(deltas.get("light", 0.0))
        self.opacity *= float(deltas.get("opacity", 1.0))
        self.scale_x *= float(deltas.get("scale_x", deltas.get("scale", 1.0)))
        self.scale_y *= float(deltas.get("scale_y", deltas.get("scale", 1.0)))
        if deltas.get("text") is not None:
            self.text = str(deltas["text"])
        return self

    def is_identity(self):
        return (abs(self.dx) < 1e-6 and abs(self.dy) < 1e-6
                and abs(self.scale_x - 1.0) < 1e-6 and abs(self.scale_y - 1.0) < 1e-6
                and abs(self.rotation) < 1e-6 and abs(self.opacity - 1.0) < 1e-6
                and abs(self.blur) < 1e-6 and abs(self.hue) < 1e-6
                and abs(self.light) < 1e-6 and self.text is None)

    def key(self):
        """Compact, comparable form (tests and cache diagnostics)."""
        return (round(self.dx, 3), round(self.dy, 3), round(self.scale_x, 4),
                round(self.scale_y, 4), round(self.rotation, 3),
                round(self.opacity, 4), round(self.blur, 3), round(self.hue, 2),
                round(self.light, 3), self.text)


class Effect:
    """A clip's whole preset layer at one frame: one pose plus one per text unit.

    `unit_poses` is keyed by unit kind, because two instances on the same clip may
    stagger letters and words independently; the text renderer composes them per glyph.
    """

    def __init__(self):
        self.pose = Pose()
        self.unit_poses = {"letter": [], "word": [], "line": []}
        self.marked = []          # preset ids that are active, for the UI

    def __bool__(self):
        return any(self.unit_poses.values()) or not self.pose.is_identity()

    def is_identity(self):
        return not any(self.unit_poses.values()) and self.pose.is_identity()


# ---------------------------------------------------------------- instances
def instances(clip):
    """The clip's preset instances (created on demand, so callers may append)."""
    data = getattr(clip, "presets", None)
    if not isinstance(data, list):
        clip.presets = data = []
    return data


def clone_instances(rows):
    """Deep copy of an instance list (clip duplicates, split, undo snapshots)."""
    return copy.deepcopy(rows or [])


def _knobs(instance):
    knobs = dict(DEFAULT_KNOBS)
    stored = instance.get("knobs")
    if isinstance(stored, dict):
        knobs.update(stored)
    knobs["intensity"] = max(0.0, min(2.0, float(knobs.get("intensity", 1.0))))
    knobs["stagger"] = max(0.0, min(1.0, float(knobs.get("stagger", 0.35))))
    knobs["seed"] = int(max(0, min(9999, float(knobs.get("seed", 7)))))
    if knobs.get("unit") not in UNITS:
        knobs["unit"] = DEFAULT_KNOBS["unit"]
    if knobs.get("direction") not in DIRECTIONS:
        knobs["direction"] = DEFAULT_KNOBS["direction"]
    return knobs


def window(instance, clip_duration):
    """(start, length) of an instance inside its clip, or None when it is empty."""
    duration = max(1, int(clip_duration))
    length = int(instance.get("duration") or 0)
    start = int(instance.get("start") or 0)
    category = instance.get("category", "in")
    if category == "out":
        length = min(duration, length or duration)
        start = duration - length if not instance.get("offset_from_end") else start
    elif category == "in":
        length = min(duration, length or duration)
        start = max(0, min(start, duration - 1))
    else:
        length = min(duration, length or duration)
        start = max(0, min(start, duration - 1))
    length = max(1, min(length, duration - start))
    return start, length


def default_duration(clip, fps=30.0, category="in"):
    """Half a second at this project's rate, bounded by the clip."""
    frames = int(round(max(1.0, float(fps)) * 0.5))
    return max(1, min(frames, max(1, int(clip.duration))))


def make(preset_id, category="in", program="fade", args=None, knobs=None, start=0,
         duration=None, name="", clip=None):
    """A new instance dict (the clip anchors it through :func:`window`)."""
    category = category if category in CATEGORIES else "in"
    clip_duration = int(getattr(clip, "duration", 0) or 0)
    length = int(duration or 0)
    if not length:
        length = default_duration(clip, 30.0, category) if clip is not None else 15
    if clip_duration:
        length = max(1, min(length, max(1, clip_duration)))
    instance = {"preset": str(preset_id), "name": str(name or preset_id),
                "category": category, "program": str(program),
                "args": dict(args or {}), "start": int(start), "duration": length,
                "knobs": dict(DEFAULT_KNOBS, **(knobs or {}))}
    if category == "out" and clip_duration:
        instance["start"] = max(0, clip_duration - length)
    return instance


def add(clip, preset_id, category="in", program="fade", args=None, knobs=None, start=0,
        duration=None, name="", replace=True):
    """Add an instance. An In/Out of the same *kind* replaces the existing one."""
    rows = instances(clip)
    if replace and category in ("in", "out") and category == "in":
        # Only one entrance per clip: the newest In replaces the previous one.
        rows[:] = [row for row in rows if row.get("category") != "in"]
    instance = make(preset_id, category, program, args, knobs, start, duration, name, clip)
    rows.append(instance)
    return instance


def remove(clip, instance):
    rows = instances(clip)
    for index, row in enumerate(rows):
        if row is instance or row is dict(instance) or row == instance:
            rows.pop(index)
            return True
    return False


def find(clip, instance):
    for row in instances(clip):
        if row is instance:
            return row
    return None


def set_knob(instance, name, value):
    knobs = instance.setdefault("knobs", {})
    knobs[name] = value
    return instance


def set_range(instance, start=None, duration=None, clip=None):
    """Move or resize an instance window (clip-local frames)."""
    clip_duration = int(getattr(clip, "duration", 0) or 0) or None
    if duration is not None:
        instance["duration"] = max(1, int(duration))
    if start is not None:
        instance["start"] = max(0, int(start))
    if clip_duration:
        instance["duration"] = max(1, min(int(instance["duration"]), clip_duration))
        instance["start"] = max(0, min(int(instance["start"]), clip_duration - 1))
        if instance.get("category") == "out":
            # An exit stays glued to the clip end unless it was dragged there.
            if instance.get("offset_from_end", True):
                instance["start"] = max(0, clip_duration - int(instance["duration"]))
    return instance


# ---------------------------------------------------------------- evaluation
def _unit_ranks(units, direction, seed):
    """0..1 position of every unit in the stagger order, by screen position."""
    count = len(units)
    if count <= 1:
        return [0.0] * count
    xs = [float(unit[0]) for unit in units]
    low, high = min(xs), max(xs)
    middle = (low + high) / 2.0
    if direction == "right":
        keys = [-x for x in xs]
    elif direction == "center":
        keys = [abs(x - middle) for x in xs]
    elif direction == "edges":
        keys = [-abs(x - middle) for x in xs]
    elif direction == "random":
        keys = [_hash(seed, 99, index) for index in range(count)]
    else:
        keys = list(xs)
    order = sorted(range(count), key=lambda index: (keys[index], index))
    rank = [0.0] * count
    for position, index in enumerate(order):
        rank[index] = position / float(count - 1)
    return rank


def _run_program(instance, phase, knobs, unit, kind):
    """One program evaluation, with the intensity knob and the orientation folded in."""
    program = PROGRAMS.get(instance.get("program"))
    if program is None:
        return {}
    args = instance.get("args") or {}
    try:
        deltas = program(phase, knobs, unit, args) or {}
    except Exception:
        return {}
    intensity = float(knobs.get("intensity", 1.0))
    out = {}
    for name, value in deltas.items():
        if name == "text":
            out[name] = value
            continue
        if name in ("opacity",):
            out[name] = 1.0 - (1.0 - float(value)) * intensity
            continue
        if name in ("scale", "scale_x", "scale_y"):
            out[name] = 1.0 + (float(value) - 1.0) * intensity
            continue
        out[name] = float(value) * intensity
    return out


def _phase(instance, knobs, local_frame, start, length):
    """(eased arrival phase, raw 0..1 position) for one instance at a frame."""
    category = instance.get("category", "in")
    if category == "loop":
        raw = ((local_frame - start) % length) / float(length)
        return raw, raw
    raw = (local_frame - start) / float(length)
    raw = 0.0 if raw < 0.0 else (1.0 if raw > 1.0 else raw)
    if category == "out":
        return ease(1.0 - raw, knobs.get("easing")), 1.0 - raw
    return ease(raw, knobs.get("easing")), raw


def active_at(instance, local_frame, clip_duration):
    start, length = window(instance, clip_duration)
    if instance.get("category") == "loop":
        return start <= local_frame < start + length
    return start <= local_frame <= start + length


def evaluate(clip, local_frame, units=None, project_size=None):
    """The clip's preset layer at a clip-local frame.

    `units` is the text layout's unit list ``[(centre_x, centre_y, text)]`` in reading
    order; when it is missing (or the instance asks for the whole clip) every program
    runs once for the whole picture.
    """
    effect = Effect()
    rows = getattr(clip, "presets", None)
    if not rows:
        return effect
    frame = float(local_frame)
    duration = max(1, int(getattr(clip, "duration", 1) or 1))
    is_text = bool(getattr(clip, "is_text", False)) and bool(units)
    for instance in rows:
        category = instance.get("category", "in")
        start, length = window(instance, duration)
        if category == "loop":
            if not (start <= frame < start + length):
                continue
        elif frame < start or frame > start + length:
            continue
        knobs = _knobs(instance)
        if project_size and "span" not in knobs:
            knobs["span"] = project_size
        phase, raw = _phase(instance, knobs, frame, start, length)
        kind = knobs.get("unit") if isinstance(knobs.get("unit"), str) else "whole"
        unit_list = (units or {}).get(kind) or []
        unit_mode = is_text and kind in ("letter", "word", "line") and bool(unit_list)
        effect.marked.append(instance.get("preset"))
        if not unit_mode:
            effect.pose.add(_run_program(instance, phase, knobs, {"phase": raw}, category))
            continue
        ranks = _unit_ranks(unit_list, knobs.get("direction", "left"), knobs.get("seed", 0))
        spread = float(knobs.get("stagger", 0.35))
        inner = max(1e-4, 1.0 - spread)
        poses = effect.unit_poses[kind]
        while len(poses) < len(unit_list):
            poses.append(Pose())
        for index, unit in enumerate(unit_list):
            if category == "loop":
                unit_phase = (phase - ranks[index] * spread) % 1.0
            else:
                unit_raw = (raw - ranks[index] * spread) / inner
                unit_raw = 0.0 if unit_raw < 0.0 else (1.0 if unit_raw > 1.0 else unit_raw)
                unit_phase = ease(unit_raw, knobs.get("easing"))
            context = {"index": index, "count": len(unit_list),
                       "place": (ranks[index] * 2.0 - 1.0), "phase": unit_phase,
                       "text": unit[2] if len(unit) > 2 else None}
            poses[index].add(_run_program(instance, unit_phase, knobs, context, category))
    return effect


def split_instances(rows, frame, left_duration, right_duration):
    """(left, right) instance lists for a clip split at `frame`.

    An In that is still running lands on both pieces, an Out belongs to whichever piece
    ends the original clip (and is re-anchored to that end), and a Loop is cut to each
    side - so a split never changes what the two halves look like.
    """
    left, right = [], []
    for row in rows or ():
        start = int(row.get("start", 0))
        length = max(1, int(row.get("duration", 1)))
        end = start + length
        category = row.get("category", "in")
        if category == "out":
            if end > frame:
                clone = copy.deepcopy(row)
                clone["duration"] = max(1, min(length, max(1, right_duration)))
                clone["start"] = max(0, int(right_duration) - clone["duration"])
                right.append(clone)
            continue
        if start < frame:
            clone = copy.deepcopy(row)
            clone["start"] = max(0, start)
            clone["duration"] = max(1, min(length, frame - start))
            left.append(clone)
        if end > frame:
            clone = copy.deepcopy(row)
            new_start = max(0, start - frame)
            clone["start"] = new_start
            clone["duration"] = max(1, min(length - max(0, frame - start),
                                            max(1, right_duration) - new_start))
            right.append(clone)
    return left, right


def cache_key(clip):
    """Stable text of a clip's instances; part of the preview cache digest."""
    rows = getattr(clip, "presets", None) or []
    parts = []
    for instance in rows:
        knobs = instance.get("knobs") or {}
        parts.append("{}:{}:{}:{}:{}:{}".format(
            instance.get("preset"), instance.get("category"), instance.get("start"),
            instance.get("duration"), instance.get("program"),
            ",".join(f"{name}={knobs.get(name)!r}" for name in sorted(knobs))))
    return ";".join(parts)


def summary(instance):
    """"Text In · Pop · 18f" for a card title."""
    category = CATEGORY_LABELS.get(instance.get("category"), "In")
    return f"{instance.get('name') or instance.get('preset')} · {category} · {int(instance.get('duration', 0))}f"


# ---------------------------------------------------------------------- baking
BAKE_CHANNELS = ("position_x", "position_y", "scale_x", "scale_y", "rotation", "opacity")


def bakeable(instance, clip):
    """Per-unit instances cannot become clip keyframes: split the text instead."""
    knobs = _knobs(instance)
    return not (getattr(clip, "is_text", False) and knobs.get("unit") in ("letter", "word", "line"))


def bake(clip, instance, project_size=None):
    """Sample an instance into ordinary keyframes and drop it. Returns the frame list.

    Only the channels the pose actually uses are written, so baking a fade never adds
    position keys the user did not ask for. Keys inside the window replace the ones that
    were there; keys outside it are kept untouched.
    """
    from . import channels as CH

    duration = max(1, int(getattr(clip, "duration", 1) or 1))
    start, length = window(instance, duration)
    knobs = _knobs(instance)
    project_size = project_size or (1920, 1080)
    frames = sorted({0, max(0, start - 1), start, start + length, duration - 1}
                    | {frame for frame in range(start, start + length + 1)})
    frames = [frame for frame in frames if 0 <= frame < duration]
    samples = {}
    for frame in frames:
        effect = evaluate(_single(clip, instance), frame, project_size=project_size)
        pose = effect.pose
        samples[frame] = pose
    base = {}
    for channel in BAKE_CHANNELS:
        value = CH.value_at(clip, channel, 0)
        base[channel] = 100.0 if value is None and channel in ("scale_x", "scale_y", "opacity") else (
            float(value) if value is not None else 0.0)
    used = {"position_x": False, "position_y": False, "scale_x": False, "scale_y": False,
            "rotation": False, "opacity": False, "blur": False}
    for pose in samples.values():
        if abs(pose.dx) > 1e-6:
            used["position_x"] = True
        if abs(pose.dy) > 1e-6:
            used["position_y"] = True
        if abs(pose.scale_x - 1.0) > 1e-6:
            used["scale_x"] = True
        if abs(pose.scale_y - 1.0) > 1e-6:
            used["scale_y"] = True
        if abs(pose.rotation) > 1e-6:
            used["rotation"] = True
        if abs(pose.opacity - 1.0) > 1e-6:
            used["opacity"] = True
        if abs(pose.blur) > 1e-6:
            used["blur"] = True
    if not any(used.values()):
        remove(clip, instance)
        return []
    written = []
    inside = {frame for frame in frames if start <= frame <= start + length}
    for name, needed in used.items():
        if not needed:
            continue
        channel = CH.text_channel("blur") if name == "blur" else CH.transform_channel(name)
        scale = 100.0 if name in ("scale_x", "scale_y", "opacity") else 0.0
        rows = [(time, value, interp, in_handle, out_handle)
                for time, value, interp, in_handle, out_handle in CH.key_records(clip, channel)
                if int(time) not in inside]
        for frame in sorted(inside):
            pose = samples[frame]
            if name == "position_x":
                value = base["position_x"] + pose.dx
            elif name == "position_y":
                value = base["position_y"] + pose.dy
            elif name == "scale_x":
                value = base["scale_x"] * pose.scale_x
            elif name == "scale_y":
                value = base["scale_y"] * pose.scale_y
            elif name == "rotation":
                value = base["rotation"] + pose.rotation
            elif name == "opacity":
                value = base["opacity"] * pose.opacity
            else:
                value = (float(CH.value_at(clip, channel, 0) or 0.0)) + pose.blur
            rows.append((int(frame), float(value), "ease_in_out", None, None))
        if not rows:
            rows = [(0, float(scale), "linear", None, None)]
        CH.replace_keys(clip, channel, sorted(rows))
        written.append(channel)
    remove(clip, instance)
    return written


def _single(clip, instance):
    """A throwaway view of the clip with only one instance (baking one at a time)."""

    class _View:
        pass

    view = _View()
    view.duration = clip.duration
    view.presets = [instance]
    view.is_text = False
    return view


def saved_as_preset(clip, preset_id, name=None, category=None):
    """A .mfxpreset payload describing the clip's animation (Export/Save as preset)."""
    rows = instances(clip)
    if not rows:
        return None
    first = rows[0]
    # The file speaks the library's category keys ("text_in", "clip_loop", ...), while an
    # instance only knows In/Out/Loop; the clip tells us which family it belongs to.
    kind = "text" if getattr(clip, "is_text", False) else "clip"
    payload = {"format": "mfxpreset", "version": 1, "id": preset_id,
               "name": name or f"{getattr(clip, 'name', 'Clip')} animation",
               "category": category or f"{kind}_{first.get('category', 'in')}",
               "instances": [copy.deepcopy(row) for row in rows],
               "unit": _knobs(first).get("unit", "whole")}
    return payload


def from_payload(payload, clip):
    """Add every instance of an imported .mfxpreset to a clip."""
    added = []
    for row in payload.get("instances") or ():
        instance = make(row.get("preset", "imported"), row.get("category", "in"),
                        row.get("program", "fade"), row.get("args"),
                        row.get("knobs"), row.get("start", 0), row.get("duration"),
                        row.get("name", ""), clip)
        instances(clip).append(instance)
        added.append(instance)
    return added
