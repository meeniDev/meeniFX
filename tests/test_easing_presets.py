"""Easing families, Auto/Continuous Bezier and the friendly preset menu.

The animation must match the curve the menu draws, so these tests compare the real
evaluator (`AnimatedValue.value_at`) against the mathematical easing formulas rather
than against a golden image.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.model import easing  # noqa: E402
from meenifx.model.keyframes import AnimatedValue, Keyframe  # noqa: E402


def animated(spec="linear", keys=((0, 0.0), (20, 100.0))):
    return AnimatedValue([Keyframe(time, float(value), spec) for time, value in keys], 0.0)


def test_every_family_has_in_out_and_in_out_and_hits_both_ends():
    families = ("Sine", "Quadratic", "Cubic", "Quartic", "Quintic", "Exponential",
                "Circular", "Back", "Bounce", "Elastic")
    names = set(easing.PRESET_ORDER)
    for family in families:
        for suffix in ("In", "Out", "In Out"):
            assert f"{family} {suffix}" in names, f"{family} {suffix} is missing"
    for name in easing.PRESET_ORDER:
        curve = easing.preset_curve(name, samples=64)
        assert curve[0][1] == pytest.approx(0.0, abs=1e-6), name
        assert curve[-1][1] == pytest.approx(1.0, abs=1e-6), name


def test_ease_in_starts_slow_and_ease_out_starts_fast():
    ease_in = easing.preset_spec("Cubic In")
    ease_out = easing.preset_spec("Cubic Out")
    assert ease_in["interpolation"] == "fn:cubic_in"
    assert easing.FUNCTIONS["cubic_in"](0.25) == pytest.approx(0.25 ** 3)
    assert easing.FUNCTIONS["cubic_out"](0.25) == pytest.approx(1.0 - 0.75 ** 3)


def test_bounce_and_elastic_are_real_curves_not_a_cubic():
    bounce = easing.preset_curve("Bounce Out", samples=200)
    values = [point[1] for point in bounce]
    # A bounce comes back up after falling: the curve is not monotonic.
    drops = sum(1 for a, b in zip(values, values[1:]) if b < a - 1e-6)
    assert drops >= 2, "bounce must fall and rebound"
    elastic = [point[1] for point in easing.preset_curve("Elastic Out", samples=200)]
    assert max(elastic) > 1.05 and min(elastic) < 0.95, "elastic must overshoot both ways"


def test_the_animation_matches_the_drawn_curve():
    for name in ("Sine In Out", "Quadratic Out", "Exponential In", "Circular In Out"):
        value = animated(easing.preset_spec(name)["interpolation"])
        curve = dict((round(x, 4), y) for x, y in easing.preset_curve(name, samples=20))
        ratio = round(10 / 20.0, 4)
        expected = 0.0 + 100.0 * curve[ratio]
        assert value.value_at(10) == pytest.approx(expected, abs=0.6), name


def test_presets_apply_to_multiple_keyframes_and_stay_editable():
    value = animated("linear", ((0, 0.0), (10, 50.0), (20, 100.0)))
    for key in value.keyframes:
        spec = easing.preset_spec("Back In Out")["interpolation"]
        key.interpolation = spec
    assert all(key.interpolation == "fn:back_in_out" for key in value.keyframes)
    # The user can go back to a hand-edited curve at any time.
    value.keyframes[1].interpolation = "bezier"
    value.keyframes[1].out_value_handle = (0.2, 0.0)
    assert 0.0 <= value.value_at(15) <= 100.0


def test_auto_bezier_uses_the_neighbours_and_stays_continuous():
    value = animated("auto", ((0, 0.0), (10, 100.0), (20, 100.0)))
    # The plateau after the second key flattens the arrival, so the middle is fast.
    assert value.value_at(5) > 50.0
    assert value.value_at(10) == pytest.approx(100.0)
    assert value.value_at(15) == pytest.approx(100.0, abs=0.01)
    # A straight line stays a straight line under auto handles.
    straight = animated("auto", ((0, 0.0), (10, 50.0), (20, 100.0)))
    assert straight.value_at(5) == pytest.approx(25.0, abs=2.0)
    assert straight.value_at(15) == pytest.approx(75.0, abs=2.0)


def test_auto_beziers_does_not_jump_at_the_keyframe():
    value = animated("auto", ((0, 0.0), (10, 100.0), (20, 100.0)))
    before = value.value_at(9.99)
    after = value.value_at(10.01)
    assert abs(after - before) < 1.0, "the curve must not step at the keyframe"


def test_hold_and_broken_specs_behave():
    hold = animated("hold")
    assert hold.value_at(19) == 0.0 and hold.value_at(20) == 100.0
    broken = animated("broken")
    assert 0.0 < broken.value_at(10) < 100.0
    assert broken.value_at(0) == 0.0 and broken.value_at(20) == 100.0


def test_legacy_preset_names_still_resolve():
    for name in ("Overshoot", "Spring-like", "Sharp Ease In", "Custom Bezier", "Hold"):
        assert easing.preset_spec(name) is not None, name
    assert "Overshoot" in easing.all_preset_names()
    assert "Overshoot" not in easing.menu_preset_names(), "legacy names stay out of the menu"


def test_menu_presets_are_the_friendly_ones():
    names = easing.menu_preset_names(include_custom=False)
    assert names[:5] == ("Linear", "Ease In", "Ease Out", "Ease In Out", "Smooth")
    for technical in ("Hold", "Auto Bézier", "Continuous Bézier", "Custom Bezier"):
        assert technical not in names
    assert [label for label, _spec, _tip in easing.ADVANCED_INTERPOLATION] == [
        "Hold", "Custom Bézier", "Auto Bézier", "Continuous Bézier", "Broken Bézier Handles"]


def test_custom_presets_are_saved_to_preferences_and_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(easing, "presets_path", lambda: str(tmp_path / "presets.json"))
    easing.save_custom_preset("My Snap", "bezier", (0.1, 0.9, 0.2, 1.0))
    assert "My Snap" in easing.menu_preset_names()
    payload = easing.preset_spec("My Snap")
    assert payload["handles"] == (0.1, 0.9, 0.2, 1.0)
    assert easing.preset_curve("My Snap")
    assert easing.rename_custom_preset("My Snap", "My Snap 2")
    assert "My Snap 2" in easing.all_preset_names()
    assert easing.delete_custom_preset("My Snap 2")
    assert "My Snap 2" not in easing.all_preset_names()
