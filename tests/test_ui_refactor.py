"""Checks for the simplified UI: tokens, top bar, preview row, timeline toolbar, effect cards."""

from PyQt6.QtCore import Qt
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
    assert layout.LAYOUT_VERSION == 8
    assert "controls" not in editor.docks


def test_effects_and_properties_are_separate_labelled_docks(editor):
    """The browser and the inspector are two docks, and View names both of them."""
    docks = editor.docks
    assert "effects" in docks and "properties" in docks
    assert docks["properties"].widget() is editor.effects_panel.props_scroll
    assert docks["properties"] not in editor.tabifiedDockWidgets(docks["effects"])
    assert editor.dockWidgetArea(docks["properties"]) == Qt.DockWidgetArea.RightDockWidgetArea
    entries = {action.text().strip() for action in editor._menu_view.actions() if action.isCheckable()}
    assert {"Effects", "Properties"} <= entries, f"View must label both panels, got {entries}"
    assert "" not in entries, "a panel entry with no text cannot be found in the View menu"


def test_default_layout_gives_the_timeline_a_third(qapp, monkeypatch, tmp_path):
    """The default shares must land on the docks, not be dropped by resizeDocks."""
    from PyQt6.QtCore import QSettings

    from meenifx.ui.main_window import MainWindow

    monkeypatch.setattr(layout, "app_settings",
                        lambda: QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat))
    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    window = MainWindow(qapp)
    window.resize(1500, 950)
    window.show()
    pump(qapp, 0.4)
    window.reset_layout()
    pump(qapp, 0.3)

    docks = window.docks
    column = docks["preview"].height() + docks["timeline"].height()
    assert column > 0
    share = docks["timeline"].height() / column
    assert 0.25 <= share <= 0.35, f"timeline takes {share:.0%} of the dock area, expected ~30%"
    width = sum(docks[key].width() for key in ("project", "preview", "effects"))
    project = docks["project"]
    assert project.width() >= project.minimumSizeHint().width(), "the project column was squeezed below its content"
    assert docks["preview"].width() > project.width(), "preview must stay the widest column"
    assert project.width() < width * 0.30, "the project column is back to half the window"
    window.close()


def _mouse(kind, y, button=Qt.MouseButton.LeftButton):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    held = Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    return QMouseEvent(kind, QPointF(600, y), QPointF(600, y), button, held,
                       Qt.KeyboardModifier.NoModifier)


def test_the_timeline_separator_drags_smaller(editor, qapp):
    """The Preview/Timeline boundary must shrink the timeline, not only grow it.

    Qt's own separator drag only makes the bottom dock bigger and silently ignores the
    other direction, so the window handles this boundary itself. The handlers are called
    directly here: Qt delivers real separator presses to the main window (its own drag
    depends on it), but a synthetic mouse event cannot be routed the same way offscreen.
    """
    from PyQt6.QtCore import QEvent

    editor.reset_layout()
    pump(qapp, 0.3)
    timeline = editor.docks["timeline"]
    # Start from a tall timeline: at the default 30 % it already sits near its minimum.
    layout.set_timeline_height(editor, 430)
    pump(qapp, 0.3)
    start_height = timeline.height()
    assert start_height > 380, f"could not grow the timeline for the test (at {start_height}px)"

    band = editor._timeline_splitter_band()
    assert band is not None, "the timeline separator is not reachable"
    editor.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, band[1] - 2))
    assert editor._split_drag is not None, "a press on the separator must start a drag"
    for step in (30, 60, 90, 120):
        editor.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, band[1] - 2 + step))
        pump(qapp, 0.02)
    editor.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, band[1] + 118))
    pump(qapp, 0.25)
    assert timeline.height() < start_height - 60, (
        f"timeline stayed at {timeline.height()}px after dragging the separator down")
    assert timeline.maximumHeight() > 100000, "the drag left the timeline pinned in place"


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
    browser = panel.browser
    editor.engine.session.set_selection({editor._test_clip_id})
    panel.refresh_effect_list()
    browser.refresh()
    categories = {card.item["subtitle"] for card in browser._cards}
    assert {"Stylize", "Tracking"} <= categories, categories
    assert "Looks" not in categories, "Looks are in the Styles tab now"
    mosaic_card = next(card for card in browser._cards if card.item["id"] == "mosaic")
    assert mosaic_card.item["applied"], "an applied effect carries its check mark"
    panel.search.setText("zzz")
    assert browser._cards == [] and not browser.empty.isHidden()
    panel.search.setText("")
    browser.refresh()
    assert browser._cards

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
