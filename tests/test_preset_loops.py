"""Loop presets repeat across the clip, in every mode, and survive trimming and splits.

The bug these cover: a Loop used its own `duration` as its window, so a 15-frame shake
played once and stopped instead of repeating for the whole clip.
"""

import pytest

from meenifx.model import presets as P
from tests.conftest import build_engine


def clip_engine(duration=90):
    engine, clip = build_engine(duration=duration)
    clip.duration = duration
    return engine, clip


class _Clip:
    """The minimal clip `evaluate` reads, so a split piece can be checked directly."""

    def __init__(self, duration, rows):
        self.duration = duration
        self.presets = rows
        self.is_text = False


def pose(clip, frame):
    return P.evaluate(clip, frame, project_size=(1920, 1080)).pose.key()


# ------------------------------------------------------------------- the window
def test_a_short_loop_repeats_across_the_whole_clip():
    _engine, clip = clip_engine(90)
    instance = P.add(clip, "shake", "loop", program="shake", duration=15)
    assert P.window(instance, 90) == (0, 90), "the cycle covers the clip, not 15 frames"
    assert P.active_at(instance, 75, 90), "a loop is still running near the clip end"


def test_the_cycle_repeats_exactly():
    _engine, clip = clip_engine(90)
    P.add(clip, "shake", "loop", program="shake", duration=15)
    assert pose(clip, 5) == pytest.approx(pose(clip, 20)), "one cycle later, same pose"
    assert pose(clip, 5) == pytest.approx(pose(clip, 35))
    assert pose(clip, 5) != pytest.approx(pose(clip, 11)), "mid-cycle it must differ"


def test_extending_the_clip_extends_the_loop():
    _engine, clip = clip_engine(90)
    instance = P.add(clip, "shake", "loop", program="shake", duration=15)
    assert P.window(instance, 240) == (0, 240), "no stored window to go stale on a trim"
    instance["start"] = 20
    assert P.window(instance, 240) == (20, 220)


def test_a_loop_placed_later_covers_the_rest_of_the_clip():
    _engine, clip = clip_engine(90)
    instance = P.add(clip, "shake", "loop", program="shake", duration=15, start=30)
    assert P.window(instance, 90) == (30, 60)
    assert not P.active_at(instance, 10, 90)


def test_an_explicit_coverage_narrows_the_loop():
    _engine, clip = clip_engine(90)
    instance = P.add(clip, "shake", "loop", program="shake", duration=15)
    P.set_loop_coverage(instance, 40)
    assert P.window(instance, 90) == (0, 40)
    assert not P.active_at(instance, 45, 90)


def test_a_repeat_count_stops_the_loop_after_that_many_cycles():
    _engine, clip = clip_engine(90)
    instance = P.add(clip, "shake", "loop", program="shake", duration=10,
                     knobs={"repeat": 3})
    assert P.window(instance, 90) == (0, 30)
    assert P.active_at(instance, 29, 90)
    assert not P.active_at(instance, 31, 90)


# ---------------------------------------------------------------------- modes
def test_pingpong_mirrors_each_cycle():
    _engine, clip = clip_engine(60)
    P.add(clip, "slide", "loop", program="slide", args={"dir": "left"}, duration=40,
          knobs={"loop_mode": "pingpong"})
    assert pose(clip, 10) == pytest.approx(pose(clip, 30)), "0.25 and 0.75 mirror"
    assert pose(clip, 5) == pytest.approx(pose(clip, 35))
    assert pose(clip, 5) != pytest.approx(pose(clip, 15))
    # The top of the cycle is reached in the middle: a real turn, not a jump to the start.
    assert pose(clip, 20) != pytest.approx(pose(clip, 0))


def test_repeat_mode_restarts_instead_of_turning():
    _engine, clip = clip_engine(60)
    P.add(clip, "slide", "loop", program="slide", args={"dir": "left"}, duration=40,
          knobs={"loop_mode": "repeat"})
    assert pose(clip, 5) != pytest.approx(pose(clip, 15))
    assert pose(clip, 30) != pytest.approx(pose(clip, 10))


def test_an_unknown_mode_falls_back_to_repeat():
    _engine, clip = clip_engine(60)
    instance = P.add(clip, "shake", "loop", program="shake", duration=10,
                     knobs={"loop_mode": "sideways"})
    assert P.loop_mode(instance) == "repeat"


def test_the_gap_holds_the_first_pose_between_cycles():
    _engine, clip = clip_engine(60)
    P.add(clip, "slide", "loop", program="slide", args={"dir": "left"}, duration=10,
          knobs={"gap": 10})
    assert pose(clip, 15) == pytest.approx(pose(clip, 0)), "the rest holds the start pose"
    assert pose(clip, 5) != pytest.approx(pose(clip, 0))
    assert pose(clip, 25) == pytest.approx(pose(clip, 5)), "and the next cycle resumes"


def test_the_phase_offset_shifts_the_pattern():
    _engine, clip = clip_engine(60)
    P.add(clip, "slide", "loop", program="slide", args={"dir": "left"}, duration=20)
    ahead = pose(clip, 13)
    P.instances(clip)[0]["knobs"]["phase"] = 5
    assert pose(clip, 8) == pytest.approx(ahead), \
        "a 5-frame offset runs five frames ahead of the pattern"


def test_a_legacy_loop_without_settings_repeats_to_the_clip_end():
    """Projects written before this fix only stored a duration: it becomes the cycle."""
    _engine, clip = clip_engine(120)
    legacy = {"preset": "shake", "name": "Shake", "category": "loop", "program": "shake",
              "args": {}, "start": 0, "duration": 12, "knobs": {}}
    P.instances(clip).append(legacy)
    assert P.window(legacy, 120) == (0, 120)
    assert P.loop_mode(legacy) == "repeat"
    assert pose(clip, 4) == pytest.approx(pose(clip, 16))


