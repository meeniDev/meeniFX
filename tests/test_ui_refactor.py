"""Checks for the simplified UI: tokens, top bar, preview row, timeline toolbar, effect cards."""

from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QToolButton

from meenifx import tokens
from meenifx.ui import layout
from meenifx.ui.track_header import BUTTON, header_layout
from test_keyframe_ui import editor, pump  # noqa: F401  (shared fixture)


def test_text_and_icon_contrast_meets_4_5():
    c = tokens.COLORS
    for fg in ("text", "muted", "disabled"):
        for bg in ("panel", "panel_soft"):
            assert tokens.contrast(c[fg], c[bg]) >= 4.5, (fg, bg)
    assert tokens.contrast("#ffffff", c["accent_fill"]) >= 4.5
    assert tokens.contrast("#ffffff", c["accent_hover"]) >= 4.5
    assert tokens.contrast(c["on_accent"], c["accent"]) >= 4.5


def test_layout_version_and_docks(editor):
    assert layout.LAYOUT_VERSION == 5
    assert "controls" not in editor.docks


def test_top_bar_and_preview_row(editor):
    texts = [b.text() for b in editor.findChildren(QPushButton)]
    assert "Import" in texts and "Export" in texts
    assert "Play" not in texts and "Record" in texts
    assert editor.undo_button and editor.redo_button
    preview = editor.preview_panel
    items = [preview.quality_combo.itemText(i) for i in range(preview.quality_combo.count())]
    assert items == ["Full", "1/2", "1/4", "1/8", "Auto"]
    zooms = [preview.zoom_combo.itemText(i) for i in range(preview.zoom_combo.count())]
    assert "Fit" in zooms
    assert not hasattr(preview, "fit_button")
    editor.set_preview_quality("1/2")
    assert editor.engine.preview_quality.get() == 50


def test_timeline_toolbar(editor):
    panel = editor.timeline_panel
    assert [a.text() for a in panel.add_track.menu().actions()] == ["Video track", "Audio track"]
    labels = [lab.text() for lab in panel.findChildren(QLabel)]
    assert not any(t.startswith("Tracks") for t in labels)
    assert panel.canvas.TOP_H == 0
    assert panel.canvas.ruler_label(0, 30) == "0:00"
    assert "." not in panel.canvas.ruler_label(300, 30)


def test_track_header_hit_areas(editor):
    track = editor.engine.timeline.tracks[0]
    info = header_layout(track, 156, 64)
    assert info["buttons"], "header icons present"
    for _action, rect in info["buttons"]:
        assert rect.width() >= 22 and rect.height() >= 22
    assert BUTTON >= 22


def test_effect_categories_cards_and_color(editor, qapp):
    panel = editor.effects_panel
    tree = panel.effect_tree
    assert tree.topLevelItemCount() >= 2
    assert {tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())} >= {"Stylize", "Tracking"}
    stylize = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                   if tree.topLevelItem(i).text(0) == "Stylize")
    mosaic = next(stylize.child(i) for i in range(stylize.childCount()) if stylize.child(i).text(0) == "Mosaic")
    assert not mosaic.icon(0).isNull(), "added effects show a check"
    panel.search.setText("zzz")
    assert tree.topLevelItemCount() == 0
    panel.search.setText("")

    panel.add_effect("face_tracker")
    pump(qapp, 0.2)
    cards = [f for f in panel.properties.findChildren(QFrame) if f.property("card") and not f.isHidden()]
    keys = [f.property("effect_key") for f in cards]
    assert keys.count("transform") == 1, "the transform section is its own card"
    assert keys.count("face_tracker") == 1, "one card per effect"
    assert len(cards) == 4, "clip card + transform card + one card per effect"
    clip = editor.engine.timeline.find_clip(editor._test_clip_id)
    entry = next(e for e in clip.effects if e["effect_id"] == "face_tracker")
    names = [row["name"] for row in panel._rows if row["kind"] == "param"]
    assert not any(n in names for n in ("color_r", "color_g", "color_b")), "no RGB sliders"
    assert any(row["kind"] == "color" for row in panel._rows)

    stack = editor.engine.session.stack
    from PyQt6.QtGui import QColor
    panel.set_color(clip, entry, QColor(10, 20, 30))
    assert [entry["params"][n] for n in ("color_r", "color_g", "color_b")] == [10, 20, 30]
    stack.undo()
    clip = editor.engine.timeline.find_clip(editor._test_clip_id)
    entry = next(e for e in clip.effects if e["effect_id"] == "face_tracker")
    assert entry["params"]["color_r"] == 52, "colour change is one undo step"

    panel._card_dropped("face_tracker", 0)
    assert [e["effect_id"] for e in clip.effects] == ["face_tracker", "mosaic"]
    panel._copy_settings(entry)
    assert panel._clipboard[0] == "face_tracker"


def test_only_numeric_params_are_animatable(editor):
    panel = editor.effects_panel
    panel.add_effect("face_tracker")
    for row in panel._rows:
        if row["kind"] == "param":
            assert row["spec"].kind in ("int", "float")


def test_empty_state(editor, qapp):
    editor.engine.session.set_selection(set())
    editor.effects_panel.refresh_properties()
    layout_ = editor.effects_panel.properties_layout
    texts = [layout_.itemAt(i).widget().text() for i in range(layout_.count())
             if isinstance(layout_.itemAt(i).widget(), QLabel)]
    assert len(texts) == layout_.count() - 1 and texts == ["Select a clip"]
