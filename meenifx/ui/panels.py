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
from PyQt6.QtGui import QColor, QCursor, QDrag, QFont
from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QFrame,
                             QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
                             QPlainTextEdit, QScrollArea, QToolButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import tokens
from .filedialogs import open_file, save_file
from ..constants import EFFECT_MIME
from ..effects.base import ParamSpec
from ..effects.tracking_viz import STYLE_KEYS, TRACKING_STYLES
from ..icons import icon
from ..model import animation as ANIM
from ..model import animation_families as FAMILIES
from ..model import looks as LOOKS
from ..model import channels as CH
from ..model import preset_library as LIB
from ..model import presets as PM
from ..model import shape as SHAPE
from ..model import text as TXT
from ..model import transform as T
from ..model.keyframes import AnimatedValue, parameter_value_at
from ..processing.blend import BLEND_LABELS, normalise_mode
from .browser import BrowserView
from .components import EmptyState
from .widgets import ScrubField

CARD_MIME = "application/x-meenifx-card"
# Effects panel order: everyday picture work first, technical/motion tools after.
CATEGORY_ORDER = ("Video Effects", "Looks", "Glitch & Distortion", "Stylize", "Color Correction",
                  "Blur & Sharpen", "Glow & Light", "Light", "Time", "Transform",
                  "Distortion & Warp", "Warp", "Text Effects", "Audio Effects",
                  "Blur and Sharpen", "Color", "Distort", "Tracking")
