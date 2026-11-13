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

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen, QPolygonF

from .. import crashlog, tokens
from ..model import channels as CH
from ..model import transform as T
from .pen_tool import PenTool

HANDLE = 7.0          # half size of a scale handle, widget px
ROTATE_OFFSET = 26.0  # distance of the rotation knob above the box, widget px
KNOB_MARGIN = 16.0    # keep the pinned rotation knob this far inside the viewport
KNOB_SLIDE = 22.0     # step used to slide a pinned knob off a scale handle
HIT = 9.0             # grab radius, widget px
SNAP_PX = 7.0         # snapping distance on screen, widget px
ROTATE_SNAP_DEGREES = 15.0      # Shift snaps rotation to this step
MIN_CONTENT_PX = 3.0  # a resize never shrinks a picture below this on either axis

# Pin keyframes copied with Ctrl+C live here, so they survive a clip or panel change.
_PIN_CLIPBOARD = {"rows": [], "start": 0, "pins": 0}
PATH_STEP = 2         # frames between motion-path samples

HANDLE_NAMES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


def _segment_distance(point, start, end):
    """Distance from a point to a line segment, in widget pixels."""
    dx, dy = end.x() - start.x(), end.y() - start.y()
    if abs(dx) + abs(dy) < 1e-9:
        return (point - start).manhattanLength()
    t = ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    projection = QPointF(start.x() + dx * t, start.y() + dy * t)
    return (point - projection).manhattanLength()


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
        self.show_transform_controls = True
        self.show_motion_path = True
        self.show_mesh = False
        self.mode = "select"          # select | transform | puppet | roto | corner | mesh | shape
        # Roto Brush: the radius is a fraction of the picture's shorter edge, so the same
        # brush covers the same part of the subject at every preview zoom and quality.
        self.roto_brush_size = 0.05
        self.roto_brush_mode = "foreground"      # foreground | background | refine
        self.roto_outline_visible = True
        self.roto_choose_subject = False
        self.roto_hover = None
        # Smart Key: the Properties card arms the next preview click, which stores the
        # chosen subject point on that effect entry. No brush mode is entered.
        self.subject_pick_effect = None
        # MainWindow hooks: a finished stroke invalidates cached masks and refreshes the
        # card, and exiting the mode has to put the transform toolbar back.
        self.on_roto_change = None
        self.on_mode_change = None
        self.on_roto_ui_change = None
        self.on_subject_pick = None
        self._roto_outline_cache = {}
        # While the shape tool is armed the next press starts a draft rectangle; it is
        # painted as a live outline and only becomes a clip on release.
        self.shape_kind = "rectangle"
        self._shape_draft = None
        self._drag = None
        self._guides = []
        self._hover = None
        self._selected_pins = set()    # puppet pins, by id; several can be picked at once
        self._selected_path_key = None
        self._selected_corner = None
        self._selected_node = None
        self.on_change = None          # callable() -> re-render + refresh panels
        self.pen = PenTool(self)
        self.session.changed.connect(self._sync_pen_edit)
        self.session.playhead_changed.connect(self._sync_pen_edit)
        self.snap_enabled = True
        self.snap_threshold = SNAP_PX
        self.snap_show_guides = True
        self.snap_edges = True
        self.snap_center = True
        self.snap_objects = True

    def _sync_pen_edit(self, *_args):
        """Undo, redo and scrubbing must update an open mask edit, not stale points."""
        if getattr(self, "pen", None) is not None and self.mode == "pen":
            self.pen.sync_from_session()

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
        if getattr(clip, "is_text", False) or getattr(clip, "is_shape", False):
            # Generated layers really are full-frame pictures. Their intrinsic content is
            # reported separately by content_rect(), so fit/anchor/transform semantics
            # stay exactly what the renderer uses.
            return self.project_size()
        size = self.engine.media.source_size(clip.source_path)
        return size or self.project_size()

    def content_rect(self, clip):
        """(x, y, w, h) of the visible content in the layer's own pixels.

        A generated layer is a full project-sized transparent picture by design, so its
        bounds must come from the geometry rather than image_size(). Media layers still
        fill their source rectangle; the fit is applied by the transform matrix.
        """
        if getattr(clip, "is_shape", False):
            from ..model import shape as shape_model
            from ..processing import shapes
            rect = shapes.bounds(shape_model.resolve(clip, self.local_frame(clip)),
                                 *self.project_size())
        elif getattr(clip, "is_text", False):
            from ..model import text as text_model
            from ..processing import text
            rect = text.bounds(text_model.resolve(clip, self.local_frame(clip)),
                               *self.project_size())
        else:
            width, height = self.image_size(clip)
            rect = (0.0, 0.0, float(width), float(height))
        if rect is None:
            # Empty text draws nothing; keep an editable point at the frame centre.
            width, height = self.project_size()
            return ((width - 1.0) / 2.0, (height - 1.0) / 2.0, 1.0, 1.0)
        return tuple(float(value) for value in rect)

    def matrix(self, clip):
        return T.matrix(self.state(clip), self.project_size(), self.image_size(clip))

    def clip_corners(self, clip):
        """The four transformed content corners, in project pixels."""
        state = self.state(clip)
        return T.content_corners_rect(state, self.project_size(), self.image_size(clip),
                                      self.content_rect(clip))

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
                if not clip.source_path and not clip.is_text and not clip.is_shape:
                    continue
                width, height = self.image_size(clip)
                source = T.invert(project, self.state(clip), self.project_size(), (width, height))
                if source is None:
                    continue
                x0, y0, content_w, content_h = self.content_rect(clip)
                if x0 <= source[0] < x0 + content_w and y0 <= source[1] < y0 + content_h:
                    return clip
        return None

    def _mask_at(self, clip, pos):
        """The visible mask outline under the cursor, if any."""
        from ..model import mask as MASK
        from ..model import path as PATHS
        if clip is None:
            return None
        frame = self.local_frame(clip)
        best = None
        for mask in MASK.at(clip, frame, self.project_size()):
            if not mask.get("show_outline", True):
                continue
            values = PATHS.sampled(mask.get("path") or {}, steps=18)
            previous = None
            for x, y in values:
                current = self.to_widget(x, y)
                if previous is not None:
                    distance = _segment_distance(pos, previous, current)
                    if distance <= HIT * 1.5 and (best is None or distance < best[0]):
                        best = (distance, int(mask.get("id", 0)))
                previous = current
        return None if best is None else best[1]

    def _shape_path_hit(self, clip, pos):
        """Whether the pointer is on a custom shape's visible outline."""
        from ..model import mask as MASK
        from ..model import path as PATHS
        from ..model import shape as SHAPE

        if clip is None or not getattr(clip, "is_shape", False):
            return False
        data = SHAPE.ensure_shape(clip)
        if str(data.get("kind") or "") != "path":
            return False
        path = PATHS.at(data.get("path") or {}, self.local_frame(clip))
        matrix = self.matrix(clip)
        if matrix is not None:
            path = PATHS.transform(path, np.asarray(matrix, dtype=np.float64))
        if len(PATHS.points(path)) < 2:
            return False
        previous = None
        for x, y in PATHS.sampled(path, steps=24):
            current = self.to_widget(x, y)
            if previous is not None and _segment_distance(pos, previous, current) <= HIT * 1.5:
                return True
            previous = current
        return False

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
        nx = inverse[0] / max(1.0, width - 1.0)
        ny = inverse[1] / max(1.0, height - 1.0)
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
        nx, ny = inverse[0] / max(1, width - 1), inverse[1] / max(1, height - 1)
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
    # ------------------------------------------------------------------ roto brush
    def roto_clip_and_entry(self):
        clip = self.primary()
        if clip is None:
            return None, None
        return clip, self._effect_entry(clip, "roto_brush")

    def roto_source_frame(self, clip):
        """The clip's source frame at the playhead (trims, speed and reverse included)."""
        try:
            return max(0, int(clip.source_frame_at(int(self.session.playhead))))
        except Exception:                                   # noqa: BLE001 - live editing
            return max(0, self.local_frame(clip))

    def picture_point(self, widget_point, clip=None):
        """Widget -> normalised picture coordinates through the real clip transform.

        This is the whole alignment story: the point goes back through the same
        position/scale/rotation matrix the renderer uses, so a stroke or subject click
        stays on the same part of the picture at every zoom, pan and preview size.
        """
        clip = clip if clip is not None else self.primary()
        if clip is None:
            return None
        project = self.to_project(widget_point)
        if project is None:
            return None
        width, height = self.image_size(clip)
        source = T.invert(tuple(project), self.state(clip), self.project_size(),
                          (width, height))
        if source is None:
            return None
        return (max(0.0, min(1.0, source[0] / max(1.0, float(width)))),
                max(0.0, min(1.0, source[1] / max(1.0, float(height)))))

    def roto_picture_point(self, widget_point):
        clip, _entry = self.roto_clip_and_entry()
        return self.picture_point(widget_point, clip)

    def begin_subject_pick(self, effect_key="smart_key"):
        """Arm the next preview click as a subject choice for one automatic effect."""
        self.subject_pick_effect = str(effect_key or "smart_key")
        self.roto_choose_subject = False
        self.widget.setCursor(Qt.CursorShape.CrossCursor)
        self.widget.update()
        return True

    def cancel_subject_pick(self):
        if self.subject_pick_effect is None:
            return False
        self.subject_pick_effect = None
        self.widget.setCursor(Qt.CursorShape.BlankCursor if self.mode == "roto"
                              else Qt.CursorShape.ArrowCursor)
        self.widget.update()
        return True

    def roto_to_project(self, clip, point):
        width, height = self.image_size(clip)
        return T.forward((float(point[0]) * width, float(point[1]) * height),
                         self.state(clip), self.project_size(), (width, height))

    def roto_brush_pixels(self, clip):
        """The brush radius in widget pixels, so the circle matches what is painted."""
        return self.roto_brush_pixels_for(clip, self.roto_brush_size)

    def roto_brush_pixels_for(self, clip, fraction):
        width, height = self.image_size(clip)
        rect = self.widget.frame_rect()
        project_scale = rect.width() / max(1e-9, float(self.project_size()[0]))
        return max(1.5, float(fraction) * min(width, height) * project_scale)

    def roto_matte(self, clip, entry, frame):
        """The raw matte for one source frame: from the cache, else from its strokes."""
        from ..roto.cache import MaskCache, mask_key, mask_size
        stored = MaskCache().load(clip, frame, mask_key(entry))
        if stored is not None:
            return stored
        runtime = self.engine.runtime_effects.get("roto_brush")
        if runtime is None:
            return None
        rgb = self.engine.media.frame_for_clip(clip, int(self.session.playhead),
                                               self.engine.timeline.fps)
        if rgb is None:
            return None
        runtime.clip = clip
        runtime.clip_entry = entry
        runtime.local_frame = self.local_frame(clip)
        runtime.param_scale = 1.0
        params = dict(getattr(runtime, "defaults", {}) or {})
        params.update(entry.get("params") or {})
        runtime.load_params(params)
        import cv2
        height, width = rgb.shape[:2]
        size = mask_size(width, height, max_edge=360)
        small = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
        return runtime.raw_matte(small, int(frame))

    def roto_outline(self, clip, entry, frame):
        """Normalised outline polylines for the current frame (memoised per frame)."""
        from ..roto.cache import mask_key
        from ..roto.segment import outline_contours
        key = (getattr(clip, "id", None), int(frame), mask_key(entry))
        if key in self._roto_outline_cache:
            return self._roto_outline_cache[key]
        matte = self.roto_matte(clip, entry, frame)
        contours = outline_contours(matte) if matte is not None else []
        self._roto_outline_cache = {key: contours}
        return contours

    def roto_mode_label(self):
        labels = {"foreground": "Green: keep", "background": "Red: remove",
                  "refine": "Refine edge"}
        return labels.get(self.roto_brush_mode, "Green: keep")

    def _paint_roto(self, painter):
        """The brush, its live stroke, the segmentation outline and the mode strip."""
        clip, entry = self.roto_clip_and_entry()
        if clip is None:
            return
        if (self.roto_outline_visible and entry is not None
                and not (self._drag is not None and self._drag.get("kind") == "roto")):
            frame = self.roto_source_frame(clip)
            for contour in self.roto_outline(clip, entry, frame):
                points = [QPointF(self.to_widget(*self.roto_to_project(clip, point)))
                          for point in contour]
                if len(points) < 3:
                    continue
                polygon = QPolygonF(points)
                painter.save()
                painter.setBrush(Qt.BrushStyle.NoBrush)
                # A dark underlay keeps the outline readable on bright and dark footage;
                # the purple itself is the "this is the segmentation" signal.
                painter.setPen(QPen(QColor(16, 8, 24, 190), 4.4))
                painter.drawPolygon(polygon)
                painter.setPen(QPen(QColor(186, 104, 255), 2.0))
                painter.drawPolygon(polygon)
                painter.restore()
        drag = self._drag
        frame = self.roto_source_frame(clip)
        if entry is not None and not (drag is not None and drag.get("kind") == "roto"):
            # Every committed correction stays visible on its frame. The active stroke
            # is drawn at full opacity below, so painting always gives feedback without
            # clearing the previous corrections from the picture.
            from ..roto.strokes import strokes_for_frame
            for stroke in strokes_for_frame(entry.get("strokes") or (), frame,
                                            entry.get("base_frame", 0)):
                points = [QPointF(self.to_widget(*self.roto_to_project(clip, point)))
                          for point in stroke.get("points") or ()]
                if not points:
                    continue
                colour = {"background": QColor(255, 82, 82, 185),
                          "refine": QColor(120, 200, 255, 185)}.get(
                    str(stroke.get("mode")), QColor(64, 220, 120, 185))
                radius = self.roto_brush_pixels_for(clip, float(stroke.get(
                    "radius", self.roto_brush_size)))
                painter.save()
                painter.setPen(QPen(colour, max(2.0, radius * 1.7),
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                    Qt.PenJoinStyle.RoundJoin))
                if len(points) == 1:
                    painter.drawPoint(points[0])
                else:
                    painter.drawPolyline(QPolygonF(points))
                painter.restore()
        if drag is not None and drag.get("kind") == "roto":
            stroke = drag["stroke"]
            points = [QPointF(self.to_widget(*self.roto_to_project(clip, point)))
                      for point in stroke.get("points") or ()]
            colour = {"background": QColor(255, 82, 82),
                      "refine": QColor(120, 200, 255)}.get(
                str(stroke.get("mode")), QColor(64, 220, 120))
            if len(points) >= 2:
                painter.save()
                painter.setPen(QPen(colour, max(2.0, self.roto_brush_pixels(clip) * 1.7),
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                    Qt.PenJoinStyle.RoundJoin))
                painter.drawPolyline(QPolygonF(points))
                painter.restore()
        hover = self.roto_hover
        if hover is not None:
            radius = self.roto_brush_pixels(clip)
            colour = {"background": QColor(255, 82, 82),
                      "refine": QColor(120, 200, 255)}.get(
                self.roto_brush_mode, QColor(64, 220, 120))
            painter.save()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 140), 3.0))
            painter.drawEllipse(hover, radius, radius)
            painter.setPen(QPen(colour, 1.6))
            painter.drawEllipse(hover, radius, radius)
            painter.drawLine(QPointF(hover.x() - 4, hover.y()),
                             QPointF(hover.x() + 4, hover.y()))
            painter.drawLine(QPointF(hover.x(), hover.y() - 4),
                             QPointF(hover.x(), hover.y() + 4))
            painter.restore()
    def exit_roto(self):
        """Leave the rotoscoping mode and hand the normal transform tools back."""
        if self.mode != "roto":
            return False
        self.mode = "select"
        self.roto_hover = None
        self.roto_choose_subject = False
        self._drag = None
        self.widget.setCursor(Qt.CursorShape.ArrowCursor)
        self.widget.update()
        if callable(self.on_mode_change):
            self.on_mode_change("select")
        return True

    def set_roto_brush_mode(self, mode):
        if mode not in ("foreground", "background", "refine"):
            return False
        self.roto_brush_mode = str(mode)
        self.widget.update()
        if callable(self.on_roto_ui_change):
            self.on_roto_ui_change()
        return True

    def adjust_roto_brush(self, factor):
        self.roto_brush_size = max(0.005,
                                   min(0.35, float(self.roto_brush_size) * float(factor)))
        self.widget.update()
        if callable(self.on_roto_ui_change):
            self.on_roto_ui_change()
        return self.roto_brush_size

    def paint(self, painter):
        if not self.enabled:
            return
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        if self.mode == "roto":
            # Rotoscoping is its own editing mode. No bounding box, resize handles,
            # rotation knob, anchor or motion path over the picture - only the brush,
            # its outline and the mode strip. The clip's transform is untouched.
            self._paint_roto(painter)
            self._paint_guides(painter)
            return
        if self.mode == "pen":
            self.pen.paint(painter)
            self._paint_guides(painter)
            return
        if not self.show_transform_controls:
            if self._shape_draft is not None:
                self._paint_shape_draft(painter)
            return
        for clip in self.video_clips():
            self._paint_clip(painter, clip)
        self.pen.paint_masks(painter)
        if self._shape_draft is not None:
            self._paint_shape_draft(painter)
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

    def _paint_shape_draft(self, painter):
        """The outline of the shape being drawn, constrained exactly like the result."""
        draft = self._shape_draft
        box = self._shape_box(draft)
        if box is None:
            return
        x, y, width, height, rotation = box
        corners = [self.to_widget(x, y), self.to_widget(x + width, y),
                   self.to_widget(x + width, y + height), self.to_widget(x, y + height)]
        colour = QColor(tokens.COLORS["accent"])
        painter.save()
        painter.setPen(QPen(colour, 1.4, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if draft.get("kind") == "line":
            start, end = self._shape_line_points(x, y, width, height, rotation)
            painter.drawLine(self.to_widget(*start), self.to_widget(*end))
        elif draft.get("kind") == "ellipse":
            painter.drawPolygon(QPolygonF([self.to_widget(*corner) for corner in
                                           self._ellipse_points(x, y, width, height)]))
        else:
            painter.drawPolygon(QPolygonF(corners))
        painter.restore()

    @staticmethod
    def _ellipse_points(x, y, width, height, steps=48):
        cx, cy = x + width / 2.0, y + height / 2.0
        return [(cx + width / 2.0 * math.cos(2 * math.pi * i / steps),
                 cy + height / 2.0 * math.sin(2 * math.pi * i / steps)) for i in range(steps)]

    @staticmethod
    def _shape_line_points(x, y, width, height, rotation):
        cx, cy = x + width / 2.0, y + height / 2.0
        angle = math.radians(rotation)
        dx, dy = math.cos(angle) * width / 2.0, math.sin(angle) * width / 2.0
        return (cx - dx, cy - dy), (cx + dx, cy + dy)

    def _shape_box(self, draft):
        """(x, y, w, h, rotation) in project pixels for a shape draft, or None.

        Shift constrains to a circle/square, Alt (or the press point) draws from the
        centre. A line keeps the exact drag: its box is as long as the drag and the
        rotation carries its direction, so squeezing a handle later still works.
        """
        start = draft["start"]
        current = draft["current"]
        dx = current[0] - start[0]
        dy = current[1] - start[1]
        kind = draft.get("kind") or "rectangle"
        mods = draft.get("modifiers", Qt.KeyboardModifier.NoModifier)
        if kind == "line":
            length = math.hypot(dx, dy)
            if length < 3.0:
                return None
            rotation = math.degrees(math.atan2(dy, dx))
            thickness = 3.0
            return (start[0] + dx / 2.0 - length / 2.0,
                    start[1] + dy / 2.0 - thickness / 2.0, length, thickness, rotation)
        if mods & Qt.KeyboardModifier.ShiftModifier:
            side = max(abs(dx), abs(dy))
            dx = math.copysign(side, dx or 1.0)
            dy = math.copysign(side, dy or 1.0)
        if bool(draft.get("from_centre")) or mods & Qt.KeyboardModifier.AltModifier:
            x0, y0, x1, y1 = start[0] - dx, start[1] - dy, start[0] + dx, start[1] + dy
        else:
            x0, y0, x1, y1 = start[0], start[1], start[0] + dx, start[1] + dy
        width, height = abs(x1 - x0), abs(y1 - y0)
        if width < 2.0 and height < 2.0:
            return None                       # a click is not a drag
        return min(x0, x1), min(y0, y1), width, height, 0.0

    def _paint_guides(self, painter):
        if not self._guides or not getattr(self, "snap_show_guides", True):
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
        lens = self.lens_handles(clip)
        if lens:
            painter.save()
            painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 1.5))
            for point in lens.values():
                painter.drawEllipse(point, 5., 5.)
            painter.drawLine(lens["center"], lens["radius"])
            painter.restore()
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
                project = T.forward((nx * max(0, width - 1), ny * max(0, height - 1)),
                                    self.state(clip),
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
        if self.subject_pick_effect is not None and self.mode not in ("roto", "shape"):
            clip = self.primary()
            entry = (self._effect_entry(clip, self.subject_pick_effect)
                     if clip is not None else None)
            point = self.picture_point(pos, clip)
            if clip is None or entry is None or point is None:
                self.cancel_subject_pick()
                return False
            # Choosing a subject is a real edit, so it is one undo step and the new
            # point becomes part of the mask key.
            self.session.begin_gesture()
            entry["subject_point"] = [float(point[0]), float(point[1])]
            entry.setdefault("params", {})["subject"] = "auto"
            self.session.commit_gesture("Choose subject")
            self.subject_pick_effect = None
            self.widget.setCursor(Qt.CursorShape.ArrowCursor)
            self.widget.update()
            self._notify()
            if callable(self.on_subject_pick):
                self.on_subject_pick(clip, entry, point)
            return True
        if self.mode == "shape":
            project = self.to_project(pos)
            if project is None:
                return False
            self._shape_draft = {"kind": self.shape_kind or "rectangle", "start": project,
                                 "current": project, "from_centre": False,
                                 "modifiers": event.modifiers()}
            self.widget.update()
            return True
        if self.mode == "pen":
            return self.pen.press(event)
        if self.mode == "roto":
            clip, entry = self.roto_clip_and_entry()
            point = self.roto_picture_point(pos)
            if clip is None or entry is None or point is None:
                return False
            if self.roto_choose_subject:
                # The chosen point selects one component of the model matte. It is stored
                # on the effect entry, so it survives reload and invalidates old masks.
                self.session.begin_gesture()
                entry["subject_point"] = [float(point[0]), float(point[1])]
                entry.setdefault("params", {})["subject"] = "auto"
                self.session.commit_gesture("Choose Roto subject")
                self.roto_choose_subject = False
                self._roto_outline_cache.clear()
                self.widget.setCursor(Qt.CursorShape.BlankCursor)
                self.widget.update()
                self._notify()
                if callable(self.on_roto_change):
                    self.on_roto_change()
                return True
            mode = ("background" if event.modifiers() & Qt.KeyboardModifier.AltModifier
                    else self.roto_brush_mode)
            frame = self.roto_source_frame(clip)
            # The first stroke defines the base frame; later frames become corrections.
            if not entry.get("strokes"):
                entry["base_frame"] = int(frame)
            # Snapshot before the first point: the whole painted stroke is one undo step.
            self.session.begin_gesture()
            stroke = {"frame": int(frame),
                      "points": [[point[0], point[1]]],
                      "radius": float(self.roto_brush_size),
                      "mode": str(mode)}
            entry.setdefault("strokes", []).append(stroke)
            self._drag = {"kind": "roto", "clip": clip, "stroke": stroke}
            # No segmentation/render on press: the model runs once when the stroke is
            # finished, not on every mouse sample.
            self.widget.update()
            return True
        clip = self.primary()
        if clip is not None and not clip.track.locked and self.mode in ("select", "transform"):
            if self._shape_path_hit(clip, pos) and self.pen.edit_shape(clip.id):
                if callable(self.on_mode_change):
                    self.on_mode_change("pen")
                self.widget.update()
                return True
            mask_id = self._mask_at(clip, pos)
            if mask_id is not None:
                self.session.select_clip_mask(clip.id, mask_id)
                if self.pen.edit_mask(clip.id, mask_id):
                    if callable(self.on_mode_change):
                        self.on_mode_change("pen")
                    self.widget.update()
                    return True
        if clip is not None and not clip.track.locked and self.mode in ("select", "transform"):
            for name, point in self.lens_handles(clip).items():
                if (point - pos).manhattanLength() <= HIT * 1.6:
                    self.session.begin_gesture()
                    self._drag = {"kind": "lens", "clip": clip, "handle": name}
                    return True
        # Warp handles win over the transform handles: corner-pin corners and mesh
        # nodes sit exactly on the box corners and centre, where the box handles would
        # otherwise swallow every click on them.
        if clip is not None and not clip.track.locked and self.mode in ("corner", "transform"):
            node = self._mesh_node_at(clip)
            if node is not None:
                row, col, _nx, _ny = node
                self.session.begin_gesture()
                self._drag = {"kind": "mesh", "clip": clip, "row": row, "col": col}
                self.widget.update()
                return True
        if clip is not None and not clip.track.locked and self.mode in ("select", "corner", "transform"):
            # Alt-dragging a frame corner is the direct perspective gesture. This must
            # be resolved before _begin_transform, which otherwise treats Alt as a
            # centre scale and consumes the press. The normal (unmodified) corner drag
            # still falls through to the established scale handler below.
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                corners = self.clip_corners(clip)
                corner_points = [self.to_widget(*point) for point in corners]
                corner_index = min(range(4), key=lambda i:
                                   (corner_points[i] - pos).manhattanLength())
                if (corner_points[corner_index] - pos).manhattanLength() <= HIT * 2.0:
                    # Snapshot before creating the effect: an Alt drag on a clip with no
                    # Corner Pin must undo to the original clip, not to an empty effect.
                    self.session.begin_gesture()
                    if self._effect_entry(clip, "corner_pin") is None:
                        clip.effects.append({"effect_id": "corner_pin", "enabled": True,
                                             "params": {}})
                    self._selected_corner = corner_index
                    self._drag = {"kind": "corner", "clip": clip,
                                  "index": corner_index}
                    self.widget.update()
                    return True
            index, _point = self._corner_at(clip)
            if index is not None:
                self._selected_corner = index
                self.session.begin_gesture()
                self._drag = {"kind": "corner", "clip": clip, "index": index}
                self.widget.update()
                return True
        if not self.show_transform_controls:
            hit = self._clip_at(pos)
            if hit is not None:
                if hit.id not in self.session.selected:
                    self.session.set_selection({hit.id}, expand=True)
                # Locked tracks remain selectable, but preview dragging must not start.
                if hit.track.locked:
                    self.widget.update()
                    return True
                clips = [item for item in (self.video_clips() or [hit])
                         if not item.track.locked]
                self.session.begin_gesture()
                starts = [(item, T.evaluate(item, self.local_frame(item))) for item in clips]
                self._drag = {"kind": "move", "clips": starts, "origin": self.to_project(pos),
                              "moved": False}
            else:
                self.session.set_selection(set(), expand=False)
            self.widget.update()
            return True
        if clip is not None and self.mode in ("transform", "select", "puppet", "corner"):
            handle = self._handle_at_priority(clip, pos)
            if handle is not None and not clip.track.locked:
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
            if hit.track.locked:
                self.widget.update()
                return True
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
        anchor = self.anchor_point(clip)
        ensure = T.ensure_transform(clip)
        kind = ("rotate" if handle == "rotate"
                else ("anchor" if handle == "anchor" else "scale"))
        project = self.to_project(pos)
        # Behaviour switches are read once per gesture: flipping a checkbox in the
        # inspector in the middle of a drag must not change the drag under the mouse.
        flags = {"scale_from_center": bool(ensure.get("scale_from_center", False)),
                 "snap_rotation": bool(ensure.get("snap_rotation", True))}
        base = None
        if kind == "scale":
            if project is None:
                return False
            # Ctrl is the free transform's proportional centre resize: the picture grows
            # around its middle, so neither corner can drift. Shift and Alt resize from
            # the centre without locking the ratio; with no modifier the opposite corner
            # stays pinned, which is the plain corner drag.
            from_centre = bool(modifiers & (Qt.KeyboardModifier.ControlModifier
                                            | Qt.KeyboardModifier.ShiftModifier
                                            | Qt.KeyboardModifier.AltModifier)) \
                or flags["scale_from_center"]
            base = self._scale_geometry(clip, state, handle, from_centre)
            if base is None:
                return False
            base["mods"] = (bool(modifiers & Qt.KeyboardModifier.ControlModifier), from_centre)
            base["pointer"] = project
        self.session.begin_gesture()
        self._drag = {
            "kind": kind, "clip": clip, "handle": handle, "state": state,
            "modifiers": modifiers, "anchor": anchor, "origin": project,
            "angle": self._angle(pos, anchor), "flags": flags,
        }
        if base is not None:
            self._drag["base"] = base
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
        if self._shape_draft is not None:
            project = self.to_project(pos)
            if project is not None:
                self._shape_draft["current"] = project
                self._shape_draft["modifiers"] = event.modifiers()
                self._shape_draft["from_centre"] = bool(
                    event.modifiers() & Qt.KeyboardModifier.AltModifier)
            self.widget.update()
            return True
        if self.mode == "pen":
            return self.pen.move(event)
        drag = self._drag
        if (drag is not None and drag.get("kind") == "scale"
                and drag.get("handle") in ("nw", "ne", "se", "sw")
                and event.modifiers() & Qt.KeyboardModifier.AltModifier):
            # Alt can be pressed after the mouse went down. Cancel the scale gesture
            # (restoring its pre-drag snapshot) and continue as a corner-pin gesture.
            # The corner starts where it was, and follows the pointer's subsequent
            # delta, so switching modifiers does not make the picture jump.
            drag = self._scale_to_corner_drag(drag, pos)
        if drag is not None and drag.get("kind") == "roto":
            clip = drag.get("clip")
            point = self.roto_picture_point(pos)
            if clip is not None and point is not None:
                stroke = drag["stroke"]
                points = stroke.setdefault("points", [])
                radius = max(1e-4, float(stroke.get("radius", self.roto_brush_size)))
                if (not points
                        or abs(point[0] - points[-1][0]) + abs(point[1] - points[-1][1])
                        >= radius * 0.05):
                    points.append([point[0], point[1]])
                    self.widget.update()
            return True
        if self.mode == "roto":
            self.roto_hover = QPointF(pos)
            # The ring is the cursor. Hiding the native arrow makes it the actual brush
            # footprint instead of an approximate decoration beside a crosshair.
            self.widget.setCursor(Qt.CursorShape.BlankCursor)
            self.widget.update()
        if drag is None:
            if self.mode in ("shape", "roto"):
                self._hover = None
                self.widget.setCursor(Qt.CursorShape.CrossCursor)
                return False
            if not self.show_transform_controls:
                self._hover = None
                self.widget.setCursor(Qt.CursorShape.ArrowCursor)
                return False
            clip = self.primary()
            self._hover = self._handle_at_priority(clip, pos) if clip is not None else None
            self.widget.setCursor(self._cursor_for(self._hover))
            return False
        kind = drag["kind"]
        # Modifier contract. Moving and rotating keep their long-standing keys:
        #   move    - Shift constrains to one axis, Ctrl skips snapping;
        #   rotate  - Shift snaps to 15 degrees, Ctrl is a fine 1:10 ratio;
        #   scale   - Ctrl resizes proportionally from the centre, Shift/Alt resize from
        #             the centre without a ratio lock, no modifier pins the opposite corner.
        # The scale keys are the free-transform convention, so Ctrl can never mean
        # "precision" there: a corner drag is already 1:1 with the pointer.
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
        if kind == "lens":
            self._lens_drag(drag, pos)
            self._notify()
            return True
        if kind == "scale":
            self._scale_drag(drag, pos, ctrl=precise, shift=shift, alt=alt)
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

    def lens_handles(self, clip):
        entry = self._effect_entry(clip, "lens_warp")
        if entry is None or not entry.get("enabled", True):
            return {}
        width, height = self.image_size(clip)
        frame = self.local_frame(clip)
        cx = self.param(clip, "lens_warp", "center_x", frame, 50) * (width - 1) / 100
        cy = self.param(clip, "lens_warp", "center_y", frame, 50) * (height - 1) / 100
        radius = self.param(clip, "lens_warp", "radius", frame, 45) * min(width, height) / 100
        aspect = self.param(clip, "lens_warp", "aspect", frame, 1)
        return {name: self.to_widget(*T.forward(point, self.state(clip), self.project_size(),
                                              (width, height)))
                for name, point in (("center", (cx, cy)), ("radius", (cx + radius * aspect, cy)))}

    def _lens_drag(self, drag, pos):
        clip = drag["clip"]
        project = self.to_project(pos)
        if project is None:
            return
        width, height = self.image_size(clip)
        point = T.invert(project, self.state(clip), self.project_size(), (width, height))
        if point is None:
            return
        if drag["handle"] == "center":
            for name, value in (("center_x", point[0] * 100 / max(1, width - 1)),
                                ("center_y", point[1] * 100 / max(1, height - 1))):
                self._write(clip, CH.effect_channel("lens_warp", name), max(0, min(100, value)))
        else:
            frame = self.local_frame(clip)
            cx = self.param(clip, "lens_warp", "center_x", frame, 50) * (width - 1) / 100
            cy = self.param(clip, "lens_warp", "center_y", frame, 50) * (height - 1) / 100
            aspect = max(.25, self.param(clip, "lens_warp", "aspect", frame, 1))
            radius = math.hypot((point[0] - cx) / aspect, point[1] - cy) * 100 / max(1, min(width, height))
            self._write(clip, CH.effect_channel("lens_warp", "radius"), max(1, min(150, radius)))

    def _write_position(self, clip, x, y):
        frame = self.local_frame(clip)
        auto = bool(self.session.auto_keyframe.get())
        T.set_position(clip, x, y, frame, auto)

    def _scale_geometry(self, clip, state, handle, from_centre):
        """The fixed point, local axes and handle offsets a scale gesture works from.

        Everything is measured in composition space on the transform captured at
        mouse-down. ``u`` and ``v`` are the picture's own axes after rotation and scale,
        so a corner drag is resolved in local space even when the clip is rotated, and a
        non-square picture does not skew the ratios. The fixed point is the opposite
        corner/edge (or the centre when asked), never the anchor: the anchor only affects
        where the scale grows from, and the translation below cancels that out.
        """
        project_size = self.project_size()
        image_size = self.image_size(clip)
        rect = self.content_rect(clip)
        x0, y0, width, height = rect
        width, height = float(max(1.0, width)), float(max(1.0, height))
        tl, tr, _br, bl = T.content_corners_rect(state, project_size, image_size, rect)
        u = (tr[0] - tl[0], tr[1] - tl[1])
        v = (bl[0] - tl[0], bl[1] - tl[1])
        uu = u[0] * u[0] + u[1] * u[1]
        vv = v[0] * v[0] + v[1] * v[1]
        if uu <= 1e-9 or vv <= 1e-9:
            return None
        source = {"nw": (x0, y0), "n": (x0 + width / 2.0, y0),
                  "ne": (x0 + width, y0), "e": (x0 + width, y0 + height / 2.0),
                  "se": (x0 + width, y0 + height),
                  "s": (x0 + width / 2.0, y0 + height),
                  "sw": (x0, y0 + height), "w": (x0, y0 + height / 2.0)}
        opposite = {"nw": (x0 + width, y0 + height),
                    "n": (x0 + width / 2.0, y0 + height),
                    "ne": (x0, y0 + height), "e": (x0, y0 + height / 2.0),
                    "se": (x0, y0), "s": (x0 + width / 2.0, y0),
                    "sw": (x0 + width, y0), "w": (x0, y0 + height / 2.0)}
        handle_src = source[handle]
        fixed_src = ((x0 + width / 2.0, y0 + height / 2.0) if from_centre
                     else opposite[handle])
        fixed = T.forward(fixed_src, state, project_size, image_size)
        grabbed = T.forward(handle_src, state, project_size, image_size)

        def fractions(point):
            dx, dy = point[0] - fixed[0], point[1] - fixed[1]
            return ((dx * u[0] + dy * u[1]) / uu, (dx * v[0] + dy * v[1]) / vv)

        hu, hv = fractions(grabbed)
        return {"state": state, "u": u, "v": v, "uu": uu, "vv": vv,
                "fixed": fixed, "fixed_src": fixed_src, "handle_src": handle_src,
                "grabbed": grabbed, "hu": hu, "hv": hv,
                "pointer": None, "mods": None}

    @staticmethod
    def _state_with_scale(state, scale_x, scale_y):
        """A copy of ``state`` with new scales, for asking where a point would land."""
        values = {name: getattr(state, name) for name in T.TransformState.__slots__}
        values["scale_x"], values["scale_y"] = scale_x, scale_y
        return T.TransformState(**values)

    def _min_ratio(self, axis_scale, axis_length):
        """Smallest factor that keeps at least MIN_CONTENT_PX of picture on one axis."""
        current = abs(float(axis_scale)) / 100.0 * max(1.0, float(axis_length))
        if current < 0.5:
            return 1.0
        return MIN_CONTENT_PX / current

    def _scale_drag(self, drag, pos, ctrl=False, shift=False, alt=False):
        """Resize from the dragged handle with the opposite corner or edge pinned.

        The term of the drag is the pointer delta from the mouse-down, applied to the
        geometry captured at mouse-down, so the result never accumulates error and never
        depends on the path the pointer took. A corner drag pins the opposite corner and
        changes each axis on its own; Ctrl keeps the aspect ratio the picture had when the
        gesture (or the current modifier state) began and grows around the centre, Shift
        resizes around the centre, Ctrl+Shift does both.
        Switching modifiers rebases on the picture as it is now, which cannot jump, and
        the pinned point is translated back into place afterwards so it stays put.
        """
        clip, handle = drag["clip"], drag["handle"]
        project = self.to_project(pos)
        if project is None:
            return
        from_centre = bool(ctrl or shift or alt)
        mods = (bool(ctrl), from_centre)
        base = drag.get("base")
        if base is None or base.get("mods") != mods:
            state = self.state(clip)
            base = self._scale_geometry(clip, state, handle, from_centre)
            if base is None:
                return
            base["mods"] = mods
            base["pointer"] = project
            drag["base"] = base
        state = base["state"]
        u, v, uu, vv = base["u"], base["v"], base["uu"], base["vv"]
        # Work from the handle's real position plus the pointer delta, not from the raw
        # pointer: under an aspect lock the handle is not under the cursor, and a rebase
        # must continue from where the gesture actually is.
        px = base["grabbed"][0] + (project[0] - base["pointer"][0])
        py = base["grabbed"][1] + (project[1] - base["pointer"][1])
        dx, dy = px - base["fixed"][0], py - base["fixed"][1]
        along_x = (dx * u[0] + dy * u[1]) / uu
        along_y = (dx * v[0] + dy * v[1]) / vv
        # A side handle sits on the opposite edge's axis, so that ratio is undefined and
        # the axis is left exactly as it was.
        ratio_x = along_x / base["hu"] if abs(base["hu"]) > 1e-6 else None
        ratio_y = along_y / base["hv"] if abs(base["hv"]) > 1e-6 else None
        if ratio_x is None and ratio_y is None:
            return
        if ctrl and ratio_x is not None and ratio_y is not None:
            ratio = max(abs(ratio_x), abs(ratio_y))
            ratio_x = ratio_y = ratio
        if getattr(clip, "is_text", False) or getattr(clip, "is_shape", False):
            fit_w, fit_h = self.content_rect(clip)[2:]
        else:
            _x0, _y0, fit_w, fit_h = T.fit_rect(self.project_size(), self.image_size(clip))
        # Positive ratios only: a resize clamps at a small minimum instead of flipping.
        # Flipping stays an explicit inspector operation (flip H/V).
        if ratio_x is not None:
            ratio_x = max(ratio_x, self._min_ratio(state.scale_x, fit_w))
        if ratio_y is not None:
            ratio_y = max(ratio_y, self._min_ratio(state.scale_y, fit_h))
        scale_x = state.scale_x * (1.0 if ratio_x is None else ratio_x)
        scale_y = state.scale_y * (1.0 if ratio_y is None else ratio_y)
        # Scaling happens around the anchor, so ask where the pinned point lands and move
        # the picture by the difference. Translation is uniform, so the pinned corner/edge
        # ends up exactly where it started in composition space.
        moved = self._state_with_scale(state, scale_x, scale_y)
        landed = T.forward(base["fixed_src"], moved, self.project_size(), self.image_size(clip))
        offset_x = base["fixed"][0] - landed[0]
        offset_y = base["fixed"][1] - landed[1]
        frame = self.local_frame(clip)
        auto = bool(self.session.auto_keyframe.get())
        if ratio_x is not None:
            CH.set_value(clip, "transform.scale_x", scale_x, frame, auto)
        if ratio_y is not None:
            CH.set_value(clip, "transform.scale_y", scale_y, frame, auto)
        T.set_position(clip, state.position_x + offset_x, state.position_y + offset_y,
                       frame, auto)

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

    def _corner_project(self, clip, index):
        """The project-space position of one Corner Pin corner."""
        names = (("corner_tl_x", "corner_tl_y"), ("corner_tr_x", "corner_tr_y"),
                 ("corner_br_x", "corner_br_y"), ("corner_bl_x", "corner_bl_y"))[index]
        frame = self.local_frame(clip)
        width, height = self.image_size(clip)
        nx = self.param(clip, "corner_pin", names[0], frame, 0.0 if index in (0, 3) else 1.0)
        ny = self.param(clip, "corner_pin", names[1], frame, 0.0 if index in (0, 1) else 1.0)
        return T.forward((nx * max(0, width - 1), ny * max(0, height - 1)),
                         self.state(clip), self.project_size(), (width, height))

    def _scale_to_corner_drag(self, drag, pos):
        """Turn an in-progress corner scale into a no-jump Corner Pin drag."""
        handle_index = {"nw": 0, "ne": 1, "se": 2, "sw": 3}.get(drag.get("handle"))
        if handle_index is None:
            return drag
        clip_id = drag["clip"].id
        self.session.cancel_gesture()
        clip = self.engine.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            self._drag = None
            return None
        project = self.to_project(pos)
        if project is None:
            self._drag = None
            return None
        self.session.begin_gesture()
        if self._effect_entry(clip, "corner_pin") is None:
            clip.effects.append({"effect_id": "corner_pin", "enabled": True,
                                 "params": {}})
        corner = self._corner_project(clip, handle_index)
        self._selected_corner = handle_index
        self._drag = {"kind": "corner", "clip": clip, "index": handle_index,
                      "origin": project, "start_corner": corner}
        self.widget.update()
        return self._drag

    def _corner_drag(self, drag, pos):
        clip, index = drag["clip"], drag["index"]
        project = self.to_project(pos)
        if project is None:
            return
        target = project
        if drag.get("start_corner") is not None and drag.get("origin") is not None:
            target = (drag["start_corner"][0] + project[0] - drag["origin"][0],
                      drag["start_corner"][1] + project[1] - drag["origin"][1])
        width, height = self.image_size(clip)
        source = T.invert(target, self.state(clip), self.project_size(), (width, height))
        if source is None:
            return
        names = (("corner_tl_x", "corner_tl_y"), ("corner_tr_x", "corner_tr_y"),
                 ("corner_br_x", "corner_br_y"), ("corner_bl_x", "corner_bl_y"))[index]
        frame = self.local_frame(clip)
        auto = bool(self.session.auto_keyframe.get())
        CH.set_value(clip, CH.effect_channel("corner_pin", names[0]),
                     source[0] / max(1, width - 1), frame, auto)
        CH.set_value(clip, CH.effect_channel("corner_pin", names[1]),
                     source[1] / max(1, height - 1), frame, auto)

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
        """Snap to composition edges/centre and every other visible clip's box."""
        self._guides = []
        if skip or not getattr(self, "snap_enabled", True):
            return dx, dy
        pw, ph = self.project_size()
        rect = self.widget.frame_rect()
        if rect.isEmpty():
            return dx, dy
        tolerance = max(1.0, float(getattr(self, "snap_threshold", SNAP_PX))) * pw / max(1e-9, rect.width())
        corners = T.content_corners(state, self.project_size(), self.image_size(clip))
        left = min(c[0] for c in corners) + dx
        right = max(c[0] for c in corners) + dx
        top = min(c[1] for c in corners) + dy
        bottom = max(c[1] for c in corners) + dy
        x_targets = []
        y_targets = []
        if getattr(self, "snap_edges", True):
            x_targets.extend([(0.0, 0), (pw - 1.0, 0)])
            y_targets.extend([(0.0, 0), (ph - 1.0, 0)])
        if getattr(self, "snap_center", True):
            x_targets.append(((pw - 1.0) / 2.0, 1))
            y_targets.append(((ph - 1.0) / 2.0, 1))
        for other in (self.video_clips() if getattr(self, "snap_objects", True) else ()):
            if other is clip:
                continue
            try:
                other_state = self.state(other)
                other_corners = T.content_corners(other_state, self.project_size(),
                                                  self.image_size(other))
            except Exception:
                continue
            ox0, ox1 = min(c[0] for c in other_corners), max(c[0] for c in other_corners)
            oy0, oy1 = min(c[1] for c in other_corners), max(c[1] for c in other_corners)
            x_targets.extend([(ox0, 2), ((ox0 + ox1) / 2.0, 2), (ox1, 2)])
            y_targets.extend([(oy0, 2), ((oy0 + oy1) / 2.0, 2), (oy1, 2)])
        best_x = None
        for edge in (left, (left + right) / 2.0, right):
            for target, priority in x_targets:
                distance = target - edge
                if abs(distance) <= tolerance:
                    key = (abs(distance), priority, target)
                    if best_x is None or key < best_x[0]:
                        best_x = (key, distance, target)
        best_y = None
        for edge in (top, (top + bottom) / 2.0, bottom):
            for target, priority in y_targets:
                distance = target - edge
                if abs(distance) <= tolerance:
                    key = (abs(distance), priority, target)
                    if best_y is None or key < best_y[0]:
                        best_y = (key, distance, target)
        if best_x is not None:
            dx += best_x[1]
            self._guides.append(("x", best_x[2]))
        if best_y is not None:
            dy += best_y[1]
            self._guides.append(("y", best_y[2]))
        return dx, dy

    @crashlog.guard("preview overlay", fallback=False)
    def mouse_release(self, event):
        if self.mode == "pen":
            return self.pen.release(event)
        if self._shape_draft is not None:
            draft = self._shape_draft
            self._shape_draft = None
            box = self._shape_box(draft)
            self.widget.update()
            if box is not None:
                self.session.add_shape_clip(draft.get("kind") or "rectangle", box[:4],
                                            rotation=box[4])
                # Hand the new clip straight to the transform tool: it is selected and
                # its handles are live, exactly like a picture dropped into the frame.
                self.mode = "transform"
                self.widget.setCursor(Qt.CursorShape.ArrowCursor)
                self._notify()
            return True
        drag = self._drag
        if drag is None:
            return False
        self._drag = None
        self._guides = []
        self._knob_pinned = False
        labels = {"move": "Move clip", "scale": "Scale clip", "rotate": "Rotate clip",
                  "anchor": "Move anchor point", "pin": "Move puppet pin",
                  "corner": "Move corner pin", "mesh": "Move mesh node", "lens": "Adjust Lens Warp",
                  "path_point": "Move motion path point",
                  "path_handle": "Adjust motion path", "roto": "Roto Brush stroke"}
        self.session.commit_gesture(labels.get(drag.get("kind"), "Edit transform"))
        self._notify()
        if drag.get("kind") == "roto" and callable(self.on_roto_change):
            self.on_roto_change()
        self.widget.update()
        return True

    def cancel_shape(self):
        """Escape while drawing: drop the draft and leave the shape tool."""
        if self.mode == "pen":
            # The preview's Pen branch owns Escape: finish is Enter/V, Escape cancels.
            return False
        if self._shape_draft is None and self.mode != "shape":
            return False
        self._shape_draft = None
        self.mode = "select"
        self.widget.setCursor(Qt.CursorShape.ArrowCursor)
        self.widget.update()
        return True

    def cancel_drag(self):
        if self._drag is not None:
            self._drag = None
            self._guides = []
            self.session.cancel_gesture()

    def delete_selection(self):
        """Delete the selected pins, corner or motion-path point (not the clip)."""
        if self.mode == "pen" and self.pen.delete_selected():
            return True
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
