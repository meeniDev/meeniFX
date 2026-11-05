"""Per-unit text animation: staggering, at-rest equality and the glyph cache.

The first test is the contract the whole feature rests on: a clip whose preset is not
running has to render the exact pixels the static renderer produced before presets
existed. The rest pin the layout maths (own-centre transforms, CJK, words) and the cache
that keeps a long title real-time.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import build_engine  # noqa: E402

from meenifx.model import presets as P  # noqa: E402
from meenifx.model import text as TXT  # noqa: E402
from meenifx.processing import text_units  # noqa: E402


def text_engine(text="HELLO WORLD", duration=60):
    engine, _base = build_engine()
    track = engine.timeline.add_track("video")
    engine.session.set_playhead(0)
    text_id = engine.session.add_text_clip(text, start=0, duration=duration, track=track)
    return engine, engine.timeline.find_clip(text_id)


def units_of(clip, frame=0, size=(640, 360)):
    spec = TXT.resolve(clip, frame)
    return text_units.layout_units(spec, size[0], size[1], 1.0)


def opacities(clip, frame, kind="letter", size=(640, 360)):
    effect = P.evaluate(clip, frame, units=units_of(clip, frame, size), project_size=size)
    return [round(pose.opacity, 3) for pose in effect.unit_poses[kind]]


def bbox(image):
    mask = image[:, :, 3] > 8
    if not mask.any():
        return None
    rows, cols = np.where(mask)
    return int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())


SPEC = {"text": "AB", "font": "DejaVu Sans", "size": 60, "color": "#FFFFFF",
        "stroke_width": 0.0, "shadow_opacity": 0.0, "align": "center", "bg_opacity": 0.0,
        "fill_opacity": 1.0, "line_spacing": 1.15, "letter_spacing": 0.0,
        "offset_x": 0.0, "offset_y": 0.0, "wrap": 0.0, "shadow_offset_x": 0.0,
        "shadow_offset_y": 0.0, "shadow_blur": 0.0, "reveal": -1}


class _Pose:
    """The smallest thing the renderer reads from a preset pose."""

    def __init__(self, **values):
        self.dx = values.get("dx", 0.0)
        self.dy = values.get("dy", 0.0)
        self.scale_x = values.get("scale", 1.0)
        self.scale_y = values.get("scale", 1.0)
        self.rotation = values.get("rotation", 0.0)
        self.opacity = values.get("opacity", 1.0)
        self.blur = values.get("blur", 0.0)
        self.hue = values.get("hue", 0.0)
        self.light = values.get("light", 0.0)
        self.text = values.get("text")

    def is_identity(self):
        return (abs(self.dx) < 1e-6 and abs(self.dy) < 1e-6 and abs(self.scale_x - 1) < 1e-6
                and abs(self.rotation) < 1e-6 and abs(self.opacity - 1) < 1e-6
                and abs(self.blur) < 1e-6 and abs(self.hue) < 1e-6
                and abs(self.light) < 1e-6 and self.text is None)


# ------------------------------------------------------------- at rest exactly
def test_a_finished_unit_preset_is_pixel_identical_to_the_static_text():
    engine, clip = text_engine("HELLO WORLD")
    static = {frame: engine.render_frame(frame, 1.0)[1].copy() for frame in range(0, 60)}
    P.add(clip, "pop", "in", program="pop", duration=24,
          knobs={"unit": "letter", "stagger": 0.7})
    for frame in (24, 25, 30, 45, 59):
        assert np.array_equal(engine.render_frame(frame, 1.0)[1], static[frame]), \
            f"frame {frame} is at rest and must not move a single pixel"


def test_before_and_between_instances_the_text_is_untouched():
    engine, clip = text_engine("TWO WORDS")
    static = {frame: engine.render_frame(frame, 1.0)[1].copy() for frame in range(0, 60)}
    # An In preset that ends at frame 10 and a Loop that only covers 20..40.
    P.add(clip, "pop", "in", program="pop", duration=10, knobs={"unit": "word"})
    P.add(clip, "wave", "loop", program="pulse", duration=20, start=20,
          knobs={"unit": "word"})
    for frame in (10, 11, 15, 19, 40, 41, 55):
        assert np.array_equal(engine.render_frame(frame, 1.0)[1], static[frame]), \
            f"frame {frame} sits between the animations and must look untouched"


def test_a_styled_multiline_title_is_untouched_at_rest():
    """Stroke, shadow, wrapping, tracking and right alignment all ride the static path."""
    engine, clip = text_engine("FIRST LINE SECOND LINE")
    clip.text.update({"size": 48, "align": "right", "wrap": 300.0, "line_spacing": 1.3,
                      "stroke_width": 3.0, "stroke_color": "#102030", "letter_spacing": 1.5,
                      "shadow_opacity": 0.7, "shadow_blur": 4.0,
                      "shadow_offset_x": 5.0, "shadow_offset_y": 6.0})
    rest = (10, 12, 30, 59)
    static = {frame: engine.render_frame(frame, 1.0)[1].copy() for frame in rest}
    P.add(clip, "fade", "in", program="fade", duration=10, knobs={"unit": "letter"})
    P.add(clip, "wave", "loop", program="pulse", duration=12, start=14,
          knobs={"unit": "letter", "stagger": 0.5})
    for frame in rest:
        assert np.array_equal(engine.render_frame(frame, 1.0)[1], static[frame]), \
            f"frame {frame} is outside every window and must not move a pixel"


# ----------------------------------------------------------------- staggering
def test_stagger_spreads_the_units_over_the_window():
    engine, clip = text_engine("ABCDEFGH")
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.0, "easing": "Linear"})
    together = opacities(clip, 6)
    assert len(set(together)) == 1, "with no stagger every letter shares one phase"
    P.instances(clip).clear()
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.8, "easing": "Linear"})
    spread = opacities(clip, 6)
    assert len(set(spread)) > 3, "a stagger has to give the letters different phases"
    assert spread[0] > spread[-1], "left to right: the first letters lead"


def test_direction_orders_the_units():
    engine, clip = text_engine("ABCDEFGH")
    order = {"left": 0, "right": -1, "center": None, "edges": None}
    for direction in ("left", "right"):
        P.instances(clip).clear()
        P.add(clip, "fade", "in", program="fade", duration=20,
              knobs={"unit": "letter", "stagger": 0.85, "direction": direction,
                     "easing": "Linear"})
        values = opacities(clip, 4)
        leader = 0 if direction == "left" else -1
        assert values[leader] == max(values), f"{direction} has to start at that end"
        assert values[-1 - leader] == min(values)

    # Center starts in the middle, edges start outside in.
    P.instances(clip).clear()
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.85, "direction": "center", "easing": "Linear"})
    centred = opacities(clip, 4)
    middle = len(centred) // 2
    assert centred[middle] >= max(centred) - 1e-9 or centred[middle - 1] >= max(centred) - 1e-9
    P.instances(clip).clear()
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.85, "direction": "edges", "easing": "Linear"})
    edges = opacities(clip, 4)
    middle = len(edges) // 2
    assert edges[0] == max(edges), "edges in: the outer letters start first"
    assert edges[-1] > max(edges[middle - 1], edges[middle]), "the middle letters lag"


def test_a_random_direction_is_stable_for_one_seed():
    engine, clip = text_engine("ABCDEFGH")
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.85, "direction": "random", "seed": 3,
                 "easing": "Linear"})
    first = opacities(clip, 4)
    P.instances(clip).clear()
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.85, "direction": "random", "seed": 3,
                 "easing": "Linear"})
    assert opacities(clip, 4) == first
    P.instances(clip).clear()
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.85, "direction": "random", "seed": 4,
                 "easing": "Linear"})
    assert opacities(clip, 4) != first


def test_words_and_lines_are_whole_units():
    engine, clip = text_engine("ONE TWO THREE")
    layout = units_of(clip)
    assert [unit[2] for unit in layout["word"]] == ["ONE", "TWO", "THREE"]
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "word", "stagger": 0.6, "easing": "Linear"})
    values = opacities(clip, 5, kind="word")
    assert len(values) == 3 and values[0] > values[-1]

    engine, clip = text_engine("FIRST\nSECOND\nTHIRD")
    layout = units_of(clip)
    assert [unit[2] for unit in layout["line"]] == ["FIRST", "SECOND", "THIRD"]
    ys = [round(unit[1]) for unit in layout["line"]]
    assert ys == sorted(ys) and len(set(ys)) == 3


def test_cjk_text_lays_out_one_unit_per_character():
    engine, clip = text_engine("第一行文字\n第二行")
    layout = units_of(clip)
    assert len(layout["line"]) == 2
    assert [unit[2] for unit in layout["letter"]][:4] == ["第", "一", "行", "文"]
    assert len(layout["letter"]) == 8


# --------------------------------------------------------- own-centre transforms
ONE_LETTER = dict(SPEC, text="A")


def centre_of(image):
    """Alpha-weighted ink centroid: a rigid transform about a point keeps it there."""
    alpha = image[:, :, 3].astype(np.float64)
    total = alpha.sum()
    assert total > 0, "nothing was drawn"
    rows, cols = np.mgrid[0:alpha.shape[0], 0:alpha.shape[1]]
    return float((alpha * cols).sum() / total), float((alpha * rows).sum() / total)


def test_a_unit_rotates_and_scales_around_its_own_centre():
    width, height = 320, 160
    static = text_units.render(ONE_LETTER, width, height, 1.0, None)
    original = centre_of(static)
    for pose, tolerance in ((_Pose(rotation=25.0), 1.0), (_Pose(scale=0.4), 1.0),
                            (_Pose(rotation=-15.0, scale=1.6), 2.5)):
        posed = text_units.render(ONE_LETTER, width, height, 1.0, {"letter": [pose]})
        moved = centre_of(posed)
        assert abs(moved[0] - original[0]) <= tolerance
        assert abs(moved[1] - original[1]) <= tolerance


def test_a_moved_unit_does_not_drag_its_neighbours():
    width, height = 320, 160
    static = text_units.render(SPEC, width, height, 1.0, None)
    layout = text_units.layout(SPEC, width, height, 1.0)
    second = layout["glyphs"][1]
    # The second glyph's own box (one pixel inside its left edge, so the neighbour's
    # moved ink cannot reach it) has to survive untouched.
    left = int(second["x"]) + 1
    right = int(second["x"] + second["w"]) + 2
    poses = {"letter": [_Pose(dy=-40.0), _Pose()]}
    posed = text_units.render(SPEC, width, height, 1.0, poses)
    assert np.array_equal(posed[:, left:right], static[:, left:right]), \
        "animating the first letter must not move the second one"
    assert not np.array_equal(posed, static), "the first glyph has to leave its place"


def test_a_scramble_reveal_replaces_glyphs_through_the_pose_text():
    width, height = 320, 160
    poses = {"letter": [_Pose(text="#"), _Pose(text="%")]}
    scrambled = text_units.render(SPEC, width, height, 1.0, poses)
    static = text_units.render(SPEC, width, height, 1.0, None)
    assert scrambled is not None and not np.array_equal(scrambled, static)


# ------------------------------------------------------------------- the cache
def test_glyph_bitmaps_are_reused_between_frames():
    text_units._STAMP_CACHE.clear()
    engine, clip = text_engine("ABCDE")
    P.add(clip, "fade", "in", program="fade", duration=24,
          knobs={"unit": "letter", "stagger": 0.9, "easing": "Linear"})
    engine.render_frame(4, 1.0)
    after_first = len(text_units._STAMP_CACHE)
    assert after_first >= 5, "every distinct glyph should have its own bitmap"
    engine.render_frame(8, 1.0)
    engine.render_frame(12, 1.0)
    assert len(text_units._STAMP_CACHE) == after_first, "frames must not rebuild bitmaps"


def test_a_long_title_does_not_explode_the_cache():
    text_units._STAMP_CACHE.clear()
    engine, clip = text_engine("THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789")
    P.add(clip, "fade", "in", program="fade", duration=30,
          knobs={"unit": "letter", "stagger": 0.9, "easing": "Linear"})
    for frame in range(0, 30, 3):
        engine.render_frame(frame, 1.0)
    assert len(text_units._STAMP_CACHE) <= 48, "one bitmap per distinct glyph, no more"
