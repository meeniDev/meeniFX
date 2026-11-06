"""The browser: Effects, Animations and Styles in one compact, searchable place.

Three tabs, one card layout:
  * **Effects** - one card per effect, with a real thumbnail (the effect applied to the
    reference picture by :mod:`meenifx.thumbnails`);
  * **Animations** - one card per animation *family* (Slide, Fade, Pop...), not one per
    variant: the mode/direction switch below the grid picks which JSON preset is applied;
  * **Styles** - the shipped and user Looks, presented as Styles.

The view knows nothing about the timeline: it reports what the user picked and the panel
decides how to apply, remove or undo it. Applied items carry a checkmark, which is what
makes "double-click again removes it" discoverable.
"""

from __future__ import annotations

import json

from PyQt6.QtCore import (QEvent, QMimeData, QPoint, QPointF, QRectF, QSettings, QSize, Qt,
                          QTimer, pyqtSignal)
from PyQt6.QtGui import QColor, QDrag, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QMenu, QScrollArea, QTabBar, QToolButton, QVBoxLayout, QWidget)

from .. import thumbnails as THUMBS
from .. import tokens
from ..constants import EFFECT_MIME
from ..icons import icon, paint as paint_icon
from ..model import animation_families as FAMILIES
from ..model import looks as LOOKS

TABS = ("effects", "animations", "styles")
TAB_LABELS = {"effects": "Effects", "animations": "Animations", "styles": "Styles"}
RECENT_LIMIT = 12
CARD_W, CARD_H = 132, 120         # grid card (logical px)
THUMB_W, THUMB_H = 124, 84        # grid thumbnail (logical px)
LIST_H, LIST_THUMB = (48, (54, 36))

# Everyday words people type instead of the effect's own name.
KEYWORDS = {
    "mosaic": ("pixel", "pixelate", "blocks", "censor"),
    "ascii": ("text", "characters", "type", "letters"),
    "gaussian_blur": ("blur", "soft", "defocus", "haze", "smooth"),
    "box_blur": ("blur", "soft", "smooth"),
    "directional_blur": ("blur", "motion", "smear"),
    "radial_blur": ("blur", "spin", "zoom"),
    "zoom_blur": ("blur", "zoom", "speed"),
    "motion_blur": ("blur", "shutter", "smear"),
    "chromatic_aberration": ("rgb", "split", "fringe", "lens"),
    "kaleidoscope": ("mirror", "symmetry", "pattern"),
    "vhs_crt": ("retro", "tape", "scanlines", "old", "tv"),
    "film_grain": ("noise", "grain", "film", "texture"),
    "color_correct": ("color", "colour", "grade", "exposure", "saturation", "contrast"),
    "lut": ("color", "colour", "grade", "look", "cinema"),
    "glow": ("light", "bloom", "shine", "soft light"),
    "bloom_streaks": ("light", "flare", "streak", "anamorphic"),
    "light_leak": ("light", "flare", "leak", "sun"),
    "vignette": ("dark", "edges", "lens", "corner"),
    "posterize": ("colours", "colors", "reduce", "flat"),
    "halftone": ("dots", "print", "comic", "newspaper"),
    "edge_outline": ("outline", "sketch", "line", "cartoon"),
    "invert": ("negative", "flip colour"),
    "duotone": ("two colour", "colors", "poster"),
    "puppet_warp": ("warp", "pin", "deform", "bend"),
    "corner_pin": ("warp", "perspective", "corner", "screen"),
    "mesh_warp": ("warp", "grid", "deform"),
    "perspective": ("warp", "tilt", "3d"),
    "face_tracker": ("tracking", "face", "blur face", "privacy"),
    "hand_tracker": ("tracking", "hand", "gesture"),
    "blob_tracker": ("tracking", "blob", "motion", "object"),
    "glitch": ("distort", "datamosh", "error", "broken"),
    "wave_warp": ("wave", "ripple", "water", "distort"),
    "twirl": ("swirl", "spin", "distort"),
    "audio_gain": ("volume", "loud", "level", "db"),
    "audio_eq": ("equalizer", "bass", "treble"),
    "audio_reverb": ("room", "echo", "space"),
    "audio_delay": ("echo", "delay"),
}


