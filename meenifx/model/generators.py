"""Metadata and animated settings for generated source clips.

A generator is a full-frame picture produced from numbers, not imported media.  It is
stored on the same generated-layer model as a text or shape clip (``shape.kind ==
"generator"``), so timeline move/trim/split/duplicate, effects, masks, transforms,
undo/redo, serialization and export all work without a parallel clip type.  This module is
the one source of truth for the five generator definitions and their defaults; the
renderer in :mod:`meenifx.processing.generators` consumes :func:`resolve`.
"""

from __future__ import annotations

import copy

from . import shape as SHAPE

KIND = "generator"


def _spec(label, icon, summary, fields):
    return {"label": label, "icon": icon, "summary": summary, "fields": tuple(fields)}


# field = (name, label, kind, default, minimum, maximum, step, decimals, unit, tooltip)
def _number(name, label, default, minimum, maximum, step=1.0, decimals=0,
            unit="", tooltip=""):
    return (name, label, "number", float(default), float(minimum), float(maximum),
            float(step), int(decimals), unit, tooltip)


def _color(name, label, default, tooltip=""):
    return (name, label, "color", default, 0, 0, 0, 0, "", tooltip)


def _choice(name, label, default, choices, tooltip=""):
    return (name, label, "choice", default, 0, 0, 0, 0, "", tooltip, tuple(choices))


def _text(name, label, default, tooltip=""):
    return (name, label, "text", default, 0, 0, 0, 0, "", tooltip)


SPECS = {
    "solid": _spec(
        "Solid Color", "shape_rectangle",
        "A flat colour source with adjustable opacity.",
        (_color("gen_color", "Color", "#4C9AFF", "The solid colour"),
         _number("gen_opacity", "Opacity", 100.0, 0.0, 100.0, 1.0, 0, "%",
                 "Opacity of the generated colour"))),
    "gradient": _spec(
        "Gradient", "gradient",
        "A multi-stop linear, radial, angled or diamond gradient.",
        (_color("gen_color_a", "Color A", "#4C9AFF", "First gradient colour"),
         _color("gen_color_b", "Color B", "#7A5CFF", "Last gradient colour"),
         _choice("gen_type", "Type", "linear",
                 (("Linear", "linear"), ("Radial", "radial"),
                  ("Angle", "angle"), ("Diamond", "diamond")),
                 "How the gradient is shaped"),
         _number("gen_angle", "Direction", 0.0, -360.0, 360.0, 1.0, 0, "°",
                 "Direction of a linear or angled gradient"),
         _text("gen_stops", "Stops", "0:#4C9AFF;100:#7A5CFF",
               "Optional multi-stop list: 0:#AABBCC; 50:#FFFFFF; 100:#112233"),
         _number("gen_opacity", "Opacity", 100.0, 0.0, 100.0, 1.0, 0, "%",
                 "Opacity of the generated picture"))),
    "checkerboard": _spec(
        "Checkerboard", "grid",
        "Two-colour checker squares with a pixel offset.",
        (_color("gen_color_a", "Color A", "#E8E8E8", "First square colour"),
         _color("gen_color_b", "Color B", "#262626", "Second square colour"),
         _number("gen_size", "Cell size", 48.0, 2.0, 2000.0, 1.0, 0, " px",
                 "Width and height of one checker square"),
         _number("gen_offset_x", "Offset X", 0.0, -10000.0, 10000.0, 1.0, 0, " px",
                 "Horizontal pattern offset"),
         _number("gen_offset_y", "Offset Y", 0.0, -10000.0, 10000.0, 1.0, 0, " px",
                 "Vertical pattern offset"),
         _number("gen_opacity", "Opacity", 100.0, 0.0, 100.0, 1.0, 0, "%",
                 "Opacity of the generated picture"))),
    "fractal_noise": _spec(
        "Fractal Noise", "sparkles",
        "Deterministic seeded fractal noise with colour mapping.",
        (_number("gen_seed", "Seed", 7.0, 0.0, 999999.0, 1.0, 0, "",
                 "The same seed always produces the same picture"),
         _number("gen_scale", "Scale", 100.0, 5.0, 400.0, 1.0, 0, "%",
                 "Size of the noise features"),
         _number("gen_detail", "Detail", 5.0, 1.0, 8.0, 1.0, 0, "",
                 "Number of noise octaves"),
         _number("gen_contrast", "Contrast", 100.0, 0.0, 300.0, 1.0, 0, "%",
                 "Contrast around mid-grey"),
         _number("gen_evolution", "Evolution", 0.0, -10000.0, 10000.0, 0.1, 1, "",
                 "Shifts the noise field; animate it for evolving clouds"),
         _color("gen_color_a", "Low color", "#111827", "Colour for dark noise"),
         _color("gen_color_b", "High color", "#E5E7EB", "Colour for bright noise"),
         _number("gen_opacity", "Opacity", 100.0, 0.0, 100.0, 1.0, 0, "%",
                 "Opacity of the generated picture"))),
    "grid": _spec(
        "Grid", "grid",
        "Evenly spaced lines on a solid background.",
        (_color("gen_color_a", "Line color", "#E5E7EB", "Colour of the grid lines"),
         _color("gen_color_b", "Background", "#111827", "Colour behind the lines"),
         _number("gen_spacing", "Spacing", 64.0, 2.0, 2000.0, 1.0, 0, " px",
                 "Distance between grid lines"),
         _number("gen_thickness", "Line thickness", 1.0, 1.0, 200.0, 1.0, 0, " px",
                 "Width of each grid line"),
         _number("gen_offset_x", "Offset X", 0.0, -10000.0, 10000.0, 1.0, 0, " px",
                 "Horizontal grid offset"),
         _number("gen_offset_y", "Offset Y", 0.0, -10000.0, 10000.0, 1.0, 0, " px",
                 "Vertical grid offset"),
         _number("gen_opacity", "Opacity", 100.0, 0.0, 100.0, 1.0, 0, "%",
                 "Opacity of the generated picture"))),
}

