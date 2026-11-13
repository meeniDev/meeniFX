"""ASCII-art rendering with explicit presets, installed fonts and real glyph coverage.

The old effect defaulted to a huge Unicode/CJK character set and painted it with a font
chosen from a hard-coded file list.  On machines without that font every cell became a
missing-glyph box.  This renderer keeps the selected characters deliberate: a conventional
preset or a custom set, an installed family resolved by the shared font registry, support
checked against the font's own .notdef glyph, and grapheme clusters kept together.
"""

from __future__ import annotations

import unicodedata

import cv2
import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont

from . import fonts as FONTS
from . import text as TEXT

PRESETS = {
    "ascii": ("ASCII", " .:-=+*#%@"),
    "blocks": ("Blocks", " \u2591\u2592\u2593\u2588"),
    "binary": ("Binary", " 01"),
    "unicode": ("Unicode dots", " \u00b7\u2022\u25cf"),
    "custom": ("Custom", ""),
}
PRESET_ORDER = ("ascii", "blocks", "binary", "unicode", "custom")

# Characters that attach to the preceding base character and must be treated as one
# glyph.  Regional-indicator pairs form one flag, and a ZWJ joins an arbitrary chain.
_VARIATION = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
_SKIN = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)
_JOINERS = {"\u200c", "\u200d"}
_MAX_GLYPH = 8


def graphemes(text):
    """Split a character set into user-perceived glyphs (base + marks + ZWJ chains)."""
    out = []
    current = ""
    for char in str(text or ""):
        code = ord(char)
        combining = unicodedata.combining(char) or any(low <= code <= high
                                                       for low, high in _VARIATION)
        skin = code in _SKIN
        regional = code in _REGIONAL
        if not current:
            current = char
            continue
        previous = current[-1]
        join = (combining or skin or char in _JOINERS or previous in _JOINERS
                or (regional and ord(previous) in _REGIONAL and len(
                    [part for part in current if ord(part) in _REGIONAL]) % 2 == 1))
        if join:
            current += char
        else:
            out.append(current)
            current = char
    if current:
        out.append(current)
    return out


def default_font():
    """A sensible installed monospace default, not a bundled CJK face."""
    for family in ("Consolas", "Courier New", "DejaVu Sans Mono", "Liberation Mono",
                   "Cascadia Mono", "Menlo", "Monaco"):
        if FONTS.installed(family):
            return family
    return "Consolas"


def _signature(font, text):
    """Rendered pixels of one glyph, comparable to the font's .notdef box.

    Pillow's ``getmask`` returns an ``ImagingCore`` without ``tobytes()`` on current
    Pillow builds, so drawing the glyph into a tiny image is the reliable way to tell a
    real glyph from a missing-glyph box across versions.
    """
    try:
        image = Image.new("L", (48, 48), 0)
        draw = ImageDraw.Draw(image)
        draw.text((2, 42), text, font=font, fill=255, anchor="ls")
        return image.tobytes()
    except Exception:
        return None


def _font_details(family, size):
    """(Pillow font, effective family, selected family installed?)."""
    selected = str(family or "").strip() or default_font()
    installed = FONTS.installed(selected)
    font = TEXT.font_for(selected, size=max(6, int(size)))
    effective = ""
    try:
        effective = str(font.getname()[0] or "")
    except Exception:
        effective = selected
    return font, effective, installed


