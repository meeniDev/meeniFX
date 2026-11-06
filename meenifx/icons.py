"""Small vector icons drawn with QPainter, so every icon shares one stroke, size and palette."""

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from .tokens import COLORS

_CACHE = {}
# One pixmap per display scale Qt may ask for. Every one is drawn natively at that size
# (the 16-unit grid is scaled, not the bitmap), so icons stay crisp at 100/125/150/200 %
# and Qt never has to upscale a small bitmap for a high-DPI screen.
_ICON_SCALES = (1.0, 1.5, 2.0, 3.0)


def _render(name, color, size, scale):
    """Draw `name` at `size` logical pixels for a display scale of `scale`."""
    pixels = max(1, int(round(size * scale)))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    # Paint in device pixels, then declare the scale: that keeps the geometry integer and
    # lets Qt pick this pixmap for the matching screen without any reinterpretation.
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(pixels / 16.0, pixels / 16.0)
    _draw(name, painter, QColor(color))
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def _pts(*values):
    return QPolygonF([QPointF(values[i], values[i + 1]) for i in range(0, len(values), 2)])


def _draw(name, p, color):
    """Paint `name` on a 16x16 grid (the painter is already scaled)."""
    pen = QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    fill = color
    if name == "play":
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen); p.drawPolygon(_pts(4.5, 2.5, 13, 8, 4.5, 13.5))
    elif name == "pause":
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(3.8, 2.8, 3, 10.4), 1, 1); p.drawRoundedRect(QRectF(9.2, 2.8, 3, 10.4), 1, 1)
    elif name in ("step_back", "step_forward"):
        s = -1 if name == "step_back" else 1
        cx = 8
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(_pts(cx + s * 4.5, 3, cx - s * 3, 8, cx + s * 4.5, 13))
        p.drawRect(QRectF(cx - s * 5 - (0 if s > 0 else 1.6), 3, 1.6, 10))
    elif name in ("skip_start", "skip_end"):
        s = -1 if name == "skip_start" else 1
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(_pts(8 - s * 1.5, 3.5, 8 + s * 4.5, 8, 8 - s * 1.5, 12.5))
        p.drawPolygon(_pts(8 - s * 6.5, 3.5, 8 - s * 0.5, 8, 8 - s * 6.5, 12.5))
    elif name == "undo" or name == "redo":
        if name == "redo":
            p.translate(16, 0); p.scale(-1, 1)
        path = QPainterPath(QPointF(4, 6.5)); path.cubicTo(7, 3.5, 13, 4.5, 13, 9.5)
        path.cubicTo(13, 12, 11.5, 12.5, 10, 12.5)
        p.drawPath(path); p.drawPolyline(_pts(3.6, 2.5, 3.6, 6.8, 7.8, 6.8))
    elif name == "camera":
        p.drawRoundedRect(QRectF(2, 4.5, 12, 8.5), 2, 2); p.drawEllipse(QPointF(8, 8.8), 2.4, 2.4)
        p.drawLine(QPointF(5.5, 4.5), QPointF(6.5, 2.8)); p.drawLine(QPointF(6.5, 2.8), QPointF(9.5, 2.8))
        p.drawLine(QPointF(9.5, 2.8), QPointF(10.5, 4.5))
    elif name == "guides":
        p.drawRoundedRect(QRectF(2, 2, 12, 12), 1.5, 1.5)
        p.drawLine(QPointF(6, 2), QPointF(6, 14)); p.drawLine(QPointF(10, 2), QPointF(10, 14))
        p.drawLine(QPointF(2, 6), QPointF(14, 6)); p.drawLine(QPointF(2, 10), QPointF(14, 10))
    elif name == "select":
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(_pts(4, 2, 4, 12.5, 6.8, 10, 8.6, 14, 10.4, 13.2, 8.6, 9.2, 12, 9.2))
    elif name == "razor":
        p.drawLine(QPointF(8, 2), QPointF(8, 14)); p.setBrush(fill)
        p.drawPolygon(_pts(3, 4, 6.2, 8, 3, 12)); p.drawPolygon(_pts(13, 4, 9.8, 8, 13, 12))
    elif name == "split":
        p.drawLine(QPointF(8, 1.8), QPointF(8, 14.2))
        p.drawRoundedRect(QRectF(2, 4.5, 4.5, 7), 1, 1); p.drawRoundedRect(QRectF(9.5, 4.5, 4.5, 7), 1, 1)
    elif name == "snap":
        path = QPainterPath(QPointF(3.5, 3)); path.lineTo(3.5, 8)
        path.arcTo(QRectF(3.5, 4, 9, 9), 180, 180); path.lineTo(12.5, 3)
        p.drawPath(path); p.drawLine(QPointF(3.5, 5.5), QPointF(6.5, 5.5)); p.drawLine(QPointF(9.5, 5.5), QPointF(12.5, 5.5))
    elif name == "loop":
        p.drawArc(QRectF(2.5, 4, 11, 8), 30 * 16, 300 * 16)
        p.setBrush(fill); p.drawPolygon(_pts(10.5, 2.2, 13.5, 4.6, 10.2, 6.4))
    elif name == "add_track" or name == "plus":
        p.drawLine(QPointF(8, 3), QPointF(8, 13)); p.drawLine(QPointF(3, 8), QPointF(13, 8))
    elif name == "gear":
        p.drawEllipse(QPointF(8, 8), 2.2, 2.2)
        for i in range(8):
            p.save(); p.translate(8, 8); p.rotate(i * 45)
            p.drawLine(QPointF(0, -4.6), QPointF(0, -6.4)); p.restore()
        p.drawEllipse(QPointF(8, 8), 4.6, 4.6)
    elif name == "fit":
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            cx, cy = (2.5 if sx > 0 else 13.5), (2.5 if sy > 0 else 13.5)
            p.drawPolyline(_pts(cx, cy + sy * 3.5, cx, cy, cx + sx * 3.5, cy))
    elif name in ("eye", "eye_off"):
        path = QPainterPath(QPointF(1.5, 8)); path.quadTo(8, 1.5, 14.5, 8); path.quadTo(8, 14.5, 1.5, 8)
        p.drawPath(path); p.drawEllipse(QPointF(8, 8), 2, 2)
        if name == "eye_off":
            p.drawLine(QPointF(3, 13), QPointF(13, 3))
    elif name in ("lock", "unlock"):
        p.drawRoundedRect(QRectF(3.5, 7, 9, 6.5), 1.5, 1.5)
        if name == "lock":
            p.drawArc(QRectF(5, 2.5, 6, 7), 0, 180 * 16)
        else:
            p.drawArc(QRectF(5, 2.5, 6, 7), 0, 180 * 16); p.drawLine(QPointF(5, 6), QPointF(5, 7))
    elif name in ("volume", "mute"):
        p.setBrush(fill); p.drawPolygon(_pts(2.5, 6, 5.5, 6, 9, 3, 9, 13, 5.5, 10, 2.5, 10)); p.setBrush(Qt.BrushStyle.NoBrush)
        if name == "volume":
            p.drawArc(QRectF(7, 5, 5, 6), -60 * 16, 120 * 16); p.drawArc(QRectF(6, 3, 8, 10), -55 * 16, 110 * 16)
        else:
            p.drawLine(QPointF(11, 6), QPointF(14.5, 10)); p.drawLine(QPointF(14.5, 6), QPointF(11, 10))
    elif name == "solo":
        p.drawArc(QRectF(3, 2.5, 10, 10), 0, 180 * 16)
        p.drawRoundedRect(QRectF(2.5, 8, 3, 5), 1, 1); p.drawRoundedRect(QRectF(10.5, 8, 3, 5), 1, 1)
    elif name == "check":
        p.drawPolyline(_pts(3, 8.5, 6.5, 12, 13, 4.5))
    elif name == "chevron_right":
        p.drawPolyline(_pts(6, 3.5, 10.5, 8, 6, 12.5))
    elif name == "chevron_down":
        p.drawPolyline(_pts(3.5, 6, 8, 10.5, 12.5, 6))
    elif name == "dots":
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        for x in (3.5, 8, 12.5):
            p.drawEllipse(QPointF(x, 8), 1.4, 1.4)
    elif name == "reset":
        p.drawArc(QRectF(3, 3, 10, 10), 60 * 16, 270 * 16)
        p.setBrush(fill); p.drawPolygon(_pts(2.2, 2.5, 7, 3.2, 3.5, 6.8))
    elif name == "stopwatch":
        p.drawEllipse(QPointF(8, 9), 4.6, 4.6); p.drawLine(QPointF(8, 9), QPointF(8, 6.2))
        p.drawLine(QPointF(6.5, 2.5), QPointF(9.5, 2.5))
    elif name in ("key", "key_filled"):
        if name == "key_filled":
            p.setBrush(fill)
        p.drawPolygon(_pts(8, 2.5, 13.5, 8, 8, 13.5, 2.5, 8))
    elif name == "warning":
        p.drawPolygon(_pts(8, 2.5, 14, 13, 2, 13)); p.drawLine(QPointF(8, 6.5), QPointF(8, 9.5))
        p.drawPoint(QPointF(8, 11.4))
    elif name == "search":
        p.drawEllipse(QPointF(7, 7), 4, 4); p.drawLine(QPointF(10, 10), QPointF(13.5, 13.5))
    elif name == "record":
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(QPointF(8, 8), 5, 5)
    elif name == "stop":
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen); p.drawRoundedRect(QRectF(3.5, 3.5, 9, 9), 1.5, 1.5)
    elif name == "mic":
        p.drawRoundedRect(QRectF(5.8, 1.8, 4.4, 8, ), 2.2, 2.2)
        p.drawArc(QRectF(3.5, 5, 9, 7), 180 * 16, 180 * 16)
        p.drawLine(QPointF(8, 12), QPointF(8, 14.5)); p.drawLine(QPointF(5.5, 14.5), QPointF(10.5, 14.5))
    elif name == "screen":
        p.drawRoundedRect(QRectF(1.8, 2.5, 12.4, 8.5), 1.5, 1.5)
        p.drawLine(QPointF(8, 11), QPointF(8, 13.5)); p.drawLine(QPointF(5, 13.5), QPointF(11, 13.5))
    elif name == "refresh":
        p.drawArc(QRectF(3, 3, 10, 10), 40 * 16, 290 * 16)
        p.setBrush(fill); p.drawPolygon(_pts(10.2, 1.8, 13.6, 4.6, 9.6, 5.8))
    elif name in ("link", "link_off"):
        # Two interlocking pills, tilted like a chain link.
        p.save()
        p.translate(8, 8)
        p.rotate(-40)
        p.drawRoundedRect(QRectF(-6.2, -2.5, 6.6, 5.0), 2.5, 2.5)
        p.drawRoundedRect(QRectF(-0.4, -2.5, 6.6, 5.0), 2.5, 2.5)
        p.restore()
        if name == "link_off":
            p.drawLine(QPointF(3, 13), QPointF(13, 3))
    elif name == "transform":
        p.drawRect(QRectF(3, 3, 10, 10))
        p.setBrush(fill)
        for x, y in ((3, 3), (8, 3), (13, 3), (3, 8), (13, 8), (3, 13), (8, 13), (13, 13)):
            p.drawRect(QRectF(x - 1.1, y - 1.1, 2.2, 2.2))
    elif name == "puppet":
        p.drawEllipse(QPointF(5.5, 4.5), 2.0, 2.0)
        p.drawLine(QPointF(5.5, 6.6), QPointF(5.5, 12.5))
        p.drawLine(QPointF(2.8, 12.5), QPointF(8.2, 12.5))
        p.drawLine(QPointF(5.5, 8.4), QPointF(10.5, 7.2))
        p.drawEllipse(QPointF(11.6, 6.9), 1.6, 1.6)
    elif name == "graph":
        path = QPainterPath(QPointF(2, 13))
        path.cubicTo(5, 13, 5.5, 3, 9, 3)
        path.cubicTo(12, 3, 12.5, 10, 14, 10)
        p.drawPath(path)
        p.setBrush(fill)
        for x, y in ((2, 13), (9, 3), (14, 10)):
            p.drawRect(QRectF(x - 1.2, y - 1.2, 2.4, 2.4))
    elif name == "path":
        path = QPainterPath(QPointF(2.5, 12))
        path.cubicTo(6, 2.5, 10, 13.5, 13.5, 4)
        p.drawPath(path)
        p.setBrush(fill)
        for x, y in ((2.5, 12), (13.5, 4)):
            p.drawEllipse(QPointF(x, y), 1.5, 1.5)
    elif name == "text":
        p.drawLine(QPointF(3.5, 4.0), QPointF(12.5, 4.0))
        p.drawLine(QPointF(8.0, 4.0), QPointF(8.0, 13.0))
    elif name == "captions":
        # A subtitle plate: two short lines inside a rounded frame ("CC").
        p.drawRoundedRect(QRectF(1.4, 3.4, 13.2, 9.2), 2.0, 2.0)
        p.drawLine(QPointF(3.8, 7.1), QPointF(6.9, 7.1))
        p.drawLine(QPointF(3.8, 9.5), QPointF(6.2, 9.5))
        p.drawLine(QPointF(9.1, 7.1), QPointF(12.2, 7.1))
        p.drawLine(QPointF(9.1, 9.5), QPointF(11.5, 9.5))
    elif name == "auto_key":
        p.drawPolygon(_pts(7, 3, 12, 8, 7, 13, 2, 8))
        p.drawLine(QPointF(12.6, 2.6), QPointF(12.6, 6.0))
        p.drawLine(QPointF(10.9, 4.3), QPointF(14.3, 4.3))
    elif name == "flip_h":
        p.drawLine(QPointF(8, 2), QPointF(8, 14))
        p.drawPolygon(_pts(7, 4.5, 2, 8, 7, 11.5))
        p.drawPolyline(_pts(9, 4.5, 14, 4.5, 14, 11.5, 9, 11.5))
    elif name == "flip_v":
        p.drawLine(QPointF(2, 8), QPointF(14, 8))
        p.drawPolygon(_pts(4.5, 7, 8, 2, 11.5, 7))
        p.drawPolyline(_pts(4.5, 9, 4.5, 14, 11.5, 14, 11.5, 9))
    elif name == "fit_frame":
        p.drawRect(QRectF(1.8, 3, 12.4, 10))
        p.drawRect(QRectF(4.6, 5.6, 6.8, 4.8))
    elif name == "fill_frame":
        p.drawRect(QRectF(1.8, 3, 12.4, 10))
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(QRectF(1.8, 3, 12.4, 10))
        p.setPen(QPen(color, 1.6)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(4.6, 5.6, 6.8, 4.8))
    elif name == "center":
        p.drawRect(QRectF(1.8, 3, 12.4, 10))
        p.drawLine(QPointF(8, 5), QPointF(8, 11)); p.drawLine(QPointF(5, 8), QPointF(11, 8))
        p.setBrush(fill); p.drawEllipse(QPointF(8, 8), 1.2, 1.2)
    elif name == "grip":
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        for x in (5.5, 10.5):
            for y in (4, 8, 12):
                p.drawEllipse(QPointF(x, y), 1, 1)
    elif name == "grid":
        for row in range(2):
            for column in range(2):
                p.drawRoundedRect(QRectF(2.5 + column * 6, 2.5 + row * 6, 5, 5), 1, 1)
    elif name == "list":
        for row in range(3):
            y = 4.0 + row * 4.0
            p.drawLine(QPointF(2.5, y), QPointF(5.5, y))
            p.drawLine(QPointF(7.5, y), QPointF(13.5, y))
    elif name == "star":
        points = []
        for step in range(10):
            angle = math.radians(-90 + step * 36)
            radius = 6.0 if step % 2 == 0 else 2.6
            points.append(QPointF(8 + radius * math.cos(angle), 8.2 + radius * math.sin(angle)))
        p.drawPolygon(QPolygonF(points))
    elif name == "clock":
        p.drawEllipse(QRectF(2.5, 2.5, 11, 11))
        p.drawLine(QPointF(8, 4.6), QPointF(8, 8.8))
        p.drawLine(QPointF(8, 8.8), QPointF(10.6, 8.8))
    elif name == "close":
        p.drawLine(QPointF(4, 4), QPointF(12, 12))
        p.drawLine(QPointF(12, 4), QPointF(4, 12))
    elif name == "tracking":
        # Detection reticle: four corner brackets around a point.
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            cx, cy = (2.5 if sx > 0 else 13.5), (2.5 if sy > 0 else 13.5)
            p.drawPolyline(_pts(cx, cy + sy * 3.2, cx, cy, cx + sx * 3.2, cy))
        p.drawEllipse(QPointF(8, 8), 2.3, 2.3)
    elif name == "clean":
        # Two sparkles: the usual "tidy this up" glyph, still readable at 16 px.
        p.setBrush(fill); p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(_pts(9.6, 1.8, 10.9, 5.9, 15.0, 7.2, 10.9, 8.5,
                           9.6, 12.6, 8.3, 8.5, 4.2, 7.2, 8.3, 5.9))
        p.drawPolygon(_pts(4.0, 9.6, 4.8, 11.4, 6.6, 12.2, 4.8, 13.0,
                           4.0, 14.8, 3.2, 13.0, 1.4, 12.2, 3.2, 11.4))
    elif name in ("folder", "folder_new"):
        p.drawPolyline(_pts(2.2, 12.8, 2.2, 4.4, 6.2, 4.4, 7.6, 6.0, 13.8, 6.0, 13.8, 12.8, 2.2, 12.8))
        if name == "folder_new":
            p.drawLine(QPointF(8, 8.2), QPointF(8, 11.4))
            p.drawLine(QPointF(6.4, 9.8), QPointF(9.6, 9.8))
    elif name == "import":
        p.drawLine(QPointF(8, 2.6), QPointF(8, 9.8))
        p.drawPolyline(_pts(5, 7, 8, 10, 11, 7))
        p.drawPolyline(_pts(2.8, 11, 2.8, 13.2, 13.2, 13.2, 13.2, 11))
    elif name == "up":
        p.drawPolyline(_pts(3.2, 8.6, 6.4, 5.4, 3.2, 8.6, 6.4, 11.8))
        p.drawPolyline(_pts(3.6, 8.6, 10.4, 8.6, 13, 6, 13, 4))
    elif name == "film":
        p.drawRoundedRect(QRectF(2.4, 3.2, 11.2, 9.6), 2, 2)
        p.drawLine(QPointF(5.4, 3.4), QPointF(5.4, 12.6))
        p.drawLine(QPointF(10.6, 3.4), QPointF(10.6, 12.6))
    elif name == "music":
        p.drawLine(QPointF(10.6, 3.2), QPointF(10.6, 10.6))
        p.drawLine(QPointF(10.6, 3.2), QPointF(13.2, 4.2))
        p.drawEllipse(QPointF(8.4, 11), 2.2, 1.8)
    elif name == "image":
        p.drawRoundedRect(QRectF(2.4, 3.2, 11.2, 9.6), 2, 2)
        p.drawPolyline(_pts(3.2, 11.2, 6.4, 7.6, 8.8, 10, 10.4, 8.6, 12.8, 11.2))
    elif name == "trash":
        p.drawLine(QPointF(3, 4.8), QPointF(13, 4.8))
        p.drawPolyline(_pts(6.2, 4.6, 6.2, 3, 9.8, 3, 9.8, 4.6))
        p.drawPolyline(_pts(4.2, 4.8, 4.8, 13, 11.2, 13, 11.8, 4.8))


def paint(painter, name, rect, color):
    """Draw icon `name` straight into `rect` of a painter."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.x(), rect.y())
    painter.scale(rect.width() / 16.0, rect.height() / 16.0)
    _draw(name, painter, QColor(color))
    painter.restore()


def clear_icon_cache():
    """Forget every rendered icon (a theme switch changes their colours)."""
    _CACHE.clear()


def icon(name, color=None, size=16):
    """Icon in `color` (default: the normal text colour); a dimmed twin is used when disabled."""
    key = (name, color, size, COLORS["text"])
    if key in _CACHE:
        return _CACHE[key]
    result = QIcon()
    states = ((QIcon.Mode.Normal, color or COLORS["text"]), (QIcon.Mode.Disabled, COLORS["disabled"]),
              (QIcon.Mode.Active, color or COLORS["text"]))
    for mode, value in states:
        for scale in _ICON_SCALES:
            result.addPixmap(_render(name, value, size, scale), mode)
    _CACHE[key] = result
    return result
