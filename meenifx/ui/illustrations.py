"""Original flat illustrations for empty states, painted with QPainter.

Soft shapes, warm brand colours and one friendly face - drawn from scratch, no third-party
artwork. Everything is scaled from a 120 x 96 design grid, so the same code is sharp at any
size or screen scale and follows the active theme.
"""

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF

from .. import tokens

GRID_W, GRID_H = 120.0, 96.0
KINDS = ("folder", "clip", "sparkles", "timeline", "selection")


def _outline():
    return QColor("#CFC6BA" if tokens.is_dark() else "#6B625A")


def _pen(width=2.0):
    return QPen(_outline(), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def _face(p, cx, cy, spread=7.0):
    """Two dot eyes, a small smile and blush: the shared personality of every scene."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_outline())
    for dx in (-spread, spread):
        p.drawEllipse(QPointF(cx + dx, cy), 1.9, 2.3)
    p.setBrush(QColor(tokens.COLORS["coral"]).lighter(112))
    for dx in (-spread - 5, spread + 5):
        p.setOpacity(0.55)
        p.drawEllipse(QPointF(cx + dx, cy + 5), 3.4, 2.2)
    p.setOpacity(1.0)
    p.setPen(_pen(1.7))
    p.setBrush(Qt.BrushStyle.NoBrush)
    smile = QPainterPath(QPointF(cx - 3.6, cy + 4.2))
    smile.quadTo(QPointF(cx, cy + 8.6), QPointF(cx + 3.6, cy + 4.2))
    p.drawPath(smile)


def _sparkle(p, cx, cy, r, color):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    k = r * 0.28
    p.drawPolygon(QPolygonF([QPointF(cx, cy - r), QPointF(cx + k, cy - k), QPointF(cx + r, cy),
                             QPointF(cx + k, cy + k), QPointF(cx, cy + r), QPointF(cx - k, cy + k),
                             QPointF(cx - r, cy), QPointF(cx - k, cy - k)]))


def _sprig(p, x, y):
    """A tiny two-leaf sprout, the same sage as the logo."""
    p.setPen(_pen(1.8))
    p.drawLine(QPointF(x, y), QPointF(x, y - 9))
    p.setBrush(QColor(tokens.COLORS["sage"]))
    for sign in (-1, 1):
        leaf = QPainterPath(QPointF(x, y - 7))
        leaf.quadTo(QPointF(x + sign * 9, y - 12), QPointF(x + sign * 10, y - 3))
        leaf.quadTo(QPointF(x + sign * 3, y - 2), QPointF(x, y - 7))
        p.drawPath(leaf)


def _cloud(p, x, y, w):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(tokens.COLORS["sky"]))
    p.setOpacity(0.55 if tokens.is_dark() else 0.8)
    p.drawRoundedRect(QRectF(x, y + w * 0.18, w, w * 0.3), w * 0.15, w * 0.15)
    p.drawEllipse(QPointF(x + w * 0.35, y + w * 0.2), w * 0.2, w * 0.2)
    p.drawEllipse(QPointF(x + w * 0.62, y + w * 0.16), w * 0.25, w * 0.25)
    p.setOpacity(1.0)


def _folder(p):
    c = tokens.COLORS
    _cloud(p, 6, 6, 30)
    p.setPen(_pen())
    p.setBrush(QColor(c["butter"]).darker(108))
    back = QPainterPath()
    back.addRoundedRect(QRectF(20, 24, 80, 56), 8, 8)
    p.drawPath(back)
    p.setBrush(QColor(c["butter"]))
    tab = QPainterPath(QPointF(20, 30))
    tab.lineTo(QPointF(20, 22))
    tab.quadTo(QPointF(20, 18), QPointF(25, 18))
    tab.lineTo(QPointF(44, 18))
    tab.quadTo(QPointF(48, 18), QPointF(51, 22))
    tab.lineTo(QPointF(54, 26))
    p.drawPath(tab)
    p.setBrush(QColor(c["butter"]).lighter(112))
    p.drawRoundedRect(QRectF(16, 36, 88, 46), 9, 9)
    _face(p, 60, 56, 9)
    _sprig(p, 94, 24)
    _sparkle(p, 106, 14, 5.5, c["coral"])
    _sparkle(p, 12, 58, 4, c["sky"])


def _clip(p):
    c = tokens.COLORS
    _cloud(p, 80, 6, 30)
    p.setPen(_pen())
    p.setBrush(QColor(c["peach"]) if not tokens.is_dark() else QColor(c["peach"]).darker(130))
    p.drawRoundedRect(QRectF(14, 20, 92, 60), 10, 10)
    p.setBrush(QColor(c["sage"]))
    p.drawRoundedRect(QRectF(22, 28, 76, 44), 6, 6)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_outline())
    for i in range(5):
        p.drawRoundedRect(QRectF(20 + i * 17, 84, 8, 4), 2, 2)
    _face(p, 60, 47, 9)
    _sparkle(p, 14, 14, 5.5, c["butter"])
    _sparkle(p, 108, 84, 4, c["coral"])


def _sparkles(p):
    c = tokens.COLORS
    _cloud(p, 6, 8, 28)
    p.setPen(_pen())
    p.setBrush(QColor(c["coral"]).lighter(118))
    wand = QPolygonF([QPointF(30, 82), QPointF(36, 88), QPointF(76, 46), QPointF(70, 40)])
    p.drawPolygon(wand)
    p.setBrush(QColor(c["butter"]))
    p.drawRoundedRect(QRectF(66, 30, 18, 18), 5, 5)
    _face(p, 75, 38, 3.4)
    _sparkle(p, 94, 22, 9, c["butter"])
    _sparkle(p, 106, 50, 6, c["sky"])
    _sparkle(p, 50, 22, 5, c["coral"])
    _sparkle(p, 22, 56, 4, c["sage"])


def _timeline(p):
    c = tokens.COLORS
    _cloud(p, 76, 6, 30)
    p.setPen(_pen())
    for i, (x, w, key) in enumerate(((12, 40, "sage"), (56, 28, "butter"), (88, 20, "sky"))):
        p.setBrush(QColor(c[key]))
        p.drawRoundedRect(QRectF(x, 30, w, 18), 6, 6)
    p.setBrush(QColor(c["peach"]) if not tokens.is_dark() else QColor(c["peach"]).darker(130))
    p.drawRoundedRect(QRectF(12, 54, 60, 18), 6, 6)
    _face(p, 42, 62, 5)
    p.setPen(_pen(2.4))
    p.drawLine(QPointF(96, 18), QPointF(96, 80))
    p.setBrush(QColor(c["coral"]))
    p.drawEllipse(QPointF(96, 18), 4.5, 4.5)
    _sparkle(p, 20, 16, 5, c["butter"])


def _selection(p):
    """A miniature timeline strip with a coral playhead and a cursor (260 x 160 grid)."""
    dark = tokens.is_dark()
    card, edge, strip, clip, coral, ink, rim = (
        ("#2E3238", "#454A52", "#26382E", "#7FA68A", "#E98C7A", "#F1ECE2", "#2E3238") if dark else
        ("#FFFFFF", "#DED8CD", "#E8F2EC", "#A8C9B0", "#E98C7A", "#454A52", "#FFFFFF"))
    p.setPen(QPen(QColor(edge), 2))
    p.setBrush(QColor(card))
    p.drawRoundedRect(QRectF(31, 45, 198, 76), 13, 13)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(strip))
    p.drawRoundedRect(QRectF(45, 61, 170, 44), 7, 7)
    p.setBrush(QColor(clip))
    for x in (54, 92, 130, 168):
        p.drawRoundedRect(QRectF(x, 69, 30, 28), 4, 4)
    p.setPen(QPen(QColor(coral), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(132, 51), QPointF(132, 115))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(coral))
    p.drawPolygon(QPolygonF([QPointF(126, 48), QPointF(138, 48), QPointF(132, 56)]))
    p.setPen(QPen(QColor(rim), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(QColor(ink))
    p.drawPolygon(QPolygonF([QPointF(201, 105), QPointF(219, 145), QPointF(224, 128), QPointF(239, 126)]))


_SCENES = {"folder": _folder, "clip": _clip, "sparkles": _sparkles, "timeline": _timeline,
           "selection": _selection}
_GRIDS = {"selection": (260.0, 160.0)}


def paint_illustration(painter, rect, kind="folder"):
    """Draw scene ``kind`` centred in ``rect`` (a QRectF), keeping its aspect ratio."""
    scene = _SCENES.get(kind, _folder)
    grid_w, grid_h = _GRIDS.get(kind, (GRID_W, GRID_H))
    rect = QRectF(rect)
    scale = min(rect.width() / grid_w, rect.height() / grid_h)
    if scale <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.center().x() - grid_w * scale / 2, rect.center().y() - grid_h * scale / 2)
    painter.scale(scale, scale)
    scene(painter)
    painter.restore()
