"""The browser contract: three tabs, double-click apply/remove, instance choice, styles.

Thumbnail rendering itself is covered by test_thumbnails.py; this file drives the parts a
person touches, without a MainWindow, so it stays fast.
"""

import copy
import time

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QFrame

from conftest import build_engine
from meenifx import thumbnails as THUMBS
from meenifx.effects import categories as EFFECTS_META
from meenifx.icons import icon
from meenifx.model import looks as LOOKS
from meenifx.ui.panels import EffectsPanel
from meenifx.ui.browser import LIST_H


@pytest.fixture
def panel_and_clip(qapp):
    engine, clip = build_engine(duration=90)
    engine.session.set_selection({clip.id})
    panel = EffectsPanel(engine)
    panel.refresh_effect_list()
    panel.browser.refresh()
    try:
        yield panel, engine, clip
    finally:
        panel.browser.shutdown()
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def cards(panel):
    return {card.item["id"]: card for card in panel.browser._cards}


def effects_of(engine, clip):
    return [entry["effect_id"] for entry in engine.timeline.find_clip(clip.id).effects]


# ----------------------------------------------------------------------- tabs
def test_the_browser_has_effects_animations_and_styles_tabs(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    assert [browser.tabs.tabText(index) for index in range(browser.tabs.count())] == \
        ["Effects", "Animations", "Styles"]
    assert "mosaic" in cards(panel)

    browser.tabs.setCurrentIndex(1)
    assert browser.tab == "animations"
    assert len(browser._cards) >= 15, "the families, not the 81 raw presets"
    assert all(card.item["kind"] == "animation" for card in browser._cards)
    assert "Looks" not in {card.item["subtitle"] for card in browser._cards}

    browser.tabs.setCurrentIndex(2)
    assert browser.tab == "styles"
    assert len(browser._cards) >= 10
    assert all(card.item["kind"] == "style" for card in browser._cards)


def test_looks_are_styles_and_no_longer_a_category(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    categories = {card.item["subtitle"] for card in browser._cards}
    assert "Looks" not in categories
    assert not any(card.item["id"] == "look" for card in browser._cards)


def test_browser_toggle_icons_share_one_grid(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    for button in (browser.favorites_button, browser.recent_button,
                   browser.grid_button, browser.list_button, browser.menu_button):
        assert (button.iconSize().width(), button.iconSize().height()) == (16, 16)
        assert button.size().width() == button.size().height() == 28


def test_browser_toggle_icons_are_real_shared_vectors(panel_and_clip):
    """Favorites/Recent must not be blank or malformed private-painter glyphs."""
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    for button in (browser.favorites_button, browser.recent_button,
                   browser.grid_button, browser.list_button):
        pixmap = button.icon().pixmap(16, 16)
        assert not pixmap.isNull()
        image = pixmap.toImage()
        assert any(image.pixelColor(x, y).alpha() for x in range(image.width())
                   for y in range(image.height())), "the button icon must not be blank"
    for name in ("star", "clock", "grid", "list"):
        image = icon(name, "#ffffff", 16).pixmap(16, 16).toImage()
        assert any(image.pixelColor(x, y).alpha() for x in range(image.width())
                   for y in range(image.height()))


def test_every_effect_has_a_visible_stroke_icon(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    icons = {card.item["id"]: card.item["icon"] for card in browser._cards}
    for key, name in icons.items():
        image = icon(name, "#ffffff", 16).pixmap(16, 16).toImage()
        assert not image.isNull(), key
        assert any(image.pixelColor(x, y).alpha() for x in range(image.width())
                   for y in range(image.height())), (key, name)
    audio = {name for key, name in icons.items() if key.startswith("audio_")}
    time = {icons[key] for key in ("posterize_time", "stutter", "echo")}
    assert len(audio) >= 8, "audio effects must not all reuse one music glyph"
    assert len(time) == 3, "each time effect needs its own frame motif"


def test_tracker_cards_fall_back_to_the_reference_picture_and_an_icon(panel_and_clip):
    """A tracker cannot detect anything in a still life, so its card shows the bundled
    reference picture with the tracker's own icon - never a blank tile and never a
    detection that did not happen."""
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    card = cards(panel)["face_tracker"]
    assert card.thumb.text() == "", "a picture fallback replaces the word placeholder"
    pixmap = card.thumb.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    image = pixmap.toImage()
    colours = {image.pixel(x, y) for x in range(0, image.width(), 7)
               for y in range(0, image.height(), 7)}
    assert len(colours) > 8, "the tile shows the real reference picture"
    assert "no face or hand" in card.toolTip()
    assert not browser._thumb_entries(card.item), "no fake detection render is scheduled"


def test_animation_cards_carry_a_real_preview(panel_and_clip):
    panel, engine, _clip = panel_and_clip
    browser = panel.browser
    browser.tabs.setCurrentIndex(1)
    entries = {card.item["id"]: browser._thumb_entries(card.item)
               for card in browser._cards}
    assert entries and all(value and value[0].get("animation")
                           for value in entries.values())
    item = next(card.item for card in browser._cards if "clip" in card.item["media"])
    image = THUMBS.render_entries(engine, browser._thumb_entries(item), 140, 96,
                                  THUMBS.PreviewSettings())
    assert image is not None and image.shape[:2] == (96, 140)
    assert float(image.std()) > 5.0, "a real picture, not a flat placeholder"
    assert browser._thumb_payload(item, browser._thumb_entries(item)), \
        "the media and variant must be part of the cache key"


def test_hover_plays_real_animation_frames_and_restores_the_static_card(panel_and_clip, qapp):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser.tabs.setCurrentIndex(1)
    panel.resize(620, 700)
    panel.show()
    qapp.processEvents()
    card = next(card for card in browser._cards if card.item["kind"] == "animation")
    static = card.thumb.pixmap().copy()

    browser._hover_begin(card.item, card)
    assert browser._hover_timer.isActive(), "preview waits for a short hover delay"
    browser._hover_start_playing()
    assert len(browser._hover_frames) == 6
    assert browser._hover_play_timer.isActive()
    first = card.thumb.pixmap().toImage()
    browser._hover_next_frame()
    assert card.thumb.pixmap().toImage() != first, "the hover tile actually animates"

    browser._hover_stop_playing()
    assert not browser._hover_play_timer.isActive()
    assert card.thumb.pixmap().toImage() == static.toImage(), "static fallback returns"
    panel.close()


def test_hover_preview_is_cancelled_when_the_pointer_leaves(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser.tabs.setCurrentIndex(1)
    card = next(card for card in browser._cards if card.item["kind"] == "animation")
    browser._hover_begin(card.item, card)
    browser._hover_end(card.item, card)
    assert not browser._hover_timer.isActive()
    assert browser._hover_card is None


def test_visible_animation_cards_get_their_thumbnail(panel_and_clip, qapp):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser.tabs.setCurrentIndex(1)
    panel.resize(560, 680)
    panel.show()
    end = time.monotonic() + 10.0
    while time.monotonic() < end:
        qapp.processEvents()
        if any(card.thumb_key for card in browser._cards):
            break
        time.sleep(0.01)
    panel.hide()
    assert any(card.thumb_key for card in browser._cards), \
        "the visible animation cards have to ask for a preview"
    browser.shutdown()


def test_search_finds_effects_by_everyday_words(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser.search.setText("pixel")
    assert "mosaic" in cards(panel)
    browser.search.setText("zzz-no-such-effect")
    assert browser._cards == []
    assert not browser.empty.isHidden()
    browser.search.setText("")


# ------------------------------------------------------------ apply and remove
def test_double_click_applies_and_double_clicking_again_removes(panel_and_clip):
    panel, engine, clip = panel_and_clip
    browser = panel.browser
    card = cards(panel)["mosaic"]
    browser._card_activated(card.item)
    assert effects_of(engine, clip) == ["mosaic"]
    panel.refresh_effect_list()
    browser.refresh()
    assert cards(panel)["mosaic"].item["applied"], "an applied effect shows its check mark"

    browser._card_activated(cards(panel)["mosaic"].item)
    assert effects_of(engine, clip) == []
    engine.session.stack.undo()
    assert effects_of(engine, clip) == ["mosaic"]
    engine.session.stack.redo()
    assert effects_of(engine, clip) == []


def add_copy(panel, engine, clip):
    """A clip can carry the same effect twice (an older project, or a pasted card)."""
    entry = engine.timeline.find_clip(clip.id).effects[0]
    panel.engine.session.apply_to_clips(
        [clip.id], "Duplicate effect", lambda target: target.effects.append(copy.deepcopy(entry)))


def test_a_second_copy_asks_which_instance_to_remove(panel_and_clip):
    panel, engine, clip = panel_and_clip
    panel.add_effect("mosaic")
    add_copy(panel, engine, clip)
    assert effects_of(engine, clip) == ["mosaic", "mosaic"]
    asked = []
    panel._choose_effect_instance = lambda clips, key, name: asked.append(
        (key, [len(c.effects) for c in clips]))
    panel._browser_remove({"kind": "effect", "id": "mosaic", "name": "Mosaic"})
    assert asked == [("mosaic", [2])], "the browser must never guess which copy to delete"
    assert effects_of(engine, clip) == ["mosaic", "mosaic"]

    panel._remove_instance_at([engine.timeline.find_clip(clip.id)], "mosaic", "Mosaic", 1)
    assert effects_of(engine, clip) == ["mosaic"]
    engine.session.stack.undo()
    assert effects_of(engine, clip) == ["mosaic", "mosaic"]


def test_remove_every_copy_is_one_step(panel_and_clip):
    panel, engine, clip = panel_and_clip
    panel.add_effect("mosaic")
    add_copy(panel, engine, clip)
    panel._remove_instance_at([engine.timeline.find_clip(clip.id)], "mosaic", "Mosaic", None)
    assert effects_of(engine, clip) == []
    engine.session.stack.undo()
    assert effects_of(engine, clip) == ["mosaic", "mosaic"]


def test_video_effects_are_unavailable_on_an_audio_clip(panel_and_clip):
    panel, engine, clip = panel_and_clip
    track = engine.timeline.add_track("audio")
    voice = engine.timeline.new_clip("voice", "synthetic.png", track, 200, 40,
                                     source_out=400, source_fps=30, stream="audio")
    engine.session.set_selection({voice.id})
    panel.refresh_effect_list()
    panel.browser.refresh()
    available = cards(panel)
    assert available["mosaic"].item["unavailable"], "a picture effect cannot run on audio"
    assert not available["mosaic"].isEnabled()
    assert not available["audio_eq"].item["unavailable"], "the audio effects are offered"


# --------------------------------------------------------------------- styles
def test_a_style_is_one_group_and_removing_it_keeps_other_effects(panel_and_clip):
    panel, engine, clip = panel_and_clip
    panel.add_effect("invert")
    look = LOOKS.all_looks()[0]
    assert panel.apply_look(look["id"]), "the style applies to the selected clip"

    entries = engine.timeline.find_clip(clip.id).effects
    group = next(entry for entry in entries if entry.get("effect_id") == "look")
    group_id = group["look_id"]
    members = [entry for entry in entries
               if entry.get("look_id") == group_id and entry.get("effect_id") != "look"]
    assert members, "the style brought its effects in as its members"
    assert effects_of(engine, clip)[0] == "invert", "the untouched effect keeps its order"

    panel.refresh_properties()
    assert not panel._opened_groups, "a style group starts collapsed"
    keys = [frame.property("effect_key") for frame
            in panel.properties_layout.parentWidget().findChildren(QFrame)
            if frame.property("effect_key")]
    assert "look" in keys, "the style gets one group card"

    panel._browser_remove({"kind": "style", "id": look["id"]})
    assert effects_of(engine, clip) == ["invert"], "only the style's own effects were removed"
    engine.session.stack.undo()
    assert len(effects_of(engine, clip)) == 1 + len(members) + 1


def test_disabling_a_style_group_disables_its_members(panel_and_clip):
    panel, engine, clip = panel_and_clip
    look = LOOKS.all_looks()[0]
    assert panel.apply_look(look["id"])
    fresh = engine.timeline.find_clip(clip.id)
    group = next(entry for entry in fresh.effects if entry.get("effect_id") == "look")
    panel._set_enabled(fresh, group, False)
    disabled_group = engine.timeline.find_clip(clip.id)
    group_id = group["look_id"]
    members = [entry for entry in disabled_group.effects
               if entry.get("look_id") == group_id and entry.get("effect_id") != "look"]
    strengths = type(engine).look_strengths(disabled_group.effects, 0)
    assert strengths.get(group_id) == 0.0, "a disabled style contributes nothing"
    assert members


# ------------------------------------------------------- library organisation
def test_effects_use_the_professional_categories_and_short_details(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    categories = {card.item["section"] for card in browser._cards}
    assert categories <= set(EFFECTS_META.CATEGORY_ORDER)
    assert {"Blur & Glow", "Color & Light", "Distortion", "Time & Motion",
            "Keying & Masking", "Tracking", "Stylize", "Audio"} <= categories
    assert "Looks" not in categories
    details = cards(panel)["posterize_time"].item["details"]
    assert details["category"] == "Time & Motion"
    assert details["keyframes"] and details["adjustment"]
    assert not details["experimental"]
    assert "Experimental: Yes" in cards(panel)["face_tracker"].item["tooltip"]


def test_alphabetical_sort_spans_the_whole_effect_library(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser.set_sort_mode("name")
    names = [card.item["name"].casefold() for card in browser._cards]
    assert names == sorted(names)
    assert all(section["header"] is None for section in browser._sections)

    browser.set_sort_mode("category")
    assert any(section["header"] is not None for section in browser._sections)


def test_list_view_has_compact_rows_and_collapsible_category_headers(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser.set_view_mode("list")
    headers = [section["header"] for section in browser._sections
               if section["header"] is not None]
    assert len(headers) >= 5
    row = cards(panel)["mosaic"]
    assert row.height() == LIST_H
    assert row.thumb.width() == 26
    category = headers[0].category
    before = len(browser._cards)
    browser._toggle_category(category)
    assert category in browser._collapsed_categories
    assert len(browser._cards) < before
    browser._toggle_category(category)
    assert category not in browser._collapsed_categories
    browser.set_view_mode("grid")


def test_the_chosen_view_and_sort_are_remembered(panel_and_clip, tmp_path):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser._store = QSettings(str(tmp_path / "browser.ini"), QSettings.Format.IniFormat)
    browser.set_view_mode("list")
    browser.set_sort_mode("name")
    assert browser._store.value("browser/view") == "list"
    assert browser._store.value("browser/sort") == "name"
    browser.set_view_mode("grid")
    browser.set_sort_mode("category")


def test_selection_explains_keyframes_adjustment_and_experimental_state(panel_and_clip):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    browser._card_selected(cards(panel)["posterize_time"].item)
    text = browser.info.text()
    assert "Time & Motion" in text
    assert "Keyframes: Yes" in text and "Adjustment: Yes" in text
    assert "Experimental" not in text
    browser._card_selected(cards(panel)["face_tracker"].item)
    assert "Experimental" in browser.info.text()


def test_multiple_selected_clips_receive_the_same_compatible_effect(panel_and_clip):
    panel, engine, clip = panel_and_clip
    browser = panel.browser
    second = engine.timeline.new_clip("second", "synthetic.png", clip.track, 200, 40,
                                      source_out=400, source_fps=30)
    engine.session.set_selection({clip.id, second.id})
    panel.refresh_effect_list()
    browser.refresh()
    browser._card_activated(cards(panel)["mosaic"].item)
    assert effects_of(engine, clip) == ["mosaic"]
    assert effects_of(engine, second) == ["mosaic"]


def test_time_effect_hover_uses_a_representative_frame_sequence(panel_and_clip, qapp):
    panel, _engine, _clip = panel_and_clip
    browser = panel.browser
    panel.resize(620, 700)
    panel.show()
    qapp.processEvents()
    card = cards(panel)["posterize_time"]
    browser._hover_begin(card.item, card)
    browser._hover_start_playing()
    assert len(browser._hover_frames) >= 6
    assert browser._hover_play_timer.isActive()
    browser._hover_stop_playing()
    panel.close()
