"""The one file dialog the app uses: dark, readable, and with its toolbar icons.

Qt's non-native ``QFileDialog`` is what the editor needs (the native one cannot follow the
dark theme at all), but out of the box it is unreadable on a dark surface: the platform
style draws its navigation glyphs in a dark colour, the header row fills a light strip, and
its own labels inherit a text colour the default background swallows.

Rather than restyle every call site, every path request goes through this module, which
builds the dialog, applies scoped QSS, tints the style's own standard icons to the theme
text colour, and returns the chosen paths.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QFileDialog, QHeaderView, QToolButton, QTreeView

from ..tokens import COLORS, RADIUS

def _qss():
    """The dialog sheet for the *current* theme.

    It used to be one f-string built at import time, which froze the palette the app
    happened to start with: switching to the dark theme left the file dialog light, with
    tinted navigation icons as its only dark-mode colours. Building the sheet when the
    dialog opens keeps the two in step.

    Scoped to the dialog: these rules must never leak into the editor's own widgets.
    """
    return f"""
QFileDialog {{ background: {COLORS['panel']}; }}
QFileDialog QLabel {{ color: {COLORS['muted']}; background: transparent; }}
QFileDialog QTreeView, QFileDialog QListView {{
    background: {COLORS['panel_alt']}; color: {COLORS['text']};
    alternate-background-color: {COLORS['panel']};
    border: 1px solid {COLORS['border']}; outline: 0; }}
