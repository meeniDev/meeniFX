"""The themed file dialog: readable text, real icons, sane columns.

The bug this pins down was visual and structural at once. `QProxyStyle()` with no base
proxies `QCommonStyle`, which has no standard pixmaps at all, so the navigation buttons and
every file/folder icon came out blank; and Qt's own dialog then sized its columns equally,
truncating "Folder" and the date into unreadable fragments on a dark background.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QFileDialog, QStyle, QToolButton, QTreeView  # noqa: E402

FILTER = "meeniFX Project Files (*.mfxpf)"


@pytest.fixture(scope="module")
def themed(qapp):
    from meenifx.theme import configure_theme
    configure_theme(qapp)
    return qapp


def test_the_theme_proxies_a_real_platform_style(themed):
    """The proxy must have an icon-bearing base, not the blank QCommonStyle default."""
    style = themed.style()
    assert style.standardIcon(QStyle.StandardPixmap.SP_DirIcon).isNull() is False
    assert style.standardIcon(QStyle.StandardPixmap.SP_FileIcon).isNull() is False
    assert style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack).isNull() is False


def test_the_base_style_stays_alive(themed):
    """QProxyStyle only holds a raw pointer; losing the base crashed the next repaint."""
    from meenifx import theme
    assert theme._BASE_STYLE is not None
    assert theme._BASE_STYLE.standardIcon(QStyle.StandardPixmap.SP_DirIcon).isNull() is False


def test_configure_theme_is_idempotent(themed):
    """Applying the theme again must not rebuild (or drop) the proxied base style."""
    from meenifx import theme

    base = theme._BASE_STYLE
    theme.configure_theme(themed)
    theme.configure_theme(themed)
    assert theme._BASE_STYLE is base
    assert themed.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon).isNull() is False


def test_every_toolbar_button_has_a_visible_icon(themed):
    from meenifx.ui.filedialogs import open_dialog

    dialog = open_dialog(None, "Open Project", "", FILTER, False, None)
    try:
        buttons = dialog.findChildren(QToolButton)
        assert buttons, "the styled dialog must still have its navigation buttons"
        for button in buttons:
            assert not button.icon().isNull(), "a navigation button lost its icon"
        # The stock icons are painted for a light surface; the tint makes them readable.
        pixmap = buttons[0].icon().pixmap(16, 16)
        image = pixmap.toImage()
        lightest = max((QColor(image.pixel(x, y)).lightness()
                        for x in range(image.width()) for y in range(image.height())
                        if QColor(image.pixel(x, y)).alpha() > 40), default=0)
        assert lightest > 150, "the icon must be tinted for the dark dialog"
    finally:
        dialog.close()
        dialog.deleteLater()


def test_the_dialog_is_dark_and_scoped(themed):
    from meenifx.tokens import COLORS
    from meenifx.ui.filedialogs import open_dialog

    dialog = open_dialog(None, "Open Project", "", FILTER, False, None)
    try:
        assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
        sheet = dialog.styleSheet()
        assert COLORS["panel_alt"] in sheet and "QHeaderView::section" in sheet
        assert dialog.minimumWidth() >= 640
        assert dialog.width() >= 760
    finally:
        dialog.close()
        dialog.deleteLater()


def test_columns_are_not_truncated(themed):
    from PyQt6.QtWidgets import QHeaderView

    from meenifx.ui.filedialogs import open_dialog

    dialog = open_dialog(None, "Open Project", "", FILTER, False, None)
    try:
        dialog.show()
        themed.processEvents()
        views = dialog.findChildren(QTreeView)
        assert views
        header = views[0].header()
        assert header.count() >= 2
        assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
        for column in range(1, header.count()):
            assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.ResizeToContents
    finally:
        dialog.close()
        dialog.deleteLater()


def test_open_dialog_selects_the_right_mode(themed):
    from meenifx.ui.filedialogs import open_dialog

    single = open_dialog(None, "Open", "", FILTER, False, None)
    many = open_dialog(None, "Import", "", FILTER, True, None)
    try:
        assert single.fileMode() == QFileDialog.FileMode.ExistingFile
        assert single.acceptMode() == QFileDialog.AcceptMode.AcceptOpen
        assert many.fileMode() == QFileDialog.FileMode.ExistingFiles
    finally:
        for dialog in (single, many):
            dialog.close()
            dialog.deleteLater()


def test_save_dialog_prefills_the_path(themed, tmp_path):
    from meenifx.ui.filedialogs import save_dialog

    target = str(tmp_path / "clip.mfxpf")
    dialog = save_dialog(None, "Save Project As", target, FILTER)
    try:
        assert dialog.acceptMode() == QFileDialog.AcceptMode.AcceptSave
        assert dialog.fileMode() == QFileDialog.FileMode.AnyFile
        assert os.path.basename(target) in dialog.selectedFiles()[0]
    finally:
        dialog.close()
        dialog.deleteLater()


def test_folder_dialog_shows_directories_only(themed, tmp_path):
    from meenifx.ui.filedialogs import folder_dialog

    dialog = folder_dialog(None, "Project location", str(tmp_path))
    try:
        assert dialog.fileMode() == QFileDialog.FileMode.Directory
        assert dialog.testOption(QFileDialog.Option.ShowDirsOnly)
        assert dialog.acceptMode() == QFileDialog.AcceptMode.AcceptOpen
    finally:
        dialog.close()
        dialog.deleteLater()


def test_no_call_site_builds_a_raw_file_dialog():
    """One dialog implementation means one place to fix: no stray getOpenFileName calls."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meenifx")
    offenders = []
    for folder, _dirs, files in os.walk(root):
        if "__pycache__" in folder:
            continue
        for name in files:
            if not name.endswith(".py") or name == "filedialogs.py":
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for call in ("QFileDialog.getOpenFileName", "QFileDialog.getOpenFileNames",
                         "QFileDialog.getSaveFileName", "QFileDialog.getExistingDirectory"):
                if call in text:
                    offenders.append(f"{os.path.relpath(path, root)}: {call}")
    assert offenders == [], "\n".join(offenders)
