"""Track header layout, vector icons and hit testing for the timeline canvas.

Kept separate from TimelineCanvas so the drawing/geometry of the header controls is
one small, testable unit: `header_layout` answers where everything is, `header_hit`
answers what the pointer is over, and `paint_header` draws it. All geometry is in
canvas coordinates relative to the header rect.
"""

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF

from .. import tokens
from ..icons import paint as paint_icon

BUTTON = tokens.HIT
GAP = 4
VOLUME_W = 44
VOLUME_H = 9
PAD = 8
NAME_H = 20
ROW_H = 24
COMPACT_H = 56      # below this the name and icons share one row
TINY_H = 30         # below this only the name fits

COLORS = {
    "text": "#dbe6f7", "dim": "#8b9bb4", "icon": "#c3d2e6",
    "active": tokens.COLORS["accent"], "warn": tokens.COLORS["accent"], "danger": tokens.COLORS["accent"],
    "hover": "#2a3a55", "press": "#0d1220", "track": "#2f3b4d",
    "fill": tokens.COLORS["accent_fill"], "volume_bg": "#1b2330",
}

TOOLTIPS = {
    "visible": "Show / hide this video track (hidden tracks are not rendered)",
    "lock": "Lock this track (locked tracks cannot be edited)",
    "mute": "Mute this audio track",
    "solo": "Solo this audio track (other audio is silenced)",
    "volume": "Track volume - drag to change",
    "name": "Double-click to rename, right-click for track options, drag the bottom edge to resize",
    "ruler": "Drag to move the playhead - playback keeps running; hold still for silence",
    "loop_strip": "Loop region: drag to create, drag an edge to resize, drag the middle to move, "
                  "double-click to clear, right-click for options (Q toggles looping, "
                  "I and O set the start and end at the playhead)",
}


def controls_for(track):
    """Which controls a track type gets, left to right."""
    if track.type == "audio":
        return ["mute", "solo", "lock", "volume"]
    return ["visible", "lock"]


def header_layout(track, width, height, origin_y=0.0):
    """Where each header control sits for a track drawn at `origin_y`.

    Three responsive modes so nothing ever overlaps:

    * full    - name row, then every icon (plus the volume bar for audio tracks);
    * compact - one row with an elided name and only the essential icons;
    * tiny    - one centered, elided name; the icons live in the right-click menu.
    """
    top = float(origin_y)
    width = float(width)
    height = float(height)
    controls = controls_for(track)
    essential = {"audio": ("mute", "solo"), "video": ("visible", "lock")}[track.type]
    if height < TINY_H:
        return {"name": QRectF(PAD, top, max(16.0, width - 2 * PAD), height),
                "buttons": [], "volume": QRectF(), "row_y": top + max(0.0, (height - BUTTON) / 2.0),
                "compact": True, "tiny": True}
    if height < COMPACT_H:
        actions = [action for action in controls if action in essential]
        row_y = top + max(1.0, (height - ROW_H) / 2.0)
        icon_w = len(actions) * BUTTON + max(0, len(actions) - 1) * GAP
        cursor = max(PAD + 24.0, width - PAD - icon_w)
        buttons = []
        for action in actions:
            buttons.append((action, QRectF(cursor, row_y + (ROW_H - BUTTON) / 2.0, BUTTON, BUTTON)))
            cursor += BUTTON + GAP
        return {"name": QRectF(PAD, row_y, max(16.0, width - 2 * PAD - icon_w - GAP), ROW_H),
                "buttons": buttons, "volume": QRectF(), "row_y": row_y,
                "compact": True, "tiny": False}
    name = QRectF(PAD, top + 2, max(20.0, width - 2 * PAD), NAME_H)
    row_y = min(top + NAME_H + 4, top + height - BUTTON - 3)
    buttons = []
    cursor = PAD
    volume = QRectF()
    for action in controls:
        if action == "volume":
            volume = QRectF(max(PAD, width - PAD - VOLUME_W),
                            row_y + (BUTTON - VOLUME_H) / 2.0, VOLUME_W, VOLUME_H)
            continue
        buttons.append((action, QRectF(cursor, row_y, BUTTON, BUTTON)))
        cursor += BUTTON + GAP
    if not volume.isEmpty() and volume.left() < cursor + GAP:
        volume = QRectF()          # narrow header: the slider would touch the icons
    return {"name": name, "buttons": buttons, "volume": volume, "row_y": row_y,
            "compact": False, "tiny": False}


def _hit(rect, x, y, pad=2):
    return rect.adjusted(-pad, -pad, pad, pad).contains(QPointF(x, y))


def header_hit(track, width, height, x, y, origin_y=0.0):
    """Return (action, rect) for the control under (x, y), or (None, None)."""
    layout = header_layout(track, width, height, origin_y)
    if not layout["volume"].isEmpty() and _hit(layout["volume"], x, y, 3):
        return "volume", layout["volume"]
    for action, rect in layout["buttons"]:
        if _hit(rect, x, y):
            return action, rect
    if layout["name"].adjusted(0, -2, 0, 2).contains(QPointF(x, y)):
        return "name", layout["name"]
    return None, None


