"""The Presets browser: a tab beside Effects.

Categories on top of the library (:mod:`meenifx.model.preset_library`), a search box, star
favourites, and a small animated preview of whatever the pointer is over. A preset is
applied with a double-click or by dragging it onto a clip in the timeline.

The hover preview runs the *real* preset programs against a throwaway clip, so what the
thumbnail shows is what the clip will do - not a hand-drawn approximation.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QMimeData, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QFont, QPainter, QPen
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QToolButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import constants, tokens
from ..model import preset_library as LIB
from ..model import presets as P
from .filedialogs import open_file, save_file
from .layout import app_settings

PRESET_MIME = constants.PRESET_MIME
FAVORITES_KEY = "presets/favorites"
FILTER = "meeniFX preset (*.mfxpreset *.json)"
CATEGORY_ORDER = LIB.CATEGORY_KEYS


class _PresetTree(QTreeWidget):
    """Drag source: the payload is "<preset id>|<unit>"."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setIndentation(12)
        self.setDragEnabled(True)
        self.setMouseTracking(True)
        self.setAlternatingRowColors(False)

    def startDrag(self, _actions):
        item = self.currentItem()
        preset = self.panel.preset_for_item(item)
        if preset is None:
            return
        mime = QMimeData()
        mime.setData(PRESET_MIME, f"{preset['id']}|{self.panel.unit()}".encode("utf-8"))
        mime.setText(preset["name"])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class _PresetPreview(QWidget):
    """A 2-second loop of the hovered preset, drawn with the real programs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(72)
        self.setMinimumWidth(120)
        self._preset = None
        # A fake clip is all the model needs: duration and the instances on it.
        self._clip = type("PreviewClip", (), {"duration": 36, "presets": [], "is_text": True})()
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._tick)

    def set_preset(self, preset, unit="whole"):
        self._preset = preset
        self._frame = 0
        self._clip.presets = []
        if preset is not None:
            category, program, args, knobs = LIB.preset_spec(preset, unit)
            clips = self._clip
            clips.is_text = LIB.CATEGORY_INFO[preset["category"]][1] == "text"
            instance = P.make(preset["id"], category, program, args, knobs,
                              duration=22, name=preset["name"], clip=clips)
            instance["start"] = 0
            instance["duration"] = 22
            instance["knobs"]["unit"] = knobs.get("unit", "whole")
            clips.presets = [instance]
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self):
        self._frame = (self._frame + 1) % 44
        self.update()

    def _units(self, count, width, height):
        step = min(26.0, (width - 24.0) / max(1, count))
        left = width / 2.0 - step * (count - 1) / 2.0
        return [(left + index * step, height / 2.0, "Aa"[index % 2] if count == 2 else "A")
                for index in range(count)]

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(tokens.COLORS["border"]), 1))
        painter.setBrush(QColor(tokens.COLORS["panel_alt"]))
        painter.drawRoundedRect(rect, 4, 4)
        if self._preset is None:
            painter.setPen(QColor(tokens.COLORS["muted"]))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Hover a preset")
            painter.end()
            return
        is_text = LIB.CATEGORY_INFO[self._preset["category"]][1] == "text"
        units = self._units(3, rect.width(), rect.height()) if is_text else []
        effect = P.evaluate(self._clip, self._frame, units={"letter": units, "word": [], "line": []},
                            project_size=(rect.width(), rect.height()))
        pose = effect.pose
        painter.setOpacity(max(0.0, min(1.0, pose.opacity)))
        if not is_text:
            # A little "picture" that the clip preset moves around.
            painter.save()
            painter.translate(rect.center().x() + pose.dx, rect.center().y() + pose.dy)
            painter.rotate(pose.rotation)
            painter.scale(max(0.02, pose.scale_x), max(0.02, pose.scale_y))
            painter.setBrush(QColor(tokens.COLORS["accent"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(-30, -18, 60, 36), 4, 4)
            painter.restore()
        else:
            font = QFont("Segoe UI", 15, QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(tokens.COLORS["text"]))
            for index, (x, y, text_value) in enumerate(units):
                unit = effect.unit_poses["letter"][index] if index < len(effect.unit_poses["letter"]) else None
                dx = getattr(unit, "dx", 0.0) + pose.dx
                dy = getattr(unit, "dy", 0.0) + pose.dy
                rotation = getattr(unit, "rotation", 0.0) + pose.rotation
                scale = getattr(unit, "scale_x", 1.0) * pose.scale_x
                painter.save()
                painter.setOpacity(max(0.0, min(1.0, getattr(unit, "opacity", 1.0) * pose.opacity)))
                painter.translate(x + dx, y + dy)
                painter.rotate(rotation)
                painter.scale(max(0.05, scale), max(0.05, scale))
                painter.drawText(QRectF(-16, -12, 32, 24), Qt.AlignmentFlag.AlignCenter, text_value)
                painter.restore()
        painter.end()


class PresetsPanel(QWidget):
    """Preset browser: categories, search, favourites, preview and the apply actions."""

    applied = pyqtSignal()            # a preset was added: refresh the inspector
    properties_requested = pyqtSignal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search presets")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self.reload())
        self.unit_box = QComboBox()
        for name in ("letter", "word", "line", "whole"):
            self.unit_box.addItem(P.UNIT_LABELS[name], name)
        self.unit_box.setToolTip("Text unit a text preset animates when you apply it")
        self.favorite_only = QToolButton()
        self.favorite_only.setText("★")
        self.favorite_only.setCheckable(True)
        self.favorite_only.setToolTip("Only show favourites")
        self.favorite_only.toggled.connect(lambda _on: self.reload())
        self.tree = _PresetTree(self)
        self.tree.setToolTip("Double-click to apply, or drag onto a clip")
        self.tree.setMinimumHeight(40)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.itemEntered.connect(self._on_hover)
        self.preview = _PresetPreview()
        self.caption = QLabel("Hover a preset to see it move")
        self.caption.setProperty("quiet", True)
        self.caption.setWordWrap(True)
        # Saving, importing and exporting live in one compact menu: the dock has to stay
        # narrow enough not to squeeze the timeline next to it.
        self.more = QToolButton()
        self.more.setText("⋯")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more.setToolTip("Save the selected clip's animation as a preset, import or "
                             "export a .mfxpreset file")
        from PyQt6.QtWidgets import QMenu
        library_menu = QMenu(self.more)
        library_menu.addAction("Save as preset…", self.save_from_selection)
        library_menu.addAction("Import preset…", self.import_preset)
        library_menu.addAction("Export preset…", self.export_selected)
        self.more.setMenu(library_menu)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(tokens.SP2)
        top.addWidget(self.unit_box)
        top.addWidget(self.favorite_only)
        top.addWidget(self.more)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SP2, tokens.SP2, tokens.SP2, tokens.SP2)
        layout.setSpacing(tokens.SP2)
        layout.addWidget(self.search)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.preview)
        layout.addWidget(self.caption)
        self.reload()

    # ------------------------------------------------------------------ library
    def unit(self):
        return self.unit_box.currentData() or "whole"

    def favorites(self):
        raw = str(app_settings().value(FAVORITES_KEY, "") or "")
        return {name for name in raw.split(",") if name}

    def _set_favorites(self, names):
        app_settings().setValue(FAVORITES_KEY, ",".join(sorted(names)))

    def _toggle_favorite(self, preset_id):
        names = self.favorites()
        names.symmetric_difference_update({preset_id})
        self._set_favorites(names)
        self.reload()

    def preset_for_item(self, item):
        if item is None:
            return None
        preset_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not preset_id:
            return None
        return LIB.load().get(preset_id)

    def preset_items(self):
        """Every preset the tree is currently listing, in tree order."""
        found = []
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            for child_index in range(top.childCount()):
                preset = self.preset_for_item(top.child(child_index))
                if preset is not None:
                    found.append(preset)
        return found

    def reload(self):
        term = self.search.text()
        favorites = self.favorites()
        only_favorites = self.favorite_only.isChecked()
        matches = {preset["id"]: preset for preset in LIB.search(term)}
        collapsed = {self.tree.topLevelItem(i).text(0)
                     for i in range(self.tree.topLevelItemCount())
                     if not self.tree.topLevelItem(i).isExpanded()}
        self.tree.clear()
        grouped = LIB.by_category(matches)
        shown = 0
        for key in CATEGORY_ORDER:
            rows = [preset for preset in grouped.get(key, [])
                    if preset["id"] in matches and (not only_favorites or preset["id"] in favorites)]
            if not rows:
                continue
            top = QTreeWidgetItem([LIB.CATEGORY_TITLES[key]])
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = QFont(self.font())
            font.setBold(True)
            top.setFont(0, font)
            self.tree.addTopLevelItem(top)
            for preset in rows:
                label = ("★ " if preset["id"] in favorites else "") + preset["name"]
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.ItemDataRole.UserRole, preset["id"])
                item.setToolTip(0, " / ".join([preset["name"], LIB.CATEGORY_TITLES[key]]
                                              + preset.get("keywords", [])))
                top.addChild(item)
                shown += 1
            top.setExpanded(bool(term) or LIB.CATEGORY_TITLES[key] not in collapsed)
        if not shown:
            top = QTreeWidgetItem(["No presets match"])
            top.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(top)
        self.caption.setText("Double-click or drag onto a clip" if shown
                             else "Adjust the search or the favourites filter")

    # ------------------------------------------------------------------ actions
    def _on_hover(self, item, _column=0):
        preset = self.preset_for_item(item)
        self.preview.set_preset(preset, self.unit())
        if preset is not None:
            self.caption.setText(f"{preset['name']} — {LIB.CATEGORY_TITLES[preset['category']]}")

    def _on_double_click(self, item, _column=0):
        preset = self.preset_for_item(item)
        if preset is not None:
            self.apply(preset)

    def apply(self, preset, ids=None):
        added = self.engine.session.apply_preset(preset, self.unit(), ids)
        if added:
            self.applied.emit()
            self.properties_requested.emit()
        return added

    def contextMenuEvent(self, event):
        item = self.tree.itemAt(self.tree.mapFrom(self, event.pos()))
        preset = self.preset_for_item(item)
        if preset is None:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("Apply to Selected Clips", lambda: self.apply(preset))
        favorite = menu.addAction("Remove from Favourites" if preset["id"] in self.favorites()
                                  else "Add to Favourites")
        favorite.triggered.connect(lambda: self._toggle_favorite(preset["id"]))
        menu.addSeparator()
        menu.addAction("Export…", lambda: self._export(preset))
        menu.exec(event.globalPos())

    def save_from_selection(self):
        clips = [self.engine.timeline.find_clip(cid) for cid in self.engine.session.selected]
        clip = next((clip for clip in clips if clip is not None and getattr(clip, "presets", None)),
                    None)
        if clip is None:
            self.engine.session.status.emit(
                "Apply a preset to a clip first, then save that animation as a preset")
            return
        name, ok = QInputDialog.getText(self, "Save as preset", "Preset name:",
                                        text=f"{clip.name} animation")
        if not ok or not name.strip():
            return
        payload = P.saved_as_preset(clip, _slug(name), name=name.strip())
        if payload is None:
            return
        path, preset = LIB.write_user_preset(payload)
        if path is None:
            QMessageBox.warning(self, "Save as preset", "Could not write that preset.")
            return
        self.reload()
        self.engine.session.status.emit(f"Saved preset '{preset['name']}'")

    def import_preset(self):
        path = open_file(self, "Import preset", LIB.user_folder(), FILTER)
        if not path:
            return
        written, preset = LIB.import_file(path)
        if written is None:
            QMessageBox.warning(self, "Import preset",
                                "That file is not a meeniFX preset (or uses a newer version).")
            return
        self.reload()
        self.engine.session.status.emit(f"Imported preset '{preset['name']}'")

    def export_selected(self):
        item = self.tree.currentItem()
        preset = self.preset_for_item(item)
        if preset is None:
            self.engine.session.status.emit("Select a preset to export")
            return
        self._export(preset)

    def _export(self, preset):
        path = save_file(self, "Export preset", os.path.join(LIB.user_folder(), preset["id"]),
                         FILTER)
        if not path:
            return
        written = LIB.export_file(preset["id"], path)
        if written:
            self.engine.session.status.emit(f"Exported preset to {os.path.basename(written)}")


def _slug(name):
    text = "".join(char.lower() if char.isalnum() else "_" for char in str(name)).strip("_")
    while "__" in text:
        text = text.replace("__", "_")
    return text or "preset"
