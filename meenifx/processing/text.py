"""Draw a text clip into a BGRA layer with Pillow.

The same function serves the preview and the exporter: the caller passes the render size
and scale, and every project-space measurement is multiplied by `scale`, so a text layer
at 25 % preview quality lines up pixel-for-pixel with the full-resolution export.

Fonts are resolved through :mod:`meenifx.processing.fonts`, which scans the fonts the
operating system actually has installed (system-wide and per-user) instead of a fixed
list. The inspector reads the same registry, so the picker, the preview, thumbnails and
the export all agree on one face.
"""

from __future__ import annotations

import math
import re
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import fonts

_WORD_RE = re.compile(r"\S+|\s+")
# Combining marks and joiners that must never be separated from their base character.
_JOINERS = {"\u200d", "\u200c"}
_VARIATION_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)

# Family -> (regular, bold, italic, bold italic) used only as a first guess. The real
# resolution scans the platform font directories, so installed families that are not in
# this table (Angelic War, for example) work exactly the same as Arial.
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


def clear_font_cache():
    """Forget resolved Pillow fonts (a font was installed or the theme changed nothing)."""
    _FONT_CACHE.clear()


def font_for(family, bold=False, italic=False, size=48):
    """A Pillow font for a family; falls back only when the family is really absent."""
    size = max(1, int(round(size)))
    key = (str(family), bool(bold), bool(italic), size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    font = None
    for path, face_index in fonts.fallback_chain(family, bold, italic):
        try:
            font = ImageFont.truetype(path, size, index=face_index)
            break
        except OSError:
            continue
    if font is None:
        # Last resort: the old bare-name lookup (message files, fonts installed after the
        # scan, unusual font directories) and finally Pillow's built-in font.
        names = list(FONT_FILES.get(str(family), ()))
        if not names:
            names = [str(family).replace(" ", "") + ".ttf"]
        index = (2 if italic else 0) + (1 if bold else 0)
        while len(names) < 4:
            names.append(names[0])
        for name in [names[index]] + names + ["arial.ttf", "DejaVuSans.ttf"]:
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


# --------------------------------------------------------------------- grapheme runs
def _is_variation(codepoint):
    return any(start <= codepoint <= end for start, end in _VARIATION_RANGES)


def _is_extender(char):
    """True for a code point that belongs to the previous grapheme cluster."""
    codepoint = ord(char)
    if char in _JOINERS or _is_variation(codepoint):
        return True
    if codepoint in _SKIN_TONES:
        return True
    return unicodedata.category(char) in ("Mn", "Mc", "Me")


def graphemes(value):
    """Split text into user-perceived characters.

    A base character keeps its combining marks, variation selectors, skin-tone modifiers
    and zero-width-joiner sequences. Regional-indicator flag pairs stay together as well.
    This is the unit Split Text works on, so an emoji or a Devanagari syllable is never
    cut in half.
    """
    text = str(value or "")
    result = []
    join_next = False
    regional_run = 0
    for char in text:
        codepoint = ord(char)
        if not result:
            result.append(char)
            regional_run = 1 if codepoint in _REGIONAL else 0
            continue
        if join_next:
            result[-1] += char
            join_next = char in _JOINERS
            regional_run = 0
            continue
        if _is_extender(char):
            result[-1] += char
            join_next = char in _JOINERS
            continue
        if codepoint in _REGIONAL and regional_run == 1:
            result[-1] += char
            regional_run = 0
            continue
        result.append(char)
        regional_run = 1 if codepoint in _REGIONAL else 0
        join_next = False
    return result


def _take_graphemes(value, count):
    if count < 0:
        return value
    return "".join(graphemes(value)[:count])


def _apply_case(value, case):
    text = str(value or "")
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return text.title()
    return text


def _display_text(spec):
    return _apply_case(str(spec.get("text", "")), spec.get("case") or "none")


def needs_shaping(value):
    """True when splitting characters would break complex shaping or bidi text."""
    text = str(value or "")
    if any(char in _JOINERS for char in text):
        return True
    for char in text:
        codepoint = ord(char)
        if (0x0590 <= codepoint <= 0x0FFF or 0x10A0 <= codepoint <= 0x10FF
                or 0x1200 <= codepoint <= 0x1CFF or 0x2D00 <= codepoint <= 0x2DFF
                or 0xA500 <= codepoint <= 0xA7FF or 0xFB1D <= codepoint <= 0xFDFF
                or 0xFE70 <= codepoint <= 0xFEFF):
            return True
    return unicodedata.bidirectional(text[:1]) in ("R", "AL", "AN")


def _line_width(draw, line, font, tracking, stroke, word_spacing=0.0):
    if not line:
        return 0.0
    if abs(tracking) < 0.01 and abs(word_spacing) < 0.01:
        return draw.textlength(line, font=font) + stroke
    total = sum(draw.textlength(cluster, font=font) for cluster in graphemes(line))
    total += tracking * max(0, len(graphemes(line)) - 1)
    total += word_spacing * line.count(" ")
    return total + stroke


def _wrap(draw, text, font, tracking, stroke, limit, word_spacing=0.0):
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
            if _line_width(draw, candidate, font, tracking, stroke, word_spacing) <= limit or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _justify_extra(line, natural_width, block_width, enabled):
    spaces = line.count(" ")
    if not enabled or spaces <= 0:
        return 0.0
    return max(0.0, float(block_width) - float(natural_width)) / spaces


def _chunks(draw, line, font, tracking, word_spacing, extra=0.0):
    """[(dx, text)] that draws the line with tracking, word spacing and justification.

    With no tracking the line is drawn word by word, so kerning inside each word is kept;
    spaces only receive the extra advances. Tracking switches to per-grapheme drawing,
    exactly like the old renderer.
    """
    if abs(tracking) < 0.01 and abs(word_spacing) < 0.01 and abs(extra) < 0.01:
        return [(0.0, line)]
    chunks, cursor = [], 0.0
    if abs(tracking) >= 0.01:
        for cluster in graphemes(line):
            chunks.append((cursor, cluster))
            cursor += draw.textlength(cluster, font=font) + tracking
            if cluster.isspace():
                cursor += word_spacing + extra
        return chunks
    for token in _WORD_RE.findall(line):
        chunks.append((cursor, token))
        cursor += draw.textlength(token, font=font)
        if token.isspace():
            cursor += word_spacing + extra
    return chunks


def _cluster_positions(draw, line, font, tracking, word_spacing, extra=0.0):
    """[(cluster, dx, advance)] with prefix advances, so kerning stays in the layout."""
    result, cursor = [], 0.0
    if abs(tracking) < 0.01:
        # Prefix measurement includes the kerning between neighbouring characters.
        prefix = 0.0
        consumed = 0
        for cluster in graphemes(line):
            consumed += len(cluster)
            next_prefix = draw.textlength(line[:consumed], font=font) + extra * line[:consumed].count(" ")
            advance = next_prefix - prefix
            result.append((cluster, cursor, advance))
            cursor += advance
            prefix = next_prefix
            if cluster.isspace():
                cursor += word_spacing
        return result
    for cluster in graphemes(line):
        advance = draw.textlength(cluster, font=font) + tracking
        result.append((cluster, cursor, advance))
        cursor += advance
        if cluster.isspace():
            cursor += word_spacing + extra
    return result


def _layout(spec, width, height, scale=1.0):
    """The shared text layout: font, wrapped lines, block metrics and origin.

    Both the renderer and :func:`bounds` use this, so the transform box can never
    disagree with the pixels Pillow is about to draw. Coordinates are device pixels,
    exactly like the renderer; `scale` is the preview/export render scale.
    """
    width, height = max(1, int(width)), max(1, int(height))
    scale = max(0.05, float(scale))
    text = _display_text(spec)
    reveal = int(spec.get("reveal", -1))
    if reveal >= 0:
        text = _take_graphemes(text, reveal)
    if not text.strip():
        return None
    size = max(2.0, float(spec.get("size", 96.0)) * scale)
    font = font_for(spec.get("font"), spec.get("bold"), spec.get("italic"), size)
    tracking = float(spec.get("letter_spacing", 0.0)) * scale
    word_spacing = float(spec.get("word_spacing", 0.0)) * scale
    stroke = max(0.0, float(spec.get("stroke_width", 0.0)) * scale)
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(scratch, text, font, tracking, stroke,
                  float(spec.get("wrap", 0.0)) * scale, word_spacing)
    if not lines:
        return None
    ascent, descent = font.getmetrics()
    line_height = float(ascent + descent)
    gap = line_height * (max(0.5, float(spec.get("line_spacing", 1.15))) - 1.0)
    widths = [_line_width(scratch, line, font, tracking, stroke, word_spacing) for line in lines]
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
    anchor = spec.get("anchor", "center")
    if anchor == "top":
        top = offset_y
    elif anchor == "bottom":
        top = height - block_h + offset_y
    else:
        top = (height - block_h) / 2.0 + offset_y
    return {"text": text, "font": font, "tracking": tracking, "word_spacing": word_spacing,
            "stroke": stroke, "lines": lines, "size": size, "ascent": ascent, "descent": descent,
            "line_height": line_height, "gap": gap, "widths": widths,
            "block_w": block_w, "block_h": block_h, "pad": pad, "align": align,
            "anchor": anchor, "offset_x": offset_x, "offset_y": offset_y,
            "left": left, "top": top}


def _line_origin(layout, index):
    """(x, y) of one line's left edge, with justification applied."""
    widths, block_w = layout["widths"], layout["block_w"]
    line = layout["lines"][index]
    # The last line of a justified paragraph keeps its natural width, like a word processor.
    justified = layout["align"] == "justify" and index < len(layout["lines"]) - 1
    extra = _justify_extra(line, widths[index], block_w, justified)
    if layout["align"] == "center":
        x = layout["left"] + (block_w - widths[index]) / 2.0
    elif layout["align"] == "right":
        x = layout["left"] + (block_w - widths[index])
    else:
        x = layout["left"]
    y = layout["top"] + index * (layout["line_height"] + layout["gap"])
    return x, y, extra


def _view(width, height, crop):
    """(x, y, w, h) of the part of a render we actually need."""
    if not crop:
        return 0, 0, width, height
    try:
        x, y, w, h = (int(round(float(value))) for value in crop)
    except (TypeError, ValueError):
        return 0, 0, width, height
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    return x, y, max(1, min(width - x, w)), max(1, min(height - y, h))


def _gradient_image(box, canvas, colour_a, colour_b, angle, alpha):
    """A two-stop linear gradient cropped to `box`, in canvas-wide coordinates."""
    x0, y0, x1, y1 = (int(value) for value in box)
    x1, y1 = max(x0 + 1, x1), max(y0 + 1, y1)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    radians = math.radians(float(angle))
    projection = xs * math.cos(radians) + ys * math.sin(radians)
    corners = []
    for px in (0, canvas[0]):
        for py in (0, canvas[1]):
            corners.append(px * math.cos(radians) + py * math.sin(radians))
    low, high = min(corners), max(corners)
    span = max(1e-6, high - low)
    fraction = np.clip((projection - low) / span, 0.0, 1.0)[:, :, None]
    first = np.array(colour_a[:3], dtype=np.float32)
    second = np.array(colour_b[:3], dtype=np.float32)
    rgb = first + (second - first) * fraction
    rgba = np.dstack([rgb, np.full(fraction.shape[:2], int(alpha), dtype=np.float32)])
    return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8), "RGBA")


