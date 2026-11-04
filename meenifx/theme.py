"""Single source of truth for the application's Qt dark palette and QSS."""

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

from .tokens import (COLORS, CONTROL_H, FONT_BODY, FONT_FAMILY, FONT_SMALL, FONT_TITLE, RADIUS,
                     SP1, SP2, SP3)

TOOLTIP_DELAY_MS = 450   # 400-500 ms: long enough not to flicker, short enough to feel instant

# Strong reference to the style the ThemeStyle proxy draws with (see _platform_style).
_BASE_STYLE = None
# Installing the style is process-wide, so it happens once - and a second call must not
# wrap the QStyleSheetStyle that setStyleSheet() installs around it.
_STYLE_INSTALLED = False


class ThemeStyle(QProxyStyle):
    """Keeps the platform style but shortens the tooltip wake-up delay.

    QToolTip's delay is a style hint, not a stylesheet property, so the only portable
    way to control it is to proxy the style. The base style is left untouched.

    The base must be the *real* platform style: a bare ``QProxyStyle()`` falls back to
    ``QCommonStyle``, which draws no standard icons at all (folder, file, back/forward,
    checkbox marks). That is why the stock file dialog arrived with empty buttons.
    """

    def __init__(self, base=None):
        super().__init__(base)

    def styleHint(self, hint, option=None, widget=None, data=None):
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_DELAY_MS
        if hint == QStyle.StyleHint.SH_ToolTip_FallAsleepDelay:
            return 1500
        return super().styleHint(hint, option, widget, data)


def _platform_style(app):
    """The concrete platform style to proxy, never the icon-less ``QCommonStyle``.

    ``app.style()`` hands back the native style wrapped in a ``QCommonStyle`` façade, so
    it is rebuilt by name through ``QStyleFactory``. On a platform without a native
    style entry the first available one (Fusion on Windows builds) is used instead.

    The result is kept in a module global on purpose: ``QProxyStyle`` only stores a raw
    pointer to the style it proxies, so letting the Python wrapper go out of scope freed
    it while the proxy kept drawing with it - a dangling pointer, and an access violation
    somewhere in the next repaint.
    """
    global _BASE_STYLE
    current = app.style()
    name = current.objectName() if current is not None else ""
    available = QStyleFactory.keys()
    if name not in available:
        name = "Fusion" if "Fusion" in available else (available[0] if available else "")
    if name:
        style = QStyleFactory.create(name)
        if style is not None:
            _BASE_STYLE = style
            return style
    return None


