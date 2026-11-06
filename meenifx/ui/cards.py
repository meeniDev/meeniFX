"""Card painting for the Project panel: media cards, folder cards and list rows.

One ``QStyledItemDelegate`` draws every kind of item so the list keeps Qt's selection,
keyboard navigation, drag and drop and accessibility, while the look (rounded card, warm
tint for folders, selection and drop-target states) lives in one place. It only reads
``tokens.COLORS`` while painting, so a theme change needs just a repaint.
"""

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate

from .. import tokens

PAD = 6
LIST_ROW_H = 44
ALIGN_LEFT = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def mix(a, b, amount):
    """``a`` blended over ``b`` by ``amount`` (0..1) as a QColor."""
    ca, cb = QColor(a), QColor(b)
    return QColor(round(cb.red() + (ca.red() - cb.red()) * amount),
                  round(cb.green() + (ca.green() - cb.green()) * amount),
                  round(cb.blue() + (ca.blue() - cb.blue()) * amount))


class CardDelegate(QStyledItemDelegate):
    """Grid cards (thumbnail over two lines of text) or list rows (thumbnail beside text)."""

    def __init__(self, parent=None, grid=True):
        super().__init__(parent)
        self.grid = grid

    def sizeHint(self, option, index):
        if self.grid:
            return super().sizeHint(option, index)
        return QSize(option.rect.width() or 160, LIST_ROW_H)

    def paint(self, painter, option, index):
        data = index.data(Qt.ItemDataRole.UserRole) or ("item", "", "")
        c = tokens.COLORS
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        background = index.data(Qt.ItemDataRole.BackgroundRole)
        dropping = background is not None and background.style() != Qt.BrushStyle.NoBrush
        folder = data[0] in ("bin", "up", "root")
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(option.rect).adjusted(2.5, 2.5, -2.5, -2.5)

        base = mix(c["butter"], c["panel_alt"], 0.14 if tokens.is_dark() else 0.22) if folder \
            else QColor(c["panel_alt"])
        fill = QColor(c["accent_soft"]) if (selected or dropping) else base
        if hover and not selected and not dropping:
            fill = mix(c["accent_soft"], base, 0.55)
        if dropping:
            pen = QPen(QColor(c["accent"]), 2, Qt.PenStyle.DashLine)
        elif selected:
            pen = QPen(QColor(c["accent"]), 2)
        else:
            pen = QPen(QColor(c["accent"] if hover else c["border"]), 1)
        painter.setBrush(fill)
        painter.setPen(pen)
        painter.drawRoundedRect(card, tokens.RADIUS_LG, tokens.RADIUS_LG)

        lines = (index.data(Qt.ItemDataRole.DisplayRole) or "").split("\n")
        name, info = lines[0], (lines[1] if len(lines) > 1 else "")
        foreground = index.data(Qt.ItemDataRole.ForegroundRole)
        name_color = QColor(foreground.color()) if foreground is not None \
            and foreground.style() != Qt.BrushStyle.NoBrush else QColor(c["text"])
        bold = QFont(option.font)
        bold.setWeight(QFont.Weight.DemiBold)
        small = QFont(option.font)
        small.setPixelSize(tokens.FONT_SMALL)

        if self.grid:
            inner = card.adjusted(PAD, PAD, -PAD, -PAD)
            thumb_h = min(inner.width() * 9 / 16, inner.height() - 34)
            thumb = QRectF(inner.left(), inner.top(), inner.width(), thumb_h)
            text_left, text_width, text_top = inner.left(), inner.width(), thumb.bottom() + 5
        else:
            thumb = QRectF(card.left() + PAD, card.top() + 4, 56, card.height() - 8)
            text_left = thumb.right() + 10
            text_width, text_top = card.right() - text_left - PAD, card.top() + 4
        self._thumb(painter, index.data(Qt.ItemDataRole.DecorationRole), thumb, folder)

        metrics = QFontMetrics(bold)
        painter.setFont(bold)
        painter.setPen(name_color)
        painter.drawText(QRectF(text_left, text_top, text_width, metrics.height()), ALIGN_LEFT,
                         metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, int(text_width)))
        if info:
            small_metrics = QFontMetrics(small)
            painter.setFont(small)
            painter.setPen(QColor(c["muted"]))
            painter.drawText(QRectF(text_left, text_top + metrics.height(), text_width, small_metrics.height()),
                             ALIGN_LEFT, small_metrics.elidedText(info, Qt.TextElideMode.ElideRight, int(text_width)))
        painter.restore()

    @staticmethod
    def _thumb(painter, icon, rect, folder):
        c = tokens.COLORS
        clip = QPainterPath()
        clip.addRoundedRect(rect, tokens.RADIUS, tokens.RADIUS)
        painter.save()
        painter.setClipPath(clip)
        # Media sits on a neutral well so colours are judged against a stable background.
        painter.fillRect(rect, mix(c["butter"], c["panel_soft"], 0.35) if folder else QColor(c["neutral_stage"]))
        if icon is not None and not icon.isNull():
            ratio = painter.device().devicePixelRatioF()
            source = icon.pixmap(icon.actualSize(QSize(1024, 1024)))
            if not source.isNull():
                fitted = source.scaled(QSize(max(1, int(rect.width() * ratio)), max(1, int(rect.height() * ratio))),
                                       Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                fitted.setDevicePixelRatio(ratio)
                w, h = fitted.width() / ratio, fitted.height() / ratio
                painter.drawPixmap(int(rect.left() + (rect.width() - w) / 2),
                                   int(rect.top() + (rect.height() - h) / 2), fitted)
        painter.restore()
