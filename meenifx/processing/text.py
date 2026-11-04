"""Draw a text clip into a BGRA layer with Pillow.

The same function serves the preview and the exporter: the caller passes the render size
and scale, and every project-space measurement is multiplied by `scale`, so a text layer
at 25 % preview quality lines up pixel-for-pixel with the full-resolution export.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# family -> (regular, bold, italic, bold italic). Pillow resolves bare file names in the
# platform font directory, so these work on Windows and degrade to the default font on
# machines without them.
FONT_FILES = {
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    "Arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "Calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "Tahoma": ("tahoma.ttf", "tahomabd.ttf", "tahoma.ttf", "tahomabd.ttf"),
    "Trebuchet MS": ("trebuc.ttf", "trebucbd.ttf", "trebucit.ttf", "trebucbi.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "Times New Roman": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "Cambria": ("cambria.ttc", "cambriab.ttf", "cambriai.ttf", "cambriaz.ttf"),
    "Courier New": ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
    "Consolas": ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"),
    "Impact": ("impact.ttf", "impact.ttf", "impact.ttf", "impact.ttf"),
    "Comic Sans MS": ("comic.ttf", "comicbd.ttf", "comici.ttf", "comicz.ttf"),
    "DejaVu Sans": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf",
                    "DejaVuSans-BoldOblique.ttf"),
    "DejaVu Serif": ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif-Italic.ttf",
                     "DejaVuSerif-BoldItalic.ttf"),
    "Liberation Sans": ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf",
                        "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"),
}

_FONT_CACHE = {}


def font_for(family, bold=False, italic=False, size=48):
    """A Pillow font for a family; falls back to the built-in bitmap font."""
    size = max(1, int(round(size)))
    key = (str(family), bool(bold), bool(italic), size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    index = (2 if italic else 0) + (1 if bold else 0)
    names = list(FONT_FILES.get(str(family), ()))
    if not names:
        names = [str(family).replace(" ", "") + ".ttf"]
    while len(names) < 4:
        names.append(names[0])
    order = [names[index]] + names + ["arial.ttf", "DejaVuSans.ttf"]
    font = None
    for name in order:
        try:
            font = ImageFont.truetype(name, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(size)
    _FONT_CACHE[key] = font
    return font


def _colour(value, alpha=255):
    """(r, g, b, a) from a "#rrggbb" (or "#aarrggbb"/name) string."""
    text = str(value or "#000000").strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 8:
            alpha = int(digits[:2], 16)
            digits = digits[2:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        try:
            return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), int(alpha))
        except ValueError:
            pass
    from PIL import ImageColor
    try:
        rgb = ImageColor.getrgb(text)
    except ValueError:
        rgb = (0, 0, 0)
    return (rgb[0], rgb[1], rgb[2], int(alpha))


def _line_width(draw, line, font, tracking, stroke):
    if not line:
        return 0.0
    if abs(tracking) < 0.01:
        return draw.textlength(line, font=font) + stroke
    return sum(draw.textlength(ch, font=font) for ch in line) + tracking * (len(line) - 1) + stroke


def _wrap(draw, text, font, tracking, stroke, limit):
    """Greedy word wrap; `limit` is the widest line in device pixels (0 disables)."""
    if limit <= 0:
        return text.split("\n")
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        words, current = paragraph.split(" "), ""
        for word in words:
            candidate = word if not current else current + " " + word
            if _line_width(draw, candidate, font, tracking, stroke) <= limit or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def render(spec, width, height, scale=1.0):
    """A (height, width, 4) BGRA uint8 layer with the text drawn on transparency."""
    width, height = max(1, int(width)), max(1, int(height))
    scale = max(0.05, float(scale))
    picture = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    text = str(spec.get("text", ""))
    # Per-character Typewriter: the reveal channel counts glyphs, -1 means "all of them".
    # Truncating here (not in the preset) keeps the animation a normal keyframe and makes
    # the preview and the export draw the exact same characters.
    reveal = int(spec.get("reveal", -1))
    if reveal >= 0:
        text = text[:reveal]
    if not text.strip():
        return None
    size = max(2.0, float(spec.get("size", 96.0)) * scale)
    font = font_for(spec.get("font"), spec.get("bold"), spec.get("italic"), size)
    tracking = float(spec.get("letter_spacing", 0.0)) * scale
    stroke = max(0.0, float(spec.get("stroke_width", 0.0)) * scale)
    scratch = ImageDraw.Draw(picture)
    lines = _wrap(scratch, text, font, tracking, stroke, float(spec.get("wrap", 0.0)) * scale)
    if not lines:
        return None
    ascent, descent = font.getmetrics()
    line_height = float(ascent + descent)
    gap = line_height * (max(0.5, float(spec.get("line_spacing", 1.15))) - 1.0)
    widths = [_line_width(scratch, line, font, tracking, stroke) for line in lines]
    block_w = max(widths) if widths else 0.0
    block_h = line_height * len(lines) + gap * (len(lines) - 1)
    pad = max(0.0, float(spec.get("bg_padding", 0.0)) * scale)
    align = spec.get("align", "center")
    offset_x = float(spec.get("offset_x", 0.0)) * scale
    offset_y = float(spec.get("offset_y", 0.0)) * scale
    left = (width - block_w) / 2.0 + offset_x
    if align == "left":
        left = offset_x
    elif align == "right":
        left = width - block_w + offset_x
    top = (height - block_h) / 2.0 + offset_y

    # Background plate first, so the shadow and the glyphs sit on top of it.
    alpha = spec.get("bg_opacity", 0.0)
    if alpha > 0.001:
        plate = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rectangle(
            [left - pad, top - pad, left + block_w + pad, top + block_h + pad],
            fill=_colour(spec.get("bg_color"), int(255 * float(alpha))))
        picture = Image.alpha_composite(picture, plate)

    def draw_text(target, fill, stroke_width, stroke_fill):
        painter = ImageDraw.Draw(target)
        for index, line in enumerate(lines):
            y = top + index * (line_height + gap)
            if align == "center":
                x = left + (block_w - widths[index]) / 2.0
            elif align == "right":
                x = left + (block_w - widths[index])
            else:
                x = left
            if abs(tracking) < 0.01:
                painter.text((x, y), line, font=font, fill=fill, anchor="la",
                             stroke_width=int(round(stroke_width)),
                             stroke_fill=stroke_fill if stroke_width > 0 else None)
            else:
                cursor = x
                for ch in line:
                    painter.text((cursor, y), ch, font=font, fill=fill, anchor="la",
                                 stroke_width=int(round(stroke_width)),
                                 stroke_fill=stroke_fill if stroke_width > 0 else None)
                    cursor += painter.textlength(ch, font=font) + tracking
            if spec.get("underline"):
                thickness = max(1.0, line_height * 0.06)
                underline_y = y + ascent + descent * 0.35
                painter.rectangle([x, underline_y, x + widths[index], underline_y + thickness],
                                  fill=fill)

    shadow_alpha = spec.get("shadow_opacity", 0.0)
    dx = float(spec.get("shadow_offset_x", 0.0)) * scale
    dy = float(spec.get("shadow_offset_y", 0.0)) * scale
    blur = float(spec.get("shadow_blur", 0.0)) * scale
    if shadow_alpha > 0.001 and (abs(dx) > 0.01 or abs(dy) > 0.01 or blur > 0.01):
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_text(layer, _colour(spec.get("shadow_color"), int(255 * float(shadow_alpha))),
                  stroke, _colour(spec.get("shadow_color"), int(255 * float(shadow_alpha))))
        if blur > 0.01:
            layer = layer.filter(ImageFilter.GaussianBlur(blur / 2.0))
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            shifted = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            shifted.paste(layer, (int(round(dx)), int(round(dy))))
            layer = shifted
        picture = Image.alpha_composite(picture, layer)

    # Fill opacity multiplies the glyph fill only: the stroke, the shadow and the
    # background plate keep their own, separately labelled opacity values.
    fill_alpha = int(round(255 * max(0.0, min(1.0, float(spec.get("fill_opacity", 1.0))))))
    glyphs = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_text(glyphs, _colour(spec.get("color"), fill_alpha), stroke,
              _colour(spec.get("stroke_color")))
    picture = Image.alpha_composite(picture, glyphs)
    blur = float(spec.get("blur", 0.0)) * scale
    if blur > 0.01:
        picture = picture.filter(ImageFilter.GaussianBlur(blur / 2.0))
    array = np.asarray(picture, dtype=np.uint8)
    return np.ascontiguousarray(array[:, :, [2, 1, 0, 3]])