# --------------------------------------------------------------------- drawing
def _icon_eye(p, rect, on, color):
    p.setPen(QPen(QColor(color), 1.4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = rect.center().x(), rect.center().y()
    p.drawPolygon(QPolygonF([QPointF(cx - 6, cy), QPointF(cx, cy - 4.2),
                             QPointF(cx + 6, cy), QPointF(cx, cy + 4.2)]))
    if on:
        p.setBrush(QColor(color))
    p.drawEllipse(QPointF(cx, cy), 1.7, 1.7)
    if not on:
        p.drawLine(QPointF(cx - 6, cy + 5), QPointF(cx + 6, cy - 5))


def _icon_lock(p, rect, locked, color):
    p.setPen(QPen(QColor(color), 1.4))
    cx, cy = rect.center().x(), rect.center().y()
    body = QRectF(cx - 4.5, cy - 1.5, 9, 7.5)
    p.setBrush(QColor(color) if locked else Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(body, 1.5, 1.5)
    p.setBrush(Qt.BrushStyle.NoBrush)
    arc = QRectF(cx - 3.4, cy - 6.4, 6.8, 7)
    p.drawArc(arc, 0 if locked else 30 * 16, (180 if locked else 120) * 16)


def _icon_speaker(p, rect, muted, color):
    p.setPen(QPen(QColor(color), 1.4))
    cx, cy = rect.center().x(), rect.center().y()
    body = QPolygonF([QPointF(cx - 5.5, cy - 2.5), QPointF(cx - 2, cy - 2.5), QPointF(cx + 1.5, cy - 5.5),
                      QPointF(cx + 1.5, cy + 5.5), QPointF(cx - 2, cy + 2.5), QPointF(cx - 5.5, cy + 2.5)])
    p.setBrush(QColor(color))
    p.drawPolygon(body)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if muted:
        p.setPen(QPen(QColor(COLORS["danger"]), 1.8))
        p.drawLine(QPointF(cx + 3.5, cy - 4), QPointF(cx + 7.5, cy + 4))
        p.drawLine(QPointF(cx + 7.5, cy - 4), QPointF(cx + 3.5, cy + 4))
    else:
        p.setPen(QPen(QColor(color), 1.3))
        p.drawArc(QRectF(cx + 1, cy - 4, 6, 8), -70 * 16, 140 * 16)
        p.drawArc(QRectF(cx + 0.5, cy - 6.5, 10, 13), -70 * 16, 140 * 16)


def _icon_letter(p, rect, letter, active, color):
    p.setPen(QPen(QColor(COLORS["danger"] if letter == "M" and active else color), 1.3))
    p.setBrush(QColor(COLORS["warn"]) if active else Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(rect.adjusted(2.5, 2.5, -2.5, -2.5), 2, 2)
    p.setBrush(Qt.BrushStyle.NoBrush)
    font = QFont("Segoe UI", 7, QFont.Weight.Bold)
    p.setFont(font)
    p.setPen(QColor("#10151f") if active else QColor(color))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, letter)


def _icon_button(p, action, rect, track, hovered, pressed):
    """One header icon: a 22 px hit area with hover, pressed and active (accent) states."""
    active = {"visible": not track.visible, "lock": track.locked, "mute": track.muted,
              "solo": track.solo}.get(action, False)
    body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
    p.setPen(Qt.PenStyle.NoPen)
    if pressed:
        p.setBrush(QColor(COLORS["press"]))
        p.drawRoundedRect(body, 4, 4)
    elif active:
        tint = QColor(COLORS["active"])
        tint.setAlpha(70 if not hovered else 110)
        p.setBrush(tint)
        p.drawRoundedRect(body, 4, 4)
    elif hovered:
        p.setBrush(QColor(COLORS["hover"]))
        p.drawRoundedRect(body, 4, 4)
    name = {"visible": "eye" if track.visible else "eye_off", "lock": "lock" if track.locked else "unlock",
            "mute": "mute" if track.muted else "volume", "solo": "solo"}.get(action)
    if name:
        color = COLORS["active"] if active else COLORS["icon"]
        paint_icon(p, name, rect.adjusted(3, 3, -3, -3), color)


def _draw_volume(p, rect, value, active, hovered):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(COLORS["volume_bg"]))
    p.drawRoundedRect(rect, rect.height() / 2.0, rect.height() / 2.0)
    span = max(0.0, min(1.5, float(value))) / 1.5
    fill = QRectF(rect.left(), rect.top(), rect.width() * span, rect.height())
    p.setBrush(QColor(COLORS["fill"] if active or hovered else COLORS["track"]))
    p.drawRoundedRect(fill, rect.height() / 2.0, rect.height() / 2.0)
    knob = QPointF(fill.right(), rect.center().y())
    p.setBrush(QColor(COLORS["active"]))
    p.drawEllipse(knob, max(2.0, rect.height() * 0.45), max(2.0, rect.height() * 0.45))


def paint_header(painter, track, rect, hover=None, pressed=None, selected=False):
    """Draw one track header: name row plus its type-specific controls."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    layout = header_layout(track, rect.width(), rect.height(), rect.top())
    if selected:
        painter.fillRect(rect, QColor(37, 52, 82))
    flags = Qt.TextFlag.TextSingleLine
    painter.setFont(QFont("Segoe UI", 8 if layout.get("tiny") else 9, QFont.Weight.Bold))
    painter.setPen(QColor(COLORS["text"] if track.visible or track.type == "audio" else COLORS["dim"]))
    metrics = painter.fontMetrics()
    available = max(8.0, layout["name"].width() - 4)
    label = metrics.elidedText(track.name, Qt.TextElideMode.ElideRight, int(available))
    # Clip the name to its own rect so it can never run into the icons.
    painter.save()
    painter.setClipRect(layout["name"].adjusted(0, 0, -2, 0), Qt.ClipOperation.IntersectClip)
    painter.drawText(layout["name"].adjusted(2, 0, 0, 0),
                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) | flags, label)
    painter.restore()
    for action, button_rect in layout["buttons"]:
        _icon_button(painter, action, button_rect, track, hover == action, pressed == action)
    if not layout["volume"].isEmpty():
        _draw_volume(painter, layout["volume"], track.volume, hover == "volume", hover == "volume")
    painter.restore()
