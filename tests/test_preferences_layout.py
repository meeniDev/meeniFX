"""Preferences is a category dialog, not one long page that runs off the screen."""

from PyQt6.QtWidgets import QScrollArea

from meenifx.ui.preferences_dialog import PreferencesDialog, SECTIONS


def test_the_dialog_fits_a_1366_by_768_screen(qapp):
    dialog = PreferencesDialog(None)
    try:
        assert dialog.windowTitle() == "Preferences"
        assert dialog.stack.count() == len(SECTIONS) == 9
        assert dialog.width() <= 1366
        assert dialog.height() <= 768
        assert dialog.minimumWidth() >= 480
        assert dialog.minimumHeight() >= 320
    finally:
        dialog.close()
        dialog.deleteLater()


def test_every_category_has_its_own_scroll_area(qapp):
    dialog = PreferencesDialog(None)
    try:
        for row in range(dialog.stack.count()):
            page = dialog.stack.widget(row)
            assert isinstance(page, QScrollArea)
            assert page.widgetResizable()
            assert page.verticalScrollBar() is not None
        dialog.sidebar.setCurrentRow(7)
        assert dialog.stack.currentIndex() == 7
    finally:
        dialog.close()
        dialog.deleteLater()
