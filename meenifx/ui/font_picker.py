"""A searchable picker for every font installed on the computer.

The combo box this replaces showed a fixed, hardcoded list. This picker reads the live
font registry (:mod:`meenifx.processing.fonts`), draws each family's own name and a short
sample with that face, and keeps Favorites, Recently Used and a Refresh action.

Only the rows Qt actually paints are measured, so opening the list with hundreds of
installed fonts stays responsive.
"""

from __future__ import annotations

from PyQt6.QtCore import QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QSizePolicy, QStyle, QStyledItemDelegate,
                             QToolButton, QVBoxLayout, QWidget)

from .. import icons, tokens
from ..processing import fonts

FAMILY_ROLE = int(Qt.ItemDataRole.UserRole)
KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 1
FAVOURITE_ROLE = int(Qt.ItemDataRole.UserRole) + 2
MISSING_ROLE = int(Qt.ItemDataRole.UserRole) + 3
MAX_RECENT = 10


def _store():
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                     "meeniFX", "Studios")


def _read_list(store, key):
    value = store.value(key, [])
    if isinstance(value, str):
        return [item for item in value.split("\n") if item]
    return [str(item) for item in (value or [])]


class _FontDelegate(QStyledItemDelegate):
    """Paints section headers and font rows (name in its own face, sample, star)."""

    ROW_H = 30

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW_H if index.data(KIND_ROLE) == "font" else 24)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect
        kind = index.data(KIND_ROLE)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            colour = QColor(tokens.COLORS["accent_fill" if selected else "accent_soft"])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(rect.adjusted(2, 1, -2, -1), tokens.RADIUS_SM, tokens.RADIUS_SM)
        text_colour = QColor("#FFFFFF" if selected else tokens.COLORS["text"])
        muted = QColor("#FFFFFF" if selected else tokens.COLORS["muted"])
        if kind == "section":
            font = painter.font()
            font.setPointSizeF(8.5)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(muted)
            label = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
            painter.drawText(rect.adjusted(8, 0, -8, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            painter.restore()
            return
        family = str(index.data(FAMILY_ROLE) or "")
        missing = bool(index.data(MISSING_ROLE))
        star = bool(index.data(FAVOURITE_ROLE))
        font = QFont(family, 10)
        if missing:
            font.setItalic(True)
        painter.setFont(font)
        painter.setPen(QColor(tokens.COLORS["warning"]) if missing else text_colour)
        name_rect = rect.adjusted(8, 0, -92, 0)
        metrics = QFontMetrics(font)
        name = metrics.elidedText(family, Qt.TextElideMode.ElideRight, max(20, name_rect.width()))
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        # The sample is drawn in the row's own family, which is the whole point of it.
        sample_font = QFont(family, 9)
        painter.setFont(sample_font)
        painter.setPen(muted)
        painter.drawText(rect.adjusted(rect.width() - 88, 0, -20, 0),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         "Aa 123")
        if star:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens.COLORS["warning"]))
            self._star(painter, rect.right() - 15, rect.center().y() + 1, 5.0)
        painter.restore()

    @staticmethod
    def _star(painter, cx, cy, radius):
        import math

        from PyQt6.QtGui import QPolygonF
        from PyQt6.QtCore import QPointF
        points = []
        for step in range(10):
            angle = math.radians(-90 + step * 36)
            r = radius if step % 2 == 0 else radius * 0.44
            points.append(QPointF(cx + r * math.cos(angle), cy + r * math.sin(angle)))
        painter.drawPolygon(QPolygonF(points))


