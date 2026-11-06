"""Application style: platform style proxy, palette and the generated stylesheet."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

from . import tokens
from .stylesheet import build_stylesheet
from .tokens import COLORS

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


def _build_palette():
    c = COLORS
    palette = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.WindowText, "text"),
                      (QPalette.ColorRole.Base, "panel_alt"), (QPalette.ColorRole.AlternateBase, "panel"),
                      (QPalette.ColorRole.ToolTipBase, "popup"), (QPalette.ColorRole.ToolTipText, "popup_text"),
                      (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "panel_soft"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent_fill"),
                      (QPalette.ColorRole.PlaceholderText, "muted"), (QPalette.ColorRole.Link, "accent")):
        palette.setColor(role, QColor(c[key]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["disabled"]))
    return palette


def configure_theme(app, name=None):
    """Install the style, palette and stylesheet of ``name`` (default: the active theme).

    Safe to call again for a live switch: the style proxy is installed once, the palette and
    one application stylesheet are replaced, and widgets that registered with
    ``ui.themed`` are re-styled. Nothing is repolished widget by widget.
    """
    global _STYLE_INSTALLED
    if name is not None:
        tokens.set_theme(name)
    # Install the style before palette and QSS: setStyle() may reset the palette, and
    # every widget created later must already see the shortened tooltip delay.
    if not _STYLE_INSTALLED and not isinstance(app.style(), ThemeStyle):
        app.setStyle(ThemeStyle(_platform_style(app)))
        _STYLE_INSTALLED = True
    from .icons import clear_icon_cache
    from .ui.themed import restyle_all
    clear_icon_cache()
    app.setPalette(_build_palette())
    app.setStyleSheet(build_stylesheet())
    restyle_all()
    for widget in app.topLevelWidgets():
        widget.update()
    return tokens.theme_name()


def system_theme(app):
    """'dark' or 'light' following the operating system (light when it cannot be told)."""
    try:
        scheme = app.styleHints().colorScheme()
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"
    except AttributeError:
        return tokens.DEFAULT_THEME


def resolve_theme(app, choice):
    """Map a saved choice ('light', 'dark' or 'system') to a palette name."""
    return system_theme(app) if choice == "system" else (choice if choice in tokens.PALETTES
                                                         else tokens.DEFAULT_THEME)


