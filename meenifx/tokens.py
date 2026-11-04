"""Design tokens: the one place for spacing, sizes, fonts and colours.

Rules: one accent colour marks everything interactive or active; red is reserved for the
playhead and recording. Every text/icon colour reaches 4.5:1 against the surfaces it is
drawn on (checked by tests/test_ui_tokens.py).
"""

# Spacing scale in pixels.
SP1, SP2, SP3, SP4 = 4, 8, 12, 16
CONTROL_H = 28          # height of buttons, combos and fields
RADIUS = 4
ICON = 16               # default icon size
HIT = 22                # minimum hit area of small icon buttons

# Font sizes in pixels.
FONT_TITLE, FONT_BODY, FONT_SMALL = 13, 12, 11
FONT_FAMILY = "Segoe UI"

COLORS = {
    "bg": "#0b0d10", "panel": "#121821", "panel_alt": "#171d27",
    "panel_soft": "#1b2330", "border": "#2f3b4d",
    "text": "#eaf2ff",
    "muted": "#9aa9ba",          # secondary text and resting icons
    "disabled": "#8392a6",       # still >= 4.5:1 on every panel surface
    "accent": "#7c9cff", "accent_hover": "#3a5ad8", "on_accent": "#0b0d10",
    "accent_fill": "#2f4fd0",    # filled selection / checked backgrounds (white text >= 4.5:1)
    "red": "#ff5a67",            # playhead and recording only
    "success": "#6be0a6", "warning": "#ffb454",
    # Popups (tooltips, menus, combo drop-downs) share one readable dark surface.
    "popup": "#1b2230", "popup_text": "#e8edf7", "popup_border": "#323d54",
}


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
