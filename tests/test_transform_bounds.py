"""Intrinsic transform bounds for generated layers.

Generated layers are full-frame pictures, but their transform box must follow the drawn
geometry. These tests pin the shape/stroke maths and the text layout against the pixels
the renderer actually produces, so a box can never silently fall back to the canvas.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.processing import shapes as SHAPES  # noqa: E402
from meenifx.processing import text as TEXT  # noqa: E402

PROJECT = (640, 360)


def shape_spec(kind="rectangle", **overrides):
    spec = {"kind": kind, "width": 200.0, "height": 100.0, "stroke_enabled": True,
            "stroke_width": 4.0, "align": "center", "cap": "butt",
            "arrow_start": False, "arrow_end": False,
            "fill_opacity": 1.0, "stroke_opacity": 1.0}
    spec.update(overrides)
    return spec


def text_spec(**overrides):
    spec = {"text": "Text", "font": "Segoe UI", "size": 64.0, "stroke_width": 0.0,
            "align": "center", "line_spacing": 1.15, "letter_spacing": 0.0,
            "offset_x": 0.0, "offset_y": 0.0, "wrap": 0.0, "reveal": -1,
            "bold": False, "italic": False}
    spec.update(overrides)
    return spec


def ink_bbox(frame):
    mask = frame[:, :, 3]
    rows = np.where(mask.max(axis=1) > 8)[0]
    cols = np.where(mask.max(axis=0) > 8)[0]
    assert len(rows) and len(cols), "the renderer drew nothing"
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def test_a_small_shape_reports_its_geometry_not_the_frame():
    left, top, width, height = SHAPES.bounds(shape_spec(), *PROJECT)
    assert abs(width - 204.0) < 0.01, "the centred stroke must be included"
    assert abs(height - 104.0) < 0.01
    assert abs(left - (320.0 - 102.0)) < 0.01
    assert abs(top - (180.0 - 52.0)) < 0.01
    assert width < PROJECT[0] * 0.5 and height < PROJECT[1] * 0.5


def test_shape_bounds_follow_stroke_alignment_and_line_geometry():
    inside = SHAPES.bounds(shape_spec(align="inside"), *PROJECT)
    centered = SHAPES.bounds(shape_spec(align="center"), *PROJECT)
    outside = SHAPES.bounds(shape_spec(align="outside"), *PROJECT)
    assert inside[2] < centered[2] < outside[2]
    assert inside[3] < centered[3] < outside[3]

    line = SHAPES.bounds(shape_spec("line", width=300.0, height=180.0,
                                   stroke_width=10.0, cap="round"), *PROJECT)
    assert abs(line[2] - 310.0) < 0.01, "round caps extend past both endpoints"
    assert abs(line[3] - 10.0) < 0.01, "a line's box is its stroke, not its height"
    assert abs((line[1] + line[3] / 2.0) - 180.0) < 0.01


def test_text_bounds_enclose_the_rendered_multiline_stroke():
    spec = text_spec(text="Hello\nWorld", size=72.0, stroke_width=5.0, bold=True,
                     align="left", offset_x=30.0, offset_y=-20.0, line_spacing=1.4)
    left, top, width, height = TEXT.bounds(spec, *PROJECT)
    rendered = ink_bbox(TEXT.render(spec, *PROJECT, 1.0))
    assert left <= rendered[0] + 1 and top <= rendered[1] + 1
    assert left + width >= rendered[2] - 1 and top + height >= rendered[3] - 1
    assert width < PROJECT[0] * 0.5, "the text box must not be the project frame"
    assert height < PROJECT[1] * 0.6
    assert width > 80.0 and height > 100.0, "both lines and their stroke must count"


def test_text_bounds_shrink_when_the_words_shrink():
    short = TEXT.bounds(text_spec(text="Hi"), *PROJECT)
    long = TEXT.bounds(text_spec(text="A much longer title"), *PROJECT)
    assert short[2] < long[2]
    assert abs((short[0] + short[2] / 2.0) - (long[0] + long[2] / 2.0)) < 0.5


def test_blank_text_has_no_visible_bounds():
    assert TEXT.bounds(text_spec(text="   "), *PROJECT) is None
