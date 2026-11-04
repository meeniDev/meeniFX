"""Effects browser and Properties editor.

They are two panels, each in its own dock: the browser is a searchable tree of
collapsible categories, and the inspector (``props_scroll``) shows one "Clip" card
followed by one card per effect; each card header has a collapse arrow,
an enable toggle, the name, Reset and a "..." menu, and can be dragged to reorder.
Rows come from each effect's `PARAMS` table, so a control can never write to a
different key than the effect reads. Only numeric parameters get keyframe controls.
"""

import copy

from PyQt6.QtCore import QMimeData, QPoint, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QFont
from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QScrollArea,
                             QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from .. import tokens
from ..constants import EFFECT_MIME
from ..icons import icon
from ..model import animation as ANIM
from ..model import channels as CH
from ..model import text as TXT
from ..model import transform as T
from ..model.keyframes import AnimatedValue, parameter_value_at
from .widgets import SliderField

CARD_MIME = "application/x-meenifx-card"
# Effects panel order: everyday picture work first, technical/motion tools after.
CATEGORY_ORDER = ("Video Effects", "Color Correction", "Blur & Sharpen", "Glow & Light",
                  "Transform", "Distortion & Warp", "Warp", "Text Effects", "Audio Effects",
                  "Stylize", "Blur and Sharpen", "Color", "Distort", "Tracking")
LABEL_W = 70
ROTATE_STEP = 15.0      # degrees per step when rotation snapping is on
COLOR_NAMES = ("color_r", "color_g", "color_b")


def qty(clip, meta):
    """A usable slider span for a channel whose true range is unbounded."""
    if meta.get("normalized"):
        return 1.0
    unit = meta.get("unit")
    if unit == "px":
        return 400.0
    if unit == "%":
        return 200.0
    if unit == "\u00b0":
        return 180.0
    return max(1.0, abs(float(meta.get("maximum", 100.0))))


def _tool(icon_name, tip, checkable=False):
    button = QToolButton()
    button.setProperty("role", "icon")
    button.setIcon(icon(icon_name))
    button.setToolTip(tip)
    button.setCheckable(checkable)
    button.setFixedSize(tokens.HIT, tokens.HIT)
    return button


def _swatch_button(color, tip):
    button = QToolButton()
    button.setFixedSize(40, 20)
    button.setToolTip(tip)
    set_swatch(button, color)
    return button


def set_swatch(button, color):
    color = QColor(color)
    button.setProperty("color", color.name())
    button.setStyleSheet(f"QToolButton {{ background: {color.name()}; border: 1px solid "
                         f"{tokens.COLORS['border']}; border-radius: {tokens.RADIUS}px; }}")


class _CardHeader(QWidget):
    """Header strip that starts a card-reorder drag when pulled."""

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self._press = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is not None and (event.position().toPoint() - self._press).manhattanLength() \
                >= QApplication.startDragDistance():
            self._press = None
            mime = QMimeData()
            mime.setData(CARD_MIME, self.key.encode("utf-8"))
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.setPixmap(self.parentWidget().grab())
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)


class _CardHost(QWidget):
    """Container of cards; accepts a dragged card and reports the new index."""

    card_dropped = pyqtSignal(str, int)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(CARD_MIME):
            event.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        key = bytes(event.mimeData().data(CARD_MIME)).decode("utf-8")
        y = event.position().y()
        index = 0
        for child in self.findChildren(QFrame, options=Qt.FindChildOption.FindDirectChildrenOnly):
            if child.property("effect_key") and child.property("effect_key") != key \
                    and child.geometry().center().y() < y:
                index += 1
        self.card_dropped.emit(key, index)
        event.acceptProposedAction()