def normalise_charset(preset, charset, font):
    """Return ``(glyphs, coverage hints, warnings)`` for the selected set and font.

    Empty/whitespace-only custom sets fall back to the conventional ASCII preset.
    Duplicate graphemes are removed (they cannot change a mapping), controls become
    spaces, and characters whose glyph is the font's missing-glyph box are replaced with
    ``?`` (or a supported dense glyph) with an explicit warning.
    """
    warnings = []
    key = str(preset or "ascii")
    if key == "custom":
        raw = str(charset or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if not raw.strip():
            raw = PRESETS["ascii"][1]
            warnings.append("Custom set was empty; using the conventional ASCII preset.")
        label = "custom"
    else:
        if key not in PRESETS:
            key = "ascii"
        raw = PRESETS[key][1]
        label = PRESETS[key][0]
    glyphs = graphemes(raw)
    if not glyphs:
        glyphs = graphemes(PRESETS["ascii"][1])
        warnings.append("No glyphs were available; using the conventional ASCII preset.")
    seen = set()
    unique = []
    duplicates = 0
    for glyph in glyphs:
        if glyph in seen:
            duplicates += 1
            continue
        seen.add(glyph)
        unique.append(glyph)
    if duplicates:
        warnings.append(f"Removed {duplicates} duplicate character"
                        f"{'s' if duplicates != 1 else ''} from the custom set.")
    glyphs = unique

    notdef = _signature(font, "\U0010FFFD")
    supported, unsupported = [], []
    for glyph in glyphs:
        signature = _signature(font, glyph)
        # A space has no ink but is still a valid density step; only a signature equal
        # to the font's .notdef box means the character is missing.
        if glyph.isspace() or signature is None or signature != notdef:
            supported.append(glyph)
        else:
            unsupported.append(glyph)
    if unsupported:
        replacement = "?"
        repl_signature = _signature(font, replacement)
        if repl_signature is None or repl_signature == notdef:
            replacement = "#" if _signature(font, "#") != notdef else " "
        missing = set(unsupported)
        mapped = []
        for glyph in glyphs:
            value = replacement if glyph in missing else glyph
            if value not in mapped:
                mapped.append(value)
        glyphs = mapped
        names = " ".join(repr(g) for g in unsupported[:5])
        more = "" if len(unsupported) <= 5 else f" and {len(unsupported) - 5} more"
        warnings.append(f"{label} uses {len(unsupported)} glyph(s) missing from this "
                        f"font ({names}{more}); showing {replacement!r} instead.")
    if not glyphs:
        glyphs = [" "]
        warnings.append("No selected character exists in this font; rendering blanks.")
    return glyphs, warnings


def _rgba(colour):
    text = str(colour or "#000000").strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        alpha = 255
        if len(digits) == 8:
            try:
                alpha = int(digits[0:2], 16)
            except ValueError:
                alpha = 255
            digits = digits[2:]
        try:
            return (int(digits[4:6], 16), int(digits[2:4], 16),
                    int(digits[0:2], 16), alpha)
        except ValueError:
            pass
    try:
        red, green, blue = ImageColor.getrgb(text)[:3]
        return int(blue), int(green), int(red), 255
    except Exception:
        return 0, 0, 0, 255


def _tile(glyph, font, cell, cell_height=None):
    height = max(3, int(cell_height or cell))
    width = max(3, int(cell))
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    # A baseline shared by every glyph keeps the rows from bobbing.  The 5 % margins
    # leave room for ascenders/descenders inside the cell.  Separate width and height
    # let Character spacing and Line spacing stretch the grid without resizing glyphs.
    try:
        draw.text((max(1, int(width * 0.05)), max(1, int(height * 0.92))), glyph,
                  font=font, fill=255, anchor="ls")
    except Exception:
        draw.text((0, 0), glyph, font=font, fill=255)
    mask = np.asarray(image, dtype=np.float32) / 255.0
    coverage = float(mask.mean())
    return mask, coverage


def build_plan(preset, charset, font_family, cell, use_coverage=True, font_size=0,
               char_spacing=0.0, line_spacing=0.0):
    """Everything one ASCII render needs, reusable across frames of the same size.

    ``cell`` is the base character cell; character and line spacing widen or narrow
    the horizontal and vertical grid independently.  ``font_size`` is an explicit
    glyph size in project pixels, with 0 meaning "fill the base cell".
    """
    cell = max(3, int(cell))
    cell_w = max(3, int(round(cell * (1.0 + float(char_spacing) / 100.0))))
    cell_h = max(3, int(round(cell * (1.0 + float(line_spacing) / 100.0))))
    size = int(font_size or 0)
    if size <= 0:
        size = max(6, int(round(cell * 0.98)))
    font_size = max(6, size)
    font, effective, installed = _font_details(font_family, font_size)
    glyphs, warnings = normalise_charset(preset, charset, font)
    if not installed:
        warnings.insert(0, f"The font {font_family!r} is not installed here; export and "
                           f"preview use {effective or 'the fallback font'} instead.")
    tiles = [_tile(glyph, font, cell_w, cell_h) for glyph in glyphs]
    coverage = [item[1] for item in tiles]
    order = list(range(len(glyphs)))
    if use_coverage:
        order.sort(key=lambda index: (coverage[index], glyphs[index].isspace()))
    max_coverage = max([coverage[i] for i in order] or [1.0])
    return {
        "cell": cell,
        "cell_w": cell_w,
        "cell_h": cell_h,
        "font_size": font_size,
        "font": effective,
        "selected_font": str(font_family or ""),
        "installed": installed,
        "glyphs": glyphs,
        "tiles": [tiles[index][0] for index in order],
        "coverage": [coverage[index] for index in order],
        "max_coverage": max(0.001, max_coverage),
        "warnings": warnings,
        "use_coverage": bool(use_coverage),
    }


def warnings_for(preset, charset, font_family, cell=10):
    plan = build_plan(preset, charset, font_family, cell)
    return tuple(plan["warnings"])


def render(layer, plan, settings):
    """Render ASCII cells over a BGRA layer, preserving the frame's alpha shape."""
    if layer is None or layer.ndim != 3 or layer.shape[2] < 4 or layer.size == 0:
        return layer
    height, width = layer.shape[:2]
    cell_w = max(3, int(plan.get("cell_w", plan["cell"])))
    cell_h = max(3, int(plan.get("cell_h", plan["cell"])))
    cols = max(1, int(np.ceil(width / float(cell_w))))
    rows = max(1, int(np.ceil(height / float(cell_h))))
    small = cv2.resize(layer[:, :, :3], (cols, rows), interpolation=cv2.INTER_AREA)
    alpha_small = cv2.resize(layer[:, :, 3], (cols, rows),
                             interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    contrast = max(0.0, float(settings.get("contrast", 100.0)) / 100.0)
    brightness = float(settings.get("brightness", 0.0)) * 2.55
    gray = np.clip((gray - 127.5) * contrast + 127.5 + brightness, 0.0, 255.0)
    density = max(0.05, float(settings.get("density", 100.0)) / 100.0)
    gamma = 1.0 / density
    target = np.power(np.clip(gray / 255.0, 0.0, 1.0), gamma)
    if bool(settings.get("invert_density", False)):
        target = 1.0 - target

    transparent = str(settings.get("background_mode") or "transparent") != "solid"
    if transparent:
        canvas = np.zeros((height, width, 4), dtype=np.float32)
    else:
        bg = np.array(_rgba(settings.get("background_color", "#000000")),
                      dtype=np.float32)
        canvas = np.empty((height, width, 4), dtype=np.float32)
        canvas[:, :, :3] = bg[:3]
        canvas[:, :, 3] = bg[3]
    foreground = np.array(_rgba(settings.get("foreground_color", "#FFFFFF")),
                          dtype=np.float32)
    mode = str(settings.get("color_mode") or "source")
    opacity = max(0.0, min(1.0, float(settings.get("opacity", 100.0)) / 100.0))
    glyphs = plan["tiles"]
    coverage = plan["coverage"]
    max_coverage = plan["max_coverage"]

    for row in range(rows):
        y0, y1 = row * cell_h, min(height, (row + 1) * cell_h)
        for col in range(cols):
            x0, x1 = col * cell_w, min(width, (col + 1) * cell_w)
            if x1 <= x0 or y1 <= y0:
                continue
            level = float(target[row, col])
            if plan["use_coverage"] and len(glyphs) > 1:
                wanted = level * max_coverage
                index = min(range(len(coverage)),
                            key=lambda i: abs(coverage[i] - wanted))
            else:
                index = min(len(glyphs) - 1,
                            max(0, int(round(level * (len(glyphs) - 1)))))
            region = canvas[y0:y1, x0:x1]
            # The last row/column can be a partial cell; crop the glyph tile to the
            # actual canvas window so a background blend never sees mismatched masks.
            mask = glyphs[index][:region.shape[0], :region.shape[1]]
            if not mask.any():
                continue
            if mode == "source":
                colour = small[row, col].astype(np.float32)
            else:
                colour = foreground[:3]
            a_glyph = mask * alpha_small[row, col] * opacity
            if transparent:
                # Colour is straight alpha.  A transparent background exists only where
                # the glyph has no coverage.
                positive = a_glyph > 0.0001
                region[..., :3] = np.where(positive[:, :, None], colour,
                                          region[..., :3])
                region[..., 3] = np.maximum(region[..., 3], a_glyph * 255.0)
            else:
                a_bg = region[..., 3] / 255.0
                out_a = a_glyph + a_bg * (1.0 - a_glyph)
                safe = np.maximum(out_a, 1e-6)
                for channel in range(3):
                    region[..., channel] = np.clip(
                        (colour[channel] * a_glyph
                         + region[..., channel] * a_bg * (1.0 - a_glyph)) / safe,
                        0.0, 255.0)
                region[..., 3] = np.clip(out_a * 255.0, 0.0, 255.0)
    return np.clip(canvas, 0.0, 255.0).astype(np.uint8)
