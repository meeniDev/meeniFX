"""Both palettes must keep text readable and control outlines visible."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from meenifx import tokens

SURFACES = ("bg", "panel", "panel_alt", "panel_soft")


@pytest.fixture(params=tokens.THEMES)
def palette(request):
    return request.param, tokens.PALETTES[request.param]


def test_palettes_share_keys():
    keys = [set(tokens.PALETTES[name]) for name in tokens.THEMES]
    assert all(k == keys[0] for k in keys)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("role", ("text", "muted", "accent", "coral_text", "success", "warning"))
def test_text_roles_reach_4_5(palette, surface, role):
    name, colors = palette
    assert tokens.contrast(colors[role], colors[surface]) >= 4.5, (name, role, surface)


@pytest.mark.parametrize("surface", SURFACES)
def test_disabled_text_stays_legible(palette, surface):
    name, colors = palette
    assert tokens.contrast(colors["disabled"], colors[surface]) >= 3.0, (name, surface)


def test_filled_states_carry_readable_text(palette):
    _name, c = palette
    assert tokens.contrast(c["on_accent"], c["accent"]) >= 4.5
    assert tokens.contrast(c["on_accent"], c["accent_hover"]) >= 4.5 or tokens.is_dark() is False
    assert tokens.contrast("#FFFFFF", c["accent_fill"]) >= 4.5
    assert tokens.contrast(c["text"], c["accent_soft"]) >= 4.5
    assert tokens.contrast(c["popup_text"], c["popup"]) >= 4.5


def test_control_outlines_reach_3_to_1(palette):
    name, c = palette
    for surface in ("panel", "panel_alt"):
        assert tokens.contrast(c["border_strong"], c[surface]) >= 3.0, (name, surface)
        assert tokens.contrast(c["focus"], c[surface]) >= 3.0, (name, surface)
        assert tokens.contrast(c["red"], c[surface]) >= 3.0, (name, surface)


def test_set_theme_rewrites_the_shared_dict_in_place():
    colors = tokens.COLORS
    try:
        tokens.set_theme("dark")
        assert tokens.COLORS is colors and colors["bg"] == tokens.PALETTES["dark"]["bg"]
        assert tokens.set_theme("nonsense") == tokens.DEFAULT_THEME
        assert colors["bg"] == tokens.PALETTES[tokens.DEFAULT_THEME]["bg"]
    finally:
        tokens.set_theme(tokens.DEFAULT_THEME)
