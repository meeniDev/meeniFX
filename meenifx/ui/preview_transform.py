"""Interactive transform, motion-path and warp editing on top of the preview picture.

This is a *controller* rather than a widget: ``PreviewWidget`` owns the pixels and
delegates mouse events and overlay painting here, so the picture keeps its zoom/pan
behaviour and none of it can ever reach the exported frames.

Coordinate spaces involved, and the one place each is converted:

* **widget** pixels - what the mouse gives us;
* **project** pixels - the animation space (a 4K project at 25 % preview quality still
  uses 3840x2160 here, so quality can never change a transform);
* **layer** pixels - the clip's picture before the transform, used for hit testing and
  for the anchor/scale maths;
* **normalized** 0..1 - puppet pins, corner-pin corners and mesh nodes.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen, QPolygonF

from .. import crashlog, tokens
from ..model import channels as CH
from ..model import transform as T

HANDLE = 7.0          # half size of a scale handle, widget px
ROTATE_OFFSET = 26.0  # distance of the rotation knob above the box, widget px
KNOB_MARGIN = 16.0    # keep the pinned rotation knob this far inside the viewport
KNOB_SLIDE = 22.0     # step used to slide a pinned knob off a scale handle
HIT = 9.0             # grab radius, widget px
SNAP_PX = 7.0         # snapping distance on screen, widget px
ROTATE_SNAP_DEGREES = 15.0      # Shift snaps rotation to this step

# Pin keyframes copied with Ctrl+C live here, so they survive a clip or panel change.
_PIN_CLIPBOARD = {"rows": [], "start": 0, "pins": 0}
PATH_STEP = 2         # frames between motion-path samples

HANDLE_NAMES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


def _clamp_to_rect(origin, direction, rect):
    """Move `origin` along `direction` until it touches `rect`; return that point.

    Used to pin the rotation knob to the edge of the viewport: the knob keeps the angle
    it would have had, so dragging it still rotates the clip normally, but it can no
    longer be dragged out of reach.
    """
    dx, dy = float(direction.x()), float(direction.y())
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return QPointF(rect.center())
    dx, dy = dx / length, dy / length
    best = None
    for edge, limit, axis in ((rect.left(), dx, "x"), (rect.right(), dx, "x"),
                              (rect.top(), dy, "y"), (rect.bottom(), dy, "y")):
        if abs(limit) < 1e-9:
            continue
        distance = (edge - (origin.x() if axis == "x" else origin.y())) / limit
        if distance < 0:
            continue                      # behind us: this edge is not in the way
        point = QPointF(origin.x() + dx * distance, origin.y() + dy * distance)
        if rect.adjusted(-0.5, -0.5, 0.5, 0.5).contains(point) and (best is None or distance < best[0]):
            best = (distance, point)
    return best[1] if best is not None else QPointF(rect.center())


def _widget_point(widget, project_size, x, y):
    rect = widget.frame_rect()
    if rect.isEmpty() or not project_size[0]:
        return QPointF(0, 0)
    return QPointF(rect.left() + x * rect.width() / project_size[0],
                   rect.top() + y * rect.height() / project_size[1])


class PreviewOverlay:
    """Selection box, handles, motion path and warp pins for one preview widget."""

    def __init__(self, widget, engine, session):
        self.widget = widget
        self.engine = engine
        self.session = session
        self.enabled = True
        self.show_motion_path = True
        self.show_mesh = False
        self.mode = "select"          # select | transform | puppet | corner | mesh
        self._drag = None
        self._guides = []
        self._hover = None
        self._selected_pins = set()    # puppet pins, by id; several can be picked at once
        self._selected_path_key = None
        self._selected_corner = None
        self._selected_node = None
        self.on_change = None          # callable() -> re-render + refresh panels

    # A single pin is still the common case, so the old attribute keeps working: reading
    # gives the primary selection and writing replaces the whole selection.
    @property
    def _selected_pin(self):
        return next(iter(self._selected_pins)) if self._selected_pins else None

    @_selected_pin.setter
    def _selected_pin(self, value):
        self._selected_pins = set() if value is None else {int(value)}

    def selected_pins(self):
        """The selected pin dicts of the current clip, in pin order."""
        clip = self.primary()
        entry = self._effect_entry(clip, "puppet_warp") if clip is not None else None
        if entry is None:
            return []
        return [pin for pin in entry.get("pins") or ()
                if int(pin.get("id", 0)) in self._selected_pins]

    def select_all_pins(self):
        clip = self.primary()
        entry = self._effect_entry(clip, "puppet_warp") if clip is not None else None
        if entry is None:
            return False
        self._selected_pins = {int(pin.get("id", 0)) for pin in entry.get("pins") or ()}
        self.widget.update()
        return True

    def clear_pin_selection(self):
        if not self._selected_pins:
            return False
        self._selected_pins.clear()
        self.widget.update()
        return True

    # ------------------------------------------------------------------ helpers
    def project_size(self):
        return self.engine.project_size()

    def to_project(self, point):
        rect = self.widget.frame_rect()
        if rect.isEmpty():
            return None
        pw, ph = self.project_size()
        return ((point.x() - rect.left()) / max(1e-9, rect.width()) * pw,
                (point.y() - rect.top()) / max(1e-9, rect.height()) * ph)

    def to_widget(self, x, y):
        return _widget_point(self.widget, self.project_size(), x, y)

    def video_clips(self):
        """Selected clips that actually have a picture (audio clips are skipped)."""
        out = []
        for cid in sorted(self.session.selected):
            clip = self.engine.timeline.find_clip(cid)
            if clip is not None and clip.track is not None and clip.track.type == "video":
                out.append(clip)
        return out

    def primary(self):
        clip = self.session.primary_clip()
        if clip is not None and clip.track is not None and clip.track.type == "video":
            return clip
        clips = self.video_clips()
        return clips[0] if clips else None

    def local_frame(self, clip):
        return max(0, int(self.session.playhead) - int(clip.timeline_start))

    def state(self, clip):
        return T.evaluate(clip, self.local_frame(clip))

    def image_size(self, clip):
        if getattr(clip, "is_text", False):
            # A text layer is generated at project size, so its picture is the frame.
            return self.project_size()
        size = self.engine.media.source_size(clip.source_path)
        return size or self.project_size()

    def matrix(self, clip):
        return T.matrix(self.state(clip), self.project_size(), self.image_size(clip))

    def clip_corners(self, clip):
        """The four transformed picture corners, in project pixels."""
        state = self.state(clip)
        return T.content_corners(state, self.project_size(), self.image_size(clip))

    def handle_points(self, clip):
        corners = self.clip_corners(clip)
        tl, tr, br, bl = corners
        mid = lambda a, b: ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        return {"nw": tl, "n": mid(tl, tr), "ne": tr, "e": mid(tr, br),
                "se": br, "s": mid(br, bl), "sw": bl, "w": mid(bl, tl)}

    def anchor_point(self, clip):
        state = self.state(clip)
        pw, ph = self.project_size()
        center = ((pw - 1) / 2.0, (ph - 1) / 2.0)
        return (center[0] + state.position_x, center[1] + state.position_y)

    def rotate_knob(self, clip):
        """The rotate handle, kept inside the viewport even when the picture is not.

        The knob naturally sits ROTATE_OFFSET pixels above the top edge, in the clip's own
        rotated frame. Once the picture is moved or scaled off screen that point leaves
        the widget and the only way to rotate the clip disappears with it. Rather than
        moving the transform, the *drawn* knob is clamped to the visible area: it keeps
        its direction from the box centre and simply stops at the edge, so dragging it
        still rotates around the real anchor.
        """
        points = self.handle_points(clip)
        top = points["n"]
        angle = math.radians(self.state(clip).rotation - 90.0)
        rect = self.widget.frame_rect()
        scale = rect.width() / max(1e-9, self.project_size()[0])
        offset = ROTATE_OFFSET / max(1e-9, scale)
        ideal = QPointF(top[0] + math.cos(angle) * offset, top[1] + math.sin(angle) * offset)
        if rect.isEmpty():
            # No picture has been laid out yet: there is nothing to clamp against, and
            # pinning a knob to an empty rect would report a false "off screen".
            self._knob_pinned = False
            return QPointF(self.widget.width() / 2.0, self.widget.height() / 2.0)
        widget_point = self.to_widget(ideal.x(), ideal.y())
        bounds = QRectF(self.widget.rect()).adjusted(KNOB_MARGIN, KNOB_MARGIN,
                                                     -KNOB_MARGIN, -KNOB_MARGIN)
        if bounds.isEmpty():
            bounds = QRectF(self.widget.rect())
        if bounds.contains(widget_point):
            self._knob_pinned = False
            return widget_point
        # Direction from the picture's centre to the knob, clamped to the viewport.
        centre = self.to_widget(*self.anchor_point(clip))
        direction = widget_point - centre
        if direction.manhattanLength() < 1.0:
            direction = QPointF(0.0, -1.0)      # straight up: rotation always has a side
        pinned = _clamp_to_rect(centre, direction, bounds)
        # A pinned knob can land on a scale handle (a full-frame picture clamps it onto
        # the top handle). Slide it along the boundary until it is clear, so the one
        # rotation control is always grabbable rather than a decoration on the edge.
        for _attempt in range(8):
            if not self._knob_blocked(clip, pinned):
                break
            pinned = QPointF(pinned.x() + KNOB_SLIDE, pinned.y())
            if not bounds.contains(pinned):
                pinned = QPointF(pinned.x() - 2 * KNOB_SLIDE, pinned.y())
                if not bounds.contains(pinned):
                    break
        self._knob_pinned = True
        self._knob_anchor = centre
        return pinned

    def _knob_blocked(self, clip, point):
        """True when a scale handle or the anchor crosshair sits on top of `point`."""
        for name in HANDLE_NAMES:
            handle = self.to_widget(*self.handle_points(clip)[name])
            if (handle - point).manhattanLength() <= HANDLE + HIT:
                return True
        anchor = self.to_widget(*self.anchor_point(clip))
        return (anchor - point).manhattanLength() <= HIT * 1.4

    def widget_rect_for(self, clip):
        points = [self.to_widget(*corner) for corner in self.clip_corners(clip)]
        xs = [p.x() for p in points]
        ys = [p.y() for p in points]
        return QRectF(min(xs), min(ys), max(1.0, max(xs) - min(xs)), max(1.0, max(ys) - min(ys)))

    # --------------------------------------------------------------- hit testing
    def _handle_at(self, clip, pos):
        for name, point in self.handle_points(clip).items():
            if (self.to_widget(*point) - pos).manhattanLength() <= HIT * 1.6:
                return name
        anchor = self.to_widget(*self.anchor_point(clip))
        if (anchor - pos).manhattanLength() <= HIT * 1.4:
            return "anchor"
        if (self.rotate_knob(clip) - pos).manhattanLength() <= HIT * 1.5:
            return "rotate"
        return None

    def _handle_at_priority(self, clip, pos):
        """Hit test where a *pinned* rotation knob wins over the scale handles.

        The knob is only clamped when the picture has left the viewport, and then it is
        the single way to rotate: if it overlaps a handle it must still be picked.
        """
        knob = self.rotate_knob(clip)
        if getattr(self, "_knob_pinned", False) and \
                (knob - pos).manhattanLength() <= HIT * 1.5:
            return "rotate"
        return self._handle_at(clip, pos)

    def _clip_at(self, pos):
        """Topmost visible clip whose picture contains `pos` (widget coordinates)."""
        project = self.to_project(pos)
        if project is None:
            return None
        frame = self.session.playhead
        for track in self.engine.timeline.tracks:
            if track.type != "video" or not track.visible:
                continue
            for clip in track.sorted_clips():
                if not (clip.timeline_start <= frame < clip.end):
                    continue
                if not clip.source_path and not clip.is_text:
                    continue
                width, height = self.image_size(clip)
                source = T.invert(project, self.state(clip), self.project_size(), (width, height))
                if source is None:
                    continue
                if 0 <= source[0] < width and 0 <= source[1] < height:
                    return clip
        return None

    def _pin_at(self, clip):
        """(effect_entry, pin) under the pointer, for puppet warp."""
        entry = self._effect_entry(clip, "puppet_warp")
        if entry is None:
            return None, None
        state = self.state(clip)
        project = self.to_project(self._last_pos)
        if project is None:
            return None, None
        inverse = T.invert(project, state, self.project_size(), self.image_size(clip))
        if inverse is None:
            return None, None
        width, height = self.image_size(clip)
        nx, ny = inverse[0] / width, inverse[1] / height
        frame = self.local_frame(clip)
        best, best_distance = None, None
        for pin in entry.get("pins") or ():
            px = self._pin_value(clip, pin, "x", frame)
            py = self._pin_value(clip, pin, "y", frame)
            distance = math.hypot((px - nx), (py - ny)) * max(width, height)
            if distance <= HIT * 2.5 and (best_distance is None or distance < best_distance):
                best, best_distance = pin, distance
        return entry, best

    def mesh_nodes(self, clip):
        """(row, col, project point) of every node of an enabled Mesh Warp grid."""
        entry = self._effect_entry(clip, "mesh_warp")
        if entry is None or not entry.get("enabled", True):
            return []
        frame = self.local_frame(clip)
        size = int(round(self.param(clip, "mesh_warp", "grid", frame, 3)))
        size = max(2, min(5, size))
        width, height = self.image_size(clip)
        nodes = []
        for row in range(size):
            for col in range(size):
                base_x, base_y = col / (size - 1.0), row / (size - 1.0)
                dx = self.param(clip, "mesh_warp", f"node_{row}_{col}_x", frame, 0.0)
                dy = self.param(clip, "mesh_warp", f"node_{row}_{col}_y", frame, 0.0)
                nx, ny = base_x + dx, base_y + dy
                project = T.forward((nx * width, ny * height), self.state(clip),
                                    self.project_size(), (width, height))
                nodes.append((row, col, project))
        return nodes

    def _mesh_node_at(self, clip):
        project = self.to_project(self._last_pos)
        if project is None:
            return None
        width, height = self.image_size(clip)
        source = T.invert(project, self.state(clip), self.project_size(), (width, height))
        if source is None:
            return None
        nx, ny = source[0] / width, source[1] / height
        for row, col, point in self.mesh_nodes(clip):
            if math.hypot(point[0] - project[0], point[1] - project[1]) <= HIT * 2.5:
                return row, col, nx, ny
        return None

    def param(self, clip, effect_id, name, frame=None, default=0.0):
        """An effect parameter at a frame, falling back to the effect's own default.

        A freshly added effect stores an empty params dict, so reading the channel
        directly returned None and painting its warp handles crashed on it.
        """
        if frame is None:
            frame = self.local_frame(clip)
        value = CH.value_at(clip, CH.effect_channel(effect_id, name), frame)
        if value is None:
            effect = self.engine.runtime_effects.get(effect_id)
            spec = effect.spec(name) if effect is not None else None
            if spec is not None:
                value = spec.default
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _pin_value(self, clip, pin, axis, frame):
        value = CH.value_at(clip, CH.pin_channel("puppet_warp", pin.get("id"), axis), frame)
        if value is None:
            value = pin.get(axis, 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _effect_entry(self, clip, effect_id):
        for entry in clip.effects:
            if entry.get("effect_id") == effect_id:
                return entry
        return None

    def _corner_at(self, clip):
        """(index, project point) of the corner-pin corner under the pointer."""
        entry = self._effect_entry(clip, "corner_pin")
        if entry is None:
            return None, None
        project = self.to_project(self._last_pos)
        if project is None:
            return None, None
        inverse = T.invert(project, self.state(clip), self.project_size(), self.image_size(clip))
        if inverse is None:
            return None, None
        width, height = self.image_size(clip)
        nx, ny = inverse[0] / width, inverse[1] / height
        frame = self.local_frame(clip)
        for index, (corner_x, corner_y) in enumerate((
                ("corner_tl_x", "corner_tl_y"), ("corner_tr_x", "corner_tr_y"),
                ("corner_br_x", "corner_br_y"), ("corner_bl_x", "corner_bl_y"))):
            cx = self.param(clip, "corner_pin", corner_x, frame)
            cy = self.param(clip, "corner_pin", corner_y, frame)
            if math.hypot((cx - nx), (cy - ny)) * max(width, height) <= HIT * 2.5:
                return index, (nx, ny)
        return None, None

    def _path_point_at(self, clip, pos):
        """Clip-local frame of a motion-path keyframe near the pointer."""
        if not self.show_motion_path:
            return None
        for time in CH.key_times(clip, "transform.position_x"):
            x, y = T.position_at(clip, time)
            point = self.to_widget(*self._position_to_project(clip, x, y))
            if (point - pos).manhattanLength() <= HIT * 1.6:
                return time
        return None

    def _position_to_project(self, clip, x, y):
        pw, ph = self.project_size()
        return ((pw - 1) / 2.0 + x, (ph - 1) / 2.0 + y)

    def dragging(self):
        """True while a transform/warp gesture is in progress (drives preview quality)."""
        return self._drag is not None

    # ------------------------------------------------------------------- painting
    # These run inside the preview widget's paint/mouse overrides, so an exception here
    # would abort the process: each entry point is guarded and logs instead.
    @crashlog.guard("preview overlay")
    def paint(self, painter):
        if not self.enabled:
            return
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        for clip in self.video_clips():
            self._paint_clip(painter, clip)
        clip = self.primary()
        if clip is not None:
            if self.show_motion_path and CH.key_times(clip, "transform.position_x"):
                self._paint_motion_path(painter, clip)
            self._paint_warps(painter, clip)
        self._paint_guides(painter)

    def _paint_clip(self, painter, clip):
        primary = clip is self.primary()
        corners = [self.to_widget(*corner) for corner in self.clip_corners(clip)]
        colour = QColor(tokens.COLORS["accent"] if primary else tokens.COLORS["muted"])
        pen = QPen(colour, 1.6 if primary else 1.0)
        if not primary:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.save()
        painter.setPen(pen)
        # Outline in the clip's own (possibly rotated) space.
        painter.drawPolygon(QPolygonF(corners))
        if not primary:
            painter.restore()
            return
        painter.setPen(QPen(colour, 1.2))
        for name in HANDLE_NAMES:
            point = self.to_widget(*self.handle_points(clip)[name])
            rect = QRectF(point.x() - HANDLE / 2, point.y() - HANDLE / 2, HANDLE, HANDLE)
            painter.fillRect(rect, QColor(tokens.COLORS["panel"]))
            painter.drawRect(rect)
        # Rotation knob and its tether. When the picture has left the viewport the knob
        # is pinned to the edge: the tether then points at the real handle position, so
        # it is obvious that the knob still belongs to this box.
        knob = self.rotate_knob(clip)
        top = self.to_widget(*self.handle_points(clip)["n"])
        painter.save()
        if getattr(self, "_knob_pinned", False):
            painter.setPen(QPen(colour, 1.0, Qt.PenStyle.DashLine))
            painter.drawLine(self.to_widget(*self.anchor_point(clip)), knob)
        else:
            painter.drawLine(top, knob)
        painter.restore()
        painter.setBrush(QBrush(colour))
        painter.drawEllipse(knob, HANDLE / 2 + 1, HANDLE / 2 + 1)
        # Anchor crosshair: the pivot the clip scales and rotates around.
        anchor = self.to_widget(*self.anchor_point(clip))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(tokens.COLORS["warning"]), 1.5))
        painter.drawEllipse(anchor, 4.5, 4.5)
        painter.drawLine(QPointF(anchor.x() - 9, anchor.y()), QPointF(anchor.x() + 9, anchor.y()))
        painter.drawLine(QPointF(anchor.x(), anchor.y() - 9), QPointF(anchor.x(), anchor.y() + 9))
        painter.restore()

    def _paint_guides(self, painter):
        if not self._guides:
            return
        rect = self.widget.frame_rect()
        painter.save()
        painter.setPen(QPen(QColor(tokens.COLORS["success"]), 1.0, Qt.PenStyle.DashLine))
        for axis, position in self._guides:
            if axis == "x":
                x = rect.left() + position * rect.width() / max(1e-9, self.project_size()[0])
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            else:
                y = rect.top() + position * rect.height() / max(1e-9, self.project_size()[1])
                painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.restore()

    def _paint_motion_path(self, painter, clip):
        times = CH.key_times(clip, "transform.position_x")
        if len(times) < 1:
            return
        painter.save()
        colour = QColor(tokens.COLORS["accent"])
        painter.setPen(QPen(colour, 1.4))
        path = QPainterPath()
        first = True
        end = max(1, clip.duration)
        for frame in range(0, end + 1, PATH_STEP):
            x, y = T.position_at(clip, frame)
            point = self.to_widget(*self._position_to_project(clip, x, y))
            if first:
                path.moveTo(point)
                first = False
            else:
                path.lineTo(point)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        # Keyframe points, and the Bézier handles of the selected one.
        for time in times:
            x, y = T.position_at(clip, time)
            point = self.to_widget(*self._position_to_project(clip, x, y))
            size = 5.0 if time == self._selected_path_key else 3.5
            painter.setBrush(QBrush(QColor(tokens.COLORS["panel"])))
            painter.setPen(QPen(colour, 1.2))
            painter.drawRect(QRectF(point.x() - size, point.y() - size, size * 2, size * 2))
            if time == self._selected_path_key:
                for side in ("in", "out"):
                    handle = T.path_handle(clip, time, side)
                    if not handle:
                        continue
                    target = self.to_widget(*self._position_to_project(
                        clip, x + handle[0], y + handle[1]))
                    painter.setPen(QPen(QColor(tokens.COLORS["muted"]), 1.0))
                    painter.drawLine(point, target)
                    painter.setBrush(QBrush(QColor(tokens.COLORS["muted"])))
                    painter.drawEllipse(target, 3.0, 3.0)
        painter.restore()

    def _paint_warps(self, painter, clip):
        frame = self.local_frame(clip)
        puppet = self._effect_entry(clip, "puppet_warp")
        if puppet is not None:
            painter.save()
            painter.setRenderHint(painter.RenderHint.Antialiasing, True)
            for pin in puppet.get("pins") or ():
                px = self._pin_value(clip, pin, "x", frame)
                py = self._pin_value(clip, pin, "y", frame)
                width, height = self.image_size(clip)
                project = (px * width, py * height)
                project = T.forward(project, self.state(clip), self.project_size(), (width, height))
                point = self.to_widget(*project)
                locked = bool(pin.get("locked"))
                enabled = bool(pin.get("enabled", True))
                selected = int(pin.get("id", 0)) in self._selected_pins
                colour = QColor(tokens.COLORS["warning"] if locked else tokens.COLORS["accent"])
                if not enabled:
                    # A disabled pin is still selectable, but it reads as switched off.
                    colour = QColor(tokens.COLORS["disabled"])
                painter.setPen(QPen(colour, 2.0 if selected else 1.3,
                                    Qt.PenStyle.DashLine if not enabled else Qt.PenStyle.SolidLine))
                painter.setBrush(QBrush(QColor(tokens.COLORS["panel"])))
                radius = 5.0 if selected else 4.0
                painter.drawEllipse(point, radius, radius)
                if locked:
                    painter.drawLine(QPointF(point.x() - 2, point.y()),
                                     QPointF(point.x() + 2, point.y()))
                if not enabled:
                    painter.drawLine(QPointF(point.x() - 2, point.y() - 2),
                                     QPointF(point.x() + 2, point.y() + 2))
                if selected:
                    # A thin ring around every selected pin: multi-selection has to be visible.
                    painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 1.0))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawEllipse(point, radius + 3.0, radius + 3.0)
            painter.restore()
        mesh = self.mesh_nodes(clip)
        if mesh:
            painter.save()
            painter.setPen(QPen(QColor(tokens.COLORS["success"]), 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            size = max(row for row, _col, _point in mesh) + 1
            for row in range(size):
                line = [self.to_widget(*point) for r, _c, point in mesh if r == row]
                painter.drawPolyline(QPolygonF(line))
            for col in range(size):
                line = [self.to_widget(*point) for _r, c, point in mesh if c == col]
                painter.drawPolyline(QPolygonF(line))
            painter.setPen(QPen(QColor(tokens.COLORS["success"]), 1.2))
            painter.setBrush(QBrush(QColor(tokens.COLORS["panel"])))
            for _row, _col, point in mesh:
                widget_point = self.to_widget(*point)
                painter.drawRect(QRectF(widget_point.x() - 3, widget_point.y() - 3, 6, 6))
            painter.restore()
        corner_pin = self._effect_entry(clip, "corner_pin")
        if corner_pin is not None:
            names = (("corner_tl_x", "corner_tl_y"), ("corner_tr_x", "corner_tr_y"),
                     ("corner_br_x", "corner_br_y"), ("corner_bl_x", "corner_bl_y"))
            painter.save()
            painter.setPen(QPen(QColor(tokens.COLORS["success"]), 1.3))
            painter.setBrush(QBrush(QColor(tokens.COLORS["panel"])))
            for index, (cx_name, cy_name) in enumerate(names):
                nx = self.param(clip, "corner_pin", cx_name, frame)
                ny = self.param(clip, "corner_pin", cy_name, frame)
                width, height = self.image_size(clip)
                project = T.forward((nx * width, ny * height), self.state(clip),
                                    self.project_size(), (width, height))
                point = self.to_widget(*project)
                size = 6.0 if index == self._selected_corner else 4.5
                painter.drawRect(QRectF(point.x() - size, point.y() - size, size * 2, size * 2))
            painter.restore()

    # ------------------------------------------------------------------- gestures
    @crashlog.guard("preview overlay", fallback=False)
    def mouse_press(self, event):
        if not self.enabled or event.button() != Qt.MouseButton.LeftButton:
            return False
        pos = event.position()
        self._last_pos = pos
        self.widget.setFocus()
        clip = self.primary()
        # Warp handles win over the transform handles: corner-pin corners and mesh
        # nodes sit exactly on the box corners and centre, where the box handles would
        # otherwise swallow every click on them.
        if clip is not None and self.mode in ("corner", "transform"):
            node = self._mesh_node_at(clip)
            if node is not None:
                row, col, _nx, _ny = node
                self.session.begin_gesture()
                self._drag = {"kind": "mesh", "clip": clip, "row": row, "col": col}
                self.widget.update()
                return True
        if clip is not None and self.mode in ("corner", "transform"):
            index, _point = self._corner_at(clip)
            if index is not None:
                self._selected_corner = index
                self.session.begin_gesture()
                self._drag = {"kind": "corner", "clip": clip, "index": index}
                self.widget.update()
                return True
        if clip is not None and self.mode in ("transform", "select", "puppet", "corner"):
            handle = self._handle_at_priority(clip, pos)
            if handle is not None:
                return self._begin_transform(clip, handle, pos, event.modifiers())
        if self.mode in ("puppet", "transform"):
            hit_entry, pin = self._pin_at(clip) if clip is not None else (None, None)
            if pin is not None:
                pin_id = int(pin.get("id", 0))
                if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                        | Qt.KeyboardModifier.ShiftModifier):
                    # Ctrl/Shift adds or removes one pin without starting a drag.
                    if pin_id in self._selected_pins:
                        self._selected_pins.discard(pin_id)
                    else:
                        self._selected_pins.add(pin_id)
                    self.widget.update()
                    return True
                if pin_id not in self._selected_pins:
                    self._selected_pins = {pin_id}
                project = self.to_project(pos)
                width, height = self.image_size(clip)
                inverse = T.invert(project, self.state(clip), self.project_size(),
                                   (width, height)) if project is not None else None
                if inverse is None:
                    return False
                frame = self.local_frame(clip)
                moving = [(item, self._pin_value(clip, item, "x", frame),
                           self._pin_value(clip, item, "y", frame))
                          for item in hit_entry.get("pins") or ()
                          if int(item.get("id", 0)) in self._selected_pins]
                self.session.begin_gesture()
                self._drag = {"kind": "pin", "clip": clip, "pins": moving,
                              "origin": (inverse[0] / width, inverse[1] / height),
                              "announced": False}
                self.widget.update()
                return True
        if clip is not None:
            key = self._path_point_at(clip, pos)
            if key is not None:
                self._selected_path_key = key
                handle_side = self._path_handle_side(clip, key, pos)
                self.session.begin_gesture()
                self._drag = {"kind": "path_handle" if handle_side else "path_point",
                              "clip": clip, "time": key, "side": handle_side}
                self.widget.update()
                return True
        hit = self._clip_at(pos)
        if hit is not None:
            additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if additive:
                ids = set(self.session.selected) | {hit.id}
                self.session.set_selection(ids, expand=False)
            elif hit.id not in self.session.selected:
                self.session.set_selection({hit.id}, expand=True)
            self._selected_path_key = None
            self._selected_pin = None
            self._selected_corner = None
            if self.mode in ("puppet", "corner"):
                self._add_warp_point(hit, pos)
                return True
            clips = self.video_clips() or [hit]
            self.session.begin_gesture()
            starts = [(c, T.evaluate(c, self.local_frame(c))) for c in clips]
            self._drag = {"kind": "move", "clips": starts, "origin": self.to_project(pos),
                          "moved": False}
            self.widget.update()
            return True
        if self.mode in ("puppet", "corner") and clip is not None:
            self._add_warp_point(clip, pos)
            return True
        self.session.set_selection(set(), expand=False)
        self.widget.update()
        return True

    def _path_handle_side(self, clip, time, pos):
        x, y = T.position_at(clip, time)
        point = self.to_widget(*self._position_to_project(clip, x, y))
        best, distance = None, HIT * 1.6
        for side in ("in", "out"):
            handle = T.path_handle(clip, time, side)
            if not handle:
                continue
            target = self.to_widget(*self._position_to_project(clip, x + handle[0], y + handle[1]))
            if (target - pos).manhattanLength() <= distance:
                best, distance = side, (target - pos).manhattanLength()
        return best

    def _add_warp_point(self, clip, pos):
        """Click on the picture: add a puppet pin, or nothing when the effect is missing."""
        if self.mode != "puppet":
            return
        entry = self._effect_entry(clip, "puppet_warp")
        project = self.to_project(pos)
        if entry is None or project is None:
            return
        width, height = self.image_size(clip)
        source = T.invert(project, self.state(clip), self.project_size(), (width, height))
        if source is None:
            return
        nx, ny = source[0] / width, source[1] / height
        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            return

        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is None or target.track.locked:
                return
            pins = CH.pins_of(target, "puppet_warp")
            pin_id = CH.next_pin_id(target, "puppet_warp")
            pins.append({"id": pin_id, "src": [nx, ny], "x": nx, "y": ny, "locked": False})

        self.session.stack.run("Add puppet pin", action)
        self._selected_pin = CH.pins_of(self.engine.timeline.find_clip(clip.id), "puppet_warp")[-1].get("id")
        self._notify()

    def _begin_transform(self, clip, handle, pos, modifiers):
        state = self.state(clip)
        self.session.begin_gesture()
        anchor = self.anchor_point(clip)
        ensure = T.ensure_transform(clip)
        self._drag = {
            "kind": "rotate" if handle == "rotate" else ("anchor" if handle == "anchor" else "scale"),
            "clip": clip, "handle": handle, "state": state, "modifiers": modifiers,
            "anchor": anchor, "origin": self.to_project(pos),
            "angle": self._angle(pos, anchor),
            # Behaviour switches are read once per gesture: flipping a checkbox in the
            # inspector in the middle of a drag must not change the drag under the mouse.
            "flags": {"scale_from_center": bool(ensure.get("scale_from_center", False)),
                      "snap_rotation": bool(ensure.get("snap_rotation", True)),
                      "uniform": bool(ensure.get("uniform", True))},
        }
        self._notify()
        return True

    def _angle(self, pos, anchor):
        project = self.to_project(pos)
        if project is None:
            return 0.0
        return math.degrees(math.atan2(project[1] - anchor[1], project[0] - anchor[0]))

    @crashlog.guard("preview overlay", fallback=False)
    def mouse_move(self, event):
        if not self.enabled:
            return False
        pos = event.position()
        self._last_pos = pos
        drag = self._drag
        if drag is None:
            clip = self.primary()
            self._hover = self._handle_at_priority(clip, pos) if clip is not None else None
            self.widget.setCursor(self._cursor_for(self._hover))
            return False
        kind = drag["kind"]
        # The modifier contract, identical for every transform gesture:
        #   Shift - constrain (one axis when moving, aspect when scaling, 15 deg when
        #           rotating);
        #   Alt   - act from the centre (scale/crop around the anchor);
        #   Ctrl  - precision: snapping is skipped and movement is 1:1 with the pointer.
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        precise = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        # The inspector's switches are the same gestures without a keyboard: "scale from
        # center" is Alt by default, "snap rotation" is Shift by default for rotation.
        flags = drag.get("flags") or {}
        if flags.get("scale_from_center"):
            alt = True
        if not flags.get("snap_rotation", True) and kind == "rotate":
            snap_rotation = False
        else:
            snap_rotation = shift
        drag["alt"] = alt
        if kind == "move":
            project = self.to_project(pos)
            origin = drag["origin"]
            if project is None or origin is None:
                return True
            dx, dy = project[0] - origin[0], project[1] - origin[1]
            if shift:
                if abs(dx) > abs(dy):
                    dy = 0.0
                else:
                    dx = 0.0
            snapped = self._snap_delta(drag["clips"][0][0], drag["clips"][0][1], dx, dy,
                                       shift or precise)
            dx, dy = snapped
            drag["moved"] = True
            drag["delta"] = (dx, dy)
            for clip, state in drag["clips"]:
                self._write_position(clip, state.position_x + dx, state.position_y + dy)
            self._notify()
            return True
        if kind == "rotate":
            project = self.to_project(pos)
            if project is None:
                return True
            angle = math.degrees(math.atan2(project[1] - drag["anchor"][1],
                                            project[0] - drag["anchor"][0]))
            rotation = drag["state"].rotation + (angle - drag["angle"])
            if snap_rotation:
                rotation = round(rotation / ROTATE_SNAP_DEGREES) * ROTATE_SNAP_DEGREES
            elif precise:
                rotation = drag["state"].rotation + (angle - drag["angle"]) * 0.1
            self._write(drag["clip"], "transform.rotation", rotation)
            self._notify()
            return True
        if kind == "anchor":
            project = self.to_project(pos)
            origin = drag["origin"]
            if project is None or origin is None:
                return True
            dx, dy = project[0] - origin[0], project[1] - origin[1]
            state = drag["state"]
            # Undo rotation/scale so the anchor follows the pointer exactly, then
            # compensate the position so the picture itself does not move.
            rotation = math.radians(-state.rotation)
            sx = state.scale_x / 100.0 or 1e-6
            sy = state.scale_y / 100.0 or 1e-6
            local_dx = (dx * math.cos(rotation) - dy * math.sin(rotation)) / sx
            local_dy = (dx * math.sin(rotation) + dy * math.cos(rotation)) / sy
            self._write(drag["clip"], "transform.anchor_x", state.anchor_x + local_dx)
            self._write(drag["clip"], "transform.anchor_y", state.anchor_y + local_dy)
            self._write_position(drag["clip"], state.position_x + dx, state.position_y + dy)
            self._notify()
            return True
        if kind == "scale":
            self._scale_drag(drag, pos, shift, alt)
            self._notify()
            return True
        if kind == "pin":
            self._pin_drag(drag, pos)
            self._notify()
            return True
        if kind == "corner":
            self._corner_drag(drag, pos)
            self._notify()
            return True
        if kind == "mesh":
            self._mesh_drag(drag, pos)
            self._notify()
            return True
        if kind == "path_point":
            self._path_point_drag(drag, pos, shift)
            self._notify()
            return True
        if kind == "path_handle":
            self._path_handle_drag(drag, pos)
            self._notify()
            return True
        return True

    def _write(self, clip, channel, value):
        CH.set_value(clip, channel, value, self.local_frame(clip),
                     bool(self.session.auto_keyframe.get()))

    def _write_position(self, clip, x, y):
        frame = self.local_frame(clip)
        auto = bool(self.session.auto_keyframe.get())
        T.set_position(clip, x, y, frame, auto)

    def _scale_drag(self, drag, pos, shift, alt=False):
        """Scale from the anchor, or from the picture's centre while Alt is held.

        Both modes share one mapping: the pointer is expressed in the clip's untransformed
        layer space and a ratio is taken against a pivot. ``anchor`` is the normal pivot;
        with Alt the pivot becomes the layer centre, so opposite edges move outward (or
        inward) together and the picture grows around its middle.
        """
        clip, handle, state = drag["clip"], drag["handle"], drag["state"]
        project = self.to_project(pos)
        if project is None:
            return
        width, height = self.image_size(clip)
        _x0, _y0, nw, nh = T.fit_rect(self.project_size(), (width, height))
        center = ((self.project_size()[0] - 1) / 2.0, (self.project_size()[1] - 1) / 2.0)
        # Pointer in the clip's untransformed fit space.
        rotation = math.radians(-state.rotation)
        dx = project[0] - (center[0] + state.position_x)
        dy = project[1] - (center[1] + state.position_y)
        local_x = (dx * math.cos(rotation) - dy * math.sin(rotation))
        local_y = (dx * math.sin(rotation) + dy * math.cos(rotation))
        local_x /= (state.scale_x / 100.0 or 1e-6)
        local_y /= (state.scale_y / 100.0 or 1e-6)
        local_x += state.anchor_x
        local_y += state.anchor_y
        sign_x = -1.0 if state.flip_h else 1.0
        sign_y = -1.0 if state.flip_v else 1.0
        half_w, half_h = nw / 2.0, nh / 2.0
        corners = {"nw": (-half_w, -half_h), "n": (0.0, -half_h), "ne": (half_w, -half_h),
                   "e": (half_w, 0.0), "se": (half_w, half_h), "s": (0.0, half_h),
                   "sw": (-half_w, half_h), "w": (-half_w, 0.0)}
        corner_x, corner_y = corners[handle]
        pivot_x = nw / 2.0 if alt else state.anchor_x
        pivot_y = nh / 2.0 if alt else state.anchor_y
        ratio_x = ratio_y = None
        if handle not in ("n", "s"):
            denominator = corner_x - pivot_x
            if abs(denominator) > 1e-6:
                ratio_x = (local_x - pivot_x) / denominator
        if handle not in ("e", "w"):
            denominator = corner_y - pivot_y
            if abs(denominator) > 1e-6:
                ratio_y = (local_y - pivot_y) / denominator
        uniform = bool(shift or drag.get("flags", {}).get("uniform", True))
        if uniform and ratio_x is not None and ratio_y is not None:
            ratio = max(abs(ratio_x), abs(ratio_y))
            ratio_x = ratio * (1 if ratio_x >= 0 else -1)
            ratio_y = ratio * (1 if ratio_y >= 0 else -1)
        if ratio_x is not None:
            CH.set_value(clip, "transform.scale_x", state.scale_x * ratio_x,
                         self.local_frame(clip), bool(self.session.auto_keyframe.get()))
        if ratio_y is not None:
            CH.set_value(clip, "transform.scale_y", state.scale_y * ratio_y,
                         self.local_frame(clip), bool(self.session.auto_keyframe.get()))

    def _pin_drag(self, drag, pos):
        """Move every selected pin by the same amount: a group keeps its shape."""
        clip = drag["clip"]
        project = self.to_project(pos)
        if project is None:
            return
        width, height = self.image_size(clip)
        source = T.invert(project, self.state(clip), self.project_size(), (width, height))
        if source is None:
            return
        frame = self.local_frame(clip)
        auto = bool(self.session.auto_keyframe.get())
        origin_x, origin_y = drag["origin"]
        dx = source[0] / width - origin_x
        dy = source[1] / height - origin_y
        for pin, start_x, start_y in drag["pins"]:
            CH.set_value(clip, CH.pin_channel("puppet_warp", pin.get("id"), "x"),
                         start_x + dx, frame, auto)
            CH.set_value(clip, CH.pin_channel("puppet_warp", pin.get("id"), "y"),
                         start_y + dy, frame, auto)
        if auto and not drag.get("announced"):
            drag["announced"] = True
            count = len(drag["pins"])
            self.session.status.emit(
                f"Keyframed {count} puppet pin{'s' if count != 1 else ''} at frame {frame}")

    def _corner_drag(self, drag, pos):
        clip, index = drag["clip"], drag["index"]
        project = self.to_project(pos)
        if project is None:
            return
        width, height = self.image_size(clip)
        source = T.invert(project, self.state(clip), self.project_size(), (width, height))
        if source is None:
            return
        names = (("corner_tl_x", "corner_tl_y"), ("corner_tr_x", "corner_tr_y"),
                 ("corner_br_x", "corner_br_y"), ("corner_bl_x", "corner_bl_y"))[index]
        frame = self.local_frame(clip)
        auto = bool(self.session.auto_keyframe.get())
        CH.set_value(clip, CH.effect_channel("corner_pin", names[0]), source[0] / width, frame, auto)
        CH.set_value(clip, CH.effect_channel("corner_pin", names[1]), source[1] / height, frame, auto)

    def _mesh_drag(self, drag, pos):
        clip, row, col = drag["clip"], drag["row"], drag["col"]
        project = self.to_project(pos)
        if project is None:
            return
        width, height = self.image_size(clip)
        source = T.invert(project, self.state(clip), self.project_size(), (width, height))
        if source is None:
            return
        frame = self.local_frame(clip)
        size = max(2, min(5, int(round(self.param(clip, "mesh_warp", "grid", frame, 3)))))
        auto = bool(self.session.auto_keyframe.get())
        base_x, base_y = col / (size - 1.0), row / (size - 1.0)
        CH.set_value(clip, CH.effect_channel("mesh_warp", f"node_{row}_{col}_x"),
                     source[0] / width - base_x, frame, auto)
        CH.set_value(clip, CH.effect_channel("mesh_warp", f"node_{row}_{col}_y"),
                     source[1] / height - base_y, frame, auto)

    def _path_point_drag(self, drag, pos, shift):
        clip, time = drag["clip"], drag["time"]
        project = self.to_project(pos)
        if project is None:
            return
        width_height = self.project_size()
        center = ((width_height[0] - 1) / 2.0, (width_height[1] - 1) / 2.0)
        x, y = project[0] - center[0], project[1] - center[1]
        if shift:
            start = drag.get("start")
            if start is None:
                start = drag["start"] = T.position_at(clip, time)
            if abs(x - start[0]) > abs(y - start[1]):
                y = start[1]
            else:
                x = start[0]
        # Dragging a path point edits an existing keyframe, so both axes get a key at
        # that time: a static Y would otherwise shift the whole path vertically.
        for axis, value in (("x", x), ("y", y)):
            stored = T.ensure_transform(clip).get(f"position_{axis}")
            animated = hasattr(stored, "value_at")
            CH.set_value(clip, f"transform.position_{axis}", value, time, animated)
        # Keep the handle geometry glued to the moved point.
        self._selected_path_key = time

    def _path_handle_drag(self, drag, pos):
        clip, time, side = drag["clip"], drag["time"], drag["side"]
        project = self.to_project(pos)
        if project is None:
            return
        width_height = self.project_size()
        center = ((width_height[0] - 1) / 2.0, (width_height[1] - 1) / 2.0)
        x, y = T.position_at(clip, time)
        T.set_path_handle(clip, time, side,
                          (project[0] - center[0] - x, project[1] - center[1] - y))

    def _snap_delta(self, clip, state, dx, dy, skip):
        """Snap the moving clip to the frame centre and edges; returns the delta and guides."""
        self._guides = []
        if skip:
            return dx, dy
        pw, ph = self.project_size()
        rect = self.widget.frame_rect()
        if rect.isEmpty():
            return dx, dy
        project_per_px = pw / max(1e-9, rect.width())
        tolerance = SNAP_PX * project_per_px
        corners = T.content_corners(state, self.project_size(), self.image_size(clip))
        left = min(c[0] for c in corners) + dx
        right = max(c[0] for c in corners) + dx
        top = min(c[1] for c in corners) + dy
        bottom = max(c[1] for c in corners) + dy
        for target, kind in ((0.0, "left"), (pw - 1, "right")):
            distance = (left - target) if kind == "left" else (right - target)
            if abs(distance) <= tolerance:
                dx -= distance
                self._guides.append(("x", target))
                left, right = left + dx - (left - min(c[0] for c in corners)), 0
                break
        for edge, target in ((left, 0.0), (right, pw - 1), ((left + right) / 2.0, (pw - 1) / 2.0)):
            if abs(edge - target) <= tolerance:
                dx += target - edge
                self._guides.append(("x", target))
                break
        for edge, target in ((top, 0.0), (bottom, ph - 1), ((top + bottom) / 2.0, (ph - 1) / 2.0)):
            if abs(edge - target) <= tolerance:
                dy += target - edge
                self._guides.append(("y", target))
                break
        return dx, dy

    @crashlog.guard("preview overlay", fallback=False)
    def mouse_release(self, event):
        drag = self._drag
        if drag is None:
            return False
        self._drag = None
        self._guides = []
        self._knob_pinned = False
        labels = {"move": "Move clip", "scale": "Scale clip", "rotate": "Rotate clip",
                  "anchor": "Move anchor point", "pin": "Move puppet pin",
                  "corner": "Move corner pin", "mesh": "Move mesh node",
                  "path_point": "Move motion path point",
                  "path_handle": "Adjust motion path"}
        self.session.commit_gesture(labels.get(drag.get("kind"), "Edit transform"))
        self._notify()
        self.widget.update()
        return True

    def cancel_drag(self):
        if self._drag is not None:
            self._drag = None
            self._guides = []
            self.session.cancel_gesture()

    def delete_selection(self):
        """Delete the selected pins, corner or motion-path point (not the clip)."""
        clip = self.primary()
        if clip is None:
            return False
        if self._selected_pins:
            removing = set(self._selected_pins)
            self._selected_pins = set()

            def action():
                target = self.engine.timeline.find_clip(clip.id)
                if target is None:
                    return
                for entry in target.effects:
                    if entry.get("effect_id") == "puppet_warp":
                        entry["pins"] = [pin for pin in entry.get("pins") or ()
                                         if int(pin.get("id", 0)) not in removing]
            label = "Delete puppet pin" if len(removing) == 1 else f"Delete {len(removing)} puppet pins"
            self.session.stack.run(label, action)
            self._notify()
            return True
        if self._selected_path_key is not None:
            time = self._selected_path_key
            self._selected_path_key = None

            def action():
                target = self.engine.timeline.find_clip(clip.id)
                if target is None:
                    return
                for name in ("position_x", "position_y"):
                    stored = T.ensure_transform(target).get(name)
                    if hasattr(stored, "remove_keyframe"):
                        stored.remove_keyframe(time)
                T.clear_path_handles(target, time)
            self.session.stack.run("Delete motion path keyframe", action)
            self._notify()
            return True
        return False

    def _edit_selected_pins(self, label, change):
        """Apply `change(pin)` to every selected pin in one undo step."""
        clip = self.primary()
        ids = set(self._selected_pins)
        if clip is None or not ids:
            return False

        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is None:
                return
            for entry in target.effects:
                if entry.get("effect_id") != "puppet_warp":
                    continue
                for pin in entry.get("pins") or ():
                    if int(pin.get("id", 0)) in ids:
                        change(pin, target)
        self.session.stack.run(label, action)
        self._notify()
        return True

    def toggle_pin_lock(self):
        """Lock a pin so it anchors the mesh instead of following the animation."""
        pins = self.selected_pins()
        if not pins:
            return False
        locking = not all(bool(pin.get("locked")) for pin in pins)
        return self._edit_selected_pins("Lock puppet pin" if locking else "Unlock puppet pin",
                                        lambda pin, _clip: pin.__setitem__("locked", locking))

    def toggle_pin_enabled(self):
        """Switch a pin off without unlocking it: a disabled pin pulls nothing at all."""
        pins = self.selected_pins()
        if not pins:
            return False
        enable = not all(bool(pin.get("enabled", True)) for pin in pins)
        return self._edit_selected_pins("Enable puppet pin" if enable else "Disable puppet pin",
                                        lambda pin, _clip: pin.__setitem__("enabled", enable))

    def reset_selected_pins(self):
        """Send the pins back to their rest position and drop their keys."""
        if not self.selected_pins():
            return False

        def reset(pin, clip):
            for axis in ("x", "y"):
                CH.replace_keys(clip, CH.pin_channel("puppet_warp", pin.get("id"), axis), [])
            source = pin.get("src") or (pin.get("x", 0.0), pin.get("y", 0.0))
            pin["x"], pin["y"] = float(source[0]), float(source[1])

        return self._edit_selected_pins("Reset puppet pin", reset)

    def copy_pin_keys(self):
        """Copy the keyframes of every selected pin, relative to the earliest one."""
        clip = self.primary()
        pins = self.selected_pins()
        if clip is None or not pins:
            return False
        rows, times = [], []
        for index, pin in enumerate(pins):
            for axis in ("x", "y"):
                keys = CH.key_records(clip, CH.pin_channel("puppet_warp", pin.get("id"), axis))
                rows.append((index, axis, keys))
                times.extend(int(key[0]) for key in keys)
        if not times:
            self.session.status.emit("The selected pins have no keyframes to copy")
            return False
        _PIN_CLIPBOARD.update({"rows": rows, "start": min(times), "pins": len(pins)})
        self.session.status.emit(f"Copied the keys of {len(pins)} puppet pin(s)")
        return True

    def paste_pin_keys(self):
        """Paste onto the selected pins, starting at the playhead, as one undo step."""
        clip = self.primary()
        pins = self.selected_pins()
        rows = _PIN_CLIPBOARD.get("rows") or []
        if clip is None or not pins or not rows:
            return False
        frame = self.local_frame(clip)
        offset = frame - int(_PIN_CLIPBOARD.get("start", 0))

        def action():
            target = self.engine.timeline.find_clip(clip.id)
            if target is None:
                return
            for index, axis, keys in rows:
                if index >= len(pins):
                    continue
                pin = pins[index]
                channel = CH.pin_channel("puppet_warp", pin.get("id"), axis)
                shifted = [(max(0, int(key[0]) + offset), *key[1:]) for key in keys]
                CH.replace_keys(target, channel, shifted)
        self.session.stack.run("Paste puppet pin keys", action)
        self._notify()
        return True

    def nudge(self, dx, dy, fine=False):
        clips = self.video_clips()
        if not clips:
            return False
        self.session.begin_gesture()
        for clip in clips:
            state = T.evaluate(clip, self.local_frame(clip))
            factor = 0.1 if fine else 1.0
            self._write_position(clip, state.position_x + dx * factor,
                                 state.position_y + dy * factor)
        self.session.commit_gesture("Nudge clip")
        self._notify()
        return True

    def _cursor_for(self, handle):
        if handle == "rotate":
            return Qt.CursorShape.CrossCursor
        if handle == "anchor":
            return Qt.CursorShape.SizeAllCursor
        if handle in ("nw", "se"):
            return Qt.CursorShape.SizeFDiagCursor
        if handle in ("ne", "sw"):
            return Qt.CursorShape.SizeBDiagCursor
        if handle in ("n", "s"):
            return Qt.CursorShape.SizeVerCursor
        if handle in ("e", "w"):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.ArrowCursor

    def _notify(self):
        if callable(self.on_change):
            self.on_change()