ORDER = ("solid", "gradient", "checkerboard", "fractal_noise", "grid")


def normalise(kind):
    text = str(kind or "").strip().lower().replace(" ", "_")
    aliases = {"solid_color": "solid", "solid_colour": "solid",
               "fractal": "fractal_noise", "noise": "fractal_noise",
               "checker": "checkerboard"}
    text = aliases.get(text, text)
    return text if text in SPECS else "solid"


def label(kind):
    return SPECS[normalise(kind)]["label"]


def icon(kind):
    return SPECS[normalise(kind)]["icon"]


def summary(kind):
    return SPECS[normalise(kind)]["summary"]


def fields(kind):
    return SPECS[normalise(kind)]["fields"]


def defaults(kind):
    return {field[0]: copy.deepcopy(field[3]) for field in fields(kind)}


def ensure(data, kind=None):
    """Fill generator settings in-place, keeping an existing selection and values."""
    if not isinstance(data, dict):
        return defaults(kind or "solid")
    key = normalise(kind if kind is not None else data.get("generator"))
    data["generator"] = key
    for field in fields(key):
        data.setdefault(field[0], copy.deepcopy(field[3]))
    return data


def setting(data, name, frame=None):
    """A generator value, including legacy AnimatedValue channels if a project has one."""
    value = (data or {}).get(name)
    from .keyframes import AnimatedValue
    if isinstance(value, AnimatedValue):
        return value.value_at(0 if frame is None else frame)
    return value


def resolve(data, frame=0):
    """Every generator setting evaluated at a frame, ready for the renderer."""
    key = normalise((data or {}).get("generator"))
    out = {"kind": key}
    for field in fields(key):
        name = field[0]
        value = setting(data, name, frame)
        if field[2] in ("number",):
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float(field[3])
            value = max(float(field[4]), min(float(field[5]), value))
        elif field[2] == "color":
            value = str(value or field[3])
        elif field[2] == "choice":
            allowed = {item[1] for item in field[10]}
            value = str(value or field[3])
            if value not in allowed:
                value = field[3]
        elif field[2] == "text":
            value = str(value if value is not None else field[3])
        out[name] = value
    return out


def set_static(clip, name, value, default_kind=None):
    """Write a static generator value on a clip, used by the inspector tests/utilities."""
    data = SHAPE.ensure_shape(clip)
    if default_kind is not None:
        data["generator"] = normalise(default_kind)
    ensure(data, data.get("generator"))
    data[name] = value
    return data