class _EffectTree(QTreeWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setHeaderHidden(True)
        self.setIndentation(12)
        self.setDragEnabled(True)
        self.setRootIsDecorated(True)
        self.setExpandsOnDoubleClick(False)
        self.setIconSize(self.iconSize().expandedTo(self.iconSize()))

    def startDrag(self, supported_actions):
        item = self.currentItem()
        key = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not key:
            return
        mime = QMimeData()
        mime.setData(EFFECT_MIME, key.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class EffectsPanel(QWidget):
    """Effect browser. The property cards live in ``props_scroll``, a dock of their own.

    Both halves stay on this object (``refresh_properties``, ``properties_layout``,
    ``_rows``) so there is still exactly one inspector; only the widget tree is split, so
    the two docks can be moved, hidden and tabbed independently.
    """

    effects_changed = pyqtSignal()
    # "Preview" in the text animation section: play this frame range in the window.
    preview_requested = pyqtSignal(int, int)
    # An effect was added: the host should bring the Properties panel forward.
    properties_requested = pyqtSignal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search effects")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.refresh_effect_list)
        self.effect_tree = _EffectTree(self)
        self.effect_tree.itemDoubleClicked.connect(self._on_double_click)
        self.effect_tree.setToolTip("Double-click or drag an effect onto a clip to add it")
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(tokens.SP2, tokens.SP2, tokens.SP2, tokens.SP2)
        page_layout.setSpacing(tokens.SP2)
        page_layout.addWidget(self.search)
        page_layout.addWidget(self.effect_tree, 1)
        self.properties = _CardHost()
        self.properties.card_dropped.connect(self._card_dropped)
        self.properties_layout = QVBoxLayout(self.properties)
        self.properties_layout.setContentsMargins(tokens.SP2, tokens.SP2, tokens.SP2, tokens.SP2)
        self.properties_layout.setSpacing(tokens.SP2)
        self.properties_layout.addStretch(1)
        self.props_scroll = QScrollArea()
        self.props_scroll.setWidgetResizable(True)
        self.props_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.props_scroll.setWidget(self.properties)
        # Parentless on purpose: MainWindow puts it straight into the Properties dock.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(page)
        self._rows = []
        self._built_key = None
        self._edit_before = None
        self._suspend_sync = False
        self._collapsed = set()
        self._clipboard = None
        self.refresh_effect_list()

    # ------------------------------------------------------------ effect list
    def _categories(self):
        groups = {}
        for effect in self.engine.effects:
            groups.setdefault(getattr(effect, "category", "Stylize"), []).append(effect)
        ordered = [name for name in CATEGORY_ORDER if name in groups]
        return [(name, groups[name]) for name in ordered + sorted(set(groups) - set(ordered))]

    def refresh_effect_list(self, *_):
        term = self.search.text().strip().lower()
        collapsed = {self.effect_tree.topLevelItem(i).text(0)
                     for i in range(self.effect_tree.topLevelItemCount())
                     if not self.effect_tree.topLevelItem(i).isExpanded()}
        self.effect_tree.clear()
        clips = self._selected_clips()
        kinds = {self.engine.track_type_of(clip) for clip in clips}
        added = set.intersection(*[{e["effect_id"] for e in c.effects} for c in clips]) if clips else set()
        bold = QFont(self.font())
        bold.setBold(True)
        for category, effects in self._categories():
            matches = [e for e in effects if not term or term in e.name.lower() or term in e.key.lower()]
            if not matches:
                continue
            top = QTreeWidgetItem([category])
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            top.setFont(0, bold)
            self.effect_tree.addTopLevelItem(top)
            for effect in matches:
                is_added = effect.key in added
                # Enabled when it can run on at least one selected clip: a linked
                # video+audio pair applies the picture effect to the video member only.
                allowed = not kinds or any(effect.supports_track_type(kind) for kind in kinds)
                item = QTreeWidgetItem([effect.name if allowed
                                        else f"{effect.name}  ({effect.media_label()} only)"])
                item.setData(0, Qt.ItemDataRole.UserRole, effect.key)
                if not allowed:
                    # Clearly unavailable, not hidden: the name stays searchable and the
                    # tooltip explains why it cannot be added to this clip.
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                    item.setForeground(0, QColor(tokens.COLORS["muted"]))
                    item.setToolTip(0, f"{effect.media_label().capitalize()}-only effect: "
                                       f"the selected clip is {'audio' if 'audio' in kinds else 'video'}")
                    top.addChild(item)
                    continue
                item.setToolTip(0, "Added to the selected clip" if is_added
                                else "Double-click or drag onto a clip to add")
                if is_added:
                    item.setIcon(0, icon("check", tokens.COLORS["accent"]))
                    item.setForeground(0, QColor(tokens.COLORS["accent"]))
                top.addChild(item)
            top.setExpanded(bool(term) or category not in collapsed)

    def _selected_clips(self):
        found = (self.engine.timeline.find_clip(i) for i in self.engine.session.selected)
        return [clip for clip in found if clip is not None]

    def _on_double_click(self, item, _column=0):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key:
            self.add_effect(key)
        else:
            item.setExpanded(not item.isExpanded())

    def add_effect(self, effect_id):
        clips = self._selected_clips()
        effect = next((e for e in self.engine.effects if e.key == effect_id), None)
        if not clips or effect is None:
            return
        # The engine decides compatibility, so the list, a drag and a double-click all
        # obey the same declaration.
        targets = [clip for clip in clips if self.engine.clip_accepts_effect(clip, effect_id)]
        if not targets:
            kind = self.engine.track_type_of(clips[0])
            self.engine.session.status.emit(
                f"{effect.name} is a {effect.media_label()}-only effect - it cannot be added "
                f"to {'an' if kind == 'audio' else 'a'} {kind} clip")
            return
        clips = targets

        def add(clip):
            if not any(entry["effect_id"] == effect_id for entry in clip.effects):
                clip.effects.append({"effect_id": effect_id, "enabled": True,
                                     "params": copy.deepcopy(effect.capture_params())})
        self.engine.session.apply_to_clips([c.id for c in clips], "Add effect", add)
        self.properties_requested.emit()
        self.refresh_properties()

    # --------------------------------------------------------------- properties
    def _structure_key(self, clips):
        if not clips:
            return ("none",)
        clip = clips[0]
        effects = tuple((id(entry), entry["effect_id"], entry.get("enabled", True),
                         tuple(sorted((name, isinstance(value, AnimatedValue))
                                      for name, value in entry.get("params", {}).items())),
                         tuple(sorted((pin.get("id"), isinstance(pin.get("x"), AnimatedValue),
                                       bool(pin.get("locked")))
                                      for pin in entry.get("pins") or ())))
                        for entry in clip.effects)
        transform = T.ensure_transform(clip)
        animated = tuple(sorted((name, isinstance(transform.get(name), AnimatedValue))
                                for name in T.TRANSFORM_CHANNELS))
        flags = (bool(transform.get("uniform")), bool(transform.get("flip_h")),
                 bool(transform.get("flip_v")))
        gain = isinstance(clip.gain, AnimatedValue)
        text_animation = ()
        if clip.is_text:
            animation = (clip.text or {}).get("animation") or {}
            text_animation = tuple(sorted((slot, meta.get("preset"), meta.get("duration"))
                                          for slot, meta in animation.items()
                                          if isinstance(meta, dict)))
        return (tuple(sorted(c.id for c in clips)), clip.id, clip.name, effects, animated, flags,
                gain, text_animation)

    def refresh_properties(self):
        """Rebuild only when the shape changed; otherwise just follow the playhead."""
        clips = self._selected_clips()
        key = self._structure_key(clips)
        self.refresh_effect_list()
        if key == self._built_key:
            self.sync_playhead()
            return
        self._built_key = key
        self._rows = []
        while self.properties_layout.count():
            item = self.properties_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if not clips:
            empty = QLabel("Select a clip")
            empty.setProperty("quiet", True)
            self.properties_layout.addWidget(empty)
            self.properties_layout.addStretch(1)
            return
        clip = clips[0]
        self._clip_card(clip)
        if clip.track is not None and clip.track.type == "video":
            if clip.is_text:
                self._text_card(clip)
            self._transform_card(clip)
        elif clip.track is not None and clip.track.type == "audio":
            self._audio_card(clip)
        for index, entry in enumerate(clip.effects):
            effect = next((e for e in self.engine.effects if e.key == entry["effect_id"]), None)
            if effect is not None:
                self._effect_card(clip, index, effect, entry)
        self.properties_layout.addStretch(1)
        self.sync_playhead()

    def _clip(self):
        clips = self._selected_clips()
        return clips[0] if clips else None

    def _clip_frame(self, clip):
        return max(0, int(self.engine.session.playhead) - int(clip.timeline_start))


    # --------------------------------------------------------------- transform
    def _clip_media_size(self, clip):
        """Picture size used by Fill Frame; generated clips are the project frame."""
        entry = self.engine.media._entries.get(clip.source_path) if clip.source_path else None
        if entry is not None and getattr(entry, "frames", None):
            height, width = entry.frames[0].shape[:2]
            return int(width), int(height)
        size = self.engine.project_size()
        if size and size[0] and size[1]:
            return int(size[0]), int(size[1])
        return 1920, 1080

    def _transform_card(self, clip):
        """The professional Transform section: precise values, keyframes and quick actions."""
        ensure = T.ensure_transform(clip)
        frame, outer = self._card("transform")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Transform")
        title.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)
        auto = QToolButton()
        auto.setProperty("role", "icon")
        auto.setIcon(icon("auto_key"))
        auto.setCheckable(True)
        auto.setChecked(bool(self.engine.session.auto_keyframe.get()))
        auto.setToolTip("Auto-Keyframe: changing an animated value at a new time creates a keyframe. "
                        "Off: the whole curve shifts instead, so nothing is keyed silently")
        auto.toggled.connect(lambda on: self.engine.session.auto_keyframe.set(bool(on)))
        header.addWidget(auto)
        reset = _tool("reset", "Reset the whole transform")
        reset.clicked.connect(lambda _=False, c=clip: self._reset_transform(c))
        header.addWidget(reset)
        outer.addLayout(header)
        body, form = self._form()

        for name in ("position_x", "position_y", "scale_x", "scale_y", "rotation",
                     "anchor_x", "anchor_y"):
            self._channel_row(form, clip, CH.transform_channel(name))
        self._anchor_grid(form, clip)
        self._section_label(form, "Compositing")
        # The universal Clip Opacity: the final transparency of the whole visual clip.
        # It is keyframeable like every other channel and is never a text/effect opacity.
        self._channel_row(form, clip, CH.transform_channel("opacity"))

        # Keyboard gestures and inspector switches are the same thing: Shift = keep the
        # aspect ratio, Alt = scale from the centre, Shift while rotating = 15 degree
        # steps. Each has a switch here so nothing depends on remembering a modifier.
        switches = QWidget()
        switch_column = QVBoxLayout(switches)
        switch_column.setContentsMargins(0, 0, 0, 0)
        switch_column.setSpacing(2)
        for label, flag, tip in (
                ("Lock aspect ratio", "uniform",
                 "Corner handles keep the picture's proportions - hold Shift while dragging"),
                ("Scale from center", "scale_from_center",
                 "Scale around the middle of the picture - hold Alt while dragging"),
                ("Snap rotation", "snap_rotation",
                 f"Rotation lands on {int(ROTATE_STEP)} degree steps - hold Shift while rotating")):
            box = QCheckBox(label)
            box.setChecked(bool(ensure.get(flag, flag != "scale_from_center")))
            box.setToolTip(tip)
            box.toggled.connect(lambda on, c=clip, f=flag: self._set_transform_flag(c, f, on))
            switch_column.addWidget(box)
        form.addWidget(switches)

        flips = QWidget()
        flip_row = QHBoxLayout(flips)
        flip_row.setContentsMargins(0, 0, 0, 0)
        flip_row.setSpacing(tokens.SP2)
        for axis, label, icon_name, tip in (("h", "Flip H", "flip_h", "Mirror the picture horizontally"),
                                            ("v", "Flip V", "flip_v", "Mirror the picture vertically")):
            button = QToolButton()
            button.setText(label)
            button.setIcon(icon(icon_name))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.setChecked(bool(ensure.get("flip_" + axis)))
            button.setToolTip(tip)
            button.toggled.connect(lambda on, a=axis, c=clip: self._set_flip(c, a, on))
            flip_row.addWidget(button)
        flip_row.addStretch(1)
        form.addWidget(flips)

        actions = QWidget()
        action_row = QHBoxLayout(actions)
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(tokens.SP2)
        for label, icon_name, tip, callback in (
                ("Fit", "fit_frame", "Fit the clip inside the frame",
                 lambda c=clip: self.engine.session.fit_frame([c.id])),
                ("Fill", "fill_frame", "Scale the clip to cover the whole frame",
                 lambda c=clip: self.engine.session.fill_frame(
                     [c.id], lambda clip: self._clip_media_size(clip))),
                ("Center", "center", "Put the anchor back on the frame centre",
                 lambda c=clip: self.engine.session.center_in_frame([c.id]))):
            button = QToolButton()
            button.setText(label)
            button.setIcon(icon(icon_name))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, cb=callback: self._guarded(cb))
            action_row.addWidget(button)
        action_row.addStretch(1)
        form.addWidget(actions)
        outer.addWidget(body)
        self.properties_layout.addWidget(frame)
        self._rows.append({"kind": "transform_flags", "clip_id": clip.id})

    @staticmethod
    def _section_label(form, text):
        """Small heading that tells the user which group of properties follows."""
        label = QLabel(text)
        label.setProperty("quiet", True)
        label.setStyleSheet("font-weight: 600; padding-top: 4px;")
        form.addWidget(label)
        return label

    def _anchor_grid(self, form, clip):
        """3x3 anchor presets plus a dedicated Center button (keeps the picture in place)."""
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(tokens.SP1)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        for index, (key, label, _fx, _fy) in enumerate(T.ANCHOR_PRESETS):
            button = QToolButton()
            button.setText("\u25cf")
            button.setFixedSize(22, 20)
            button.setToolTip(f"Anchor: {label}")
            button.clicked.connect(lambda _=False, preset=key, c=clip:
                                   self._set_anchor_preset(c, preset))
            grid.addWidget(button, index // 3, index % 3)
        grid.setColumnStretch(3, 1)
        column.addLayout(grid)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(tokens.SP2)
        center = QToolButton()
        center.setText("Center Anchor Point")
        center.setToolTip("Put the anchor back in the middle of the picture")
        center.clicked.connect(lambda _=False, c=clip: self._set_anchor_preset(c, "center"))
        keep = QCheckBox("Keep picture in place")
        keep.setChecked(bool(self.engine.session.keep_anchor_position.get()))
        keep.setToolTip("Compensate the position so the picture does not jump when the anchor moves")
        keep.toggled.connect(lambda on: self.engine.session.keep_anchor_position.set(bool(on)))
        actions.addWidget(center)
        actions.addWidget(keep)
        actions.addStretch(1)
        column.addLayout(actions)
        form.addWidget(self._labelled("Anchor", holder,
                                      "Presets for the anchor point; the anchor can also be "
                                      "dragged in the preview and keyframed"))

    def _set_anchor_preset(self, clip, preset):
        keep = bool(self.engine.session.keep_anchor_position.get())
        self.engine.session.set_anchor_preset(clip.id, preset, keep)
        self.refresh_properties()

    # ---------------------------------------------------------------------- text
    def _text_card(self, clip):
        """The text generator: words, typography and its own keyframes.

        The words are a held channel, so keying them at the playhead changes what the clip
        says later without splitting it; every numeric control below is a normal animated
        channel and gets the same diamonds and stopwatches as the transform.
        """
        settings = TXT.ensure_text(clip)
        frame = self._clip_frame(clip)
        card, outer = self._card("text")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Text")
        title.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)
        key = _tool("key", "Add or remove a text keyframe at the playhead")
        key.setCheckable(True)
        key.setChecked(TXT.has_content_key(clip, frame))
        key.clicked.connect(lambda _=False, c=clip: self._toggle_text_key(c))
        header.addWidget(key)
        outer.addLayout(header)

        body, form = self._form()
        editor = QPlainTextEdit()
        editor.setPlainText(TXT.content_at(clip, frame))
        editor.setFixedHeight(64)
        editor.setTabChangesFocus(True)
        editor.setToolTip("The words on screen. With Auto-Keyframe on (or once the text is "
                          "already keyed) editing here keys the new words at the playhead, so "
                          "one clip can change text without being cut")
        editor.textChanged.connect(lambda c=clip, e=editor: self._edit_text(c, e))
        form.addWidget(self._labelled("Words", editor))
        hint = QLabel(f"Text keyframes: {len(TXT.content_keys(clip))}")
        hint.setProperty("quiet", True)
        hint.setToolTip("Every key changes the words from its frame on (held, not interpolated)")
        form.addWidget(hint)

        font_row = QWidget()
        font_layout = QHBoxLayout(font_row)
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.setSpacing(tokens.SP2)
        family = QComboBox()
        family.addItems(TXT.FAMILIES)
        family.setCurrentText(str(settings.get("font") or TXT.DEFAULTS["font"]))
        family.currentTextChanged.connect(
            lambda name, c=clip: self._set_text_setting(c, "font", name, "Text font"))
        font_layout.addWidget(family, 1)
        for name, label, tip in (("bold", "B", "Bold"), ("italic", "I", "Italic"),
                                 ("underline", "U", "Underline")):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(bool(settings.get(name)))
            button.setToolTip(tip)
            button.setFixedWidth(26)
            button.toggled.connect(
                lambda on, key_name=name, c=clip: self._set_text_setting(
                    c, key_name, bool(on), "Text style"))
            font_layout.addWidget(button)
        form.addWidget(self._labelled("Font", font_row))

        align = QComboBox()
        align.addItems([name.title() for name in TXT.ALIGNMENTS])
        align.setCurrentText(str(settings.get("align") or "center").title())
        align.currentTextChanged.connect(
            lambda name, c=clip: self._set_text_setting(c, "align", name.lower(), "Text alignment"))
        form.addWidget(self._labelled("Align", align))

        colours = QWidget()
        colour_row = QHBoxLayout(colours)
        colour_row.setContentsMargins(0, 0, 0, 0)
        colour_row.setSpacing(tokens.SP2)
        for label, name in (("Fill", "color"), ("Stroke", "stroke_color"),
                            ("Shadow", "shadow_color"), ("Back", "bg_color")):
            colour_row.addWidget(QLabel(label))
            self._text_swatch(colour_row, clip, name)
        colour_row.addStretch(1)
        form.addWidget(colours)

        # Each group is labelled so no opacity can be mistaken for the clip's own.
        self._section_label(form, "Typography")
        for name in ("size", "stroke_width", "letter_spacing", "line_spacing", "wrap",
                     "offset_x", "offset_y"):
            self._channel_row(form, clip, CH.text_channel(name))
        self._section_label(form, "Shadow (this opacity affects the shadow only)")
        for name in ("shadow_opacity", "shadow_offset_x", "shadow_offset_y", "shadow_blur"):
            self._channel_row(form, clip, CH.text_channel(name))
        self._section_label(form, "Background plate")
        for name in ("bg_opacity", "bg_padding"):
            self._channel_row(form, clip, CH.text_channel(name))
        self._section_label(form, "Glyphs (fill opacity, blur, per-character reveal)")
        for name in ("fill_opacity", "blur", "reveal"):
            self._channel_row(form, clip, CH.text_channel(name))
        self._text_animation_section(form, clip)
        outer.addWidget(body)
        self.properties_layout.addWidget(card)
        self._rows.append({"kind": "text", "clip_id": clip.id, "editor": editor})

    def _text_animation_section(self, form, clip):
        """In/Out animation presets for a text clip, built on real keyframes.

        Every preset writes ordinary keys on the transform/text channels, so the result
        can be edited, re-eased or deleted from the timeline and the Graph Editor - the
        preset is a starting point, not a hidden visual effect.
        """
        self._section_label(form, "Animation")
        settings = TXT.ensure_text(clip)
        animation = settings.get("animation") if isinstance(settings.get("animation"), dict) else {}
        fps = self.engine.timeline.fps
        default = ANIM.default_duration(fps, clip.duration)
        meta = next((animation.get(slot) for slot in ("in", "out")
                     if isinstance(animation.get(slot), dict)), {})

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(tokens.SP2)
        in_combo = QComboBox()
        in_combo.addItems(["None", *ANIM.TEXT_IN_PRESETS])
        in_combo.setCurrentText((animation.get("in") or {}).get("preset") or "None")
        out_combo = QComboBox()
        out_combo.addItems(["None", *ANIM.TEXT_OUT_PRESETS])
        out_combo.setCurrentText((animation.get("out") or {}).get("preset") or "None")
        row_layout.addWidget(QLabel("In"))
        row_layout.addWidget(in_combo, 1)
        row_layout.addWidget(QLabel("Out"))
        row_layout.addWidget(out_combo, 1)
        form.addWidget(self._labelled("Animate", row, "In and Out animation for this text clip"))

        duration = SliderField("Duration", int(meta.get("duration", default)), 1,
                               max(1, clip.duration), default=default, step=1, unit=" f",
                               soft_min=1, soft_max=max(1, clip.duration // 2),
                               label_width=LABEL_W, box_width=56,
                               tooltip="Length of the in/out animation, in frames")
        delay = SliderField("Delay", int(meta.get("delay", 0)), 0,
                            max(0, clip.duration - 1), default=0, step=1, unit=" f",
                            soft_min=0, soft_max=max(1, clip.duration // 2),
                            label_width=LABEL_W, box_width=56,
                            tooltip="Frames to wait before the animation starts")
        easing = QComboBox()
        from ..model import easing as EASING
        easing.addItems(list(EASING.all_preset_names()))
        current_easing = str(meta.get("easing") or ANIM.DEFAULT_EASING)
        if easing.findText(current_easing) >= 0:
            easing.setCurrentText(current_easing)
        form.addWidget(duration)
        form.addWidget(delay)
        form.addWidget(self._labelled("Easing", easing, "Curve applied to the preset's keyframes"))

        actions = QWidget()
        action_row = QHBoxLayout(actions)
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(tokens.SP2)
        preview = QToolButton()
        preview.setText("Preview")
        preview.setToolTip("Play this clip to check the animation")
        preview.clicked.connect(lambda _=False, c=clip: self.preview_requested.emit(
            int(c.timeline_start), int(c.end)))
        clear = QToolButton()
        clear.setText("Clear")
        clear.setToolTip("Remove both animation presets (the keyframes they generated are removed)")
        clear.clicked.connect(lambda _=False, c=clip: self._clear_text_animation(c))
        action_row.addWidget(preview)
        action_row.addWidget(clear)
        action_row.addStretch(1)
        form.addWidget(actions)

        def apply_slot(slot):
            combo = in_combo if slot == "in" else out_combo
            self.engine.session.apply_text_animation(
                clip.id, slot, combo.currentText(), int(round(duration.value())),
                easing.currentText(), int(round(delay.value())))
            self.refresh_properties()

        def apply_all():
            for slot in ("in", "out"):
                apply_slot(slot)

        def later(callback):
            # The card is rebuilt by the callback, so never delete the sender mid-signal.
            QTimer.singleShot(0, callback)

        in_combo.currentTextChanged.connect(lambda _text: later(lambda: apply_slot("in")))
        out_combo.currentTextChanged.connect(lambda _text: later(lambda: apply_slot("out")))
        duration.committed.connect(lambda _value: later(apply_all))
        delay.committed.connect(lambda _value: later(apply_all))
        easing.currentTextChanged.connect(lambda _text: later(apply_all))

        generated = sum(len(times) for slot in ("in", "out")
                        for times in ((animation.get(slot) or {}).get("keys") or {}).values())
        hint = QLabel(f"Keyframes generated: {generated}")
        hint.setProperty("quiet", True)
        hint.setToolTip("These are real keyframes: edit, retime or re-ease them in the timeline "
                        "or the Graph Editor")
        form.addWidget(hint)

    def _clear_text_animation(self, clip):
        for slot in ("in", "out"):
            self.engine.session.apply_text_animation(clip.id, slot, "None")
        self.refresh_properties()

    def _audio_card(self, clip):
        """Audio properties: the clip's gain in dB, keyframeable and mirrored by the envelope."""
        card, outer = self._card("audio")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Audio")
        title.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)
        reset = _tool("reset", "Reset the gain to 0 dB")
        reset.clicked.connect(lambda _=False, c=clip: self._reset_gain(c))
        header.addWidget(reset)
        outer.addLayout(header)
        body, form = self._form()
        self._channel_row(form, clip, CH.gain_channel())
        hint = QLabel("Gain is applied to the audio signal in playback and export; "
                      "the same value is drawn as the clip's gain envelope.")
        hint.setWordWrap(True)
        hint.setProperty("quiet", True)
        form.addWidget(hint)
        outer.addWidget(body)
        self.properties_layout.addWidget(card)

    def _reset_gain(self, clip):
        self.engine.session.set_clip_gain(clip.id, 0.0)
        self.refresh_properties()

    def _text_swatch(self, layout, clip, name):
        settings = TXT.ensure_text(clip)
        button = _swatch_button(settings.get(name) or "#000000", "Click to choose the color")
        button.clicked.connect(lambda _=False, c=clip, key_name=name, b=button:
                               self._pick_text_color(c, key_name, b))
        layout.addWidget(button)

    def _pick_text_color(self, clip, name, button):
        colour = QColorDialog.getColor(QColor(TXT.ensure_text(clip).get(name) or "#000000"),
                                       self, "Text color")
        if colour.isValid():
            self._set_text_setting(clip, name, colour.name(), "Text color")
            set_swatch(button, colour)

    def _set_text_setting(self, clip, name, value, label):
        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is not None:
                TXT.ensure_text(target)[name] = value
        self.engine.session.stack.run(label, action)
        self.engine.session.changed.emit()

    def _edit_text(self, clip, editor):
        """Write the words at the playhead; one undo entry per typing session."""
        target = self.engine.timeline.find_clip(clip.id)
        if target is None:
            return
        auto = bool(self.engine.session.auto_keyframe.get()) or bool(TXT.content_keys(target))
        if self.engine.session._gesture_snapshot is None:
            self.engine.session.begin_gesture()
        # The gesture stays open while typing and is pushed once the user pauses, so a
        # word is one undo step instead of one step per keystroke.
        timer = getattr(self, "_text_commit_timer", None)
        if timer is None:
            from PyQt6.QtCore import QTimer
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(700)
            timer.timeout.connect(lambda: self.engine.session.commit_gesture("Edit text"))
            self._text_commit_timer = timer
        timer.start()
        TXT.set_content(target, editor.toPlainText(), self._clip_frame(target), auto)
        self.engine.session.changed.emit()

    def _toggle_text_key(self, clip):
        target = self.engine.timeline.find_clip(clip.id)
        if target is None:
            return
        frame = self._clip_frame(target)
        self.engine.session.stack.run("Text keyframe",
                                      lambda: CH.toggle_key(target, CH.text_content_channel(), frame))
        self.refresh_properties()
        self.engine.session.changed.emit()

    def _guarded(self, callback):
        callback()
        self.refresh_properties()

    def _set_transform_flag(self, clip, flag, on):
        """One undoable toggle for a transform behaviour flag (uniform, snapping...)."""
        label = {"uniform": "Lock aspect ratio", "scale_from_center": "Scale from center",
                 "snap_rotation": "Snap rotation"}.get(flag, flag.replace("_", " ").title())

        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is not None:
                T.ensure_transform(target)[flag] = bool(on)
        self.engine.session.stack.run(label, action)
        self.engine.session.changed.emit()

    def _set_uniform(self, clip, on):
        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is not None:
                T.ensure_transform(target)["uniform"] = bool(on)
        self.engine.session.stack.run("Uniform scaling", action)

    def _set_flip(self, clip, axis, on):
        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is not None:
                T.ensure_transform(target)["flip_" + axis] = bool(on)
        self.engine.session.stack.run("Flip clip", action)
        self.engine.session.changed.emit()

    def _reset_transform(self, clip):
        self.engine.session.reset_transform([clip.id])
        self.refresh_properties()

    def _channel_row(self, form, clip, channel):
        """One numeric channel row: field, animation stopwatch and keyframe diamonds."""
        meta = CH.spec(clip, channel, self.engine.runtime_effects)
        frame = max(0, int(self.engine.session.playhead) - int(clip.timeline_start))
        animated = CH.is_animated(clip, channel)
        current = CH.value_at(clip, channel, frame)
        field = SliderField(meta["label"], 0.0 if current is None else float(current),
                            meta["minimum"], meta["maximum"], default=0.0,
                            step=max(1e-6, float(meta["step"])), unit=meta["unit"],
                            decimals=int(meta["decimals"]),
                            soft_min=-qty(clip, meta), soft_max=qty(clip, meta),
                            label_width=LABEL_W, box_width=64, tooltip=meta["label"])
        field.interaction_started.connect(self._begin_edit)
        field.edited.connect(lambda value, c=clip, ch=channel: self._set_channel(c, ch, value, True))
        field.committed.connect(lambda value, c=clip, ch=channel: self._commit_channel(c, ch, value))
        stopwatch = _tool("stopwatch", "Animate this property with keyframes", checkable=True)
        stopwatch.setChecked(animated)
        stopwatch.toggled.connect(lambda on, c=clip, ch=channel: self._toggle_channel_animation(c, ch, on))
        field.add_trailing(stopwatch)
        cluster = self._channel_cluster(clip, channel)
        self._bind_channel_cluster(clip, channel, cluster)
        cluster.setVisible(animated)
        field.add_trailing(cluster)
        self._rows.append({"kind": "channel", "clip_id": clip.id, "channel": channel,
                           "field": field, "stopwatch": stopwatch, "cluster": cluster})
        form.addWidget(field)

    @staticmethod
    def _channel_cluster(clip, channel):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        prev = _tool("step_back", "Previous keyframe")
        diamond = _tool("key", "Add or remove a keyframe at the playhead")
        nxt = _tool("step_forward", "Next keyframe")
        for widget in (prev, diamond, nxt):
            row.addWidget(widget)
        box._diamond = diamond
        return box

    def _bind_channel_cluster(self, clip, channel, cluster):
        widgets = cluster.findChildren(QToolButton)
        if len(widgets) < 3:
            return
        prev, diamond, nxt = widgets[0], widgets[1], widgets[2]
        prev.clicked.connect(lambda _=False, c=clip, ch=channel: self._jump_channel(c, ch, -1))
        nxt.clicked.connect(lambda _=False, c=clip, ch=channel: self._jump_channel(c, ch, 1))
        diamond.clicked.connect(lambda _=False, c=clip, ch=channel: self._toggle_channel_key(c, ch))

    def _set_channel(self, clip, channel, value, live=False):
        if live:
            self._begin_edit()
        target = self.engine.timeline.find_clip(clip.id)
        if target is None or target.track.locked:
            return
        frame = max(0, int(self.engine.session.playhead) - int(target.timeline_start))
        CH.set_value(target, channel, value, frame, bool(self.engine.session.auto_keyframe.get()))
        if live:
            self._notify()

    def _commit_channel(self, clip, channel, value):
        self._set_channel(clip, channel, value)
        before = self._edit_before
        self._edit_before = None
        label = "Edit " + CH.spec(clip, channel, self.engine.runtime_effects)["label"]
        if before is not None:
            self.engine.session.stack.commit(label, before)
        self._notify()
        self.refresh_properties()

    def _toggle_channel_animation(self, clip, channel, on):
        self.engine.session.toggle_channel_animation(clip, channel)
        self.refresh_properties()

    def _toggle_channel_key(self, clip, channel):
        self.engine.session.toggle_channel_key(clip, channel)
        self.refresh_properties()
        self._notify()

    def _jump_channel(self, clip, channel, direction):
        self.engine.session.jump_to_channel_key(clip, channel, direction)

    # ------------------------------------------------------------------- cards
    @staticmethod
    def _card(key=None):
        frame = QFrame()
        frame.setProperty("card", True)
        if key:
            frame.setProperty("effect_key", key)
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(tokens.SP2, tokens.SP2, tokens.SP2, tokens.SP2)
        outer.setSpacing(tokens.SP2)
        return frame, outer

    @staticmethod
    def _form():
        body = QWidget()
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(tokens.SP1 + 2)
        return body, form

    @staticmethod
    def _labelled(text, widget, tip=""):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(tokens.SP2)
        label = QLabel(text)
        label.setFixedWidth(LABEL_W)
        if tip:
            label.setToolTip(tip)
            widget.setToolTip(tip)
        layout.addWidget(label)
        layout.addWidget(widget, 1)
        return row

    def _clip_card(self, clip):
        frame, outer = self._card()
        title = QLabel(clip.name or "Clip")
        title.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        title.setToolTip(clip.name or "Clip")
        outer.addWidget(title)
        body, form = self._form()
        speed = SliderField("Speed", clip.speed * 100, 10, 1000, default=100, step=1, unit="%",
                            soft_min=25, soft_max=400, label_width=LABEL_W, box_width=56,
                            tooltip="Clip speed: also changes how long the clip plays")
        speed.committed.connect(lambda value, c=clip.id: self.engine.session.apply_clip_settings(
            c, speed=value / 100.0))
        form.addWidget(speed)
        self._rows.append({"kind": "speed", "clip_id": clip.id, "field": speed})
        toggles = QHBoxLayout()
        toggles.setContentsMargins(0, 0, 0, 0)
        reverse = QCheckBox("Reverse")
        reverse.setChecked(clip.reverse)
        reverse.toggled.connect(lambda v, c=clip.id: self.engine.session.apply_clip_settings(c, reverse=v))
        loop = QCheckBox("Loop")
        loop.setChecked(clip.loop)
        loop.toggled.connect(lambda v, c=clip.id: self.engine.session.apply_clip_settings(c, loop=v))
        toggles.addWidget(reverse)
        toggles.addWidget(loop)
        toggles.addStretch(1)
        form.addLayout(toggles)
        for edge, label in (("in", "Fade in"), ("out", "Fade out")):
            frames = clip.fade_in if edge == "in" else clip.fade_out
            field = SliderField(label, frames, 0, max(1, clip.duration), default=0, step=1,
                                unit=" f", soft_min=0, soft_max=max(1, clip.duration // 2),
                                label_width=LABEL_W, box_width=56, tooltip="Fade length in frames")
            field.interaction_started.connect(self._begin_edit)
            field.edited.connect(lambda value, c=clip.id, e=edge: self._live_fade(c, e, value))
            field.committed.connect(lambda value, c=clip.id, e=edge: self._commit_fade(c, e, value))
            form.addWidget(field)
            self._rows.append({"kind": "fade", "clip_id": clip.id, "edge": edge, "field": field})
        outer.addWidget(body)
        self.properties_layout.addWidget(frame)

    def _effect_card(self, clip, index, effect, entry):
        key = effect.key
        frame, outer = self._card(key)
        header = _CardHeader(key)
        header.setToolTip("Drag to reorder")
        head = QHBoxLayout(header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(tokens.SP1)
        collapsed = key in self._collapsed
        arrow = _tool("chevron_right" if collapsed else "chevron_down", "Collapse / expand")
        enabled = _tool("check", "Enable or disable this effect", checkable=True)
        enabled.setChecked(entry.get("enabled", True))
        enabled.toggled.connect(lambda on, c=clip, e=entry: self._set_enabled(c, e, on))
        name = QLabel(effect.name)
        name.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        reset = _tool("reset", "Reset this effect to its defaults")
        reset.clicked.connect(lambda _=False, c=clip, e=entry, f=effect: self._reset_effect(c, e, f))
        more = _tool("dots", "More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        last = len(clip.effects) - 1
        menu.addAction("Remove").triggered.connect(lambda _=False, c=clip, k=key: self._remove_effect(c, k))
        up = menu.addAction("Move up")
        up.setEnabled(index > 0)
        up.triggered.connect(lambda _=False, c=clip, k=key: self._move_effect(c, k, -1))
        down = menu.addAction("Move down")
        down.setEnabled(index < last)
        down.triggered.connect(lambda _=False, c=clip, k=key: self._move_effect(c, k, 1))
        menu.addSeparator()
        menu.addAction("Copy settings").triggered.connect(lambda _=False, e=entry: self._copy_settings(e))
        paste = menu.addAction("Paste settings")
        paste.setEnabled(bool(self._clipboard and self._clipboard[0] == key))
        paste.triggered.connect(lambda _=False, c=clip, e=entry: self._paste_settings(c, e))
        more.setMenu(menu)
        more.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        for widget in (arrow, enabled):
            head.addWidget(widget)
        head.addWidget(name)
        head.addWidget(self._preset_badge(clip, effect, entry), 0)
        head.addStretch(1)
        head.addWidget(reset)
        head.addWidget(more)
        outer.addWidget(header)
        body, form = self._form()
        self._effect_fields(form, clip, effect, entry)
        outer.addWidget(body)
        body.setVisible(not collapsed)

        def toggle(_=False, b=body, a=arrow, k=key):
            hidden = b.isVisible()
            b.setVisible(not hidden)
            a.setIcon(icon("chevron_right" if hidden else "chevron_down"))
            (self._collapsed.add if hidden else self._collapsed.discard)(k)
        arrow.clicked.connect(toggle)
        if not self.engine.clip_accepts_effect(clip, key):
            warning = QLabel(f"{effect.media_label().capitalize()}-only effect: this "
                             f"{self.engine.track_type_of(clip)} clip cannot use it. "
                             "It is switched off and never rendered.")
            warning.setWordWrap(True)
            warning.setProperty("quiet", True)
            outer.insertWidget(1, warning)
            body.setEnabled(False)
            enabled.blockSignals(True)
            enabled.setChecked(False)
            enabled.blockSignals(False)
            enabled.setEnabled(False)
        elif not entry.get("enabled", True):
            body.setEnabled(False)
        self.properties_layout.addWidget(frame)

    # ------------------------------------------------------------- timing rows
    def _live_fade(self, clip_id, edge, frames):
        self._begin_edit()
        self.engine.timeline.set_fade(clip_id, edge, int(frames))
        self._notify()

    def _commit_fade(self, clip_id, edge, frames):
        before = self._edit_before
        self._edit_before = None
        self.engine.timeline.set_fade(clip_id, edge, int(frames))
        if before is not None:
            self.engine.session.stack.commit("Fade in" if edge == "in" else "Fade out", before)
        self._notify()

    # ---------------------------------------------------------- effect controls
    def _effect_fields(self, form, clip, effect, entry):
        advanced_box = None
        advanced_form = None
        color_done = False
        for spec in effect.param_specs():
            target = form
            if spec.advanced:
                if advanced_box is None:
                    advanced_box, advanced_form = self._advanced_fold(form)
                target = advanced_form
            if spec.name in COLOR_NAMES:
                if not color_done:
                    color_done = True
                    self._color_row(target, clip, effect, entry)
                continue
            stored = entry.get("params", {}).get(spec.name, spec.default)
            animated = isinstance(stored, AnimatedValue)
            # Coerce through the parameter's own rules: a value the spec cannot express
            # (text in a number, NaN from an older project) becomes the default instead
            # of reaching a spin box, which used to take the process down.
            current = spec.clamp(parameter_value_at(stored, self._clip_frame(clip)))
            tip = spec.help or spec.label
            if spec.kind in ("int", "float"):
                field = SliderField(spec.label, current, spec.minimum, spec.maximum,
                                    default=spec.default, step=spec.step, unit=spec.unit,
                                    decimals=0 if spec.kind == "int" else 2,
                                    soft_min=spec.soft_min, soft_max=spec.soft_max,
                                    label_width=LABEL_W, box_width=56, tooltip=tip)
                field.interaction_started.connect(self._begin_edit)
                field.edited.connect(lambda value, c=clip, e=entry, n=spec.name:
                                     self._set_param(c, e, n, value, live=True))
                field.committed.connect(lambda value, c=clip, e=entry, n=spec.name:
                                        self._commit_param(c, e, n, value))
                stopwatch = self._stopwatch(clip, entry, spec.name, animated)
                field.add_trailing(stopwatch)
                cluster = self._key_cluster(clip, entry, spec.name)
                cluster.setVisible(animated)
                field.add_trailing(cluster)
                self._rows.append({"kind": "param", "clip_id": clip.id, "entry": entry,
                                   "name": spec.name, "field": field, "stopwatch": stopwatch,
                                   "cluster": cluster, "effect": effect, "spec": spec})
                target.addWidget(field)
            elif spec.kind == "bool":
                box = QCheckBox(spec.label)
                box.setChecked(bool(current))
                box.setToolTip(tip)
                box.toggled.connect(lambda value, c=clip, e=entry, n=spec.name, s=spec:
                                    self._commit_choice(c, e, n, bool(value), s))
                target.addWidget(box)
            elif spec.kind == "choice":
                target.addWidget(self._choice_row(clip, entry, spec, current, tip))
            elif spec.name == "palette_text":
                self._palette_row(target, clip, entry, spec, str(current or ""))
            else:
                edit = QLineEdit("" if current is None else str(current))
                edit.editingFinished.connect(lambda c=clip, e=entry, n=spec.name, w=edit, s=spec:
                                             self._commit_choice(c, e, n, w.text(), s))
                target.addWidget(self._labelled(spec.label, edit, tip))

    def _preset_badge(self, clip, effect, entry):
        """A small badge naming the active preset, so the choice is visible when collapsed."""
        spec = next((item for item in effect.param_specs() if item.kind == "choice"), None)
        label = QLabel()
        label.setObjectName("presetBadge")
        label.setVisible(False)
        if spec is None:
            return label
        stored = (entry.get("params") or {}).get(spec.name)
        key = spec.clamp(stored) if stored is not None else spec.choices[0]
        self._rows.append({"kind": "badge", "clip_id": clip.id, "entry": entry,
                           "spec": spec, "label": label})
        self._sync_badge(label, spec, key)
        return label

    @staticmethod
    def _sync_badge(label, spec, key):
        default = spec.choices[0] if spec.choices else key
        if key != default:
            text = dict(spec.choice_items()).get(key, str(key))
            label.setText(text)
            label.setToolTip(f"{spec.label}: {text} (applies to the whole clip)")
            label.setVisible(True)
        else:
            label.setVisible(False)

    def _choice_row(self, clip, entry, spec, current, tip):
        """Labelled dropdown for a choice parameter, plus its whole-clip menu.

        The combo shows human labels (``Vintage``, not ``vintage``) while the model keeps
        the key, so the renderer's comparison and the dropdown's text can never drift
        apart. A preset is a property of the whole clip: changing it is one undo step and
        it never creates keyframes, which a chevron menu states explicitly.
        """
        combo = QComboBox()
        for key, label in spec.choice_items():
            combo.addItem(label, key)
        combo.setCurrentIndex(spec.choice_index(current))
        combo.setToolTip(tip)
        combo.activated.connect(lambda index, c=clip, e=entry, s=spec, w=combo:
                                self._choice_picked(c, e, s, w, index))
        more = QToolButton()
        more.setProperty("role", "icon")
        more.setIcon(icon("chevron_down"))
        more.setToolTip("Whole-clip options for this choice")
        more.setFixedSize(tokens.HIT, tokens.HIT)
        more.clicked.connect(lambda _=False, c=clip, e=entry, s=spec, w=combo, b=more:
                             self._choice_menu(c, e, s, w, b))
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.SP1)
        row.addWidget(combo, 1)
        row.addWidget(more)
        self._rows.append({"kind": "choice", "clip_id": clip.id, "entry": entry,
                           "name": spec.name, "combo": combo, "effect": None, "spec": spec})
        return self._labelled(spec.label, holder, tip)

    def _choice_picked(self, clip, entry, spec, combo, index):
        key = combo.itemData(index) if index >= 0 else combo.currentData()
        self._commit_choice(clip, entry, spec.name, key, spec)
        # The combo is a view of the model: show what was actually stored (a value the
        # spec rejected snaps back instead of lying about the state).
        stored = self._entry(clip, entry["effect_id"]).get("params", {}).get(spec.name)
        combo.blockSignals(True)
        combo.setCurrentIndex(spec.choice_index(stored))
        combo.blockSignals(False)

    def _choice_menu(self, clip, entry, spec, combo, button):
        menu = QMenu(self)
        reset_key = spec.choices[0] if spec.choices else None
        if reset_key is not None:
            first = spec.choice_items()[0][1]
            action = menu.addAction(f"Reset to {first}")
            action.setEnabled(combo.currentData() != reset_key)
            action.triggered.connect(lambda _=False: (self._commit_choice(clip, entry, spec.name,
                                                                         reset_key, spec),
                                                      combo.setCurrentIndex(spec.choice_index(reset_key))))
        menu.addSeparator()
        note = menu.addAction("Applies to the whole clip \u00b7 no keyframes")
        note.setEnabled(False)
        menu.exec(button.mapToGlobal(QPoint(0, button.height())))

    def _advanced_fold(self, form):
        toggle = QToolButton()
        toggle.setText("Advanced")
        toggle.setIcon(icon("chevron_right"))
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.setCheckable(True)
        toggle.setStyleSheet("QToolButton { border: 0; background: transparent; }")
        box, inner = self._form()
        box.setVisible(False)

        def flip(on, b=box, t=toggle):
            b.setVisible(on)
            t.setIcon(icon("chevron_down" if on else "chevron_right"))
        toggle.toggled.connect(flip)
        form.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        form.addWidget(box)
        return box, inner

    def _entry_color(self, clip, entry):
        frame = self._clip_frame(clip)
        params = entry.get("params", {})
        return QColor(*[int(parameter_value_at(params.get(n), frame) or 0) for n in COLOR_NAMES])

    def _color_row(self, form, clip, effect, entry):
        button = _swatch_button(self._entry_color(clip, entry), "Click to choose the color")
        button.clicked.connect(lambda _=False, c=clip, e=entry, b=button: self._pick_color(c, e, b))
        form.addWidget(self._labelled("Color", button))
        self._rows.append({"kind": "color", "clip_id": clip.id, "entry": entry, "button": button})

    def _pick_color(self, clip, entry, button):
        color = QColorDialog.getColor(self._entry_color(clip, entry), self, "Color")
        if color.isValid():
            self.set_color(clip, entry, color)

    def set_color(self, clip, entry, color):
        """One undo step that writes all three channels (keyframing animated ones)."""
        color = QColor(color)
        self._begin_edit()
        for name, value in zip(COLOR_NAMES, (color.red(), color.green(), color.blue())):
            self._set_param(clip, entry, name, value)
        before = self._edit_before
        self._edit_before = None
        if before is not None:
            self.engine.session.stack.commit("Change color", before)
        self._notify()
        self.refresh_properties()

    def _palette_row(self, form, clip, entry, spec, text):
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.SP1)
        colors = [c.strip() for c in text.split(",") if c.strip()]

        def write(new):
            self._begin_edit()
            self._set_param(clip, entry, spec.name, ",".join(new))
            before = self._edit_before
            self._edit_before = None
            if before is not None:
                self.engine.session.stack.commit("Edit palette", before)
            self._notify()
            self.refresh_properties()

        for i, value in enumerate(colors):
            swatch = _swatch_button(value, "Click to change, right-click to remove")
            swatch.setFixedSize(24, 20)

            def pick(_=False, i=i):
                chosen = QColorDialog.getColor(QColor(colors[i]), self, "Palette color")
                if chosen.isValid():
                    write(colors[:i] + [chosen.name()] + colors[i + 1:])
            swatch.clicked.connect(pick)
            swatch.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            swatch.customContextMenuRequested.connect(
                lambda _pos, i=i: write(colors[:i] + colors[i + 1:]) if len(colors) > 1 else None)
            row.addWidget(swatch)
        add = _tool("plus", "Add a color")
        add.clicked.connect(lambda _=False: write(colors + ["#ffffff"]))
        row.addWidget(add)
        row.addStretch(1)
        form.addWidget(self._labelled(spec.label, holder, spec.help))

    def _stopwatch(self, clip, entry, name, animated):
        button = _tool("stopwatch", "Animate this parameter with keyframes", checkable=True)
        button.setChecked(animated)
        button.toggled.connect(lambda on, c=clip, e=entry, n=name: self._toggle_animation(c, e, n, on))
        return button

    def _key_cluster(self, clip, entry, name):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        prev = _tool("step_back", "Jump to the previous keyframe")
        prev.clicked.connect(lambda _=False, c=clip, e=entry, n=name: self._jump_key(c, e, n, -1))
        diamond = _tool("key", "Add or remove a keyframe at the playhead")
        diamond.clicked.connect(lambda _=False, c=clip, e=entry, n=name: self._toggle_key(c, e, n))
        nxt = _tool("step_forward", "Jump to the next keyframe")
        nxt.clicked.connect(lambda _=False, c=clip, e=entry, n=name: self._jump_key(c, e, n, 1))
        for widget in (prev, diamond, nxt):
            row.addWidget(widget)
        box._diamond = diamond
        diamond.setProperty("on", False)
        return box

    # -------------------------------------------------------------- syncing UI
    def sync_playhead(self):
        """Make every built row show the value at the playhead (cheap, no rebuilding)."""
        if self._suspend_sync:
            return
        clip = self._clip()
        if clip is None:
            return
        local = self._clip_frame(clip)
        for row in self._rows:
            kind = row["kind"]
            if kind == "text":
                editor = row.get("editor")
                # Follow the playhead with the keyed words, but never while the user is
                # typing: that would move the caret and fight the edit.
                if editor is not None and not editor.hasFocus():
                    words = TXT.content_at(clip, local)
                    if editor.toPlainText() != words:
                        editor.blockSignals(True)
                        editor.setPlainText(words)
                        editor.blockSignals(False)
                continue
            if kind == "fade":
                row["field"].set_value(clip.fade_in if row["edge"] == "in" else clip.fade_out)
            elif kind == "speed":
                row["field"].set_value(clip.speed * 100)
            elif kind == "badge":
                entry = row["entry"]
                if not any(entry is e for e in clip.effects):
                    continue
                spec = row["spec"]
                stored = (entry.get("params") or {}).get(spec.name)
                self._sync_badge(row["label"], spec, spec.clamp(stored))
            elif kind == "choice":
                entry = row["entry"]
                if not any(entry is e for e in clip.effects):
                    continue
                spec = row["spec"]
                stored = entry.get("params", {}).get(row["name"])
                index = spec.choice_index(stored)
                if row["combo"].currentIndex() != index:
                    row["combo"].blockSignals(True)
                    row["combo"].setCurrentIndex(index)
                    row["combo"].blockSignals(False)
            elif kind == "color":
                if any(row["entry"] is e for e in clip.effects):
                    set_swatch(row["button"], self._entry_color(clip, row["entry"]))
            elif kind == "channel":
                animated = CH.is_animated(clip, row["channel"])
                row["stopwatch"].blockSignals(True)
                row["stopwatch"].setChecked(animated)
                row["stopwatch"].blockSignals(False)
                row["cluster"].setVisible(animated)
                if animated:
                    hit = CH.has_key(clip, row["channel"], local)
                    diamond = row["cluster"]._diamond
                    diamond.setProperty("on", hit)
                    diamond.setIcon(icon("key_filled" if hit else "key",
                                         tokens.COLORS["accent"] if hit else None))
                value = CH.value_at(clip, row["channel"], local)
                if value is not None:
                    row["field"].set_value(value)
            elif kind == "param":
                entry = row["entry"]
                if not any(entry is e for e in clip.effects):
                    continue
                stored = entry.get("params", {}).get(row["name"])
                if stored is None:
                    continue
                animated = isinstance(stored, AnimatedValue)
                row["stopwatch"].blockSignals(True)
                row["stopwatch"].setChecked(animated)
                row["stopwatch"].blockSignals(False)
                row["cluster"].setVisible(animated)
                if animated:
                    hit = stored.key_at(local) is not None
                    diamond = row["cluster"]._diamond
                    diamond.setProperty("on", hit)
                    diamond.setIcon(icon("key_filled" if hit else "key", tokens.COLORS["accent"] if hit else None))
                row["field"].set_value(parameter_value_at(stored, local))

    # ----------------------------------------------------------- edit plumbing
    def _begin_edit(self):
        """Snapshot the timeline once per gesture, so a drag is a single undo step."""
        if self._edit_before is None:
            self._edit_before = self.engine.timeline.snapshot()

    def _notify(self):
        self.engine.session.changed.emit()

    def _set_param(self, clip, entry, name, value, live=False):
        """Write one parameter.

        `live` is a slider drag in progress: it opens the gesture so the whole drag is
        one undo step. Everything else (a checkbox, a dropdown, a typed value) is written
        by `_write_param` as a complete, undoable change - the old code wrote those into
        the model without an undo step and without a repaint, which is why choosing a
        colour preset looked like it did nothing until some other edit forced a refresh.
        """
        if live:
            self._begin_edit()
        self._write_param(clip, entry, name, value, self._clip_frame(clip))
        if live:
            self._notify()

    @staticmethod
    def _write_param(clip, entry, name, value, frame=0):
        """Apply one parameter value to the entry, respecting an existing animation."""
        stored = entry.get("params", {}).get(name)
        if isinstance(stored, AnimatedValue):
            # Keep the animation: a new value at the playhead becomes a keyframe there,
            # never a silent rewrite of the whole curve.
            stored.set_keyframe(max(0, int(frame or 0)), value)
            return True
        entry.setdefault("params", {})[name] = value
        return False

    def _commit_choice(self, clip, entry, name, value, spec=None):
        """A dropdown/checkbox edit: one undo step, one repaint, no keyframes by default."""
        value = spec.clamp(value) if spec is not None else value
        if entry.get("params", {}).get(name) == value:
            return False

        frame = self._clip_frame(clip)

        def write(target):
            target_entry = self._entry(target, entry["effect_id"])
            if target_entry is not None:
                self._write_param(target, target_entry, name, value, frame)

        label = f"Set {spec.label if spec is not None else name}"
        self.engine.session.apply_to_clips([clip.id], label, write)
        self._notify()
        self.refresh_properties()
        return True

    def _commit_param(self, clip, entry, name, value):
        self._set_param(clip, entry, name, value)
        before = self._edit_before
        self._edit_before = None
        if before is not None:
            self.engine.session.stack.commit("Edit effect parameter", before)
        self._notify()
        self.refresh_properties()

    def _set_enabled(self, clip, entry, enabled):
        self.engine.session.apply_to_clips([clip.id], "Toggle effect",
                                           lambda c: self._entry(c, entry["effect_id"]).__setitem__("enabled", enabled))
        self.refresh_properties()

    @staticmethod
    def _entry(clip, key):
        return next(e for e in clip.effects if e["effect_id"] == key)

    def _remove_effect(self, clip, key):
        self.engine.session.apply_to_clips([clip.id], "Remove effect",
                                           lambda c: setattr(c, "effects", [e for e in c.effects if e["effect_id"] != key]))
        self.refresh_properties()

    def _move_effect(self, clip, key, direction):
        def move(c):
            i = next((i for i, e in enumerate(c.effects) if e["effect_id"] == key), None)
            j = i + direction if i is not None else -1
            if i is not None and 0 <= j < len(c.effects):
                c.effects[i], c.effects[j] = c.effects[j], c.effects[i]
        self.engine.session.apply_to_clips([clip.id], "Reorder effect", move)
        self.refresh_properties()

    def _card_dropped(self, key, index):
        clip = self._clip()
        if clip is None:
            return

        def move(c):
            i = next((i for i, e in enumerate(c.effects) if e["effect_id"] == key), None)
            if i is None:
                return
            entry = c.effects.pop(i)
            c.effects.insert(max(0, min(index, len(c.effects))), entry)
        before = [e["effect_id"] for e in clip.effects]
        self.engine.session.apply_to_clips([clip.id], "Reorder effect", move)
        if before != [e["effect_id"] for e in self._clip().effects]:
            self.refresh_properties()

    def _reset_effect(self, clip, entry, effect):
        defaults = {spec.name: spec.default for spec in effect.param_specs()}

        def reset(c):
            target = self._entry(c, entry["effect_id"])
            target["params"] = {**target.get("params", {}), **defaults}
        self.engine.session.apply_to_clips([clip.id], "Reset effect", reset)
        self.refresh_properties()

    def _copy_settings(self, entry):
        self._clipboard = (entry["effect_id"], copy.deepcopy(entry.get("params", {})))

    def _paste_settings(self, clip, entry):
        if not self._clipboard or self._clipboard[0] != entry["effect_id"]:
            return
        params = copy.deepcopy(self._clipboard[1])
        self.engine.session.apply_to_clips(
            [clip.id], "Paste effect settings",
            lambda c: self._entry(c, entry["effect_id"]).__setitem__("params", params))
        self.refresh_properties()

    def _toggle_animation(self, clip, entry, name, enabled):
        frame = self._clip_frame(clip)
        stored = entry.get("params", {}).get(name)
        current = parameter_value_at(stored, frame)

        def action():
            target = self._entry(self.engine.timeline.find_clip(clip.id), entry["effect_id"])
            if enabled:
                animated = target["params"].get(name)
                if not isinstance(animated, AnimatedValue):
                    animated = AnimatedValue(base_value=current)
                animated.set_keyframe(frame, current)
                target["params"][name] = animated
            else:
                target["params"][name] = parameter_value_at(target["params"].get(name), frame)

        self.engine.session.stack.run("Toggle parameter animation", action)
        self.refresh_properties()
        self._notify()

    def _toggle_key(self, clip, entry, name):
        frame = self._clip_frame(clip)

        def action():
            target = self._entry(self.engine.timeline.find_clip(clip.id), entry["effect_id"])
            stored = target["params"].get(name)
            if not isinstance(stored, AnimatedValue):
                return
            if not stored.remove_keyframe(frame):
                stored.set_keyframe(frame, parameter_value_at(stored, frame))

        self.engine.session.stack.run("Edit parameter keyframe", action)
        self.refresh_properties()
        self._notify()

    def _jump_key(self, clip, entry, name, direction):
        stored = entry.get("params", {}).get(name)
        if not isinstance(stored, AnimatedValue):
            return
        frame = self._clip_frame(clip)
        times = [key.time for key in stored.keyframes]
        candidates = [t for t in times if t < frame] if direction < 0 else [t for t in times if t > frame]
        if candidates:
            target = max(candidates) if direction < 0 else min(candidates)
            self.engine.session.set_playhead(clip.timeline_start + target)
