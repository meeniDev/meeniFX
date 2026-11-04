"""Easing curves for keyframes: cubic Beziers, spring/bounce/elastic presets and user presets.

A keyframe stores how the segment that *starts* at it is eased:

* ``linear`` and ``hold`` need no data;
* ``bezier`` uses the segment's two normalised control points, ``(x1, y1)`` on the
  left keyframe's ``out_value_handle`` and ``(x2, y2)`` on the right keyframe's
  ``in_value_handle``. Normalised coordinates make a curve independent of the
  segment's length and of the property's numeric range, so the same curve means the
  same motion for position, rotation or opacity;
* ``fn:<name>`` is a real mathematical curve (bounce, elastic, back, spring) that is
  evaluated directly, not approximated with a cubic Bezier.

Everything here is toolkit-free: the preview, the exporter and the graph editor all
run the very same evaluator, which is what keeps their curves identical.
"""

from __future__ import annotations

import json
import math
import os

# Default control points for the named Bezier presets. (x1, y1, x2, y2), x in [0, 1].
BEZIER_DEFAULTS = {
    "ease_in": (0.42, 0.0, 1.0, 1.0),
    "ease_out": (0.0, 0.0, 0.58, 1.0),
    "ease_in_out": (0.42, 0.0, 0.58, 1.0),
    "auto": (0.42, 0.0, 0.58, 1.0),
    "smooth": (0.25, 0.1, 0.35, 1.0),
}

# ------------------------------------------------------------------ easing families
# Each family is a real mathematical curve evaluated per sample of the segment, not a
# cubic Bezier standing in for one: that is what makes Bounce bounce and Elastic wobble.
# In/Out/In Out variants are derived from the "in" shape, so the families stay consistent.

def _clamped(t):
    return 0.0 if t <= 0.0 else (1.0 if t >= 1.0 else float(t))


def _out_of(shape):
    """Mirror an "in" shape so it decelerates instead of accelerating."""
    return lambda t: 1.0 - shape(1.0 - _clamped(t))


def _in_out_of(shape):
    """Accelerate over the first half, decelerate over the second."""
    def fn(t):
        t = _clamped(t)
        if t < 0.5:
            return shape(2.0 * t) / 2.0
        return 1.0 - shape(2.0 - 2.0 * t) / 2.0
    return fn


def _expo_in(t):
    t = _clamped(t)
    return 0.0 if t <= 0.0 else 2.0 ** (10.0 * t - 10.0)


def _circ_in(t):
    t = _clamped(t)
    return 1.0 - math.sqrt(max(0.0, 1.0 - t * t))


def _back_in(t, overshoot=1.70158):
    t = _clamped(t)
    return (overshoot + 1.0) * t ** 3 - overshoot * t ** 2


def _bounce_in(t):
    return 1.0 - ease_out_bounce(1.0 - _clamped(t))


def _elastic_in(t):
    t = _clamped(t)
    if t <= 0.0 or t >= 1.0:
        return t
    return -(2.0 ** (10.0 * t - 10.0)) * math.sin((10.0 * t - 10.75) * (2.0 * math.pi) / 3.0)


FAMILY_SHAPES = {
    "sine": lambda t: 1.0 - math.cos(_clamped(t) * math.pi / 2.0),
    "quad": lambda t: _clamped(t) ** 2,
    "cubic": lambda t: _clamped(t) ** 3,
    "quartic": lambda t: _clamped(t) ** 4,
    "quintic": lambda t: _clamped(t) ** 5,
    "exponential": _expo_in,
    "circular": _circ_in,
    "back": _back_in,
    "bounce": _bounce_in,
    "elastic": _elastic_in,
}

FAMILY_LABELS = {
    "sine": "Sine", "quad": "Quadratic", "cubic": "Cubic", "quartic": "Quartic",
    "quintic": "Quintic", "exponential": "Exponential", "circular": "Circular",
    "back": "Back", "bounce": "Bounce", "elastic": "Elastic",
}

# name -> (interpolation spec, normalised control points or None)
PRESETS = (
    ("Linear", "linear", None),
    ("Ease In", "ease_in", BEZIER_DEFAULTS["ease_in"]),
    ("Ease Out", "ease_out", BEZIER_DEFAULTS["ease_out"]),
    ("Ease In Out", "ease_in_out", BEZIER_DEFAULTS["ease_in_out"]),
    ("Smooth", "smooth", BEZIER_DEFAULTS["smooth"]),
) + tuple(
    (f"{label} {suffix}", f"fn:{key}_{direction}", None)
    for key, label in FAMILY_LABELS.items()
    for direction, suffix in (("in", "In"), ("out", "Out"), ("in_out", "In Out"))
)

