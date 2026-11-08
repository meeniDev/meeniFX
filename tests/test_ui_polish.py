"""Focused regression tests for the UI polish pass.

These cover the behaviours that were broken: an inspector edit must not rebuild the
panel, the preview workspace must follow the theme, selection rules must exist in both
palettes, and the first-launch tour must persist completion in user preferences.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QWidget

from meenifx import theme, tokens
from meenifx.engine import EditorEngine
from meenifx.stylesheet import build_stylesheet
from meenifx.ui import panels as PANELS
from meenifx.ui.onboarding import OnboardingDialog, STEPS, has_completed, mark_completed
from meenifx.ui.preview import PreviewWidget


def _shape_panel(qapp):
    engine = EditorEngine(30)
    engine.session.project_size = (640, 360)
    clip_id = engine.session.add_shape_clip("rectangle", start=0, duration=60)
    clip = engine.timeline.find_clip(clip_id)
    engine.session.set_selection({clip.id})
    panel = PANELS.EffectsPanel(engine)
    panel.props_scroll.resize(360, 260)
    panel.props_scroll.show()
    qapp.processEvents()
    return engine, clip, panel


def test_shape_value_edits_keep_the_same_rows_and_scroll(qapp):
    engine, clip, panel = _shape_panel(qapp)
    try:
        width_row = next(row for row in panel._rows
                         if row.get("channel") == "shape.width")
        field = width_row["field"]
        key = panel._built_key
        bar = panel.props_scroll.verticalScrollBar()
        bar.setValue(min(120, bar.maximum()))
        scroll = bar.value()

        panel._set_channel(clip, "shape.width", 240.0, live=True)
        panel._commit_channel(clip, "shape.width", 240.0)
        qapp.processEvents()

        assert panel._built_key == key, "a value edit rebuilt the shape card"
        assert width_row["field"] is field, "the edited control was destroyed"
        assert bar.value() == scroll
        assert engine.timeline.find_clip(clip.id).shape["width"] != 0
    finally:
        panel.props_scroll.close()
        panel.close()


def test_shape_structure_flags_are_the_only_rebuild_trigger(qapp):
    _engine, clip, panel = _shape_panel(qapp)
    try:
        before = panel._structure_key([clip])
        clip.shape["width"] = float(clip.shape.get("width") or 100) + 25
        clip.shape["fill_color"] = "#123456"
        assert panel._structure_key([clip]) == before

        clip.shape["kind"] = "star"
        assert panel._structure_key([clip]) != before, "type changes must still rebuild"
    finally:
        panel.props_scroll.close()
        panel.close()


def test_shape_toolbar_gives_the_disclosure_its_own_space(qapp, monkeypatch):
    from meenifx.ui.main_window import MainWindow
    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    window = MainWindow(qapp)
    window.resize(1100, 700)
    window.show()
    qapp.processEvents()
    try:
        assert window.shape_button.menu() is None
        assert window.shape_menu_button.menu() is not None
        assert (window.shape_button.geometry().right()
                < window.shape_menu_button.geometry().left())
    finally:
        window.close()


def test_selection_rules_are_explicit_in_the_stylesheet():
    css = build_stylesheet()
    for rule in ("QComboBox QAbstractItemView::item:selected",
                 "QListView::item:selected",
                 "QTreeView::item:selected"):
        assert rule in css
    assert "color: #FFFFFF" in css


def test_preview_workspace_uses_the_active_theme(qapp):
    previous = tokens.theme_name()
    try:
        theme.configure_theme(qapp, "light")
        view = PreviewWidget(quality_getter=lambda: 100)
        view.resize(400, 300)
        view.show()
        qapp.processEvents()
        image = QImage(400, 300, QImage.Format.Format_RGB32)
        image.fill(0)
        view.render(image)
        assert image.pixelColor(2, 2).name().upper() == tokens.COLORS["bg"].upper()

        theme.configure_theme(qapp, "dark")
        view.update()
        qapp.processEvents()
        image.fill(0)
        view.render(image)
        assert image.pixelColor(2, 2).name().upper() == tokens.COLORS["bg"].upper()
        view.close()
    finally:
        theme.configure_theme(qapp, previous)


def test_onboarding_finish_skip_and_reopen(qapp, tmp_path):
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    assert not has_completed(store)

    host = QWidget()
    host.tutorial_target = lambda _key: None
    host.resize(900, 600)
    host.show()
    dialog = OnboardingDialog(host, store=store)
    dialog.restart()
    try:
        assert dialog.next_button.text() == "Next"
        assert dialog.back_button.isEnabled() is False
        for _ in range(len(STEPS) - 1):
            dialog._next()
        assert dialog.next_button.text() == "Finish"
        dialog._next()
        assert has_completed(store)
        assert not dialog.isVisible()

        # Help > Getting Started must be able to restart a completed tour.
        dialog.restart()
        assert dialog.isVisible()
        assert dialog.progress.text().startswith("Step 1")
        dialog._skip()
        assert has_completed(store)
        assert has_completed(QSettings(str(tmp_path / "settings.ini"),
                                       QSettings.Format.IniFormat))
    finally:
        dialog.close()
        host.close()


def test_onboarding_completion_is_persisted_in_user_preferences(qapp, tmp_path):
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    mark_completed(store)
    reopened = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    assert has_completed(reopened)