LABEL_W = 88
# Property groups an effect card opens by default. The everyday controls live here;
# everything else (Labels, Connectors, Face Effects...) starts folded so a tracker card
# stays short without hiding a group the user can reach in one click.
OPEN_PROPERTY_GROUPS = ("Detection", "Visualization")
ROTATE_STEP = 15.0      # degrees per step when rotation snapping is on
COLOR_NAMES = ("color_r", "color_g", "color_b")
# Every effect card carries this control; it is not part of an effect's own PARAMS table
# because it never reaches the effect - the engine blends the result with the original.
MIX_SPEC = ParamSpec("mix", "Mix", "float", 0.0, 100.0, 100.0, step=1.0, unit="%",
                     help="Blend the original picture with this effect's result")


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
        self.browser = BrowserView(engine, self)
        self.browser.apply_requested.connect(self._browser_apply)
        self.browser.remove_requested.connect(self._browser_remove)
        self.browser.menu_requested.connect(self._browser_menu)
        # The browser owns the search box; the panel keeps the old name so scripts and
        # tests can still drive it.
        self.search = self.browser.search
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
        layout.addWidget(self.browser)
        self._rows = []
        self._built_key = None
        self._edit_before = None
        self._suspend_sync = False
        self._collapsed = set()
        # Style groups and their members start collapsed; these remember what was opened.
        self._opened_groups = set()
        self._opened_members = set()
        # {(effect key, group title): open}; kept across card rebuilds so a playhead tick
        # never folds a section the user just opened.
        self._property_groups = {}
        self._clipboard = None
        self.refresh_effect_list()

    # ------------------------------------------------------------ effect list
    def refresh_effect_list(self, *_):
        """Tell the browser what the selected clips already carry (checkmarks, media)."""
        clips = self._selected_clips()
        keys, presets, looks = set(), set(), set()
        for clip in clips:
            for entry in clip.effects:
                if entry.get("effect_id") == "look":
                    looks.add((entry.get("params") or {}).get("look") or entry.get("look_id"))
                elif entry.get("effect_id"):
                    keys.add(entry.get("effect_id"))
            for row in PM.instances(clip):
                if row.get("preset"):
                    presets.add(row["preset"])
        media = "video"
        if clips:
            first = clips[0]
            media = "text" if first.is_text else self.engine.track_type_of(first)
        self.browser.set_context(keys, presets, looks, media, multiple=len(clips) > 1)

    def _browser_apply(self, item):
        """One click path for every card: an effect, a Style or an animation family."""
        kind = item.get("kind")
        if kind == "effect":
            self._add_effect_item(item)
        elif kind == "style":
            if self.apply_look(item["id"]):
                self.browser.note_recent(item)
        elif kind == "animation":
            self._apply_animation_item(item)

    def _add_effect_item(self, item):
        effect = item.get("effect")
        key = item.get("id")
        clips = self._selected_clips()
        if not clips or effect is None:
            self.engine.session.status.emit("Select a clip, then double-click an effect")
            return
        # The engine decides compatibility, so a card, a drag and a double-click agree.
        targets = [clip for clip in clips if self.engine.clip_accepts_effect(clip, key)]
        if not targets:
            track = self.engine.track_type_of(clips[0])
            self.engine.session.status.emit(
                f"{effect.name} is a {effect.media_label()}-only effect - it cannot be added "
                f"to {'an' if track == 'audio' else 'a'} {track} clip")
            return
        params = copy.deepcopy(effect.capture_params())
        another = bool(item.get("another"))

        def add(clip):
            clip.effects.append({"effect_id": key, "enabled": True,
                                 "params": copy.deepcopy(params)})
        label = f"Add another {effect.name}" if another else f"Add {effect.name}"
        self.engine.session.apply_to_clips([clip.id for clip in targets], label, add)
        self.browser.note_recent(item)
        self.properties_requested.emit()
        self._notify()
        self.refresh_properties()

    def _browser_remove(self, item):
        """Double-clicking an applied item removes it again (one undo step)."""
        if item.get("kind") == "style":
            self._remove_style(item.get("id"))
            return
        if item.get("kind") != "effect":
            return
        key = item.get("id")
        name = item.get("name") or key
        clips = [clip for clip in self._selected_clips()
                 if any(entry.get("effect_id") == key for entry in clip.effects)]
        if not clips:
            return
        counts = [sum(1 for entry in clip.effects if entry.get("effect_id") == key)
                  for clip in clips]
        if max(counts) > 1:
            # Never guess which copy the user meant: offer the instances by name.
            self._choose_effect_instance(clips, key, name)
            return

        def strip(clip):
            clip.effects = [entry for entry in clip.effects
                            if entry.get("effect_id") != key]
        self.engine.session.apply_to_clips([clip.id for clip in clips], f"Remove {name}", strip)
        self._notify()
        self.refresh_properties()

    def _choose_effect_instance(self, clips, key, name):
        """Several copies of one effect exist: let the user pick, never delete silently."""
        menu = QMenu(self)
        for index in range(max(sum(1 for entry in clip.effects if entry.get("effect_id") == key)
                               for clip in clips)):
            menu.addAction(f"Remove {name} #{index + 1}",
                           lambda i=index: self._remove_instance_at(clips, key, name, i))
        menu.addSeparator()
        menu.addAction(f"Remove every {name}",
                       lambda: self._remove_instance_at(clips, key, name, None))
        menu.exec(QCursor.pos())

    def _remove_instance_at(self, clips, key, name, index):
        """Remove the nth copy (or all of them) on every selected clip, in one undo step."""
        def strip(clip):
            seen = 0
            kept = []
            for entry in clip.effects:
                if entry.get("effect_id") != key:
                    kept.append(entry)
                    continue
                if index is not None and seen == index:
                    seen += 1
                    continue
                if index is None:
                    continue
                seen += 1
                kept.append(entry)
            clip.effects = kept
        label = f"Remove every {name}" if index is None else f"Remove {name} #{index + 1}"
        self.engine.session.apply_to_clips([clip.id for clip in clips], label, strip)
        self._notify()
        self.refresh_properties()

    def _remove_style(self, look_id):
        """Drop one Style group and the effects it brought in; unrelated effects stay."""
        if not look_id:
            return
        clips = self._selected_clips()
        if not clips:
            return

        def strip(clip):
            # A style instance is a group id ("dreamy_bloom#2"); its members share it, so
            # removing the group removes exactly the effects that style brought in.
            groups = {entry.get("look_id") for entry in clip.effects
                      if entry.get("effect_id") == "look"
                      and (entry.get("params") or {}).get("look") == look_id}
            groups.discard(None)
            clip.effects = [entry for entry in clip.effects
                            if entry.get("look_id") not in groups]
        self.engine.session.apply_to_clips([clip.id for clip in clips], "Remove style", strip)
        self._notify()
        self.refresh_properties()

    def _apply_animation_item(self, item):
        """Apply one family variant (or an In+Out pair) and keep the family single."""
        family = item.get("family")
        if family is None:
            return
        mode = item.get("mode") or FAMILIES.default_mode(family)
        direction = item.get("direction") or ""
        media = "text" if item.get("media") == "text" else "clip"
        presets = []
        for one_mode in (("in", "out") if mode == "both" else (mode,)):
            preset = FAMILIES.variant(family, media, one_mode, direction)
            if preset is not None and preset not in presets:
                presets.append(preset)
        if not presets:
            self.engine.session.status.emit(
                f"{family['name']} has no {mode} variant for this clip")
            return
        clips = [clip for clip in self._selected_clips()
                 if clip.track is not None and clip.track.type == "video"]
        if not clips:
            self.engine.session.status.emit("Select a clip to apply an animation")
            return
        self.engine.session.apply_animation(presets, ids=[clip.id for clip in clips],
                                            family_program=family["program"])
        self.browser.note_recent(item)
        self.properties_requested.emit()
        self.refresh_properties()

    def _browser_menu(self, position):
        """The browser's ... menu: looks on disk, preview image, thumbnail refresh."""
        menu = QMenu(self)
        clip = self._clip()
        save = menu.addAction("Save effects as style\u2026",
                              lambda: self._save_look(clip) if clip else None)
        save.setEnabled(clip is not None)
        menu.addAction("Import style\u2026", self._import_look)
        export_menu = menu.addMenu("Export style\u2026")
        for look in LOOKS.all_looks():
            export_menu.addAction(look["name"],
                                  lambda _checked=False, look_id=look["id"]:
                                  self._export_look(look_id))
        menu.addSeparator()
        menu.addAction("Change preview image\u2026", self._preview_preferences)
        menu.addAction("Refresh thumbnails", self.browser.reload_previews)
        menu.exec(position)

    def _preview_preferences(self):
        """Open Preferences straight on Appearance > Effect Previews."""
        window = self.window()
        opener = getattr(window, "show_preferences", None)
        if callable(opener):
            opener("previews")
        else:
            from .start_window import PreferencesDialog
            PreferencesDialog(window, section="previews").exec()

    def _selected_clips(self):
        found = (self.engine.timeline.find_clip(i) for i in self.engine.session.selected)
        return [clip for clip in found if clip is not None]

    def add_effect(self, effect_id):
        # Look items carry "look:<id>" from the tree and the drag payload.
        if str(effect_id).startswith("look:"):
            self.apply_look(str(effect_id).split(":", 1)[1])
            return
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
        self._notify()
        self.refresh_properties()

    # ------------------------------------------------------------------- looks
    def apply_look(self, look_id):
        """Insert a look's stack on every selected video clip (one undo step)."""
        clips = self._selected_clips()
        if not clips:
            self.engine.session.status.emit("Select a clip or adjustment layer to apply a look")
            return []
        applied = self.engine.session.apply_look(look_id, ids=[clip.id for clip in clips])
        if applied:
            self.properties_requested.emit()
            self._notify()
            self.refresh_properties()
        return applied

    def _save_look(self, clip, name=None):
        """Save the clip's current effect stack as a user look."""
        if clip is None:
            return None
        stack = LOOKS.from_effects(clip.effects)
        if not stack:
            self.engine.session.status.emit("Add at least one effect before saving a look")
            return None
        if name is None:
            name, accepted = QInputDialog.getText(self, "Save look", "Look name:",
                                                  text="My look")
            if not accepted:
                return None
        name = str(name or "").strip()
        if not name:
            return None
        _path, look = LOOKS.write_user_look(name, stack)
        if look is not None:
            self.refresh_effect_list()
            self.browser.refresh()
            self.engine.session.status.emit(f"Look saved: {look['name']}")
        return look

    def _import_look(self, path=None):
        if path is None:
            path = open_file(
                self, "Import look", "", "Looks (*.mfxlook *.json);;All files (*)")
        if not path:
            return None
        _saved, look = LOOKS.import_file(path)
        if look is not None:
            self.refresh_effect_list()
            self.browser.refresh()
            self.engine.session.status.emit(f"Look imported: {look['name']}")
        else:
            self.engine.session.status.emit("That file is not a meeniFX look")
        return look

    def _export_look(self, look_id, path=None):
        look = LOOKS.get(look_id)
        if look is None:
            return None
        if path is None:
            path = save_file(
                self, "Export look", f"{look['name']}.mfxlook", "Looks (*.mfxlook)")
        if not path:
            return None
        written = LOOKS.export_file(look_id, path)
        if written:
            self.engine.session.status.emit(f"Look exported: {written}")
        return written

    def _remove_look(self, clip, entry):
        """Remove a look's group card and every effect it brought in, as one undo step."""
        group = entry.get("look_id")

        def remove(target):
            target.effects = [item for item in target.effects
                              if group is None or item.get("look_id") != group]
        self.engine.session.apply_to_clips([clip.id], "Remove look", remove)
        self._notify()
        self.refresh_properties()

    # --------------------------------------------------------------- properties
    def _structure_key(self, clips):
        if not clips:
            return ("none",)
        clip = clips[0]
        effects = tuple((id(entry), entry["effect_id"], entry.get("enabled", True),
                         entry.get("look_id"), entry.get("name"),
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
        shape_key = ()
        if clip.is_shape:
            # The shape card is rebuilt when the kind changes or a switch (stroke, caps,
            # arrowheads) flips; its numeric values are rows that follow the playhead.
            shape_key = tuple(sorted(
                (name, isinstance(value, AnimatedValue),
                 str(value) if isinstance(value, (str, dict)) else value)
                for name, value in SHAPE.ensure_shape(clip).items()))
        text_animation = ()
        if clip.is_text:
            animation = (clip.text or {}).get("animation") or {}
            text_animation = tuple(sorted((slot, meta.get("preset"), meta.get("duration"))
                                          for slot, meta in animation.items()
                                          if isinstance(meta, dict)))
        # The preset card is rebuilt when an instance appears, goes or changes kind.
        preset_rows = tuple((row.get("preset"), row.get("category"), int(row.get("duration", 0)),
                             (row.get("knobs") or {}).get("unit"))
                            for row in PM.instances(clip)) if hasattr(clip, "presets") else ()
        return (tuple(sorted(c.id for c in clips)), clip.id, clip.name, effects, animated, flags,
                gain, text_animation, shape_key, preset_rows)

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
            empty = EmptyState("Nothing selected", "Click a clip on the timeline to adjust its size, "
                               "position and effects.", "sparkles")
            self.properties_layout.addWidget(empty, 1)
            return
        clip = clips[0]
        self._clip_card(clip)
        if clip.track is not None and clip.track.type == "video":
            if clip.is_text:
                self._text_card(clip)
            elif clip.is_shape:
                self._shape_card(clip)
            self._transform_card(clip)
            self._animation_card(clip)
        elif clip.track is not None and clip.track.type == "audio":
            self._audio_card(clip)
        self._effect_cards(clip)
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
        size = self.engine.media.source_size(clip.source_path) if clip.source_path else None
        if size:
            return size
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

        # Keyboard gestures and inspector switches are the same thing: Ctrl = keep the
        # aspect ratio while resizing (the switch does the same for typed/scrubbed
        # scale), Shift or Alt = resize from the centre, Shift while rotating = 15
        # degree steps. Each has a switch here so nothing depends on remembering a key.
        switches = QWidget()
        switch_column = QVBoxLayout(switches)
        switch_column.setContentsMargins(0, 0, 0, 0)
        switch_column.setSpacing(2)
        for label, flag, tip in (
                ("Lock Scale X/Y", "uniform",
                 "Typing or scrubbing one scale axis moves the other by the same factor. "
                 "Handle drags stay free: hold Ctrl while resizing to keep the proportions"),
                ("Scale from center", "scale_from_center",
                 "Resize around the middle of the picture instead of the opposite handle - "
                 "hold Shift or Alt while dragging"),
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

    # --------------------------------------------------------------------- shape
    def _shape_card(self, clip):
        """The shape generator: geometry, paint and the same transform as every clip.

        Colours are held keys (like text words) so they can be keyed without pretending
        to be a number; every numeric control is an ordinary ``shape.<name>`` channel and
        gets the same stopwatch and diamonds as the transform.
        """
        settings = SHAPE.ensure_shape(clip)
        frame = self._clip_frame(clip)
        kind = str(settings.get("kind") or "rectangle")
        card, outer = self._card("shape")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Shape")
        title.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)
        outer.addLayout(header)
        body, form = self._form()

        kinds = QComboBox()
        for key, label in SHAPE.LABELS.items():
            kinds.addItem(icon(SHAPE.ICONS.get(key, "shape_rectangle")), label, key)
        kinds.setCurrentIndex(max(0, kinds.findData(kind)))
        kinds.currentIndexChanged.connect(
            lambda _index, c=clip, box=kinds: self._set_shape_kind(c, box.currentData()))
        form.addWidget(self._labelled("Type", kinds,
                                      "The shape this clip draws; the change is undoable and "
                                      "keeps the clip's transform, effects and keys"))

        self._section_label(form, "Fill")
        fill_row = QWidget()
        fill_layout = QHBoxLayout(fill_row)
        fill_layout.setContentsMargins(0, 0, 0, 0)
        fill_layout.setSpacing(tokens.SP2)
        self._shape_swatch(fill_layout, clip, "fill_color")
        fill_layout.addStretch(1)
        form.addWidget(self._labelled("Color", fill_row,
                                      "Fill colour; keyframe it to animate the colour"))
        self._channel_row(form, clip, SHAPE.shape_channel("fill_opacity"))

        self._section_label(form, "Stroke")
        stroke = QCheckBox("Stroke this shape")
        stroke.setChecked(bool(settings.get("stroke_enabled")))
        stroke.toggled.connect(lambda on, c=clip: self._set_shape_static(
            c, "stroke_enabled", bool(on), "Shape stroke"))
        form.addWidget(stroke)
        stroke_row = QWidget()
        stroke_layout = QHBoxLayout(stroke_row)
        stroke_layout.setContentsMargins(0, 0, 0, 0)
        stroke_layout.setSpacing(tokens.SP2)
        self._shape_swatch(stroke_layout, clip, "stroke_color")
        stroke_layout.addStretch(1)
        form.addWidget(self._labelled("Color", stroke_row, "Stroke colour"))
        for name in ("stroke_width", "stroke_opacity"):
            self._channel_row(form, clip, SHAPE.shape_channel(name))
        align = QComboBox()
        align.addItems([name.title() for name in SHAPE.ALIGNMENTS])
        align.setCurrentText(str(settings.get("stroke_align") or "center").title())
        align.currentTextChanged.connect(lambda text, c=clip: self._set_shape_static(
            c, "stroke_align", text.lower(), "Stroke alignment"))
        form.addWidget(self._labelled("Align", align,
                                      "Where the stroke sits relative to the outline"))

        self._section_label(form, "Geometry")
        for name in ("width", "height"):
            self._channel_row(form, clip, SHAPE.shape_channel(name))
        if kind == "rounded_rectangle":
            self._channel_row(form, clip, SHAPE.shape_channel("radius"))
        if kind == "polygon":
            self._channel_row(form, clip, SHAPE.shape_channel("sides"))
        if kind == "star":
            for name in ("points", "inner_ratio"):
                self._channel_row(form, clip, SHAPE.shape_channel(name))
        if kind == "line":
            cap = QComboBox()
            cap.addItems([name.title() for name in SHAPE.CAPS])
            cap.setCurrentText(str(settings.get("cap") or "round").title())
            cap.currentTextChanged.connect(lambda text, c=clip: self._set_shape_static(
                c, "cap", text.lower(), "Line caps"))
            form.addWidget(self._labelled("Caps", cap, "How the ends of the line are drawn"))
            arrows = QWidget()
            arrow_row = QHBoxLayout(arrows)
            arrow_row.setContentsMargins(0, 0, 0, 0)
            arrow_row.setSpacing(tokens.SP2)
            for name, label in (("arrow_start", "Start"), ("arrow_end", "End")):
                box = QCheckBox(label)
                box.setChecked(bool(settings.get(name)))
                box.toggled.connect(lambda on, c=clip, n=name: self._set_shape_static(
                    c, n, bool(on), "Line arrowheads"))
                arrow_row.addWidget(box)
            arrow_row.addStretch(1)
            form.addWidget(self._labelled("Arrows", arrows, "Optional arrowheads"))

        # Position, rotation and the clip's own opacity are the shared transform values:
        # the same rows as the Transform section, never a second set of numbers.
        self._section_label(form, "Transform (shared with the Transform section)")
        for name in ("position_x", "position_y", "rotation", "opacity"):
            self._channel_row(form, clip, CH.transform_channel(name))
        outer.addWidget(body)
        self.properties_layout.addWidget(card)

    def _shape_swatch(self, layout, clip, name):
        frame = self._clip_frame(clip)
        button = _swatch_button(SHAPE.color_at(clip, name, frame),
                                "Pick this shape colour")
        button.clicked.connect(lambda _=False, c=clip, n=name: self._pick_shape_color(c, n))
        layout.addWidget(button)
        key = _tool("key", "Add or remove a colour keyframe at the playhead", checkable=True)
        key.setChecked(SHAPE.has_color_key(clip, name, frame))
        key.clicked.connect(lambda _=False, c=clip, n=name: self._toggle_shape_color_key(c, n))
        layout.addWidget(key)
        self._rows.append({"kind": "shape_color", "clip_id": clip.id, "name": name,
                           "button": button, "key": key})

    def _pick_shape_color(self, clip, name):
        frame = self._clip_frame(clip)
        chosen = QColorDialog.getColor(QColor(SHAPE.color_at(clip, name, frame)), self,
                                       "Shape colour")
        if not chosen.isValid():
            return
        channel = SHAPE.shape_channel(name)
        auto = bool(self.engine.session.auto_keyframe.get())

        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is not None:
                CH.set_value(target, channel, chosen.name(), frame, auto)
        self.engine.session.stack.run("Shape colour", action)
        self._notify()
        self.refresh_properties()

    def _toggle_shape_color_key(self, clip, name):
        self.engine.session.toggle_channel_key(clip, SHAPE.shape_channel(name))
        self._notify()
        self.refresh_properties()

    def _set_shape_static(self, clip, name, value, label):
        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is not None:
                SHAPE.ensure_shape(target)[name] = value
        self.engine.session.stack.run(label, action)
        self._notify()
        self.refresh_properties()

    def _set_shape_kind(self, clip, kind):
        """Switch the shape's type in place, keeping transform, effects and keys."""
        if not kind or kind == (clip.shape or {}).get("kind"):
            return

        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is not None:
                SHAPE.ensure_shape(target)["kind"] = str(kind)
        self.engine.session.stack.run("Change shape type", action)
        self._notify()
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

        duration = ScrubField("Duration", int(meta.get("duration", default)), 1,
                               max(1, clip.duration), default=default, step=1, unit=" f",
                               label_width=LABEL_W,
                               tooltip="Length of the in/out animation, in frames")
        delay = ScrubField("Delay", int(meta.get("delay", 0)), 0,
                            max(0, clip.duration - 1), default=0, step=1, unit=" f",
                            label_width=LABEL_W,
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
        field = ScrubField(meta["label"], 0.0 if current is None else float(current),
                            meta["minimum"], meta["maximum"], default=0.0,
                            step=max(1e-6, float(meta["step"])), unit=meta["unit"],
                            decimals=int(meta["decimals"]),
                            label_width=LABEL_W, tooltip=meta["label"])
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
        auto = bool(self.engine.session.auto_keyframe.get())
        before = CH.value_at(target, channel, frame)
        CH.set_value(target, channel, value, frame, auto)
        # The Scale lock (the chain that sits beside Scale X/Y) keeps the picture's own
        # aspect ratio: the other axis moves by the same factor. With the lock off the two
        # axes are independent, which is what a squash or a stretch needs.
        kind, _effect_id, name = CH.parse(channel)
        if kind == "transform" and name in ("scale_x", "scale_y") and self._scale_locked(target):
            other = CH.transform_channel("scale_y" if name == "scale_x" else "scale_x")
            current, base = CH.value_at(target, other, frame), before
            try:
                ratio = float(value) / float(base) if base else 0.0
            except (TypeError, ValueError):
                ratio = 0.0
            if ratio and current is not None:
                CH.set_value(target, other, float(current) * ratio, frame, auto)
        if live:
            self._notify()

    def _scale_locked(self, clip):
        return bool(T.ensure_transform(clip).get("uniform", True))

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
        speed = ScrubField("Speed", clip.speed * 100, 10, 1000, default=100, step=1, unit="%",
                            label_width=LABEL_W,
                            tooltip="Clip speed: also changes how long the clip plays")
        speed.committed.connect(lambda value, c=clip.id: self.engine.session.apply_clip_settings(
            c, speed=value / 100.0))
        form.addWidget(speed)
        self._rows.append({"kind": "speed", "clip_id": clip.id, "field": speed})
        duration_field = ScrubField("Duration", clip.duration, 1, 200000, default=clip.duration,
                                    step=1, unit=" f", label_width=LABEL_W,
                                    tooltip="Exact clip length in frames; a linked audio clip "
                                            "changes with it. Media is limited by the frames "
                                            "its source still has")
        duration_field.committed.connect(
            lambda value, c=clip.id: self.engine.session.set_clip_duration(c, int(round(value))))
        form.addWidget(duration_field)
        self._rows.append({"kind": "duration", "clip_id": clip.id, "field": duration_field})
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
        if clip.track is not None and clip.track.type == "video":
            blend = QComboBox()
            for key, label in BLEND_LABELS:
                blend.addItem(label, key)
            blend.setCurrentIndex(max(0, [key for key, _label in BLEND_LABELS].index(
                normalise_mode(clip.blend))))
            blend.setToolTip("How this clip is mixed with the tracks below it")
            blend.activated.connect(lambda index, cid=clip.id, w=blend:
                                    self.engine.session.set_blend_mode(cid, w.itemData(index)))
            form.addWidget(self._labelled("Blend", blend, "How this clip is mixed with the layers below it"))
        for edge, label in (("in", "Fade in"), ("out", "Fade out")):
            frames = clip.fade_in if edge == "in" else clip.fade_out
            field = ScrubField(label, frames, 0, max(1, clip.duration), default=0, step=1,
                                unit=" f", label_width=LABEL_W, tooltip="Fade length in frames")
            field.interaction_started.connect(self._begin_edit)
            field.edited.connect(lambda value, c=clip.id, e=edge: self._live_fade(c, e, value))
            field.committed.connect(lambda value, c=clip.id, e=edge: self._commit_fade(c, e, value))
            form.addWidget(field)
            self._rows.append({"kind": "fade", "clip_id": clip.id, "edge": edge, "field": field})
        outer.addWidget(body)
        self.properties_layout.addWidget(frame)

    def _animation_card(self, clip):
        """The preset instances on this clip: knobs, Bake and remove.

        Every row edits the instance through the session, so each change is one undo step
        and the compositor picks it up on the next frame, just like a transform value.
        """
        rows = PM.instances(clip)
        if not rows:
            return
        frame, outer = self._card("animation")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Animation")
        title.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        header.addWidget(title)
        count = QLabel(f"{len(rows)} preset{'s' if len(rows) != 1 else ''}")
        count.setProperty("quiet", True)
        header.addWidget(count)
        header.addStretch(1)
        outer.addLayout(header)
        body, form = self._form()
        for index, instance in enumerate(rows):
            form.addWidget(self._preset_row(clip, index, instance))
        outer.addWidget(body)
        self.properties_layout.addWidget(frame)

    def _preset_row(self, clip, index, instance):
        """One instance: its name, knobs, Bake and Remove."""
        knobs = PM._knobs(instance)
        category = PM.CATEGORY_LABELS.get(instance.get("category"), "In")
        box = QFrame()
        box.setProperty("card", False)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, tokens.SP2)
        layout.setSpacing(tokens.SP1)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        name = QLabel(f"{instance.get('name') or instance.get('preset')}  ·  {category}")
        name.setStyleSheet("font-weight: 600;")
        name.setToolTip(PM.summary(instance))
        top.addWidget(name, 1)
        bake = QToolButton()
        bake.setText("Bake")
        bake.setToolTip("Turn this preset into ordinary keyframes (one undo step)")
        bake.setEnabled(PM.bakeable(instance, clip))
        if not PM.bakeable(instance, clip):
            bake.setToolTip("Bake needs Unit = Whole: per-unit motion lives in the text "
                            "layout. Use Split text to give each letter its own clip")
        bake.clicked.connect(lambda _=False, c=clip.id, i=index: self._bake_preset(c, i))
        top.addWidget(bake)
        remove = QToolButton()
        remove.setText("\u2715")
        remove.setToolTip("Remove this preset")
        remove.clicked.connect(lambda _=False, c=clip.id, i=index: self._remove_preset(c, i))
        top.addWidget(remove)
        layout.addLayout(top)

        family = self._animation_family(instance)
        media = "text" if clip.is_text else "clip"
        if family is not None:
            modes = list(family.get("modes") or [])
            if len(modes) > 1:
                mode = QComboBox()
                for key in modes:
                    mode.addItem(FAMILIES.MODE_LABELS.get(key, key.title()), key)
                mode.setCurrentIndex(max(0, mode.findData(instance.get("category"))))
                mode.setToolTip("When this animation plays: the same instance switches")
                mode.currentIndexChanged.connect(
                    lambda _row, c=clip.id, i=index, f=str(family["key"]), box=mode:
                    self._switch_animation(c, i, f, mode=box.currentData()))
                layout.addWidget(self._labelled("Mode", mode))
            options = FAMILIES.directions(family, media, instance.get("category", "in"))
            current = str((instance.get("args") or {}).get("dir") or "")
            if len(options) > 1 or (current and current in options):
                direction = QComboBox()
                direction.addItem("Default", "")
                for key in options:
                    direction.addItem(PM.DIRECTION_LABELS.get(key, key.title()), key)
                direction.setCurrentIndex(max(0, direction.findData(current)))
                direction.setToolTip("Direction of the motion for this animation")
                direction.currentIndexChanged.connect(
                    lambda _row, c=clip.id, i=index, f=str(family["key"]), box=direction:
                    self._switch_animation(c, i, f, direction=box.currentData()))
                layout.addWidget(self._labelled("Direction", direction))

        is_loop = instance.get("category") == "loop"
        duration = ScrubField("Cycle" if is_loop else "Duration",
                              int(instance.get("duration", 15)), 1,
                              max(1, int(clip.duration)), default=15, step=1, unit=" f",
                              label_width=LABEL_W,
                              tooltip=("Length of one pass of the loop, in frames: the "
                                       "cycle repeats across the clip")
                              if is_loop else
                              "Length of this preset's window, in frames")
        duration.committed.connect(
            lambda value, c=clip.id, i=index: self._commit_preset(
                c, i, "Preset duration", duration=int(value)))
        layout.addWidget(duration)

        if is_loop:
            mode = QComboBox()
            for key in PM.LOOP_MODES:
                mode.addItem(PM.LOOP_MODE_LABELS[key], key)
            mode.setCurrentIndex(max(0, mode.findData(PM.loop_mode(instance))))
            mode.setToolTip("Repeat: each cycle starts over · Ping-Pong: it runs back")
            mode.currentIndexChanged.connect(
                lambda _row, c=clip.id, i=index, box=mode: self._commit_preset(
                    c, i, "Loop mode", knobs={"loop_mode": box.currentData()}))
            layout.addWidget(self._labelled("Loop", mode))
            cycle = PM.loop_cycle(instance)
            period = PM.loop_period(instance)
            repeat_count = PM.loop_repeat(instance)
            span = PM.window(instance, clip.duration)[1]
            values = (
                ("coverage", "Span", span, 0, max(span, int(clip.duration)),
                 " f", "Frames this loop covers from its start (0 and the full clip "
                 "length both mean: to the end of the clip)"),
                ("repeat", "Cycles", min(repeat_count, 999), 0, 999, "",
                 "How many cycles to play; 0 repeats until the clip ends"),
                ("gap", "Gap", PM.loop_gap(instance), 0, max(period, cycle, 60), " f",
                 "Frames held at rest between cycles"),
                ("phase", "Phase", int(round(PM.loop_phase(instance))), -999, 999, " f",
                 "Shift the cycle pattern, in frames"),
            )
            for key, label, value, low, high, unit, tip in values:
                field = ScrubField(label, int(value), low, max(int(high), int(value), 1),
                                   default=0, step=1, unit=unit, label_width=LABEL_W,
                                   tooltip=tip)
                field.committed.connect(
                    lambda number, c=clip.id, i=index, k=key: self._commit_preset(
                        c, i, "Loop settings", knobs={k: int(number)}))
                layout.addWidget(field)


        for key, label, span in (("intensity", "Intensity", 200.0), ("stagger", "Stagger", 100.0),
                                 ("seed", "Seed", 9999.0)):
            if key == "stagger" and not (clip.is_text and knobs["unit"] != "whole"):
                continue
            field = ScrubField(label, float(knobs[key]) * (100.0 if key != "seed" else 1.0),
                                0.0, span, default=35.0 if key == "stagger" else 100.0,
                                step=1.0, decimals=0,
                                unit=" %" if key != "seed" else "",
                                label_width=LABEL_W,
                                tooltip={"intensity": "How far the preset pushes",
                                         "stagger": "Share of the window used to spread the units",
                                         "seed": "Random seed: the same seed gives the same frames"}[key])
            field.committed.connect(
                lambda value, c=clip.id, i=index, k=key: self._commit_preset(
                    c, i, "Preset settings",
                    knobs={k: (int(value) if k == "seed" else float(value) / 100.0)}))
            layout.addWidget(field)

        easing = QComboBox()
        easing.addItems(PM.EASINGS)
        easing.setCurrentText(str(knobs.get("easing") or PM.DEFAULT_EASING))
        easing.currentTextChanged.connect(
            lambda text_value, c=clip.id, i=index: self._commit_preset(
                c, i, "Preset easing", knobs={"easing": text_value}))
        layout.addWidget(self._labelled("Easing", easing))

        if clip.is_text and knobs["unit"] != "whole":
            direction = QComboBox()
            for key in PM.DIRECTIONS:
                direction.addItem(PM.DIRECTION_LABELS[key], key)
            direction.setCurrentIndex(max(0, direction.findData(knobs.get("direction"))))
            direction.currentIndexChanged.connect(
                lambda _row, c=clip.id, i=index, box=direction: self._commit_preset(
                    c, i, "Preset direction", knobs={"direction": box.currentData()}))
            layout.addWidget(self._labelled("Order", direction))
        if clip.is_text:
            unit = QComboBox()
            for key in PM.UNITS:
                unit.addItem(PM.UNIT_LABELS[key], key)
            unit.setCurrentIndex(max(0, unit.findData(knobs.get("unit"))))
            unit.currentIndexChanged.connect(
                lambda _row, c=clip.id, i=index, box=unit: self._commit_preset(
                    c, i, "Preset unit", knobs={"unit": box.currentData()}, rebuild=True))
            layout.addWidget(self._labelled("Unit", unit))
        return box

    def _animation_family(self, instance):
        """The browser family an instance came from (families fold presets by program)."""
        program = str(instance.get("program") or "")
        if not program:
            return None
        return next((family for family in FAMILIES.families()
                     if family.get("key") == program), None)

    def _switch_animation(self, clip_id, index, family_key, mode=None, direction=None):
        """Switch Mode/Direction of an applied animation without stacking another one."""
        clip = self.engine.timeline.find_clip(clip_id)
        if clip is None or not 0 <= int(index) < len(PM.instances(clip)):
            return
        instance = PM.instances(clip)[index]
        family = next((row for row in FAMILIES.families()
                       if row.get("key") == family_key), None)
        if family is None:
            return
        media = "text" if clip.is_text else "clip"
        mode = mode or instance.get("category", "in")
        if direction is None:
            direction = str((instance.get("args") or {}).get("dir") or "")
        preset = FAMILIES.variant(family, media, mode, direction)
        if not preset:
            return
        category, program, args, _knobs = LIB.preset_spec(preset, None)
        name = family.get("name") or "Animation"
        label = f"Change {name} to {FAMILIES.MODE_LABELS.get(category, category)}"
        if self.engine.session.switch_preset_variant(
                clip_id, index, label, preset.get("id", ""), category, program, args, name):
            self._built_key = None
            self.refresh_properties()
            self._notify()

    def _commit_preset(self, clip_id, index, label, knobs=None, duration=None, rebuild=False):
        if self.engine.session.update_preset(clip_id, index, label, knobs=knobs, duration=duration):
            self._notify()
        if rebuild:
            self._built_key = None
            self.refresh_properties()

    def _remove_preset(self, clip_id, index):
        if self.engine.session.remove_preset(clip_id, index):
            self._built_key = None
            self.refresh_properties()
            self._notify()

    def _bake_preset(self, clip_id, index):
        if self.engine.session.bake_preset(clip_id, index):
            self._built_key = None
            self.refresh_properties()
            self._notify()

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
        name = QLabel(entry.get("name") or effect.name)
        name.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        reset = _tool("reset", "Reset this effect to its defaults")
        reset.clicked.connect(lambda _=False, c=clip, e=entry, f=effect: self._reset_effect(c, e, f))
        more = _tool("dots", "More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        last = len(clip.effects) - 1
        if key == "look":
            menu.addAction("Remove look").triggered.connect(
                lambda _=False, c=clip, e=entry: self._remove_look(c, e))
            menu.addAction("Save stack as look\u2026").triggered.connect(
                lambda _=False, c=clip: self._save_look(c))
            look_id = (entry.get("params") or {}).get("look")
            if LOOKS.get(look_id) is not None:
                menu.addAction("Export look\u2026").triggered.connect(
                    lambda _=False, look=look_id: self._export_look(look))
        else:
            menu.addAction("Remove").triggered.connect(
                lambda _=False, c=clip, k=key: self._remove_effect(c, k))
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

    def _effect_cards(self, clip):
        """Effect cards in their real order, with every Style's members nested inside it."""
        groups = {}
        for position, entry in enumerate(clip.effects):
            if entry.get("effect_id") == "look" and entry.get("look_id"):
                groups[entry["look_id"]] = {"index": position, "members": []}
        for position, entry in enumerate(clip.effects):
            group = groups.get(entry.get("look_id"))
            if group is not None and group["index"] != position:
                group["members"].append(position)
        rendered = set()
        for index, entry in enumerate(clip.effects):
            if index in rendered:
                continue
            group = groups.get(entry.get("look_id"))
            if group is not None and group["index"] == index:
                members = list(group["members"])
                self._style_card(clip, index, entry, members)
                rendered.update(members)
                continue
            effect = next((e for e in self.engine.effects if e.key == entry.get("effect_id")), None)
            if effect is not None:
                self._effect_card(clip, index, effect, entry)

    def _style_card(self, clip, index, entry, members):
        """One collapsible group per applied Style: Strength first, its real effects below.

        The members are the ordinary effect entries the Style inserted, so they render,
        key and bake exactly like hand-added effects; the group only nests them.
        """
        group_id = entry.get("look_id") or f"style{index}"
        state_key = ("style", group_id)
        collapsed = state_key not in self._opened_groups
        frame, outer = self._card("look")
        header = _CardHeader("look")
        header.setToolTip("Drag to reorder the style group")
        head = QHBoxLayout(header)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(tokens.SP1)
        arrow = _tool("chevron_right" if collapsed else "chevron_down", "Collapse / expand")
        enabled = _tool("check", "Enable or disable the whole style", checkable=True)
        enabled.setChecked(entry.get("enabled", True))
        enabled.toggled.connect(lambda on, c=clip, e=entry: self._set_enabled(c, e, on))
        name = QLabel(entry.get("name") or "Style")
        name.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
        count = QLabel(f"{len(members)} effect{'s' if len(members) != 1 else ''}")
        count.setProperty("quiet", True)
        more = _tool("dots", "More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction("Remove style").triggered.connect(
            lambda _=False, c=clip, e=entry: self._remove_look(c, e))
        menu.addAction("Save stack as style\u2026").triggered.connect(
            lambda _=False, c=clip: self._save_look(c))
        look_id = (entry.get("params") or {}).get("look")
        if LOOKS.get(look_id) is not None:
            menu.addAction("Export style\u2026").triggered.connect(
                lambda _=False, look=look_id: self._export_look(look))
        more.setMenu(menu)
        more.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        head.addWidget(arrow)
        head.addWidget(enabled)
        head.addWidget(name)
        head.addWidget(count)
        head.addStretch(1)
        head.addWidget(more)
        outer.addWidget(header)

        body, form = self._form()
        look_effect = next((effect for effect in self.engine.effects if effect.key == "look"),
                           None)
        if look_effect is not None:
            self._effect_fields(form, clip, look_effect, entry)
        for position, member_index in enumerate(members):
            member = clip.effects[member_index]
            effect = next((item for item in self.engine.effects
                           if item.key == member.get("effect_id")), None)
            if effect is not None:
                self._style_member(form, clip, member_index, effect, member, group_id, position,
                                   last=(position == len(members) - 1))
        outer.addWidget(body)
        body.setVisible(not collapsed)

        def toggle(_=False, b=body, a=arrow, k=state_key):
            expanded = not b.isVisible()
            b.setVisible(expanded)
            a.setIcon(icon("chevron_down" if expanded else "chevron_right"))
            (self._opened_groups.add if expanded else self._opened_groups.discard)(k)
        arrow.clicked.connect(toggle)
        if not entry.get("enabled", True):
            body.setEnabled(False)
        self.properties_layout.addWidget(frame)

    def _style_member(self, form, clip, index, effect, entry, group_id, position, last=False):
        """A member effect of a Style: its own header, collapsed until asked for."""
        opened_key = ("member", group_id, position)
        expanded = opened_key in self._opened_members
        row = QFrame()
        row.setProperty("subCard", True)
        column = QVBoxLayout(row)
        column.setContentsMargins(tokens.SP1 + 2, 2, tokens.SP1 + 2, 2)
        column.setSpacing(2)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(tokens.SP1)
        arrow = _tool("chevron_down" if expanded else "chevron_right", "Expand this effect")
        enabled = _tool("check", "Enable or disable this effect", checkable=True)
        enabled.setChecked(entry.get("enabled", True))
        enabled.toggled.connect(lambda on, c=clip, e=entry: self._set_enabled(c, e, on))
        name = QLabel(entry.get("name") or effect.name)
        more = _tool("dots", "More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        reset = menu.addAction("Reset")
        reset.triggered.connect(lambda _=False, c=clip, e=entry, f=effect: self._reset_effect(c, e, f))
        up = menu.addAction("Move up in style")
        up.setEnabled(position > 0)
        up.triggered.connect(lambda _=False, c=clip, i=index: self._move_member(c, i, -1))
        down = menu.addAction("Move down in style")
        down.setEnabled(not last)
        down.triggered.connect(lambda _=False, c=clip, i=index: self._move_member(c, i, 1))
        menu.addSeparator()
        menu.addAction("Move out of style").triggered.connect(
            lambda _=False, c=clip, i=index: self._leave_style(c, i))
        menu.addAction("Remove").triggered.connect(
            lambda _=False, c=clip, i=index: self._remove_entry(c, i))
        more.setMenu(menu)
        more.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        header.addWidget(arrow)
        header.addWidget(enabled)
        header.addWidget(name)
        header.addStretch(1)
        header.addWidget(more)
        column.addLayout(header)
        body, inner = self._form()
        self._effect_fields(inner, clip, effect, entry)
        column.addWidget(body)
        body.setVisible(expanded)

        def toggle(_=False, b=body, a=arrow, k=opened_key):
            shown = not b.isVisible()
            b.setVisible(shown)
            a.setIcon(icon("chevron_down" if shown else "chevron_right"))
            (self._opened_members.add if shown else self._opened_members.discard)(k)
        arrow.clicked.connect(toggle)
        if not entry.get("enabled", True):
            body.setEnabled(False)
        form.addWidget(row)

    # ------------------------------------------------------- style member edits
    def _change_effects(self, clip, label, change):
        """Apply one edit to the clip's effect list as a single undo step."""
        def action(target):
            change(target.effects)
        self.engine.session.apply_to_clips([clip.id], label, action)
        self._notify()
        self.refresh_properties()

    def _move_member(self, clip, index, delta):
        """Swap a Style member with its neighbour, staying inside the group."""
        def change(effects):
            group = effects[index].get("look_id")
            target = index + delta
            if not 0 <= target < len(effects) or effects[target].get("look_id") != group:
                return
            effects[index], effects[target] = effects[target], effects[index]
        self._change_effects(clip, "Reorder style effect", change)

    def _leave_style(self, clip, index):
        """Pull an effect out of its Style: it keeps its settings, the group keeps the rest."""
        def change(effects):
            if 0 <= index < len(effects):
                effects[index].pop("look_id", None)
        self._change_effects(clip, "Move effect out of style", change)

    def _remove_entry(self, clip, index):
        def change(effects):
            if 0 <= index < len(effects):
                effects.pop(index)
        self._change_effects(clip, "Remove effect", change)

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
        """Effect controls in collapsible groups, with Advanced last.

        Sections come from ``ParamSpec.group``: a card only shows groups the effect really
        declares, so an audio effect grows no headings and no card has an empty one.
        Trackers therefore open on Detection and Visualization while Labels, Connectors
        and Face Effects stay folded until they are needed.
        """
        self._mix_row(form, clip, effect, entry)
        sections = {}
        color_done = False

        def target_for(spec):
            if not spec.group:
                return form
            body = sections.get(spec.group)
            if body is None:
                body = self._property_group(form, effect.key, spec.group)
                sections[spec.group] = body
            return body

        for spec in effect.param_specs():
            if spec.advanced:
                continue
            target = target_for(spec)
            if spec.name in COLOR_NAMES:
                if not color_done:
                    color_done = True
                    self._color_row(target, clip, effect, entry)
                continue
            self._param_row(target, clip, effect, entry, spec)
        # Advanced is one folded section at the very end of the card, after every group.
        advanced = [spec for spec in effect.param_specs() if spec.advanced]
        if advanced:
            _box, advanced_form = self._advanced_fold(form)
            for spec in advanced:
                self._param_row(advanced_form, clip, effect, entry, spec)

    def _param_row(self, target, clip, effect, entry, spec):
        """One parameter control (numeric, switch, choice or text) in `target`."""
        stored = entry.get("params", {}).get(spec.name, spec.default)
        animated = isinstance(stored, AnimatedValue)
        # Coerce through the parameter's own rules: a value the spec cannot express
        # (text in a number, NaN from an older project) becomes the default instead
        # of reaching a spin box, which used to take the process down.
        current = spec.clamp(parameter_value_at(stored, self._clip_frame(clip)))
        tip = spec.help or spec.label
        if spec.kind in ("int", "float"):
            field = ScrubField(spec.label, current, spec.minimum, spec.maximum,
                                default=spec.default, step=spec.step, unit=spec.unit,
                                decimals=0 if spec.kind == "int" else 2,
                                label_width=LABEL_W, tooltip=tip)
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
            self._rows.append({"kind": "toggle", "clip_id": clip.id, "entry": entry,
                               "name": spec.name, "box": box, "effect": effect,
                               "spec": spec})
            target.addWidget(box)
        elif spec.kind == "choice":
            target.addWidget(self._choice_row(clip, entry, spec, current, tip))
        elif spec.name == "palette_text":
            self._palette_row(target, clip, entry, spec, str(current or ""))
        else:
            edit = QLineEdit("" if current is None else str(current))
            clear = edit.addAction(icon("close", tokens.COLORS["muted"]),
                                   QLineEdit.ActionPosition.TrailingPosition)
            clear.setObjectName("clearText")
            clear.setToolTip(f"Clear {spec.label.lower()}")
            # The global toolbutton rule gives every button a 26 px minimum.  The
            # line edit's action button then hangs below its 28 px field, which is
            # the off-centre X the user sees.  Keep this one inside the field.
            clear_button = next((child for child in edit.findChildren(QToolButton)
                                 if child.defaultAction() is clear), None)
            if clear_button is not None:
                clear_button.setFixedSize(18, 18)
                clear_button.setStyleSheet(
                    "QToolButton { min-width: 18px; max-width: 18px; min-height: 18px; "
                    "max-height: 18px; padding: 0; margin: 0; border: 0; background: transparent; }")
            clear.triggered.connect(lambda _checked=False, c=clip, e=entry, n=spec.name,
                                    w=edit, s=spec: self._clear_text(c, e, n, w, s))
            edit.editingFinished.connect(lambda c=clip, e=entry, n=spec.name, w=edit, s=spec:
                                         self._commit_choice(c, e, n, w.text(), s))
            self._rows.append({"kind": "text_param", "clip_id": clip.id, "entry": entry,
                               "name": spec.name, "field": edit, "spec": spec})
            target.addWidget(self._labelled(spec.label, edit, tip))

    def _mix_row(self, form, clip, effect, entry):
        """The Mix slider every effect card gets (animatable like any other parameter)."""
        stored = entry.get("params", {}).get("mix", 100)
        animated = isinstance(stored, AnimatedValue)
        current = MIX_SPEC.clamp(parameter_value_at(stored, self._clip_frame(clip)))
        field = ScrubField(MIX_SPEC.label, current, MIX_SPEC.minimum, MIX_SPEC.maximum,
                            default=MIX_SPEC.default, step=MIX_SPEC.step, unit=MIX_SPEC.unit,
                            decimals=0, label_width=LABEL_W, tooltip=MIX_SPEC.help)
        field.interaction_started.connect(self._begin_edit)
        field.edited.connect(lambda value, c=clip, e=entry:
                             self._set_param(c, e, MIX_SPEC.name, value, live=True))
        field.committed.connect(lambda value, c=clip, e=entry:
                                self._commit_param(c, e, MIX_SPEC.name, value))
        stopwatch = self._stopwatch(clip, entry, MIX_SPEC.name, animated)
        field.add_trailing(stopwatch)
        cluster = self._key_cluster(clip, entry, MIX_SPEC.name)
        cluster.setVisible(animated)
        field.add_trailing(cluster)
        self._rows.append({"kind": "param", "clip_id": clip.id, "entry": entry,
                           "name": MIX_SPEC.name, "field": field, "stopwatch": stopwatch,
                           "cluster": cluster, "effect": effect, "spec": MIX_SPEC})
        form.addWidget(field)

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

    def _property_group(self, form, effect_key, title):
        """A collapsible section inside an effect card (Detection, Labels, Connectors...).

        The section is only created when the effect declares members for it, so no card
        shows an empty heading. Whether it is open is remembered per effect and group, and
        starts open only for :data:`OPEN_PROPERTY_GROUPS`.
        """
        state_key = (effect_key, title)
        expanded = self._property_groups.get(state_key, title in OPEN_PROPERTY_GROUPS)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(tokens.SP1)
        toggle = QToolButton()
        toggle.setText(title)
        toggle.setIcon(icon("chevron_down" if expanded else "chevron_right"))
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.setCheckable(True)
        toggle.setChecked(expanded)
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setToolTip(f"{title}: click to fold or unfold this group")
        toggle.setStyleSheet("QToolButton { border: 0; background: transparent; "
                             "font-weight: 600; padding: 2px 0; }")
        body, inner = self._form()
        body.setVisible(expanded)

        def flip(on, b=body, t=toggle, key=state_key):
            b.setVisible(bool(on))
            t.setIcon(icon("chevron_down" if on else "chevron_right"))
            self._property_groups[key] = bool(on)
        toggle.toggled.connect(flip)
        column.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        column.addWidget(body)
        form.addWidget(holder)
        # Exposed for tests and for the panel's own state queries.
        holder.group_toggle = toggle
        holder.group_body = body
        holder.group_title = title
        return inner

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
            elif kind == "duration":
                row["field"].set_value(clip.duration)
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
            elif kind == "toggle":
                entry = row["entry"]
                if not any(entry is e for e in clip.effects):
                    continue
                stored = (entry.get("params") or {}).get(row["name"])
                row["box"].blockSignals(True)
                row["box"].setChecked(bool(row["spec"].clamp(stored)))
                row["box"].blockSignals(False)
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
            if target_entry is None:
                return
            self._write_param(target, target_entry, name, value, frame)
            if name in STYLE_KEYS and str(value) != "custom":
                # A preset is a starting point.  The first manual change after choosing
                # one returns the Look dropdown to Custom, so the visible controls and
                # the renderer can never disagree about which values are in force.
                self._write_param(target, target_entry, "viz_preset", "custom", frame)
            if name == "viz_preset" and value != "custom":
                # Store the preset's values as ordinary parameters too, so switching
                # back to Custom starts from what the user saw instead of stale defaults.
                runtime = self.engine.runtime_effects.get(entry["effect_id"])
                valid = {spec.name for spec in runtime.param_specs()} if runtime else set()
                for key, preset_value in TRACKING_STYLES.get(value, {}).items():
                    if key in valid:
                        self._write_param(target, target_entry, key, preset_value, frame)

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

    def _clear_text(self, clip, entry, name, edit, spec):
        if not edit.text():
            return
        edit.clear()
        self._commit_choice(clip, entry, name, "", spec)

    def _set_enabled(self, clip, entry, enabled):
        self.engine.session.apply_to_clips([clip.id], "Toggle effect",
                                           lambda c: self._entry(c, entry["effect_id"]).__setitem__("enabled", enabled))
        self._notify()
        self.refresh_properties()

    @staticmethod
    def _entry(clip, key):
        return next(e for e in clip.effects if e["effect_id"] == key)

    def _remove_effect(self, clip, key):
        self.engine.session.apply_to_clips([clip.id], "Remove effect",
                                           lambda c: setattr(c, "effects", [e for e in c.effects if e["effect_id"] != key]))
        self._notify()
        self.refresh_properties()

    def _move_effect(self, clip, key, direction):
        def move(c):
            i = next((i for i, e in enumerate(c.effects) if e["effect_id"] == key), None)
            j = i + direction if i is not None else -1
            if i is not None and 0 <= j < len(c.effects):
                c.effects[i], c.effects[j] = c.effects[j], c.effects[i]
        self.engine.session.apply_to_clips([clip.id], "Reorder effect", move)
        self._notify()
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
            self._notify()
            self.refresh_properties()

    def _reset_effect(self, clip, entry, effect):
        defaults = {spec.name: spec.default for spec in effect.param_specs()}
        # Reset leaves the card at full strength, like a freshly added effect.
        defaults["mix"] = MIX_SPEC.default

        def reset(c):
            target = self._entry(c, entry["effect_id"])
            target["params"] = {**target.get("params", {}), **defaults}
        self.engine.session.apply_to_clips([clip.id], "Reset effect", reset)
        self._notify()
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
        self._notify()
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