def configure_theme(app):
    # Install the style before palette and QSS: setStyle() may reset the palette, and
    # every widget created later must already see the shortened tooltip delay.
    global _STYLE_INSTALLED
    if not _STYLE_INSTALLED and not isinstance(app.style(), ThemeStyle):
        app.setStyle(ThemeStyle(_platform_style(app)))
        _STYLE_INSTALLED = True
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["panel_alt"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS["panel"]))
    # Tooltips: the QSS rule below styles the panel, the palette covers platforms where
    # the QToolTip selector is not honoured (notably some native styles).
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(COLORS["popup"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(COLORS["popup_text"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["panel_soft"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(COLORS["muted"]))
    app.setPalette(palette)
    app.setStyleSheet(f"""
        QWidget {{ background: {COLORS['panel']}; color: {COLORS['text']}; font: {FONT_BODY}px '{FONT_FAMILY}'; }}
        QWidget:disabled {{ color: {COLORS['disabled']}; }}
        QMainWindow {{ background: {COLORS['bg']}; }}
        /* Tooltips: dark surface, light text. The palette above is the fallback. */
        QToolTip {{ background-color: {COLORS['popup']}; color: {COLORS['popup_text']};
                    border: 1px solid {COLORS['popup_border']}; border-radius: {RADIUS}px;
                    padding: 5px 6px; font: {FONT_BODY}px '{FONT_FAMILY}'; }}
        QMenuBar {{ background: {COLORS['panel_alt']}; color: {COLORS['text']}; }}
        QMenuBar::item {{ background: transparent; padding: 4px 9px; }}
        QMenuBar::item:selected {{ background: {COLORS['border']}; border-radius: 3px; }}
        QMenu {{ background: {COLORS['panel_alt']}; color: {COLORS['text']};
                 border: 1px solid {COLORS['border']}; padding: 4px; }}
        QMenu::item {{ padding: 5px 24px 5px 10px; border-radius: 3px; background: transparent; }}
        QMenu::item:selected {{ background: {COLORS['accent_fill']}; color: #ffffff; }}
        QMenu::item:disabled {{ color: {COLORS['disabled']}; }}
        QMenu::separator {{ height: 1px; background: {COLORS['border']}; margin: 4px 6px; }}
        QToolBar {{ background: {COLORS['panel_alt']}; border: 0; spacing: {SP2}px; padding: {SP1}px {SP2}px; }}
        QToolBar::separator {{ background: {COLORS['border']}; width: 1px; margin: 4px {SP1}px; }}
        QDockWidget {{ border: 1px solid {COLORS['border']}; titlebar-close-icon: none; }}
        QDockWidget::title {{ background: {COLORS['panel_alt']}; padding: 6px; font-size: {FONT_TITLE}px; font-weight: bold; }}
        QTabWidget::pane, QTabBar::tab {{ border: 1px solid {COLORS['border']}; }}
        QTabBar::tab {{ background: {COLORS['panel_soft']}; color: {COLORS['muted']}; padding: 6px {SP3}px; font-size: {FONT_TITLE}px; }}
        QTabBar::tab:selected {{ background: {COLORS['panel']}; color: {COLORS['text']}; }}
        QPushButton, QToolButton, QComboBox {{ background: {COLORS['panel_soft']}; border: 1px solid {COLORS['border']};
                                              padding: 0 {SP3}px; border-radius: {RADIUS}px; min-height: {CONTROL_H - 2}px; }}
        QPushButton:hover, QToolButton:hover {{ background: {COLORS['panel_alt']}; border-color: {COLORS['accent']}; }}
        QPushButton:disabled, QToolButton:disabled {{ color: {COLORS['disabled']}; border-color: {COLORS['panel_soft']}; }}
        QPushButton[primary="true"] {{ background: {COLORS['accent']}; color: {COLORS['on_accent']}; border: 0; font-weight: bold; }}
        QPushButton[primary="true"]:hover {{ background: #9bb4ff; }}
        /* Icon buttons: flat until hovered or active. */
        QToolButton[role="icon"], QToolButton[role="transport"] {{ background: transparent; border: 1px solid transparent;
                                   padding: 0; min-width: {CONTROL_H - 2}px; color: {COLORS['text']}; }}
        QToolButton[role="icon"]:hover, QToolButton[role="transport"]:hover {{ background: {COLORS['panel_soft']}; border-color: {COLORS['border']}; }}
        QToolButton[role="icon"]:checked, QToolButton[role="transport"]:checked {{ background: {COLORS['accent_fill']}; border-color: {COLORS['accent']}; }}
        QToolButton[role="icon"]:disabled {{ background: transparent; }}
        QToolButton[role="primary-icon"] {{ background: {COLORS['accent']}; border: 0; border-radius: 16px;
                                            min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; padding: 0; }}
        QToolButton[role="primary-icon"]:hover {{ background: #9bb4ff; }}
        QToolButton::menu-indicator {{ image: none; width: 0; }}
        /* Tool toggles (Select / Razor / Snap / Loop): flat when off, accent when on. */
        QToolButton[role="tool"] {{ background: {COLORS['panel_soft']}; color: {COLORS['muted']};
                                   border: 1px solid {COLORS['border']}; padding: 0 {SP2}px; font-weight: normal; }}
        QToolButton[role="tool"]:hover {{ background: {COLORS['panel_alt']}; color: {COLORS['text']};
                                          border: 1px solid {COLORS['accent']}; }}
        QToolButton[role="tool"]:checked {{ background: {COLORS['accent_fill']}; color: #ffffff;
                                            border: 1px solid {COLORS['accent']}; font-weight: bold; }}
        QToolButton[role="tool"]:checked:hover {{ background: {COLORS['accent_hover']}; }}
        QLineEdit, QSpinBox, QDoubleSpinBox, QListWidget, QTreeWidget, QPlainTextEdit {{ background: {COLORS['panel_alt']}; border: 1px solid {COLORS['border']}; padding: {SP1}px; selection-background-color: {COLORS['accent_fill']}; }}
        QLineEdit {{ min-height: {CONTROL_H - 10}px; }}
        QFrame[card="true"] {{ background: {COLORS['panel_alt']}; border: 1px solid {COLORS['border']}; border-radius: {RADIUS + 2}px; }}
        QFrame[card="true"] QWidget {{ background: transparent; }}
        QLabel[quiet="true"] {{ color: {COLORS['muted']}; font-size: {FONT_SMALL}px; }}
        /* Drop-down lists (the zoom / quality combos and friends) open as a popup view. */
        QComboBox QAbstractItemView {{ background: {COLORS['panel_alt']}; color: {COLORS['text']};
                                       border: 1px solid {COLORS['border']}; outline: 0;
                                       selection-background-color: {COLORS['accent_fill']};
                                       selection-color: #ffffff; }}
        QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 2px 6px; }}
        QComboBox QAbstractItemView::item:hover {{ background: {COLORS['panel_soft']}; }}
        QSlider::groove:horizontal {{ height: 5px; background: {COLORS['panel_soft']}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {COLORS['accent']}; width: 14px; margin: -5px 0; border-radius: 7px; }}
        QCheckBox::indicator {{ width: 14px; height: 14px; }}
        QScrollBar:vertical, QScrollBar:horizontal {{ background: {COLORS['panel']}; border: 0; }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {COLORS['panel_soft']}; min-height: 22px; min-width: 22px; border-radius: 3px; }}
        QStatusBar {{ background: {COLORS['panel_alt']}; color: {COLORS['muted']}; }}
        QProgressBar {{ background: {COLORS['panel_soft']}; border: 1px solid {COLORS['border']}; text-align: center; }}
        QProgressBar::chunk {{ background: {COLORS['accent']}; }}
        /* Dialogs, message boxes and the (non-native) file dialog follow the same theme. */
        QDialog, QMessageBox {{ background: {COLORS['panel']}; color: {COLORS['text']}; }}
        QMessageBox QLabel {{ color: {COLORS['text']}; }}
        QDialogButtonBox QPushButton {{ min-width: 78px; }}
        QFileDialog {{ background: {COLORS['panel']}; }}
        QFileDialog QListView, QFileDialog QTreeView {{ background: {COLORS['panel_alt']}; color: {COLORS['text']};
                                                        border: 1px solid {COLORS['border']}; }}
        QFileDialog QLabel {{ color: {COLORS['text']}; }}
    """)
