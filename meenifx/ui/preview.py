"""Qt preview widget with screen-space fit/zoom/pan and QImage painting.

Two sizes exist and are never confused:

* the **logical** frame size (the project resolution) drives fit, zoom, pan, guides
  and every crop/clip decision - none of those can change when the render quality
  changes;
* the **render** size (logical size x quality) only controls how many pixels the
  image carries, i.e. sharpness.

The rendered image is therefore always drawn into the whole logical frame rectangle,
cropping it in normalized 0..1 coordinates for panning/zooming. A lowered preview
quality can only make the picture softer.
"""

import cv2
import numpy as np
from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton,
                             QVBoxLayout, QWidget)

from .. import tokens
from ..constants import MEDIA_MIME
from ..icons import icon
from .master_meter import MasterMeter
from .themed import bind_theme

# Preview quality choices -> render scale in percent (Auto is picked from the panel size).
QUALITY_LABELS = ("Full", "1/2", "1/4", "1/8", "Auto")
QUALITY_PERCENT = {"Full": 100.0, "1/2": 50.0, "1/4": 25.0, "1/8": 12.5}


class PreviewWidget(QWidget):
    frame_size_changed = pyqtSignal(int, int)
    zoom_changed = pyqtSignal(str, int)

    def __init__(self, parent=None, quality_getter=None):
        super().__init__(parent)
        self.setObjectName("previewCanvas")
        # The preview is the star of the window: it takes every spare pixel and keeps a
        # usable floor so the dock can never collapse it to nothing.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 150)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        # The transform modifier keys, where the gestures happen. Ctrl is the free
        # transform's aspect lock; the inspector switches mirror all three.
        self.setToolTip("Corner handles resize freely and keep the opposite corner fixed. "
                         "Ctrl resizes proportionally from the centre, Shift from the "
                         "centre without a ratio lock. Side handles change one axis; drag "
                         "inside to move, the knob to rotate, the crosshair to move the anchor.")
        # Every colour is painted from the active tokens, so the workspace follows the
        # theme instead of staying dark after a light-theme switch.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        bind_theme(self, lambda w: w.update())
        self._checker = None
        self._quality_getter = quality_getter or (lambda: 100)
        self._image = QImage()
        self._frame_size = (0, 0)      # logical/project size only
        self._render_size = (0, 0)     # pixels actually held by the image
        self.fit_mode = True
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.guides = False
        self._panning = False
        self._last_pos = QPointF()
        self._frame_key = None
        # Filled in by MainWindow: the interactive transform/warp overlay. It paints
        # after the picture and only here, so it can never reach the exported frames.
        self.overlay = None
        # Set by MainWindow: paints real FPS/dropped/cache/backend numbers when enabled.
        self.perf_overlay = None
        self.setAcceptDrops(True)

    def minimumSizeHint(self):
        return QSize(240, 150)

    def sizeHint(self):
        return QSize(960, 540)

    @staticmethod
    def to_qimage(frame):
        """Convert a BGR/RGB/RGBA numpy frame; QImage owns its copied pixels safely."""
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return QImage()
        if frame.ndim == 2:
            height, width = frame.shape
            image = QImage(frame.data, width, height, frame.strides[0], QImage.Format.Format_Grayscale8)
        elif frame.shape[2] == 3:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width = rgb.shape[:2]
            image = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888)
        else:
            rgba = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGBA)
            height, width = rgba.shape[:2]
            image = QImage(rgba.data, width, height, rgba.strides[0], QImage.Format.Format_RGBA8888)
        return image.copy()

    def set_frame(self, frame, *, quality=None, frame_key=None, logical_size=None):
        if frame is None or not isinstance(frame, np.ndarray) or frame.size == 0:
            return
        render_h, render_w = frame.shape[:2]
        if logical_size and logical_size[0] and logical_size[1]:
            logical_w, logical_h = int(logical_size[0]), int(logical_size[1])
        else:
            logical_w, logical_h = render_w, render_h
        key = frame_key if frame_key is not None else (logical_w, logical_h)
        if (logical_w, logical_h) != self._frame_size or key != self._frame_key:
            self._frame_size, self._frame_key = (logical_w, logical_h), key
            self.fit()
            self.frame_size_changed.emit(logical_w, logical_h)
        self._render_size = (render_w, render_h)
        self._image = self.to_qimage(frame)
        self.update()
        self._emit_zoom()

    def clear_frame(self):
        """Forget the picture: an empty timeline must never keep showing a stale frame.

        The frame size is kept, so a clip added afterwards keeps the zoom the user set;
        paintEvent already draws the empty workspace when there is no image.
        """
        self._image = QImage()
        self._render_size = (0, 0)
        self.update()

    # ------------------------------------------------------------------ geometry
    def fit_scale(self):
        w, h = self._frame_size
        if not w or not h:
            return 1.0
        return max(0.001, min(self.width() / w, self.height() / h))

    def current_scale(self):
        if self.fit_mode:
            self.zoom = self.fit_scale()
        return self.zoom

    def frame_rect(self):
        w, h = self._frame_size
        if not w or not h:
            return QRectF()
        scale = self.current_scale()
        left = (self.width() - w * scale) / 2.0 + self.pan_x
        top = (self.height() - h * scale) / 2.0 + self.pan_y
        return QRectF(left, top, w * scale, h * scale)

    def fit(self):
        self.fit_mode = True
        self.zoom = self.fit_scale()
        self.pan_x = self.pan_y = 0.0
        self.update()
        self._emit_zoom()

    def set_zoom(self, zoom, anchor=None):
        if not self._frame_size[0]:
            self.fit_mode = zoom is None
            self.update()
            return
        if zoom is None:
            self.fit()
            return
        point = anchor or QPointF(self.width() / 2, self.height() / 2)
        old_rect = self.frame_rect()
        frame_x = (point.x() - old_rect.left()) / max(old_rect.width(), 1e-9) * self._frame_size[0]
        frame_y = (point.y() - old_rect.top()) / max(old_rect.height(), 1e-9) * self._frame_size[1]
        self.fit_mode = False
        self.zoom = max(0.05, min(12.0, float(zoom)))
        self.pan_x = point.x() - frame_x * self.zoom - (self.width() - self._frame_size[0] * self.zoom) / 2
        self.pan_y = point.y() - frame_y * self.zoom - (self.height() - self._frame_size[1] * self.zoom) / 2
        self._clamp_pan()
        self.update()
        self._emit_zoom()

    def _clamp_pan(self):
        if self.fit_mode or not self._frame_size[0]:
            self.pan_x = self.pan_y = 0.0
            return
        w, h = self._frame_size
        sw, sh = w * self.zoom, h * self.zoom
        centered_x, centered_y = (self.width() - sw) / 2, (self.height() - sh) / 2
        margin_x, margin_y = min(40.0, self.width(), sw), min(40.0, self.height(), sh)
        self.pan_x = min(max(self.pan_x, margin_x - sw - centered_x), self.width() - margin_x - centered_x)
        self.pan_y = min(max(self.pan_y, margin_y - sh - centered_y), self.height() - margin_y - centered_y)

    def _emit_zoom(self):
        if self.fit_mode:
            self.zoom_changed.emit("Fit", int(round(self.fit_scale() * 100)))
        else:
            self.zoom_changed.emit(f"{int(round(self.zoom * 100))}%", int(round(self.zoom * 100)))

    def resizeEvent(self, event):
        if self.fit_mode:
            self.zoom = self.fit_scale()
            self.pan_x = self.pan_y = 0.0
        else:
            self._clamp_pan()
        self.update()
        self._emit_zoom()
        super().resizeEvent(event)

    def _checker_brush(self):
        """A small themed checkerboard for transparent parts of the composition."""
        key = (tokens.theme_name(), round(self.devicePixelRatioF(), 2))
        if self._checker is not None and self._checker[0] == key:
            return self._checker[1]
        dpr = max(1.0, self.devicePixelRatioF())
        tile = 12
        pixmap = QPixmap(int(tile * 2 * dpr), int(tile * 2 * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(QColor(tokens.COLORS["panel_alt"]))
        painter = QPainter(pixmap)
        painter.fillRect(0, 0, tile, tile, QColor(tokens.COLORS["panel_soft"]))
        painter.fillRect(tile, tile, tile, tile, QColor(tokens.COLORS["panel_soft"]))
        painter.end()
        self._checker = (key, QBrush(pixmap))
        return self._checker[1]

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # The workspace around the composition follows the UI theme; the picture itself
        # remains untouched and the transparent checkerboard lives only inside the frame.
        painter.fillRect(self.rect(), QColor(tokens.COLORS["bg"]))
        if self._image.isNull() or not self._frame_size[0]:
            # Nothing to show yet, but the statistics are live: an empty timeline still
            # answers "is the overlay on?" instead of drawing nothing at all.
            self._paint_perf_overlay(painter)
            painter.end()
            return
        rect = self.frame_rect()
        visible = rect.intersected(QRectF(self.rect()))
        if visible.isEmpty():
            self._paint_perf_overlay(painter)
            painter.end()
            return
        # Normalized coordinates inside the logical frame: the image (whatever its
        # render resolution) is mapped through the same 0..1 window, so quality can
        # never shift the framing.
        u0 = (visible.left() - rect.left()) / rect.width()
        v0 = (visible.top() - rect.top()) / rect.height()
        uw = visible.width() / rect.width()
        vh = visible.height() / rect.height()
        image_w, image_h = self._image.width(), self._image.height()
        source_rect = QRectF(u0 * image_w, v0 * image_h, uw * image_w, vh * image_h)
        painter.save()
        painter.setClipRect(visible)
        painter.fillRect(rect, self._checker_brush())
        painter.drawImage(visible, self._image, source_rect)
        painter.setClipping(False)
        # The frame edge is outlined just outside the picture: drawing it on the frame rect
        # itself painted a light grey line over the picture's first row and column (and was
        # clipped to nothing on the other two sides), which looked like a stray white border
        # and made the preview differ from the exported frame at the edges.
        painter.setPen(QPen(QColor(tokens.COLORS["border_strong"]), 1))
        painter.drawRect(rect.adjusted(-1, -1, 1, 1))
        if self.guides:
            painter.setPen(QPen(QColor(220, 220, 220, 180), 1, Qt.PenStyle.DashLine))
            for margin in (0.05, 0.10):
                painter.drawRect(rect.adjusted(rect.width()*margin, rect.height()*margin,
                                               -rect.width()*margin, -rect.height()*margin))
            cx, cy = rect.center().x(), rect.center().y()
            painter.drawLine(QPointF(cx - 24, cy), QPointF(cx + 24, cy))
            painter.drawLine(QPointF(cx, cy - 24), QPointF(cx, cy + 24))
        if self.overlay is not None:
            self.overlay.paint(painter)
        painter.restore()
        self._paint_perf_overlay(painter)
        painter.end()

    def _paint_perf_overlay(self, painter):
        """The FPS/cache HUD, over the picture but never part of it."""
        if self.perf_overlay is not None:
            self.perf_overlay.paint(painter, QRectF(self.rect()))

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.set_zoom(self.current_scale() * (1.15 ** steps), event.position())
        event.accept()

    def contextMenuEvent(self, event):
        """Right-click the picture for the puppet pin actions when pins are in play."""
        overlay = self.overlay
        clip = overlay.primary() if overlay is not None else None
        entry = overlay._effect_entry(clip, "puppet_warp") if overlay is not None and clip else None
        if entry is None:
            super().contextMenuEvent(event)
            return
        pins = overlay.selected_pins()
        menu = QMenu(self)
        menu.addAction("Select All Pins", overlay.select_all_pins).setEnabled(bool(entry.get("pins")))
        menu.addSeparator()
        copy = menu.addAction("Copy Pin Keys", overlay.copy_pin_keys)
        copy.setShortcut("Ctrl+C")
        copy.setEnabled(bool(pins))
        paste = menu.addAction("Paste Pin Keys", overlay.paste_pin_keys)
        paste.setShortcut("Ctrl+V")
        paste.setEnabled(bool(pins))
        reset = menu.addAction("Reset Pins to Rest", overlay.reset_selected_pins)
        reset.setShortcut("Ctrl+R")
        reset.setEnabled(bool(pins))
        menu.addSeparator()
        locked = bool(pins) and all(bool(pin.get("locked")) for pin in pins)
        menu.addAction("Unlock Pin" if locked else "Lock Pin", overlay.toggle_pin_lock).setEnabled(bool(pins))
        enabled = bool(pins) and all(bool(pin.get("enabled", True)) for pin in pins)
        menu.addAction("Disable Pin" if enabled else "Enable Pin",
                       overlay.toggle_pin_enabled).setEnabled(bool(pins))
        menu.addSeparator()
        delete = menu.addAction("Delete Pin" if len(pins) < 2 else f"Delete {len(pins)} Pins",
                                overlay.delete_selection)
        delete.setEnabled(bool(pins))
        menu.exec(event.globalPos())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            if event.type() == event.Type.MouseButtonDblClick:
                self.fit()
                return
            self._panning = True
            self._last_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and event.type() == event.Type.MouseButtonDblClick:
            self.fit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.overlay is not None:
            if self.overlay.mouse_press(event):
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._last_pos
            self._last_pos = event.position()
            self.fit_mode = False
            self.pan_x += delta.x()
            self.pan_y += delta.y()
            self._clamp_pan()
            self.update()
            self._emit_zoom()
            event.accept()
            return
        if self.overlay is not None and self.overlay.mouse_move(event):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._panning and event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        if self.overlay is not None and self.overlay.mouse_release(event):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        """Arrow keys nudge the selected clip; Delete removes a pin or path point."""
        overlay = self.overlay
        if overlay is not None:
            key = event.key()
            # Escape always means "abandon this": a shape being drawn leaves the draft
            # behind and puts the normal tool back.
            if key == Qt.Key.Key_Escape and overlay.cancel_shape():
                event.accept()
                return
            step = 1.0
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                step = 10.0
            elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                step = 0.1
            nudges = {Qt.Key.Key_Left: (-step, 0.0), Qt.Key.Key_Right: (step, 0.0),
                      Qt.Key.Key_Up: (0.0, -step), Qt.Key.Key_Down: (0.0, step)}
            if key in nudges:
                dx, dy = nudges[key]
                if overlay.nudge(dx, dy, fine=step < 1.0):
                    event.accept()
                    return
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and overlay.delete_selection():
                event.accept()
                return
            # Pin keyframes: Ctrl+C/Ctrl+V copy and paste, Ctrl+R resets the pins to rest.
            if key == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if overlay.copy_pin_keys():
                    event.accept()
                    return
            if key == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if overlay.paste_pin_keys():
                    event.accept()
                    return
            if (key == Qt.Key.Key_R and event.modifiers() & Qt.KeyboardModifier.ControlModifier
                    and overlay.reset_selected_pins()):
                event.accept()
                return
            if key == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if overlay.select_all_pins():
                    event.accept()
                    return
            if key == Qt.Key.Key_E and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if overlay.toggle_pin_enabled():
                    event.accept()
                    return
            if key == Qt.Key.Key_L and overlay.toggle_pin_lock():
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                overlay.cancel_drag()
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton or event.button() == Qt.MouseButton.LeftButton:
            self.fit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # Space is the global play/pause shortcut, so the preview leaves it to the window
    # action and pans with the middle mouse button only.


class PreviewPanel(QWidget):
    """Preview image area, transport bar and view controls."""
    zoom_preset = pyqtSignal(str)
    guides_changed = pyqtSignal(bool)
    quality_changed = pyqtSignal(str)
    save_frame_requested = pyqtSignal()
    files_dropped = pyqtSignal(list)
    media_ids_dropped = pyqtSignal(list)
    play_toggled = pyqtSignal()
    step_requested = pyqtSignal(int)
    start_requested = pyqtSignal()
    end_requested = pyqtSignal()

    def __init__(self, quality_getter, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(300, 220)
        self.view = PreviewWidget(self, quality_getter)
        # One control row under the picture: timecode | transport | view options.
        self.start_button = self._transport_button("skip_start", "Go to the start of the timeline (Home)")
        self.back_button = self._transport_button("step_back", "Step back one frame (Left)")
        self.play_button = self._transport_button("play", "Play (Space)", checkable=True, primary=True)
        self.forward_button = self._transport_button("step_forward", "Step forward one frame (Right)")
        self.end_button = self._transport_button("skip_end", "Go to the end of the timeline (End)")
        self.start_button.clicked.connect(lambda _=False: self.start_requested.emit())
        self.back_button.clicked.connect(lambda _=False: self.step_requested.emit(-1))
        self.play_button.clicked.connect(lambda _=False: self.play_toggled.emit())
        self.forward_button.clicked.connect(lambda _=False: self.step_requested.emit(1))
        self.end_button.clicked.connect(lambda _=False: self.end_requested.emit())
        self.timecode = QLabel("00:00:00:00")
        self.timecode.setObjectName("previewTimecode")
        self.timecode.setToolTip("Playhead position")
        self.timecode.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.timecode.setMinimumWidth(96)
        self.timecode.setStyleSheet(f"font-family: Consolas, 'Courier New', monospace;"
                                    f" font-size: {tokens.FONT_TITLE}px; font-weight: 600;"
                                    f" color: {tokens.COLORS['text']};")
        left = QHBoxLayout()
        left.addWidget(self.timecode)
        left.addStretch(1)
        center = QHBoxLayout()
        center.setSpacing(tokens.SP1)
        for button in (self.start_button, self.back_button, self.play_button,
                       self.forward_button, self.end_button):
            center.addWidget(button)
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItems(["Fit", "25%", "50%", "100%", "200%", "400%"])
        self.zoom_combo.setToolTip("Preview zoom (Fit scales the picture to the panel)")
        self.zoom_combo.setFixedHeight(tokens.CONTROL_H)
        self.zoom_combo.currentTextChanged.connect(self.zoom_preset)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(QUALITY_LABELS)
        self.quality_combo.setCurrentText("1/2")
        self.quality_combo.setToolTip("Preview render quality: lower is faster, the project resolution "
                                      "and exports are never affected")
        self.quality_combo.setFixedHeight(tokens.CONTROL_H)
        self.quality_combo.currentTextChanged.connect(self.quality_changed)
        self.guides = QToolButton()
        self.guides.setIcon(icon("guides"))
        self.guides.setCheckable(True)
        self.guides.setProperty("role", "icon")
        self.guides.setToolTip("Composition guides: thirds and centre")
        self.guides.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
        self.guides.toggled.connect(self.view_guides)
        self.save_frame_button = QToolButton()
        self.save_frame_button.setIcon(icon("camera"))
        self.save_frame_button.setProperty("role", "icon")
        self.save_frame_button.setToolTip("Save the current frame as an image (Ctrl+Alt+S)")
        self.save_frame_button.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
        self.save_frame_button.clicked.connect(lambda _=False: self.save_frame_requested.emit())
        self.view.zoom_changed.connect(self.sync_zoom)
        right = QHBoxLayout()
        right.setSpacing(tokens.SP1)
        right.addStretch(1)
        for widget in (self.zoom_combo, self.quality_combo, self.guides, self.save_frame_button):
            right.addWidget(widget)
        row = QHBoxLayout()
        row.setSpacing(tokens.SP2)
        row.addLayout(left, 1)
        row.addLayout(center, 0)
        row.addLayout(right, 1)
        # The slim master meter + volume slider sits beside the picture.
        self.master = MasterMeter()
        top = QHBoxLayout()
        top.setSpacing(tokens.SP1)
        top.addWidget(self.view, 1)
        top.addWidget(self.master)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SP1, tokens.SP1, tokens.SP1, tokens.SP1)
        layout.setSpacing(tokens.SP1)
        layout.addLayout(top, 1)
        layout.addLayout(row)

    def _transport_button(self, name, tooltip, checkable=False, primary=False):
        button = QToolButton()
        button.setIcon(icon(name, size=20 if primary else 16))
        button.setIconSize(QSize(20, 20) if primary else QSize(16, 16))
        button.setToolTip(tooltip)
        button.setCheckable(checkable and not primary)
        button.setProperty("role", "primary-icon" if primary else "transport")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if not primary:
            button.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
        if primary:
            button.setIcon(icon(name, tokens.COLORS["on_accent"], 20))
        return button

    def set_playing(self, playing):
        """Keep the transport button and the menu action on the same state."""
        playing = bool(playing)
        name = "pause" if playing else "play"
        self.play_button.setIcon(icon(name, tokens.COLORS["on_accent"], 20))
        self.play_button.setToolTip("Pause (Space)" if playing else "Play (Space)")

    def set_timecode(self, text):
        if self.timecode.text() != text:
            self.timecode.setText(text)

    def minimumSizeHint(self):
        return QSize(300, 220)

    def sizeHint(self):
        return QSize(960, 600)

    def view_guides(self, enabled):
        self.view.guides = bool(enabled)
        self.view.update()
        self.guides_changed.emit(bool(enabled))

    def sync_zoom(self, text, percent):
        """Show the real zoom in the one dropdown: a preset, or a temporary custom entry."""
        combo = self.zoom_combo
        presets = ["Fit", "25%", "50%", "100%", "200%", "400%"]
        label = "Fit" if text == "Fit" else (text if text in presets else f"{percent}%")
        combo.blockSignals(True)
        while combo.count() > len(presets):
            combo.removeItem(combo.count() - 1)
        if label not in presets:
            combo.addItem(label)
        combo.setCurrentText(label)
        combo.blockSignals(False)

    def set_quality_label(self, label):
        self.quality_combo.blockSignals(True)
        self.quality_combo.setCurrentText(label)
        self.quality_combo.blockSignals(False)

    def apply_preset(self, text):
        if text.endswith("%") and text not in ("Fit",):
            self.view.set_zoom(float(text.rstrip("%")) / 100.0)
        else:
            self.view.set_zoom(None)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(MEDIA_MIME) or mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        ids = []
        if mime.hasFormat(MEDIA_MIME):
            try:
                ids = [item for item in bytes(mime.data(MEDIA_MIME)).decode("utf-8").splitlines() if item]
            except UnicodeDecodeError:
                ids = []
        if ids:
            # Project media dropped on the picture: insert at the playhead, like double-click.
            self.media_ids_dropped.emit(ids)
        else:
            self.files_dropped.emit([url.toLocalFile() for url in mime.urls() if url.isLocalFile()])
        event.acceptProposedAction()