QFileDialog QTreeView::item, QFileDialog QListView::item {{ padding: 3px 4px; }}
QFileDialog QTreeView::item:selected, QFileDialog QListView::item:selected {{
    background: {COLORS['accent_fill']}; color: #ffffff; }}
QFileDialog QTreeView::item:hover, QFileDialog QListView::item:hover {{
    background: {COLORS['panel_soft']}; }}
QFileDialog QHeaderView {{ background: {COLORS['panel_soft']}; }}
QFileDialog QHeaderView::section {{
    background: {COLORS['panel_soft']}; color: {COLORS['muted']};
    border: 0; border-right: 1px solid {COLORS['border']};
    border-bottom: 1px solid {COLORS['border']}; padding: 4px 6px; }}
QFileDialog QComboBox {{ background: {COLORS['panel_soft']}; border: 1px solid {COLORS['border']};
    border-radius: {RADIUS}px; color: {COLORS['text']}; padding: 0 8px; min-height: 22px; }}
QFileDialog QComboBox::drop-down {{ border: 0; width: 18px; }}
QFileDialog QComboBox QAbstractItemView {{
    background: {COLORS['panel_alt']}; color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    selection-background-color: {COLORS['accent_fill']}; selection-color: #ffffff; }}
QFileDialog QLineEdit {{ background: {COLORS['panel_alt']}; color: {COLORS['text']};
    border: 1px solid {COLORS['border']}; border-radius: {RADIUS}px; padding: 3px 6px; }}
QFileDialog QToolButton {{ background: {COLORS['panel_soft']}; border: 1px solid {COLORS['border']};
    border-radius: {RADIUS}px; padding: 0; min-width: 26px; min-height: 24px; }}
QFileDialog QToolButton:hover {{ background: {COLORS['panel_alt']}; border-color: {COLORS['accent']}; }}
QFileDialog QToolButton:disabled {{ background: {COLORS['panel']}; border-color: {COLORS['panel_soft']}; }}
QFileDialog QDialogButtonBox QPushButton {{ min-width: 84px; }}
QFileDialog QSplitter::handle {{ background: {COLORS['border']}; width: 1px; }}
"""


def tinted_icon(icon, colour=None, size=16):
    """Re-colour a style icon by keeping its alpha: the platform glyph, theme colour.

    The Windows style paints its standard pixmaps for a light surface, so on the dark
    dialog they were invisible. Masking keeps the platform's shapes (back, forward,
    parent, new folder, view modes) while making them legible.
    """
    if icon is None or icon.isNull():
        return icon
    pixmap = icon.pixmap(QSize(size, size))
    if pixmap.isNull():
        return icon
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    tinted = QImage(image.size(), QImage.Format.Format_ARGB32)
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawImage(0, 0, image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), QColor(colour or COLORS["text"]))
    painter.end()
    result = QPixmap.fromImage(tinted)
    result.setDevicePixelRatio(pixmap.devicePixelRatio())
    return QIcon(result)


def _tidy_columns(dialog):
    """Name takes the spare width; size, type and date fit their content.

    Qt's dialog hands every column the same width, which truncates "Folder" and the date
    into unreadable fragments ("Fil...der") as soon as the window is not tiny.
    """
    for view in dialog.findChildren(QTreeView):
        header = view.header()
        if header is None or header.count() < 2:
            continue
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, header.count()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(64)


def style_dialog(dialog):
    """Apply the dialog theme and restore the icons the platform style tints invisibly."""
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setStyleSheet(_qss())
    dialog.setMinimumSize(660, 420)
    if dialog.width() < 760:
        dialog.resize(880, 540)
    for button in dialog.findChildren(QToolButton):
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setIconSize(QSize(16, 16))
        icon = button.icon()
        if icon is not None and not icon.isNull():
            button.setIcon(tinted_icon(icon))
    _tidy_columns(dialog)
    return dialog


def open_dialog(parent, caption, directory, file_filter, multiple, selected_filter):
    dialog = QFileDialog(parent, caption, directory or "", file_filter or "")
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    dialog.setFileMode(QFileDialog.FileMode.ExistingFiles if multiple
                       else QFileDialog.FileMode.ExistingFile)
    dialog.setViewMode(QFileDialog.ViewMode.Detail)
    if selected_filter:
        dialog.selectNameFilter(selected_filter)
    return style_dialog(dialog)


def open_file(parent, caption, directory="", file_filter="", selected_filter=None):
    """A single existing file, or "" when the user cancels."""
    dialog = open_dialog(parent, caption, directory, file_filter, False, selected_filter)
    if dialog.exec() != QFileDialog.DialogCode.Accepted.value:
        return ""
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else ""


def open_files(parent, caption, directory="", file_filter="", selected_filter=None):
    """Existing files (multi-select), or [] when the user cancels."""
    dialog = open_dialog(parent, caption, directory, file_filter, True, selected_filter)
    if dialog.exec() != QFileDialog.DialogCode.Accepted.value:
        return []
    return list(dialog.selectedFiles())


def folder_dialog(parent, caption, directory=""):
    """The configured folder picker (built by ``choose_folder``; tests build it too)."""
    dialog = QFileDialog(parent, caption, directory or "")
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setViewMode(QFileDialog.ViewMode.Detail)
    return style_dialog(dialog)


def choose_folder(parent, caption, directory=""):
    """An existing directory, or "" when the user cancels."""
    dialog = folder_dialog(parent, caption, directory)
    if dialog.exec() != QFileDialog.DialogCode.Accepted.value:
        return ""
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else ""


def save_dialog(parent, caption, path="", file_filter="", selected_filter=None):
    """The configured save picker (built by ``save_file``; tests build it too)."""
    dialog = QFileDialog(parent, caption, path or "", file_filter or "")
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setViewMode(QFileDialog.ViewMode.Detail)
    if path:
        dialog.selectFile(path)
    if selected_filter:
        dialog.selectNameFilter(selected_filter)
    return style_dialog(dialog)


def save_file(parent, caption, path="", file_filter="", selected_filter=None):
    """A destination path ("" when cancelled); the suffix is left exactly as typed."""
    dialog = save_dialog(parent, caption, path, file_filter, selected_filter)
    if dialog.exec() != QFileDialog.DialogCode.Accepted.value:
        return ""
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else ""
