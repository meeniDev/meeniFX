"""Regressions for first-open font selection and directional text animation."""

import pytest

from conftest import build_engine
from meenifx.model import animation as ANIM, channels as CH
from test_keyframe_ui import editor, pump


def test_font_popup_is_populated_before_search(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QSettings
    from meenifx.ui import font_picker as picker
    monkeypatch.setattr(picker, "_store", lambda: QSettings(
        str(tmp_path / "fonts.ini"), QSettings.Format.IniFormat))
    popup = picker._FontPopup("Arial")
    try:
        assert popup.search.text() == ""
        assert popup.list.count() > 10
        popup.favourite.click()
        assert "Arial" in picker._read_list(popup._store, "fonts/favorites")
        popup.favourite.click()
        assert "Arial" not in picker._read_list(popup._store, "fonts/favorites")
        popup.search.setText("no such font 987654321")
        assert popup.list.count() == 2
        popup.search.clear()
        assert popup.list.count() > 10
    finally:
        popup.close()
        popup.deleteLater()


@pytest.mark.parametrize("slot", ["in", "out"])
@pytest.mark.parametrize("vertical,horizontal", [
    ("Up", "Left"), ("Up", "Right"), ("Down", "Left"), ("Down", "Right")])
def test_diagonal_animation_moves_both_axes_and_undoes(slot, vertical, horizontal):
    engine, _ = build_engine()
    clip_id = engine.session.add_text_clip("Direction", 0, 90)
    clip = engine.timeline.find_clip(clip_id)
    name = f"Slide {slot.title()} {vertical} {horizontal}"
    assert engine.session.apply_text_animation(clip_id, slot, name, 12)
    meta = ANIM.text_animation(clip, slot)
    start, end = meta["start"], meta["start"] + meta["duration"]
    x0, x1 = [CH.value_at(clip, "transform.position_x", t) for t in (start, end)]
    y0, y1 = [CH.value_at(clip, "transform.position_y", t) for t in (start, end)]
    sign = -1 if slot == "in" else 1
    assert (x1 - x0) * sign * (-1 if horizontal == "Left" else 1) > 0
    assert (y1 - y0) * sign * (-1 if vertical == "Up" else 1) > 0
    engine.session.stack.undo()
    clip = engine.timeline.find_clip(clip_id)
    assert not ANIM.text_animation(clip, slot)


def test_all_picker_fonts_load_the_selected_family(qapp):
    from meenifx.processing import fonts, text
    for family in fonts.system_families():
        face = fonts.resolve(family)
        assert face is not None, f"{family} would silently fall back"
        loaded = text.font_for(family, size=48)
        assert loaded.getname()[0].casefold() == family.casefold()


def test_qt_only_font_alias_cannot_silently_select_another_face(monkeypatch):
    from meenifx.processing import fonts
    monkeypatch.setattr(fonts, "qt_families", lambda: ["Unrenderable Qt Alias 123"])
    assert "Unrenderable Qt Alias 123" not in fonts.system_families()
    assert not fonts.installed("Unrenderable Qt Alias 123")


def test_in_and_out_share_a_resting_pose_without_drift():
    engine, _ = build_engine()
    cid = engine.session.add_text_clip("Resting pose", 0, 120)
    for duration in (20, 15, 25):
        engine.session.apply_text_animation(cid, "in", "Slide In Down Left", duration)
        engine.session.apply_text_animation(cid, "out", "Slide Out Up Right", duration)
        clip = engine.timeline.find_clip(cid)
        assert CH.value_at(clip, "transform.position_x", 50) == pytest.approx(0)
        assert CH.value_at(clip, "transform.position_y", 50) == pytest.approx(0)


def test_direction_control_applies_a_diagonal_in_the_inspector(editor, qapp):
    from PyQt6.QtWidgets import QComboBox
    cid = editor.engine.session.add_text_clip("Slide", 0, 120)
    editor.engine.session.apply_text_animation(cid, "in", "Slide In Left", 20)
    editor.engine.session.set_selection({cid})
    pump(qapp)
    panel = editor.effects_panel
    direction = next(box for box in panel.properties.findChildren(QComboBox)
                     if box.objectName() == "text_animation_in_direction" and box.isVisible())
    direction.setCurrentIndex(direction.findData("Down Right"))
    pump(qapp)
    clip = editor.engine.timeline.find_clip(cid)
    assert ANIM.text_animation(clip, "in")["preset"] == "Slide In Down Right"
    from PyQt6.QtWidgets import QWidget
    from meenifx import theme, tokens
    section = next(child for child in panel.properties.findChildren(QWidget)
                   if child.objectName() == "textAnimationSection" and child.isVisible())
    original = tokens.theme_name()
    try:
        theme.configure_theme(qapp, "dark")
        assert tokens.COLORS["panel_alt"] in section.styleSheet()
        dark = section.styleSheet()
        theme.configure_theme(qapp, "light")
        assert tokens.COLORS["panel_alt"] in section.styleSheet()
        assert section.styleSheet() != dark
    finally:
        theme.configure_theme(qapp, original)
