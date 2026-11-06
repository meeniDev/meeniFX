"""Design tokens: the one place for spacing, sizes, fonts and colours.

Two warm palettes (``light`` and ``dark``) share the same keys. ``COLORS`` is a single dict
that ``set_theme`` rewrites in place, so every module that reads ``tokens.COLORS[...]`` at
paint time follows a theme change without being re-imported. Widgets that bake a colour into
a stylesheet should go through ``ui.themed.themed_style`` so they are re-styled on a switch.

Rules: one accent colour (sage) marks everything interactive or active; red is reserved for
the playhead and recording; peach, butter and sky are decoration and clip colours, never the
only carrier of meaning. Every text/icon colour reaches 4.5:1 against the surfaces it is
drawn on and control outlines reach 3:1 (checked by tests/test_ui_tokens.py).
"""

# Spacing scale in pixels.
SP1, SP2, SP3, SP4, SP5, SP6 = 4, 8, 12, 16, 20, 24
CONTROL_H = 28          # height of buttons, combos and fields
CONTROL_H_SM = 24       # dense rows (inspector rows, track headers)
HIT = 22                # minimum hit area of small icon buttons
SECTION_H = 30          # collapsible-section header
CARD_PAD = 10

# Corner radii: soft but not bubbly.
RADIUS_SM = 4           # chips, tiny buttons, inline fields
RADIUS = 6              # buttons, combos, inputs
RADIUS_LG = 10          # cards, popups, thumbnails
RADIUS_PILL = 999

# Icon sizes (the glyph grid is 16 units and scales cleanly to these).
ICON_SM, ICON, ICON_LG = 14, 16, 20

# Font sizes in pixels; nothing in the app should go below FONT_SMALL.
FONT_TITLE, FONT_BODY, FONT_SMALL = 13, 12, 11
FONT_FAMILY = "Segoe UI"
# Fallbacks keep Japanese UI text and captions in a matching UI face instead of a serif.
FONT_STACK = "'Segoe UI', 'Yu Gothic UI', 'Meiryo UI', 'Noto Sans JP', sans-serif"

_SHARED = {
    # Brand artwork colours: decoration, illustrations, clip tints. Not for text.
    "peach": "#F7E6D9", "sage": "#A8C9B0", "butter": "#F4D98C", "sky": "#A9CDE5",
    "coral": "#E98C7A",
}

PALETTES = {
    "light": {
        "bg": "#F2EFE8", "panel": "#F9F7F2", "panel_alt": "#FFFFFF", "panel_soft": "#ECE8DE",
        "border": "#DAD3C4",         # separators and card outlines
        "border_strong": "#8F8777",  # outlines of inputs and toggles (>= 3:1)
        "text": "#333A40",
        "muted": "#5A6269",          # secondary text and resting icons
        "disabled": "#626A65",
        "accent": "#2E7260", "accent_hover": "#235A4B", "on_accent": "#FFFFFF",
        "accent_fill": "#33735F",    # filled selection / checked backgrounds (white text >= 4.5:1)
        "accent_soft": "#E1EDE5",    # hover / selected row tint (dark text stays readable)
        "focus": "#2E7260",
        "red": "#C8372D",            # playhead and recording only
        "success": "#276F44", "warning": "#8F5A00", "coral_text": "#A54430",
        "popup": "#FFFFFF", "popup_text": "#333A40", "popup_border": "#CFC7B6",
        "neutral_stage": "#1C1E20",  # preview canvas surround: neutral in both themes
        "scroll": "#C9C1B1", "scroll_hover": "#A89F8D",
    },
    "dark": {
        "bg": "#1B1F20", "panel": "#252A2C", "panel_alt": "#2D3336", "panel_soft": "#353C3F",
        "border": "#3F4749", "border_strong": "#7A8386",
        "text": "#F1EEE8",
        "muted": "#B4BBB8",
        "disabled": "#A2AAA7",
        "accent": "#8CC4AC", "accent_hover": "#A8D6C3", "on_accent": "#14201B",
        "accent_fill": "#2F6B58",
        "accent_soft": "#33433E",
        "focus": "#8CC4AC",
        "red": "#FF7B6E",
        "success": "#7FD0A0", "warning": "#F0B65A", "coral_text": "#F2A18F",
        "popup": "#353C3F", "popup_text": "#F1EEE8", "popup_border": "#4C5557",
        "neutral_stage": "#141617",
        "scroll": "#566063", "scroll_hover": "#78838A",
    },
}

THEMES = tuple(PALETTES)
DEFAULT_THEME = "light"

COLORS = {}
_state = {"name": None}


def set_theme(name):
    """Make ``name`` the active palette (in place) and return the name actually applied."""
    name = name if name in PALETTES else DEFAULT_THEME
    COLORS.clear()
    COLORS.update(_SHARED)
    COLORS.update(PALETTES[name])
    _state["name"] = name
    return name


def theme_name():
    return _state["name"]


def is_dark():
    return _state["name"] == "dark"


set_theme(DEFAULT_THEME)


def _lin(channel):
    value = channel / 255.0
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(color):
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(foreground, background):
    a, b = luminance(foreground), luminance(background)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)