# Kept so a project or a text animation saved with an older preset name still resolves.
LEGACY_PRESETS = (
    ("Hold", "hold", None),
    ("Fast In, Slow Out", "bezier", (0.12, 0.72, 0.2, 1.0)),
    ("Slow In, Fast Out", "bezier", (0.8, 0.0, 0.9, 0.35)),
    ("Sharp Ease In", "bezier", (0.75, 0.0, 1.0, 0.45)),
    ("Sharp Ease Out", "bezier", (0.0, 0.55, 0.25, 1.0)),
    ("Overshoot", "fn:back_out", None),
    ("Back", "fn:back_in_out", None),
    ("Bounce", "fn:bounce_out", None),
    ("Elastic", "fn:elastic_out", None),
    ("Spring-like", "fn:spring", None),
    ("Custom Bezier", "bezier", None),
)
PRESET_ORDER = tuple(name for name, _spec, _handles in PRESETS)
LEGACY_ORDER = tuple(name for name, _spec, _handles in LEGACY_PRESETS)

# The technical interpolation types live in their own submenu: they are the right tool
# for a specific job, but they are not "easing presets" a beginner should meet first.
ADVANCED_INTERPOLATION = (
    ("Hold", "hold", "Freeze the value until the next keyframe"),
    ("Custom B\u00e9zier", "bezier", "A cubic B\u00e9zier; its handles open in the Graph Editor"),
    ("Auto B\u00e9zier", "auto", "Smooth tangents derived from the neighbouring keyframes"),
    ("Continuous B\u00e9zier", "continuous", "Auto tangents kept continuous through the key"),
    ("Broken B\u00e9zier Handles", "broken", "Independent incoming and outgoing handles"),
)


def ease_out_bounce(t):
    """Bounce-out: a ball dropping and settling, exact at 0 and 1."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    n1, d1 = 7.5625, 2.75
    if t < 1 / d1:
        return n1 * t * t
    if t < 2 / d1:
        t -= 1.5 / d1
        return n1 * t * t + 0.75
    if t < 2.5 / d1:
        t -= 2.25 / d1
        return n1 * t * t + 0.9375
    t -= 2.625 / d1
    return n1 * t * t + 0.984375


def ease_out_back(t, overshoot=1.70158):
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    t -= 1.0
    return 1.0 + (overshoot + 1.0) * t * t * t + overshoot * t * t


def ease_in_out_back(t, overshoot=1.70158 * 1.525):
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    if t < 0.5:
        return 2.0 * (2.0 * t) ** 2 * ((overshoot + 1.0) * 2.0 * t - overshoot) / 2.0
    x = 2.0 * t - 2.0
    return (x * x * ((overshoot + 1.0) * x + overshoot) + 2.0) / 2.0


def ease_out_elastic(t):
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 2.0 ** (-10.0 * t) * math.sin((t - 0.075) * (2.0 * math.pi) / 0.3) + 1.0


def ease_spring(t):
    """Damped oscillation that settles on 1 with a small, decaying overshoot."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 1.0 - math.exp(-5.5 * t) * math.cos(9.0 * t)


FUNCTIONS = {
    "linear": lambda t: t,
    "back_out": ease_out_back,
    "back_in_out": ease_in_out_back,
    "bounce_out": ease_out_bounce,
    "elastic_out": ease_out_elastic,
    "spring": ease_spring,
}
# Every family in every direction: sine_in, quad_out, bounce_in_out, ...
for _key, _shape in FAMILY_SHAPES.items():
    FUNCTIONS[f"{_key}_in"] = _shape
    FUNCTIONS[f"{_key}_out"] = _out_of(_shape)
    FUNCTIONS[f"{_key}_in_out"] = _in_out_of(_shape)


def _bezier_axis(t, a, b):
    """One axis of a cubic Bezier whose end points are 0 and 1: 3at(1-t)^2 + 3bt^2(1-t) + t^3."""
    mt = 1.0 - t
    return 3.0 * a * t * mt * mt + 3.0 * b * t * t * mt + t * t * t


def bezier_t_for_x(x, x1, x2):
    """Parameter t with axis(t) == x (monotonic in t for x1, x2 in [0, 1])."""
    x = 0.0 if x < 0.0 else (1.0 if x > 1.0 else float(x))
    lo, hi = 0.0, 1.0
    t = x
    for _ in range(28):
        value = _bezier_axis(t, x1, x2)
        error = value - x
        if abs(error) < 1e-6:
            return t
        if error > 0.0:
            hi = t
        else:
            lo = t
        # Newton step, clamped into the bracket; bisection keeps it safe on flat regions.
        mt = 1.0 - t
        slope = 3.0 * x1 * mt * mt + 6.0 * (x2 - x1) * t * mt + 3.0 * (1.0 - x2) * t * t
        t_next = t - error / slope if slope > 1e-6 else (lo + hi) / 2.0
        if not (lo < t_next < hi):
            t_next = (lo + hi) / 2.0
        t = t_next
    return t


