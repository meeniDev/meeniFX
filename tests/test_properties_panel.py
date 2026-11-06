"""Properties panel: collapsible parameter groups and the card preview fallback."""

from PyQt6.QtWidgets import QWidget

from meenifx.effects.builtins import (BlobTrackerEffect, FaceTrackerEffect,
                                      HandTrackerEffect, MosaicEffect)
from meenifx.ui import panels as PANELS
from test_keyframe_ui import editor, pump  # noqa: F401  (shared MainWindow fixture)


def _groups(panel):
    """{title: holder} for the group sections currently in the properties panel."""
    found = {}
    for widget in panel.properties.findChildren(QWidget):
        title = getattr(widget, "group_title", None)
        if title:
            found[title] = widget
    return found


def _add(editor, qapp, effect_id):
    panel = editor.effects_panel
    panel.add_effect(effect_id)
    pump(qapp, 0.25)
    return panel


def test_face_tracker_settings_are_split_into_groups(editor, qapp):
    panel = _add(editor, qapp, "face_tracker")
    groups = _groups(panel)
    assert set(groups) == {"Detection", "Visualization", "Labels", "Connectors",
                           "Face Effects"}


def test_common_groups_open_and_advanced_groups_stay_folded(editor, qapp):
    panel = _add(editor, qapp, "face_tracker")
    groups = _groups(panel)
    assert groups["Detection"].group_body.isVisible()
    assert groups["Visualization"].group_body.isVisible()
    for title in ("Labels", "Connectors", "Face Effects"):
        assert not groups[title].group_body.isVisible(), f"{title} should start folded"
        assert groups[title].group_toggle.isChecked() is False


def test_a_folded_group_opens_and_keeps_its_state_across_a_rebuild(editor, qapp):
    panel = _add(editor, qapp, "face_tracker")
    labels = _groups(panel)["Labels"]
    labels.group_toggle.setChecked(True)
    pump(qapp, 0.05)
    assert labels.group_body.isVisible()

    # A structural change rebuilds every card; the group must stay where the user left it.
    panel._built_key = None
    panel.refresh_properties()
    pump(qapp, 0.1)
    labels = _groups(panel)["Labels"]
    assert labels.group_toggle.isChecked()
    assert labels.group_body.isVisible()


def test_every_visible_tracker_parameter_is_reachable(editor, qapp):
    panel = _add(editor, qapp, "face_tracker")
    rendered = {row.get("name") for row in panel._rows if row.get("name")}
    # The RGB channels collapse into one colour swatch row and the palette is a strip,
    # so neither appears under its own name.
    collapsed = {*PANELS.COLOR_NAMES, "palette_text"}
    specs = [spec for spec in FaceTrackerEffect.PARAMS
             if spec.name not in collapsed]
    missing = [spec.name for spec in specs if spec.name not in rendered]
    assert not missing, missing


def test_hand_and_blob_trackers_group_only_what_they_implement(editor, qapp):
    for effect_id, effect_class in (("hand_tracker", HandTrackerEffect),
                                    ("blob_tracker", BlobTrackerEffect)):
        panel = _add(editor, qapp, effect_id)
        groups = set(_groups(panel))
        declared = {spec.group for spec in effect_class.PARAMS if spec.group}
        assert groups == declared, (effect_id, groups, declared)
        # No Sprite Attachment exists in the implementation, so no empty heading either.
        assert "Sprite Attachment" not in groups


def test_effects_without_groups_render_flat(editor, qapp):
    panel = _add(editor, qapp, "mosaic")
    assert not _groups(panel)
    assert not any(spec.group for spec in MosaicEffect.PARAMS)


def test_tracker_cards_fall_back_to_the_reference_picture_and_an_icon(editor, qapp):
    browser = editor.effects_panel.browser
    pump(qapp, 0.2)
    card = next(card for card in browser._cards if card.item.get("id") == "face_tracker")
    assert card.thumb.text() == "", "the tracker tile should not be a word placeholder"
    pixmap = card.thumb.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    assert "no face or hand to detect" in card.toolTip()


def test_audio_cards_show_an_icon(editor, qapp):
    browser = editor.effects_panel.browser
    pump(qapp, 0.2)
    card = next(card for card in browser._cards if card.item.get("id") == "audio_gain")
    assert not card.thumb.text(), "audio cards use an icon, not words"
    assert card.thumb.pixmap() is not None and not card.thumb.pixmap().isNull()