class _FontPopup(QFrame):
    """Frameless popup: search field, sectioned list and a small action footer."""

    picked = pyqtSignal(str)
    refreshed = pyqtSignal()

    def __init__(self, current, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("fontPopup")
        self.setStyleSheet(
            f"QFrame#fontPopup {{ background: {tokens.COLORS['popup']};"
            f" border: 1px solid {tokens.COLORS['popup_border']};"
            f" border-radius: {tokens.RADIUS_LG}px; }}")
        self.setFixedWidth(360)
        self._current = str(current or "")
        self._store = _store()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SP2, tokens.SP2, tokens.SP2, tokens.SP2)
        layout.setSpacing(tokens.SP1)

        search_row = QWidget()
        search_layout = QHBoxLayout(search_row)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(tokens.SP1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search installed fonts")
        self.search.setClearButtonEnabled(True)
        self.search.setProperty("role", "search")
        self.search.textChanged.connect(self._populate)
        self.search.installEventFilter(self)
        search_layout.addWidget(self.search, 1)
        layout.addWidget(search_row)

        self.list = QListWidget()
        # QAbstractItemView does not own its delegate; keep the wrapper referenced by the
        # list so it cannot be collected while the view still paints through it.
        self._delegate = _FontDelegate(self.list)
        self.list.setItemDelegate(self._delegate)
        self.list.setUniformItemSizes(False)
        self.list.setMouseTracking(True)
        self.list.setFixedHeight(286)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.itemClicked.connect(self._choose)
        self.list.itemActivated.connect(self._choose)
        layout.addWidget(self.list)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(tokens.SP1)
        self.favourite = QToolButton()
        self.favourite.setProperty("role", "icon")
        self.favourite.setToolTip("Add or remove the highlighted font from Favorites")
        self.favourite.clicked.connect(self._toggle_favourite)
        self.refresh = QToolButton()
        self.refresh.setProperty("role", "icon")
        self.refresh.setIcon(icons.icon("refresh"))
        self.refresh.setToolTip("Rescan installed fonts (do this after installing a font)")
        self.refresh.clicked.connect(self._refresh)
        self.count = QLabel("")
        self.count.setProperty("quiet", True)
        footer_layout.addWidget(self.favourite)
        footer_layout.addWidget(self.refresh)
        footer_layout.addWidget(self.count, 1)
        layout.addWidget(footer)
        self.search.setFocus()

    # -- data ---------------------------------------------------------------
    def _families(self):
        try:
            return fonts.system_families()
        except Exception:
            return list(fonts._index()["names"].values())

    def _populate(self, _text=None):
        query = self.search.text().strip().casefold()
        families = self._families()
        installed = set(families)
        favourites = [name for name in _read_list(self._store, "fonts/favorites")
                      if name in installed or fonts.installed(name)]
        recents = [name for name in _read_list(self._store, "fonts/recent")
                   if name in installed or fonts.installed(name)]
        recent_set, favourite_set = set(recents), set(favourites)
        missing = []
        if self._current and not fonts.installed(self._current):
            missing = [self._current]

        self.list.clear()
        if query:
            matches = [name for name in families if query in name.casefold()]
            matches += [name for name in missing if query in name.casefold()]
            self._section("Results" if not matches else f"Results ({len(matches)})")
            self._rows(matches, favourite_set, missing)
        else:
            added = set()
            if missing:
                self._section("Missing font in this clip")
                self._rows(missing, favourite_set, missing)
                added.update(missing)
            if favourites:
                self._section("Favorites")
                self._rows([name for name in favourites if name not in added], favourite_set, [])
                added.update(favourites)
            if recents:
                self._section("Recently Used")
                self._rows([name for name in recents if name not in added], favourite_set, [])
                added.update(recents)
            self._section("System Fonts")
            self._rows([name for name in families if name not in added], favourite_set, [])
        self.count.setText(f"{len(families)} installed")
        selected = None
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(KIND_ROLE) == "font" and item.data(FAMILY_ROLE) == self._current:
                selected = item
                break
        if selected is not None:
            self.list.setCurrentItem(selected)
            self.list.scrollToItem(selected, QListWidget.ScrollHint.PositionAtCenter)
        self._update_favourite()

    def _section(self, label):
        item = QListWidgetItem(label)
        item.setData(KIND_ROLE, "section")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.list.addItem(item)

    def _rows(self, names, favourite_set, missing_names):
        missing_set = set(missing_names)
        for name in names:
            item = QListWidgetItem(name)
            item.setData(FAMILY_ROLE, name)
            item.setData(KIND_ROLE, "font")
            item.setData(FAVOURITE_ROLE, name in favourite_set)
            item.setData(MISSING_ROLE, name in missing_set)
            item.setData(Qt.ItemDataRole.ToolTipRole,
                         f"{name} - installed" if name not in missing_set
                         else f"{name} is not installed; pick a replacement")
            item.setSizeHint(QSize(0, _FontDelegate.ROW_H))
            self.list.addItem(item)

    def _highlighted_family(self):
        item = self.list.currentItem()
        if item is not None and item.data(KIND_ROLE) == "font":
            return str(item.data(FAMILY_ROLE) or "")
        return self._current

    def _update_favourite(self):
        family = self._highlighted_family()
        favourites = set(_read_list(self._store, "fonts/favorites"))
        active = bool(family and family in favourites)
        self.favourite.setIcon(icons.icon("star"))
        self.favourite.setCheckable(True)
        self.favourite.setChecked(active)
        self.favourite.setToolTip("Add or remove the highlighted font from Favorites")

    # -- actions ------------------------------------------------------------
    def _choose(self, item):
        if item is None or item.data(KIND_ROLE) != "font":
            return
        family = str(item.data(FAMILY_ROLE) or "")
        if not family:
            return
        recents = [name for name in _read_list(self._store, "fonts/recent") if name != family]
        recents.insert(0, family)
        self._store.setValue("fonts/recent", recents[:MAX_RECENT])
        self._store.sync()
        self.picked.emit(family)
        self.close()

    def _toggle_favourite(self):
        family = self._highlighted_family()
        if not family:
            return
        favourites = [name for name in _read_list(self._store, "fonts/favorites") if name != family]
        if not self.favourite.isChecked():
            favourites.append(family)
        self._store.setValue("fonts/favorites", favourites)
        self._store.sync()
        self._populate()

    def _refresh(self):
        fonts.refresh()
        self._populate()
        self.refreshed.emit()

    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    row = self.list.currentRow()
                    step = 1 if key == Qt.Key.Key_Down else -1
                    while 0 <= row + step < self.list.count():
                        row += step
                        item = self.list.item(row)
                        if item.data(KIND_ROLE) == "font":
                            self.list.setCurrentRow(row)
                            break
                else:
                    self._choose(self.list.currentItem())
                return True
        return super().eventFilter(obj, event)


class FontPickerButton(QToolButton):
    """Compact font field that opens the searchable installed-font picker."""

    familyChanged = pyqtSignal(str)

    def __init__(self, family="", parent=None):
        super().__init__(parent)
        self._family = str(family or "")
        self.setProperty("role", "tool")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(90)
        self.setToolTip("Choose an installed font")
        self.clicked.connect(self.open_picker)
        self.set_family(self._family)

    def family(self):
        return self._family

    def set_family(self, family, emit=False):
        self._family = str(family or "")
        self.setText(self._display_name())
        self.setToolTip(f"Font: {self._family or 'none'} - click to choose an installed font")
        if emit:
            self.familyChanged.emit(self._family)

    def _display_name(self):
        if not self._family:
            return "Choose font"
        if not fonts.installed(self._family):
            return f"{self._family} (missing)"
        return self._family

    def open_picker(self):
        popup = _FontPopup(self._family, self)
        popup.picked.connect(self._picked)
        popup.refreshed.connect(self._fonts_refreshed)
        point = self.mapToGlobal(self.rect().bottomLeft())
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        if screen is not None:
            if point.x() + popup.width() > screen.right():
                point.setX(max(screen.left(), screen.right() - popup.width()))
            if point.y() + popup.height() > screen.bottom():
                point.setY(self.mapToGlobal(self.rect().topLeft()).y() - popup.height())
        popup.move(point)
        popup.show()

    def _picked(self, family):
        self.set_family(family, emit=True)

    def _fonts_refreshed(self):
        self.set_family(self._family)