# ------------------------------------------------------------------------------ cards
class BrowserCard(QFrame):
    """One effect / animation / style, as a tile or a compact row."""

    activated = pyqtSignal(dict)
    menu_requested = pyqtSignal(dict, QPoint)
    selected = pyqtSignal(dict)
    hover_started = pyqtSignal(dict)
    hover_stopped = pyqtSignal(dict)

    def __init__(self, item, view_mode="grid", parent=None):
        super().__init__(parent)
        self.item = item
        self.view_mode = view_mode
        self.thumb_key = None
        self._hover = False
        self.setProperty("browserCard", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(item.get("tooltip") or item["name"])
        if view_mode == "grid":
            self.setFixedSize(CARD_W, CARD_H)
            column = QVBoxLayout(self)
        else:
            self.setFixedHeight(LIST_H)
            column = QHBoxLayout(self)
        column.setContentsMargins(4, 4, 4, 4)
        column.setSpacing(tokens.SP1)

        self.thumb = QLabel(self)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if view_mode == "grid":
            self.thumb.setFixedSize(THUMB_W, THUMB_H)
        else:
            self.thumb.setFixedSize(*LIST_THUMB)
        self._placeholder()
        if item.get("kind") == "effect" and item["id"] in THUMBS.ICON_ONLY:
            # Some effects have no meaningful still preview (audio and time effects).
            # The card used to leave an empty tile; a readable, effect-specific label is
            # the last fallback the browser promises.
            if item["id"] in THUMBS.TRACKERS:
                # A tracker on a still life would look like the untouched picture, so the
                # card shows the bundled reference image with the tracker's own icon on
                # top. It never pretends a detection happened.
                self._tracker_preview()
                self.setToolTip(
                    item["name"] + " (live tracking: the reference picture has no face or "
                    "hand to detect, so the card shows the effect icon instead)")
            else:
                self._words_fallback()

        self.name = QLabel(item["name"], self)
        self.name.setStyleSheet("font-weight: 600;")
        self.badge = QLabel("\u2713", self)
        self.badge.setToolTip("Applied to the selected clip")
        self.badge.setStyleSheet(f"color: {tokens.COLORS['accent']}; font-weight: bold;")
        self.badge.setVisible(bool(item.get("applied")))
        if view_mode == "grid":
            column.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignHCenter)
            row = QHBoxLayout()
            row.setContentsMargins(2, 0, 2, 0)
            row.setSpacing(2)
            row.addWidget(self.name, 1)
            row.addWidget(self.badge, 0)
            column.addLayout(row)
            column.addStretch(1)
        else:
            text = QVBoxLayout()
            text.setContentsMargins(0, 0, 0, 0)
            text.setSpacing(0)
            text.addWidget(self.name)
            subtitle = QLabel(item.get("subtitle") or "", self)
            subtitle.setProperty("quiet", True)
            text.addWidget(subtitle)
            column.addWidget(self.thumb, 0)
            column.addLayout(text, 1)
            column.addWidget(self.badge, 0)
        if item.get("unavailable"):
            self.setEnabled(False)
            self.setToolTip(item.get("tooltip") or "This effect cannot run on the selected clip")

    # ------------------------------------------------------------------ drawing
    def _placeholder(self):
        """The resting tile: a subtle surface, drawn at the screen's pixel ratio."""
        ratio = self.devicePixelRatioF() or 1.0
        size = self.thumb.size()
        pixmap = QPixmap(max(1, int(size.width() * ratio)), max(1, int(size.height() * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
        painter.setBrush(QColor(tokens.COLORS["panel_soft"]))
        painter.drawRoundedRect(QRectF(0.5, 0.5, size.width() - 1, size.height() - 1),
                                tokens.RADIUS, tokens.RADIUS)
        painter.end()
        self.thumb.setPixmap(pixmap)

    def _words_fallback(self):
        """Spell the effect's name on the tile when no picture can be shown."""
        words = str(self.item.get("name") or self.item["id"]).split()
        self.thumb.setText("\n".join(words[:2]).upper())
        self.thumb.setWordWrap(True)
        self.thumb.setStyleSheet(
            f"color:{tokens.COLORS['muted']};font-size:10px;font-weight:600;")

    def _tracker_preview(self):
        """Fallback for trackers: the reference picture plus a detection-reticle plate.

        Nothing is drawn over the face of the reference - there is nothing detected - so
        the icon plate is the only addition and the card cannot be mistaken for a result.
        """
        size = self.thumb.size()
        try:
            image = THUMBS.reference_preview(max(16, size.width()), max(16, size.height()))
        except Exception:          # a broken custom preview must not empty the tile
            image = None
        if image is None or image.isNull():
            self._words_fallback()
            return
        pixmap = QPixmap.fromImage(image)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        plate = QRectF((pixmap.width() - 32) / 2.0, (pixmap.height() - 24) / 2.0, 32, 24)
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
        painter.setBrush(QColor(11, 13, 16, 200))
        painter.drawRoundedRect(plate, tokens.RADIUS + 1, tokens.RADIUS + 1)
        paint_icon(painter, "tracking",
                   QRectF(plate.center().x() - 8, plate.center().y() - 8, 16, 16),
                   tokens.COLORS["accent"])
        painter.end()
        self.thumb.setPixmap(pixmap)

    def set_thumbnail(self, image, ratio=1.0):
        if image is None or image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        # The image is rendered at dpr x quality pixels; this maps it back onto the card.
        pixmap.setDevicePixelRatio(max(0.5, float(ratio or 1.0)))
        self.thumb.setPixmap(pixmap)

    def set_applied(self, applied):
        self.item["applied"] = bool(applied)
        self.badge.setVisible(bool(applied))
        self.update()

    # ------------------------------------------------------------------- events
    def enterEvent(self, event):
        self._hover = True
        self.update()
        if self.isEnabled():
            self.hover_started.emit(self.item)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        self.hover_stopped.emit(self.item)
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.item)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.item)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        self.selected.emit(self.item)
        self.menu_requested.emit(self.item, event.globalPos())
        event.accept()

    def mouseMoveEvent(self, event):
        payload = self.item.get("drag_key")
        if not payload or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(EFFECT_MIME, payload.encode("utf-8"))
        drag.setMimeData(mime)
        if self.thumb.pixmap() is not None:
            drag.setPixmap(self.thumb.pixmap().scaled(
                64, 44, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        drag.exec(Qt.DropAction.CopyAction)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.item.get("applied"):
            border = QColor(tokens.COLORS["accent"])
        elif self._hover:
            border = QColor(tokens.COLORS["border"]).lighter(150)
        else:
            border = QColor(tokens.COLORS["border"])
        painter.setPen(QPen(border, 1.2))
        painter.setBrush(QColor(tokens.COLORS["panel_alt"]))
        painter.drawRoundedRect(rect, tokens.RADIUS + 1, tokens.RADIUS + 1)
        painter.end()


# ------------------------------------------------------------------------------- view
class BrowserView(QWidget):
    """Tabs + search + filters + a responsive card grid (or a compact list)."""

    apply_requested = pyqtSignal(dict)
    remove_requested = pyqtSignal(dict)
    menu_requested = pyqtSignal(QPoint)

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.tab = "effects"
        self.filter = "all"
        self.category = ""
        self.term = ""
        self.applied_keys = set()
        self.applied_presets = set()
        self.applied_looks = set()
        self.selection_media = "video"
        self.multiple_clips = False
        self._cards = []
        self._thumb_ratios = {}
        self._selected_item = None
        self._columns_now = None
        self._store = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                "meeniFX", "Studios")
        self.view_mode = str(self._store.value("browser/view", "grid") or "grid")
        self._cache = THUMBS.ThumbnailCache(engine, self)
        self._cache.ready.connect(self._thumbnail_ready)
        self._hover_card = None
        self._hover_item = None
        self._hover_static = None
        self._hover_frames = []
        self._hover_index = 0
        self._hover_steps = 0
        self._hover_cache = {}
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(380)
        self._hover_timer.timeout.connect(self._hover_start_playing)
        self._hover_play_timer = QTimer(self)
        self._hover_play_timer.setInterval(95)
        self._hover_play_timer.timeout.connect(self._hover_next_frame)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search effects")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._search_changed)

        self.favorites_button = self._toggle(icon("star", tokens.COLORS["muted"]), "Favorites")
        self.recent_button = self._toggle(icon("clock", tokens.COLORS["muted"]), "Recently used")
        self.favorites_button.clicked.connect(lambda: self._set_filter("favorites"))
        self.recent_button.clicked.connect(lambda: self._set_filter("recent"))
        self.category_combo = QComboBox()
        self.category_combo.setToolTip("Filter by category")
        self.category_combo.currentIndexChanged.connect(self._category_changed)
        self.grid_button = self._toggle(icon("grid", tokens.COLORS["muted"]), "Grid view")
        self.list_button = self._toggle(icon("list", tokens.COLORS["muted"]), "List view")
        self.grid_button.clicked.connect(lambda: self.set_view_mode("grid"))
        self.list_button.clicked.connect(lambda: self.set_view_mode("list"))
        self.menu_button = QToolButton()
        self.menu_button.setProperty("role", "icon")
        self.menu_button.setIcon(icon("dots"))
        self.menu_button.setIconSize(QSize(16, 16))
        self.menu_button.setFixedSize(QSize(28, 28))
        self.menu_button.setToolTip("Browser options")
        self.menu_button.clicked.connect(lambda: self.menu_requested.emit(
            self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height()))))

        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        for tab in TABS:
            self.tabs.addTab(TAB_LABELS[tab])
        self.tabs.currentChanged.connect(self._tab_changed)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(tokens.SP1)
        header.addWidget(self.search, 1)
        header.addWidget(self.menu_button, 0)

        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(tokens.SP1)
        filters.addWidget(self.favorites_button)
        filters.addWidget(self.recent_button)
        filters.addWidget(self.category_combo, 1)
        filters.addWidget(self.grid_button)
        filters.addWidget(self.list_button)

        self.mode_combo = QComboBox()
        self.mode_combo.setToolTip("When this animation plays")
        for key in ("in", "out", "loop", "both"):
            self.mode_combo.addItem(FAMILIES.MODE_LABELS[key], key)
        self.direction_combo = QComboBox()
        self.direction_combo.setToolTip("Direction of the animation")
        self.mode_combo.currentIndexChanged.connect(self._refresh_directions)
        self.apply_button = QToolButton()
        self.apply_button.setText("Apply")
        self.apply_button.setToolTip("Apply the chosen variant to the selected clip")
        self.apply_button.clicked.connect(self._apply_selected)
        self.options_bar = QWidget()
        options = QHBoxLayout(self.options_bar)
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(tokens.SP1)
        options.addWidget(QLabel("Mode"))
        options.addWidget(self.mode_combo, 1)
        options.addWidget(self.direction_combo, 1)
        options.addWidget(self.apply_button)
        self.options_bar.setVisible(False)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(tokens.SP1, tokens.SP1, tokens.SP1, tokens.SP4)
        self.grid.setSpacing(tokens.SP1 + 2)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.grid_host)
        self.scroll.verticalScrollBar().valueChanged.connect(self._queue_visible)
        self.scroll.viewport().installEventFilter(self)

        self.empty = QLabel("Nothing matches this search.")
        self.empty.setProperty("quiet", True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SP2, tokens.SP2, tokens.SP2, tokens.SP2)
        layout.setSpacing(tokens.SP1)
        layout.addLayout(header)
        layout.addWidget(self.tabs)
        layout.addLayout(filters)
        layout.addWidget(self.options_bar)
        layout.addWidget(self.empty)
        layout.addWidget(self.scroll, 1)

        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(30)
        self._visible_timer.timeout.connect(self._load_visible)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(0)
        self._layout_timer.timeout.connect(self._relayout)
        self._sync_buttons()
        self._sync_categories()
        self.refresh()

    # ------------------------------------------------------------------ settings
    def _favorites(self):
        value = self._store.value("browser/favorites", [])
        if isinstance(value, str):
            value = [value]
        return set(str(item) for item in (value or []))

    def _save_favorites(self, values):
        self._store.setValue("browser/favorites", sorted(values))
        self._store.sync()

    def is_favorite(self, item):
        return f"{item['kind']}:{item['id']}" in self._favorites()

    def toggle_favorite(self, item):
        favorites = self._favorites()
        key = f"{item['kind']}:{item['id']}"
        if key in favorites:
            favorites.discard(key)
        else:
            favorites.add(key)
        self._save_favorites(favorites)
        if self.filter == "favorites":
            self.refresh()

    def _recent(self):
        value = self._store.value("browser/recent", [])
        if isinstance(value, str):
            value = [value]
        return [str(item) for item in (value or [])]

    def note_recent(self, item):
        """Called when something is applied, so it is one click away afterwards."""
        if not item or not item.get("id"):
            return
        key = f"{item['kind']}:{item['id']}"
        entries = [entry for entry in self._recent() if entry != key]
        entries.insert(0, key)
        self._store.setValue("browser/recent", entries[:RECENT_LIMIT])
        self._store.sync()
        if self.filter == "recent":
            self.refresh()

    def set_view_mode(self, mode):
        self.view_mode = mode if mode in ("grid", "list") else "grid"
        self._store.setValue("browser/view", self.view_mode)
        self._store.sync()
        self._sync_buttons()
        self.refresh()

    def _toggle(self, icon_value, tip):
        button = QToolButton()
        button.setProperty("role", "icon")
        button.setIcon(icon_value)
        # One icon grid for every browser toggle (Favorites, Recent, Grid, List).
        # Leaving this implicit let some buttons inherit a larger size and drift off
        # their baseline at 125/150 % display scaling.
        button.setIconSize(QSize(16, 16))
        button.setFixedSize(QSize(28, 28))
        button.setToolTip(tip)
        button.setCheckable(True)
        return button

    def _sync_buttons(self):
        for button, active in ((self.favorites_button, self.filter == "favorites"),
                               (self.recent_button, self.filter == "recent"),
                               (self.grid_button, self.view_mode == "grid"),
                               (self.list_button, self.view_mode == "list")):
            button.blockSignals(True)
            button.setChecked(active)
            button.blockSignals(False)

    # ------------------------------------------------------------------ filtering
    def _search_changed(self, text):
        self.term = str(text or "").strip().lower()
        self.refresh()

    def _category_changed(self, _index):
        self.category = str(self.category_combo.currentData() or "")
        self.refresh()

    def _tab_changed(self, index):
        self.tab = TABS[max(0, min(index, len(TABS) - 1))]
        self.filter = "all"
        self.category = ""
        self._selected_item = None
        self.options_bar.setVisible(False)
        self.search.setPlaceholderText({"effects": "Search effects",
                                        "animations": "Search animations",
                                        "styles": "Search styles"}[self.tab])
        self._sync_buttons()
        self._sync_categories()
        self.refresh()

    def _set_filter(self, name):
        self.filter = name if self.filter != name else "all"
        self._sync_buttons()
        self.refresh()

    def _sync_categories(self):
        values = []
        if self.tab == "effects":
            values = sorted({getattr(effect, "category", "") or "Stylize"
                             for effect in self.engine.effects
                             if getattr(effect, "category", "") != "Looks"})
        elif self.tab == "animations":
            values = sorted({family["behaviour"] for family in FAMILIES.families()})
        else:
            folders = {"Your styles" if not look.get("builtin") else "Shipped styles"
                       for look in LOOKS.all_looks()}
            values = sorted(folders)
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("All categories", "")
        for value in values:
            self.category_combo.addItem(value, value)
        self.category_combo.setCurrentIndex(max(0, self.category_combo.findData(self.category)))
        self.category_combo.setVisible(self.category_combo.count() > 1)
        self.category_combo.blockSignals(False)

    # --------------------------------------------------------------------- items
    def set_context(self, applied_keys, applied_presets, applied_looks, media="video",
                    multiple=False):
        """What the selected clips already have, and what media they are."""
        self.applied_keys = set(applied_keys or ())
        self.applied_presets = set(applied_presets or ())
        # A style instance is stored as "dreamy_bloom#2"; the browser only needs the style.
        self.applied_looks = {str(item).split("#", 1)[0] for item in (applied_looks or ())}
        self.selection_media = media if media in ("video", "audio", "text") else "video"
        self.multiple_clips = bool(multiple)
        for card in self._cards:
            if card.item["kind"] == "effect":
                card.set_applied(card.item["id"] in self.applied_keys)
            elif card.item["kind"] == "style":
                card.set_applied(card.item["id"] in self.applied_looks)

    def _matches(self, text):
        return not self.term or self.term in text

    def _effect_items(self):
        items = []
        track = "audio" if self.selection_media == "audio" else "video"
        for effect in self.engine.effects:
            key = type(effect).key
            category = getattr(effect, "category", "") or "Stylize"
            if key == "look" or category == "Looks":
                continue
            words = " ".join([effect.name.lower(), key, category.lower(),
                              *KEYWORDS.get(key, ())])
            if not self._matches(words):
                continue
            if self.category and category != self.category:
                continue
            allowed = effect.supports_track_type(track)
            items.append({"kind": "effect", "id": key, "name": effect.name,
                          "subtitle": category, "applied": key in self.applied_keys,
                          "drag_key": key, "effect": effect, "unavailable": not allowed,
                          "tooltip": f"{effect.name} ({effect.media_label()}) - "
                                     f"double-click to add or remove"})
        return items

    def _animation_items(self):
        media = "text" if self.selection_media == "text" else "clip"
        items = []
        for family in FAMILIES.families():
            if media not in family["media"]:
                continue
            words = " ".join([family["name"].lower(), family["program"],
                              family["behaviour"].lower(), *family["keywords"]])
            if not self._matches(words):
                continue
            if self.category and family["behaviour"] != self.category:
                continue
            preset = FAMILIES.variant(family, media, FAMILIES.default_mode(family), "")
            items.append({"kind": "animation", "id": family["key"], "name": family["name"],
                          "subtitle": FAMILIES.summary(family), "family": family,
                          "media": media,
                          "applied": bool(preset and preset["id"] in self.applied_presets),
                          "tooltip": f"{family['name']} - choose a mode and direction, "
                                     f"then double-click to apply"})
        return items

    def _style_items(self):
        items = []
        for look in LOOKS.all_looks():
            words = " ".join([look["name"].lower(), look["id"],
                              (look.get("description") or "").lower(),
                              " ".join(entry["effect_id"].replace("_", " ")
                                       for entry in look["stack"])])
            if not self._matches(words):
                continue
            folder = "Your styles" if not look.get("builtin") else "Shipped styles"
            if self.category and folder != self.category:
                continue
            items.append({"kind": "style", "id": look["id"], "name": look["name"],
                          "subtitle": look.get("description") or "Style",
                          "applied": look["id"] in self.applied_looks, "look": look,
                          "drag_key": f"look:{look['id']}",
                          "tooltip": f"{look['name']} - double-click to apply or remove "
                                     f"this style group"})
        return items

    def _filtered(self, items):
        if self.filter == "favorites":
            favorites = self._favorites()
            return [item for item in items
                    if f"{item['kind']}:{item['id']}" in favorites]
        if self.filter == "recent":
            order = {key: index for index, key in enumerate(self._recent())}
            chosen = [item for item in items
                      if f"{item['kind']}:{item['id']}" in order]
            chosen.sort(key=lambda item: order.get(f"{item['kind']}:{item['id']}", 99))
            return chosen
        return items

    def items(self):
        if self.tab == "effects":
            return self._filtered(self._effect_items())
        if self.tab == "animations":
            return self._filtered(self._animation_items())
        return self._filtered(self._style_items())

    # ------------------------------------------------------------------ rendering
    def refresh(self):
        self._hover_stop_playing()
        items = self.items()
        self.options_bar.setVisible(self.tab == "animations" and self._selected_item is not None)
        self._cards = []
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.empty.setVisible(not items)
        self.scroll.setVisible(bool(items))
        if not items:
            self.empty.setText("Nothing here yet." if not self.term
                               else "Nothing matches this search.")
            return
        self._columns_now = self._columns()
        for index, item in enumerate(items):
            card = BrowserCard(item, self.view_mode, self.grid_host)
            card.activated.connect(self._card_activated)
            card.menu_requested.connect(self._card_menu)
            card.selected.connect(self._card_selected)
            card.hover_started.connect(lambda item, c=card: self._hover_begin(item, c))
            card.hover_stopped.connect(lambda item, c=card: self._hover_end(item, c))
            card.show()
            row, column = divmod(index, self._columns_now)
            self.grid.addWidget(card, row, column)
            self._cards.append(card)
        self.grid.setColumnStretch(self._columns_now, 1)
        # Lay out now, so the first thumbnail pass knows which cards are really on screen.
        self.grid.activate()
        self._queue_visible()

    def _columns(self):
        available = max(1, self.scroll.viewport().width() - 12)
        if self.view_mode == "list":
            return 1
        return max(1, available // (CARD_W + 12))

    def _relayout(self):
        """A dock resize only moves the cards; it must not throw their thumbnails away."""
        if not self._cards:
            return
        columns = self._columns()
        if columns == self._columns_now:
            return
        self._columns_now = columns
        for index, card in enumerate(self._cards):
            row, column = divmod(index, columns)
            self.grid.addWidget(card, row, column)
        for column in range(self.grid.columnCount()):
            self.grid.setColumnStretch(column, 1 if column == columns else 0)
        self.grid.activate()

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._layout_timer.start()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_timer.start()

    # --------------------------------------------------------------- thumbnails
    def _thumb_entries(self, item):
        if item["kind"] == "effect":
            effect = item.get("effect")
            if effect is None or item["id"] in THUMBS.ICON_ONLY:
                return None
            return [THUMBS.preview_entry(effect)]
        if item["kind"] == "style":
            return THUMBS.style_entries(item["look"])
        if item["kind"] == "animation":
            entry = THUMBS.animation_entry(item["family"], item.get("media") or "clip")
            return [entry] if entry else None
        return None

    def _card_in_view(self, card):
        """Is this card on screen? A freshly built card is not mapped yet, so its
        ``visibleRegion`` is empty until the next paint; fall back to its geometry against
        the viewport, otherwise switching tabs in a hidden dock would never load a
        preview."""
        if not card.visibleRegion().isEmpty():
            return True
        viewport = self.scroll.viewport()
        origin = card.mapTo(viewport, QPoint(0, 0))
        return QRectF(viewport.rect()).intersects(
            QRectF(float(origin.x()), float(origin.y()),
                   float(card.width()), float(card.height())))

    def _thumb_payload(self, item, entries):
        """Extra cache-key text: an animation's picture depends on its media and variant."""
        if item["kind"] == "animation":
            return json.dumps(entries, sort_keys=True, default=str)
        return ""

    def _queue_visible(self, *_args):
        if not self.isVisible() or not self._cards:
            return
        self._visible_timer.start()

    def _load_visible(self):
        if not self.isVisible():
            return
        if self._layout_timer.isActive():
            # Wait for the columns to settle: re-laying out moves every card.
            self._visible_timer.start()
            return
        ratio = ((self.devicePixelRatioF() or 1.0)
                 * max(1, int(self._cache.settings.scale())))
        width, height = ((THUMB_W, THUMB_H) if self.view_mode == "grid" else LIST_THUMB)
        size = (max(16, int(round(width * ratio))), max(16, int(round(height * ratio))))
        for card in self._cards:
            if card.thumb_key is not None or not self._card_in_view(card):
                continue
            entries = self._thumb_entries(card.item)
            if entries is None:
                continue
            payload = self._thumb_payload(card.item, entries)
            image = self._cache.image(card.item["kind"], card.item["id"], payload, entries, *size)
            card.thumb_key = self._cache.key(card.item["kind"], card.item["id"], payload,
                                             *size, 0)
            self._thumb_ratios[card.thumb_key] = ratio
            if image is not None:
                card.set_thumbnail(image, ratio)

    def _thumbnail_ready(self, key, image):
        for card in self._cards:
            if card.thumb_key == key:
                card.set_thumbnail(image, self._thumb_ratios.get(key, 1.0))

    # ---------------------------------------------------------- hover previews
    def _hover_begin(self, item, card):
        """A short hover delay, then sample the real preset only for animation cards."""
        if item.get("kind") != "animation" or self.tab != "animations" or not self.isVisible():
            return
        if getattr(self.window(), "_playing", False):
            return
        self._hover_stop_playing()
        self._hover_card, self._hover_item = card, item
        self._hover_static = card.thumb.pixmap().copy()
        self._hover_timer.start()

    def _hover_end(self, item, card):
        if card is self._hover_card:
            self._hover_stop_playing()

    def _hover_stop_playing(self):
        self._hover_timer.stop()
        self._hover_play_timer.stop()
        if self._hover_card is not None and self._hover_static is not None:
            self._hover_card.thumb.setPixmap(self._hover_static)
        self._hover_card = None
        self._hover_item = None
        self._hover_static = None
        self._hover_frames = []

    def _hover_start_playing(self):
        card, item = self._hover_card, self._hover_item
        if card is None or item is None or not self.isVisible():
            return
        if getattr(self.window(), "_playing", False):
            self._hover_stop_playing()
            return
        ratio = ((self.devicePixelRatioF() or 1.0)
                 * max(1, int(self._cache.settings.scale())))
        width = max(16, int(round(THUMB_W * ratio)))
        height = max(16, int(round(THUMB_H * ratio)))
        entry = THUMBS.animation_entry(item.get("family"), item.get("media") or "clip")
        preset = (entry or {}).get("animation") or item.get("animation")
        if not preset:
            return
        key = (item["id"], str(preset.get("id")), width, height)
        frames = self._hover_cache.get(key)
        if frames is None:
            frames = []
            for fraction in (0.04, 0.22, 0.4, 0.58, 0.76, 0.94):
                image = THUMBS.render_animation_frame(
                    self.engine, preset, width, height, self._cache.settings, fraction)
                if image is None:
                    frames = []
                    break
                frames.append(THUMBS.to_qimage(image))
            if frames:
                self._hover_cache = {key: frames}      # one small sequence at a time
        if not frames:
            return
        self._hover_frames = frames
        self._hover_index = 0
        self._hover_steps = 0
        card.set_thumbnail(frames[0], ratio)
        self._hover_play_timer.start()

    def _hover_next_frame(self):
        card = self._hover_card
        if card is None or not self._hover_frames:
            self._hover_stop_playing()
            return
        self._hover_index = (self._hover_index + 1) % len(self._hover_frames)
        card.set_thumbnail(self._hover_frames[self._hover_index],
                           (self.devicePixelRatioF() or 1.0)
                           * max(1, int(self._cache.settings.scale())))
        self._hover_steps += 1
        if self._hover_steps >= len(self._hover_frames) * 2:
            # Two short loops is enough to read the motion; go back to the static
            # thumbnail instead of running an animation for as long as the pointer sits.
            self._hover_stop_playing()

    def shutdown(self):
        """Stop thumbnail work: called when the window or the panel goes away."""
        self._hover_timer.stop()
        self._hover_play_timer.stop()
        self._visible_timer.stop()
        self._layout_timer.stop()
        self._cache.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def hideEvent(self, event):
        # Rendering stops with the panel: it must never compete with playback.
        self._hover_stop_playing()
        self._visible_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._queue_visible()

    # -------------------------------------------------------------------- actions
    def _card_activated(self, item):
        if item.get("unavailable"):
            return
        if item["kind"] == "animation":
            self._card_selected(item)
            self._apply_selected()
            return
        if item.get("applied"):
            self.remove_requested.emit(item)
        else:
            self.apply_requested.emit(item)

    def _card_selected(self, item):
        self._selected_item = item
        if item.get("kind") != "animation":
            self.options_bar.setVisible(False)
            return
        family = item["family"]
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        for mode in family["modes"] or ["in"]:
            self.mode_combo.addItem(FAMILIES.MODE_LABELS[mode], mode)
        if "in" in family["modes"] and "out" in family["modes"]:
            self.mode_combo.addItem(FAMILIES.MODE_LABELS["both"], "both")
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(
            FAMILIES.default_mode(family))))
        self.mode_combo.blockSignals(False)
        self._refresh_directions()
        self.options_bar.setVisible(True)

    def _refresh_directions(self, *_args):
        """Out and In can have different directions: offer only the ones that exist."""
        if self._selected_item is None or self._selected_item.get("kind") != "animation":
            return
        family = self._selected_item["family"]
        media = "text" if self.selection_media == "text" else "clip"
        mode = self.mode_combo.currentData() or "in"
        previous = self.direction_combo.currentData() or ""
        values = FAMILIES.directions(family, media, mode) or family.get("directions") or []
        self.direction_combo.blockSignals(True)
        self.direction_combo.clear()
        for value in values:
            self.direction_combo.addItem(value.title(), value)
        if not values:
            self.direction_combo.addItem("None", "")
        self.direction_combo.setCurrentIndex(max(0, self.direction_combo.findData(previous)))
        self.direction_combo.blockSignals(False)
        self.direction_combo.setVisible(bool(values))

    def animation_choice(self):
        """{family, mode, direction, media} for the selected animation card."""
        if self._selected_item is None or self._selected_item.get("kind") != "animation":
            return None
        return {"family": self._selected_item["family"],
                "mode": self.mode_combo.currentData() or "in",
                "direction": self.direction_combo.currentData() or "",
                "media": "text" if self.selection_media == "text" else "clip"}

    def _apply_selected(self):
        if self._selected_item is None:
            return
        if self._selected_item["kind"] == "animation":
            choice = self.animation_choice()
            if choice:
                self.apply_requested.emit({**self._selected_item, **choice, "explicit": True})
        elif self._selected_item.get("applied"):
            self.remove_requested.emit(self._selected_item)
        else:
            self.apply_requested.emit(self._selected_item)

    def _card_menu(self, item, position):
        self._card_selected(item)
        menu = QMenu(self)
        favorite = "Remove from favorites" if self.is_favorite(item) else "Add to favorites"
        menu.addAction(favorite, lambda: self.toggle_favorite(item))
        menu.addSeparator()
        if item["kind"] == "effect":
            menu.addAction("Add to selected clip",
                           lambda: self.apply_requested.emit(item))
            if item.get("applied"):
                menu.addAction("Add another instance",
                               lambda: self.apply_requested.emit({**item, "another": True}))
                menu.addAction("Remove from selected clip",
                               lambda: self.remove_requested.emit(item))
        elif item["kind"] == "style":
            menu.addAction("Apply style", lambda: self.apply_requested.emit(item))
            if item.get("applied"):
                menu.addAction("Remove style", lambda: self.remove_requested.emit(item))
        else:
            choice = self.animation_choice() or {}
            menu.addAction("Apply animation",
                           lambda: self.apply_requested.emit({**item, **choice,
                                                              "explicit": True}))
        menu.addSeparator()
        menu.addAction("Change preview image\u2026",
                       lambda: self.menu_requested.emit(position))
        menu.exec(position)

    def reload_previews(self):
        """A new reference image / background / quality: regenerate every thumbnail."""
        self._cache.reload_settings()
        self._thumb_ratios.clear()
        for card in self._cards:
            card.thumb_key = None
        self.refresh()

    def refresh_previews(self):
        """Re-check the applied marks without rebuilding the grid."""
        states = {f"{card.item['kind']}:{card.item['id']}": card for card in self._cards}
        for key, card in states.items():
            kind, item_id = key.split(":", 1)
            if kind == "effect":
                card.set_applied(item_id in self.applied_keys)
            elif kind == "style":
                card.set_applied(item_id in self.applied_looks)