def bezier_ease(ratio, x1, y1, x2, y2):
    """Eased progress at `ratio` for control points clamped to a valid x range."""
    if ratio <= 0.0:
        return 0.0
    if ratio >= 1.0:
        return 1.0
    x1 = min(1.0, max(0.0, float(x1)))
    x2 = min(1.0, max(0.0, float(x2)))
    t = bezier_t_for_x(ratio, x1, x2)
    return _bezier_axis(t, y1, y2)


def parse_spec(spec):
    """('linear'|'hold'|'bezier'|'fn', payload) for a stored interpolation string."""
    text = str(spec or "linear").strip()
    if text == "hold":
        return "hold", None
    if text.startswith("fn:"):
        name = text[3:].strip()
        return ("fn", name) if name in FUNCTIONS else ("linear", None)
    if text in ("linear", ""):
        return "linear", None
    return "bezier", text


def spec_handles(spec):
    """Default (x1, y1, x2, y2) for a named Bezier interpolation, or None."""
    _kind, name = parse_spec(spec)
    return BEZIER_DEFAULTS.get(name) if name else None


def segment_progress(left_key, right_key, ratio, prev_key=None, next_key=None):
    """Eased progress for the segment between two keyframes.

    `left_key` decides the curve, exactly like "keyframe interpolation" means in
    After Effects: the curve stored on a keyframe eases the span after it. `prev_key`
    and `next_key` are only needed by Auto/Continuous Bezier, which derive their
    tangents from the neighbouring keyframes.
    """
    spec = getattr(left_key, "interpolation", "linear")
    kind, name = parse_spec(spec)
    if kind == "hold":
        return 0.0
    if kind == "fn":
        function = FUNCTIONS.get(name) or FUNCTIONS["linear"]
        return function(max(0.0, min(1.0, float(ratio))))
    if kind == "linear":
        return max(0.0, min(1.0, float(ratio)))
    handles = handles_for(left_key, right_key, spec, prev_key, next_key)
    if handles is None:
        return max(0.0, min(1.0, float(ratio)))
    x1, y1, x2, y2 = handles
    return bezier_ease(ratio, x1, y1, x2, y2)


AUTO_SPECS = ("auto", "continuous")


def auto_handles(left_key, right_key, prev_key=None, next_key=None):
    """Smooth tangents from the neighbouring keyframes (what Auto Bezier means).

    The tangent at each end of the segment is estimated from the key on the other side,
    so the curve leaves and arrives along the direction the animation is already going.
    That is what keeps a moving pin or a motion path from jumping at a keyframe.
    """
    dt = float(right_key.time) - float(left_key.time)
    if dt <= 0:
        return None
    dv = float(right_key.value) - float(left_key.value) if isinstance(left_key.value, (int, float)) \
        else 0.0
    if abs(dv) < 1e-9:
        return (1.0 / 3.0, 1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0)
    if prev_key is not None and isinstance(prev_key.value, (int, float)):
        span = max(1e-6, float(right_key.time) - float(prev_key.time))
        slope_left = (float(right_key.value) - float(prev_key.value)) / span
    else:
        slope_left = dv / dt
    if next_key is not None and isinstance(next_key.value, (int, float)):
        span = max(1e-6, float(next_key.time) - float(left_key.time))
        slope_right = (float(next_key.value) - float(left_key.value)) / span
    else:
        slope_right = dv / dt
    y1 = slope_left * dt / (3.0 * dv)
    y2 = 1.0 - slope_right * dt / (3.0 * dv)
    return (1.0 / 3.0, max(-2.0, min(3.0, y1)), 2.0 / 3.0, max(-2.0, min(3.0, y2)))