# --------------------------------------------------------------------- splits
def test_a_split_keeps_both_halves_looking_the_same():
    _engine, clip = clip_engine(90)
    P.add(clip, "shake", "loop", program="shake", duration=17,
          knobs={"intensity": 0.8})
    left_rows, right_rows = P.split_instances(P.instances(clip), 40, 40, 50)
    left, right = _Clip(40, left_rows), _Clip(50, right_rows)
    assert [row["category"] for row in left_rows] == ["loop"]
    assert left_rows[0]["duration"] == 17, "the split must not change the cycle"
    assert P.window(left_rows[0], 40) == (0, 40)
    assert P.window(right_rows[0], 50) == (0, 50)
    for frame in range(0, 40, 3):
        assert pose(left, frame) == pytest.approx(pose(clip, frame)), f"left frame {frame}"
    for frame in range(40, 90, 3):
        assert pose(right, frame - 40) == pytest.approx(pose(clip, frame)), \
            f"right frame {frame}"


def test_a_split_keeps_a_late_loop_late():
    _engine, clip = clip_engine(90)
    P.add(clip, "shake", "loop", program="shake", duration=13, start=60)
    left_rows, right_rows = P.split_instances(P.instances(clip), 40, 40, 50)
    assert left_rows == [], "a loop that starts at 60 is not on the left piece"
    assert right_rows[0]["start"] == 20
    assert P.window(right_rows[0], 50) == (20, 30)


def test_a_split_of_a_pingpong_loop_stays_mirrored():
    _engine, clip = clip_engine(90)
    P.add(clip, "slide", "loop", program="slide", args={"dir": "left"}, duration=24,
          knobs={"loop_mode": "pingpong"})
    left_rows, right_rows = P.split_instances(P.instances(clip), 31, 31, 59)
    left, right = _Clip(31, left_rows), _Clip(59, right_rows)
    for frame in (0, 6, 12, 18, 24, 30):
        assert pose(left, frame) == pytest.approx(pose(clip, frame))
    for frame in (31, 37, 45, 60, 75, 89):
        assert pose(right, frame - 31) == pytest.approx(pose(clip, frame)), f"frame {frame}"


def test_loop_settings_survive_a_project_round_trip():
    from meenifx.project import MediaLibrary
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = clip_engine(90)
    P.add(clip, "slide", "loop", program="slide", args={"dir": "left"}, duration=24,
          knobs={"loop_mode": "pingpong", "repeat": 3, "gap": 2, "phase": 4,
                 "coverage": 70}, name="Slide")
    library = MediaLibrary()
    sections = timeline_to_sections(engine.timeline, lambda path: library.add(path).id)
    restored = sections_to_timeline(sections, 30, lambda mid: library.items[mid].path)
    loaded = restored.find_clip(clip.id)
    row = P.instances(loaded)[0]
    assert P.loop_mode(row) == "pingpong"
    assert P.loop_repeat(row) == 3
    assert P.loop_gap(row) == 2
    assert P.loop_phase(row) == 4
    assert P.window(row, loaded.duration) == (0, 70)
    assert pose(loaded, 33) == pytest.approx(pose(clip, 33)), \
        "the reopened project plays exactly the same frames"


# ---------------------------------------------------------- cache and summary
def test_the_cache_digest_notices_the_loop_settings():
    from meenifx.preview import invalidation
    _engine, clip = clip_engine(90)
    P.add(clip, "shake", "loop", program="shake", duration=15)
    before = invalidation.clip_digest(clip)
    P.instances(clip)[0]["knobs"]["loop_mode"] = "pingpong"
    assert invalidation.clip_digest(clip) != before
    P.instances(clip)[0]["knobs"]["loop_mode"] = "repeat"
    P.instances(clip)[0]["knobs"]["repeat"] = 2
    assert invalidation.clip_digest(clip) != before


def test_the_summary_says_the_mode_and_the_cycle():
    _engine, clip = clip_engine(90)
    instance = P.add(clip, "shake", "loop", program="shake", duration=15, name="Shake")
    assert P.summary(instance) == "Shake · Loop · Repeat · 15f cycle"
    instance["knobs"]["loop_mode"] = "pingpong"
    assert "Ping-Pong" in P.summary(instance)


# ------------------------------------------------------------------------ UI
def test_the_animation_card_offers_loop_controls(qapp):
    from PyQt6.QtWidgets import QComboBox
    from meenifx.ui.panels import EffectsPanel
    from meenifx.ui.widgets import ScrubField

    engine, clip = clip_engine(90)
    instance = P.add(clip, "shake", "loop", program="shake", duration=15)
    panel = EffectsPanel(engine)
    engine.session.set_selection({clip.id})
    panel.refresh_properties()
    row = panel._preset_row(clip, 0, instance)
    labels = [field.label.text() for field in row.findChildren(ScrubField)]
    assert labels[:5] == ["Cycle", "Span", "Cycles", "Gap", "Phase"], labels
    combo = row.findChildren(QComboBox)[0]
    assert [combo.itemData(index) for index in range(combo.count())] == list(P.LOOP_MODES)
    assert combo.currentData() == "repeat"
    combo.setCurrentIndex(combo.findData("pingpong"))
    assert P.instances(engine.timeline.find_clip(clip.id))[0]["knobs"]["loop_mode"] == \
        "pingpong"
    panel.deleteLater()
