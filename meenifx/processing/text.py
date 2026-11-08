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


def _layout(spec, width, height, scale=1.0):
    """The shared text layout: font, wrapped lines, block metrics and origin.

    Both the renderer and :func:`bounds` use this, so the transform box can never
    disagree with the pixels Pillow is about to draw. Coordinates are device pixels,
    exactly like the renderer; `scale` is the preview/export render scale.
    """
    width, height = max(1, int(width)), max(1, int(height))
    scale = max(0.05, float(scale))
    text = str(spec.get("text", ""))
    reveal = int(spec.get("reveal", -1))
    if reveal >= 0:
        text = text[:reveal]
    if not text.strip():
        return None
    size = max(2.0, float(spec.get("size", 96.0)) * scale)
    font = font_for(spec.get("font"), spec.get("bold"), spec.get("italic"), size)
    tracking = float(spec.get("letter_spacing", 0.0)) * scale
    stroke = max(0.0, float(spec.get("stroke_width", 0.0)) * scale)
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(scratch, text, font, tracking, stroke,
                  float(spec.get("wrap", 0.0)) * scale)
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
    return {"text": text, "font": font, "tracking": tracking, "stroke": stroke,
            "lines": lines, "ascent": ascent, "descent": descent,
            "line_height": line_height, "gap": gap, "widths": widths,
            "block_w": block_w, "block_h": block_h, "pad": pad, "align": align,
            "offset_x": offset_x, "offset_y": offset_y, "left": left, "top": top}


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
    text = layout["text"]
    font = layout["font"]
    tracking = layout["tracking"]
    stroke = layout["stroke"]
    lines = layout["lines"]
    ascent, descent = layout["ascent"], layout["descent"]
    line_height, gap = layout["line_height"], layout["gap"]
    widths = layout["widths"]
    block_w, block_h = layout["block_w"], layout["block_h"]
    pad = layout["pad"]
    align = layout["align"]
    left, top = layout["left"], layout["top"]
    picture = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # Background plate first, so the shadow and the glyphs sit on top of it.
    alpha = spec.get("bg_opacity", 0.0)
    if alpha > 0.001:
        plate = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rectangle(
            [left - pad - view_x, top - pad - view_y,
             left + block_w + pad - view_x, top + block_h + pad - view_y],
            fill=_colour(spec.get("bg_color"), int(255 * float(alpha))))
        picture = Image.alpha_composite(picture, plate)

    def draw_text(target, fill, stroke_width, stroke_fill):
        painter = ImageDraw.Draw(target)
        for index, line in enumerate(lines):
            y = top + index * (line_height + gap) - view_y
            if align == "center":
                x = left + (block_w - widths[index]) / 2.0 - view_x
            elif align == "right":
                x = left + (block_w - widths[index]) - view_x
            else:
                x = left - view_x
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
        layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw_text(layer, _colour(spec.get("shadow_color"), int(255 * float(shadow_alpha))),
                  stroke, _colour(spec.get("shadow_color"), int(255 * float(shadow_alpha))))
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
    glyphs = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw_text(glyphs, _colour(spec.get("color"), fill_alpha), stroke,
              _colour(spec.get("stroke_color")))
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
    line_height, gap = layout["line_height"], layout["gap"]
    align, top = layout["align"], layout["top"]
    left, block_w, block_h, pad = layout["left"], layout["block_w"], layout["block_h"], layout["pad"]
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    boxes = []
    for index, line in enumerate(lines):
        y = top + index * (line_height + gap)
        if align == "center":
            x = left + (block_w - widths[index]) / 2.0
        elif align == "right":
            x = left + (block_w - widths[index])
        else:
            x = left
        if abs(tracking) < 0.01:
            boxes.append(scratch.textbbox((x, y), line, font=font, anchor="la",
                                          stroke_width=stroke_px))
        else:
            cursor = x
            for character in line:
                boxes.append(scratch.textbbox((cursor, y), character, font=font, anchor="la",
                                              stroke_width=stroke_px))
                cursor += scratch.textlength(character, font=font) + tracking
        if spec.get("underline"):
            thickness = max(1.0, line_height * 0.06)
            underline_y = y + ascent + descent * 0.35
            boxes.append((x, underline_y, x + widths[index], underline_y + thickness))
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