def handles_for(left_key, right_key, spec=None, prev_key=None, next_key=None):
    """(x1, y1, x2, y2) from the two keyframes, falling back to the preset defaults."""
    name = spec or getattr(left_key, "interpolation", "linear")
    if str(name).split(":")[-1] in AUTO_SPECS:
        computed = auto_handles(left_key, right_key, prev_key, next_key)
        if computed is not None:
            return computed
    own = getattr(left_key, "out_value_handle", None)
    other = getattr(right_key, "in_value_handle", None)
    own_ok = isinstance(own, (tuple, list)) and len(own) == 2
    other_ok = isinstance(other, (tuple, list)) and len(other) == 2
    if own_ok and other_ok:
        return (float(own[0]), float(own[1]), float(other[0]), float(other[1]))
    base = spec_handles(name)
    if base is None:
        # Nothing stored yet: a straight line to drag from (or an S curve for the
        # broken-handle mode, which is meant to be shaped by hand).
        base = (1.0 / 3.0, 0.0, 2.0 / 3.0, 1.0) if str(name) == "broken" \
            else (1.0 / 3.0, 1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0)
    # A dragged handle must count even when the other side is still at its default:
    # mixing the two is what makes a single-handle edit visible in the animation.
    x1, y1 = (float(own[0]), float(own[1])) if own_ok else (base[0], base[1])
    x2, y2 = (float(other[0]), float(other[1])) if other_ok else (base[2], base[3])
    return (x1, y1, x2, y2)


def normalized_handles(left_key, right_key):
    """Control points of a segment, always four numbers (used by the graph editor)."""
    handles = handles_for(left_key, right_key)
    if handles is not None:
        return handles
    kind, name = parse_spec(getattr(left_key, "interpolation", "linear"))
    if kind == "fn":
        # Sample the function at a third and two thirds so the graph can show its shape.
        return (1.0 / 3.0, FUNCTIONS[name](1.0 / 3.0), 2.0 / 3.0, FUNCTIONS[name](2.0 / 3.0))
    return (1.0 / 3.0, 1.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0)


def preset_spec(name):
    """({'interpolation': spec, 'handles': (x1,y1,x2,y2)|None} or None) for a preset name."""
    for preset_name, spec, handles in PRESETS + LEGACY_PRESETS:
        if preset_name == name:
            return {"interpolation": spec, "handles": handles}
    custom = load_custom_presets()
    payload = custom.get(name)
    if payload:
        return {"interpolation": payload.get("interpolation", "bezier"),
                "handles": tuple(payload["handles"]) if payload.get("handles") else None}
    return None


def preset_curve(name, samples=48):
    """[(x, y)] of a preset, for the little curve drawings in the preset menu."""
    payload = preset_spec(name)
    if payload is None:
        return []
    spec, handles = payload["interpolation"], payload["handles"]
    points = []
    for index in range(samples + 1):
        ratio = index / float(samples)
        kind, fn_name = parse_spec(spec)
        if kind == "fn":
            y = FUNCTIONS[fn_name](ratio)
        elif kind == "hold":
            y = 0.0 if ratio < 1.0 else 1.0
        elif kind == "linear":
            y = ratio
        else:
            curve = handles or spec_handles("ease_in_out")
            y = bezier_ease(ratio, *curve)
        points.append((ratio, y))
    return points


# ------------------------------------------------------------------ custom presets
def presets_path():
    """User preset file; outside the executable bundle so it survives upgrades."""
    from ..config import user_config_dir
    return os.path.join(user_config_dir(), "easing_presets.json")


def load_custom_presets():
    """{name: {"interpolation": str, "handles": [x1, y1, x2, y2]}} from disk."""
    try:
        with open(presets_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out = {}
    for name, value in payload.items():
        if isinstance(value, dict) and isinstance(name, str):
            out[name] = value
    return out


def _write_custom(presets):
    path = presets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(presets, handle, indent=1)


def save_custom_preset(name, interpolation="bezier", handles=None):
    """Store (or overwrite) a named reusable preset."""
    name = str(name).strip()
    if not name:
        raise ValueError("A preset needs a name")
    presets = load_custom_presets()
    presets[name] = {"interpolation": interpolation,
                     "handles": list(handles) if handles else None}
    _write_custom(presets)
    return name


def rename_custom_preset(old, new):
    presets = load_custom_presets()
    if old not in presets:
        return False
    new = str(new).strip()
    if not new:
        return False
    presets[new] = presets.pop(old)
    _write_custom(presets)
    return True


def delete_custom_preset(name):
    presets = load_custom_presets()
    if name not in presets:
        return False
    presets.pop(name)
    _write_custom(presets)
    return True


def all_preset_names():
    """Every name a stored animation may use: presets, legacy names, then the user's."""
    return tuple(PRESET_ORDER) + tuple(LEGACY_ORDER) + tuple(load_custom_presets())


def menu_preset_names(include_custom=True):
    """What the easing menus list: the friendly families first, custom presets last.

    The technical interpolation types are deliberately absent - they live under
    Advanced Interpolation (see ADVANCED_INTERPOLATION), not in the main list.
    """
    names = tuple(PRESET_ORDER)
    return names + tuple(load_custom_presets()) if include_custom else names