def render(spec, width, height, scale=1.0, poses=None, crop=None):
    """A BGRA uint8 layer with the text drawn on transparency.

    `poses` is a preset layer's per-unit animation (``{"letter": [...], "word": [...],
    "line": [...]}`` of pose objects, see :mod:`meenifx.processing.text_units`). Without
    it the static path below runs exactly as before, which is what keeps a clip at rest
    pixel-identical to what it renders with no presets at all.

    `crop` renders only an (x, y, w, h) window of the full canvas. The layout is still
    computed against the full frame, so a detail render at 10x for a transformed text
    clip allocates only the window that can reach the output, never a 10x frame.
    """
    width, height = max(1, int(width)), max(1, int(height))
    view_x, view_y, canvas_w, canvas_h = _view(width, height, crop)
    if poses:
        # Only a pose that actually moves something takes the animated path: the last
        # frame of a fade evaluates to the identity pose, and a clip at rest has to draw
        # through the static (kerning-exact) code below.
        moving = any(not pose.is_identity()
                     for name in ("letter", "word", "line")
                     for pose in (poses.get(name) or ()))
        if moving:
            from . import text_units
            animated = text_units.render(spec, width, height, scale, poses, crop=crop)
            if animated is not None:
                return animated
    layout = _layout(spec, width, height, scale)
    if layout is None:
        return None
    stroke = layout["stroke"]
    widths = layout["widths"]
    ascent, descent = layout["ascent"], layout["descent"]
    line_height = layout["line_height"]
    block_w, block_h = layout["block_w"], layout["block_h"]
    pad = layout["pad"]
    align = layout["align"]
    left, top = layout["left"], layout["top"]
    picture = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # Background plate first, so the shadow and the glyphs sit on top of it.
    alpha = spec.get("bg_opacity", 0.0)
    if alpha > 0.001:
        plate = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        rect = [left - pad - view_x, top - pad - view_y,
                left + block_w + pad - view_x, top + block_h + pad - view_y]
        radius = max(0.0, float(spec.get("bg_radius", 0.0)) * scale)
        fill = _colour(spec.get("bg_color"), int(255 * float(alpha)))
        painter = ImageDraw.Draw(plate)
        if radius > 0.5:
            painter.rounded_rectangle(rect, radius=radius, fill=fill)
        else:
            painter.rectangle(rect, fill=fill)
        picture = Image.alpha_composite(picture, plate)

    def paint(painter, fill, stroke_width, stroke_fill, mask=False):
        for index, line in enumerate(layout["lines"]):
            x, y, extra = _line_origin(layout, index)
            x -= view_x
            y -= view_y
            for dx, chunk in _chunks(painter, line, layout["font"], layout["tracking"],
                                     layout["word_spacing"], extra):
                painter.text((x + dx, y), chunk, font=layout["font"], fill=fill, anchor="la",
                             stroke_width=int(round(stroke_width)),
                             stroke_fill=stroke_fill if stroke_width > 0 else None)
            drawn_width = widths[index] + extra * max(0, line.count(" "))
            if spec.get("underline"):
                thickness = max(1.0, line_height * 0.06)
                underline_y = y + ascent + descent * 0.35
                painter.rectangle([x, underline_y, x + drawn_width, underline_y + thickness],
                                  fill=fill)
            if spec.get("strikethrough"):
                thickness = max(1.0, line_height * 0.055)
                strike_y = y + ascent * 0.62
                painter.rectangle([x, strike_y, x + drawn_width, strike_y + thickness], fill=fill)

    def draw_text(target, fill, stroke_width, stroke_fill):
        paint(ImageDraw.Draw(target), fill, stroke_width, stroke_fill)

    shadow_alpha = spec.get("shadow_opacity", 0.0)
    dx = float(spec.get("shadow_offset_x", 0.0)) * scale
    dy = float(spec.get("shadow_offset_y", 0.0)) * scale
    blur = float(spec.get("shadow_blur", 0.0)) * scale
    if shadow_alpha > 0.001 and (abs(dx) > 0.01 or abs(dy) > 0.01 or blur > 0.01):
        layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        shadow_colour = _colour(spec.get("shadow_color"), int(255 * float(shadow_alpha)))
        draw_text(layer, shadow_colour, stroke, shadow_colour)
        # The shadow is a thin band of a title frame; blurring the whole 1080p canvas was
        # the most expensive thing in a text render, so the blur runs on its own bounds.
        box = layer.getbbox()
        if box is not None:
            shadow_pad = int(round(blur)) + 2
            bx0, by0 = max(0, box[0] - shadow_pad), max(0, box[1] - shadow_pad)
            bx1, by1 = min(canvas_w, box[2] + shadow_pad), min(canvas_h, box[3] + shadow_pad)
            small = layer.crop((bx0, by0, bx1, by1))
            if blur > 0.01:
                small = small.filter(ImageFilter.GaussianBlur(blur / 2.0))
            if abs(dx) > 0.01 or abs(dy) > 0.01:
                shifted = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
                shifted.paste(small, (int(round(bx0 + dx)), int(round(by0 + dy))))
                small = shifted
            picture.paste(small, (0, 0) if small.size == picture.size else (bx0, by0), small)

    # Fill opacity multiplies the glyph fill only: the stroke, the shadow and the
    # background plate keep their own, separately labelled opacity values.
    fill_alpha = int(round(255 * max(0.0, min(1.0, float(spec.get("fill_opacity", 1.0))))))
    stroke_alpha = int(round(255 * max(0.0, min(1.0, float(spec.get("stroke_opacity", 1.0))))))
    stroke_colour = _colour(spec.get("stroke_color"), stroke_alpha)
    gradient = bool(spec.get("gradient"))
    glyphs = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    if gradient:
        # The outline is drawn first as a solid silhouette, then the gradient is applied
        # through the fill-only mask so the stroke keeps its own colour.
        if stroke > 0.01 and stroke_alpha > 0:
            draw_text(glyphs, stroke_colour, stroke, stroke_colour)
        mask = Image.new("L", (canvas_w, canvas_h), 0)
        paint(ImageDraw.Draw(mask), 255, 0, None, mask=True)
        box = mask.getbbox()
        if box is not None:
            gradient_layer = _gradient_image(
                box, (canvas_w, canvas_h), _colour(spec.get("color")),
                _colour(spec.get("gradient_color") or spec.get("color")),
                float(spec.get("gradient_angle", 90.0)), fill_alpha)
            gradient_layer.putalpha(Image.eval(mask.crop(box),
                                               lambda value: int(value * fill_alpha / 255)))
            glyphs.alpha_composite(gradient_layer, dest=(box[0], box[1]))
    else:
        draw_text(glyphs, _colour(spec.get("color"), fill_alpha), stroke, stroke_colour)
    picture = Image.alpha_composite(picture, glyphs)
    blur = float(spec.get("blur", 0.0)) * scale
    if blur > 0.01:
        picture = picture.filter(ImageFilter.GaussianBlur(blur / 2.0))
    # Only the drawn band needs converting: a full-canvas BGRA copy was three passes over
    # 8 MB for sixty characters.
    frame = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)
    box = picture.getbbox()
    if box is not None:
        rgba = np.asarray(picture.crop(box), dtype=np.uint8)
        frame[box[1]:box[3], box[0]:box[2]] = rgba[:, :, [2, 1, 0, 3]]
    return frame


def bounds(spec, width, height, scale=1.0):
    """Visible text bounds in layer pixels: (x, y, width, height).

    This uses the same layout as :func:`render` and Pillow's glyph boxes, so handles
    follow the words rather than the full generated frame. The background plate and
    blur are included when they are visible. The drop shadow deliberately is not: it
    is asymmetric, and letting it define the box would pull the transform centre away
    from the words. There is no separate paragraph box in the model - ``wrap`` only
    changes the line layout - so this is both the visible and editable extent of a
    text layer.
    """
    width, height = max(1, int(width)), max(1, int(height))
    layout = _layout(spec, width, height, scale)
    if layout is None:
        return None
    font = layout["font"]
    tracking, stroke = layout["tracking"], layout["stroke"]
    stroke_px = int(round(stroke))
    lines, widths = layout["lines"], layout["widths"]
    ascent, descent = layout["ascent"], layout["descent"]
    line_height = layout["line_height"]
    left, top = layout["left"], layout["top"]
    block_w, block_h, pad = layout["block_w"], layout["block_h"], layout["pad"]
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    boxes = []
    for index, line in enumerate(lines):
        x, y, extra = _line_origin(layout, index)
        for cluster, dx, advance in _cluster_positions(scratch, line, font, tracking,
                                                        layout["word_spacing"], extra):
            if cluster.strip():
                boxes.append(scratch.textbbox((x + dx, y), cluster, font=font, anchor="la",
                                              stroke_width=stroke_px))
        drawn_width = widths[index] + extra * max(0, line.count(" "))
        if spec.get("underline"):
            thickness = max(1.0, line_height * 0.06)
            underline_y = y + ascent + descent * 0.35
            boxes.append((x, underline_y, x + drawn_width, underline_y + thickness))
        if spec.get("strikethrough"):
            thickness = max(1.0, line_height * 0.055)
            strike_y = y + ascent * 0.62
            boxes.append((x, strike_y, x + drawn_width, strike_y + thickness))
    if not boxes:
        boxes.append((left, top, left + max(1.0, block_w), top + max(1.0, block_h)))
    x0 = min(box[0] for box in boxes)
    y0 = min(box[1] for box in boxes)
    x1 = max(box[2] for box in boxes)
    y1 = max(box[3] for box in boxes)
    if float(spec.get("bg_opacity", 0.0)) > 0.001:
        x0, y0 = min(x0, left - pad), min(y0, top - pad)
        x1, y1 = max(x1, left + block_w + pad), max(y1, top + block_h + pad)
    text_blur = max(0.0, float(spec.get("blur", 0.0))) * max(0.05, float(scale))
    if text_blur > 0.01:
        x0, y0, x1, y1 = x0 - text_blur, y0 - text_blur, x1 + text_blur, y1 + text_blur
    return x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0)
