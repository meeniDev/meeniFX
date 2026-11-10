"""Pen tool for custom shapes, clip masks and motion paths.

The overlay owns the picture and coordinate conversion; this helper owns one in-progress
path and its editing gestures.  Committing hands the path to the session, where it becomes
an ordinary undoable clip/shape/mask/motion setting.  Nothing here is ever drawn into the
exported frame: the overlay bypasses the compositor entirely.
"""

from __future__ import annotations

import copy

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QPen, QPolygonF

from .. import tokens
from ..model import mask as MASK
from ..model import path as PATHS

HIT = 9.0


class PenTool:
    def __init__(self, overlay):
        self.overlay = overlay
        self.mode = "shape"          # shape | mask | motion
        self.path = None
        self.selected = set()
        self._drag = None
        self._hover = None
        self._hover_index = None
        self._editing = None          # (clip_id, mask_id) when editing an existing mask

    # ------------------------------------------------------------------ modes
    def set_mode(self, mode):
        if mode not in ("shape", "mask", "motion"):
            return False
        if mode in ("mask", "motion") and self.overlay.primary() is None:
            self.overlay.session.status.emit("Select a clip first, then draw its path")
            return False
        self.mode = mode
        self._editing = None
        self.path = None
        self.selected = set()
        self._drag = None
        self.overlay.mode = "pen"
        self.overlay.widget.setCursor(Qt.CursorShape.CrossCursor)
        self.overlay.widget.update()
        return True

    def edit_mask(self, clip_id, mask_id):
        """Load an existing mask into the Pen instead of starting another mask."""
        clip = self.overlay.session._find_clip(clip_id)
        mask = MASK.find(clip, mask_id) if clip is not None else None
        if mask is None:
            return False
        frame = self.overlay.local_frame(clip)
        self.mode = "mask"
        self._editing = (int(clip_id), int(mask_id))
        # Edit in project pixels for both coordinate spaces: project_path() applies the
        # clip transform for a layer-space mask, so a rotated/scaled clip stays intuitive.
        self.path = copy.deepcopy(MASK.project_path(clip, mask, frame,
                                                    self.overlay.project_size()))
        # Start with no point selected: clicking one selects it (Shift adds), and a
        # pre-selected set made the first click appear to do nothing.
        self.selected = set()
        self._drag = None
        self.overlay.session.active_mask = (int(clip_id), int(mask_id))
        self.overlay.session.status.emit(
            "Editing mask: drag points, Alt-drag to move the mask, T toggles smooth")
        self.overlay.mode = "pen"
        self.overlay.widget.setCursor(Qt.CursorShape.CrossCursor)
        self.overlay.widget.update()
        return True

    # -------------------------------------------------------------- coordinates
    def _project(self, point):
        if self.mode == "motion":
            width, height = self.overlay.project_size()
            return point[0] + (width - 1) / 2.0, point[1] + (height - 1) / 2.0
        return point

    def _coordinate(self, project):
        if self.mode == "motion":
            width, height = self.overlay.project_size()
            return project[0] - (width - 1) / 2.0, project[1] - (height - 1) / 2.0
        return project

    def _widget_point(self, point):
        return self.overlay.to_widget(*self._project((point["x"], point["y"])))

    def _point_at(self, pos):
        if not self.path:
            return None
        best = None
        for index, point in enumerate(self.path.get("points") or ()):
            distance = (self._widget_point(point) - pos).manhattanLength()
            if distance <= HIT * 1.6 and (best is None or distance < best[0]):
                best = (distance, index)
        return None if best is None else best[1]

    def _handle_at(self, pos):
        if not self.path:
            return None
        indexes = self.selected or set(range(len(self.path.get("points") or ())))
        best = None
        for index in indexes:
            points = self.path.get("points") or ()
            if not 0 <= index < len(points):
                continue
            point = points[index]
            for side in ("in", "out"):
                handle = point.get(side) or (0.0, 0.0)
                if abs(handle[0]) + abs(handle[1]) < 0.01:
                    continue
                target = self.overlay.to_widget(*self._project(
                    (point["x"] + handle[0], point["y"] + handle[1])))
                distance = (target - pos).manhattanLength()
                if distance <= HIT * 1.4 and (best is None or distance < best[0]):
                    best = (distance, index, side)
        return None if best is None else (best[1], best[2])

    def _segment_at(self, pos):
        """(index, point) of the curve segment under the cursor, if any."""
        if not self.path or len(self.path.get("points") or ()) < 2:
            return None
        from PyQt6.QtCore import QPointF
        best = None
        for index, (p0, c0, c1, p1) in enumerate(PATHS.segments(self.path)):
            previous = None
            for step in range(17):
                t = step / 16.0
                mt = 1.0 - t
                point = (mt ** 3 * p0[0] + 3 * mt * mt * t * c0[0]
                         + 3 * mt * t * t * c1[0] + t ** 3 * p1[0],
                         mt ** 3 * p0[1] + 3 * mt * mt * t * c0[1]
                         + 3 * mt * t * t * c1[1] + t ** 3 * p1[1])
                widget = self.overlay.to_widget(*self._project(point))
                if previous is not None:
                    distance = _segment_distance(pos, previous, widget)
                    if distance <= HIT and (best is None or distance < best[0]):
                        best = (distance, index, point)
                previous = widget
        return None if best is None else (best[1], best[2])

    # ------------------------------------------------------------------ gestures
    def press(self, event):
        project = self.overlay.to_project(event.position())
        if project is None:
            return False
        if self.path is None:
            self.path = PATHS.blank(False)
        points = self.path.get("points") or []
        if self._editing is None and len(points) >= 2:
            first = self._widget_point(points[0])
            if (first - event.position()).manhattanLength() <= HIT * 1.7:
                self.path["closed"] = True
                return self.commit()
        handle = self._handle_at(event.position())
        if handle is not None:
            index, side = handle
            self.selected = {index}
            self._drag = {"kind": "handle", "index": index, "side": side}
            return True
        index = self._point_at(event.position())
        if index is not None:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if index in self.selected:
                    self.selected.discard(index)
                else:
                    self.selected.add(index)
            elif index not in self.selected:
                # Pressing a point that is already part of a multi-selection keeps that
                # selection, so several points can be dragged together with the mouse.
                self.selected = {index}
            starts = {item: (points[item]["x"], points[item]["y"])
                      for item in self.selected}
            self._drag = {"kind": "point", "origin": project, "starts": starts}
            self.overlay.widget.update()
            return True
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            # Alt-drag anywhere on the mask moves the whole path; a plain click on a
            # segment inserts a point, so an existing mask is never replaced by a new one.
            self.selected = set(range(len(points)))
            starts = {item: (points[item]["x"], points[item]["y"])
                      for item in self.selected}
            self._drag = {"kind": "point", "origin": project, "starts": starts}
            self.overlay.widget.update()
            return True
        segment = self._segment_at(event.position())
        if segment is not None:
            segment_index, projected = segment
            value = self._coordinate(projected)
            points.insert(segment_index + 1, PATHS.point(value[0], value[1]))
            self.path = PATHS.normalise({"points": points,
                                         "closed": self.path.get("closed", False)})
            self.selected = {segment_index + 1}
            self._drag = {"kind": "point", "origin": project,
                          "starts": {segment_index + 1: (value[0], value[1])}}
            self.overlay.widget.update()
            return True
        value = self._coordinate(project)
        points.append(PATHS.point(value[0], value[1]))
        self.path = PATHS.normalise({"points": points,
                                     "closed": self.path.get("closed", False)})
        self.selected = {len(points) - 1}
        self._drag = {"kind": "new", "index": len(points) - 1,
                      "origin": project, "moved": False}
        self.overlay.widget.update()
        return True

    def move(self, event):
        project = self.overlay.to_project(event.position())
        self._hover = event.position()
        drag = self._drag
        if drag is None or project is None:
            self._hover_index = self._point_at(event.position())
            if self._handle_at(event.position()) is not None:
                cursor = Qt.CursorShape.SizeAllCursor
            elif self._hover_index is not None:
                cursor = Qt.CursorShape.PointingHandCursor
            else:
                cursor = Qt.CursorShape.CrossCursor
            self.overlay.widget.setCursor(cursor)
            self.overlay.widget.update()
            return True
        points = list(self.path.get("points") or ())
        if drag["kind"] == "new":
            index = drag["index"]
            if not 0 <= index < len(points):
                return True
            dx, dy = project[0] - drag["origin"][0], project[1] - drag["origin"][1]
            if abs(dx) + abs(dy) >= 2.0:
                points[index]["out"] = [dx, dy]
                points[index]["in"] = [-dx, -dy]
                points[index]["smooth"] = True
                drag["moved"] = True
        elif drag["kind"] == "point":
            base = drag["origin"]
            dx, dy = project[0] - base[0], project[1] - base[1]
            for index, start in drag["starts"].items():
                if 0 <= index < len(points):
                    points[index]["x"] = start[0] + dx
                    points[index]["y"] = start[1] + dy
        elif drag["kind"] == "handle":
            index = drag["index"]
            if 0 <= index < len(points):
                point = points[index]
                absolute = self._coordinate(project)
                points[index] = PATHS.set_handle(
                    {"points": points}, index, drag["side"],
                    absolute[0] - point["x"], absolute[1] - point["y"])["points"][index]
        self.path = PATHS.normalise({"points": points,
                                     "closed": self.path.get("closed", False)})
        self.overlay.widget.update()
        return True

    def release(self, event):
        drag = self._drag
        if drag is None:
            return True
        self._drag = None
        if drag.get("kind") == "new" and not drag.get("moved"):
            points = list(self.path.get("points") or ())
            if 0 <= drag["index"] < len(points):
                points[drag["index"]]["in"] = [0.0, 0.0]
                points[drag["index"]]["out"] = [0.0, 0.0]
                points[drag["index"]]["smooth"] = False
                self.path = PATHS.normalise({"points": points,
                                             "closed": self.path.get("closed", False)})
        self.overlay.widget.update()
        self._persist_edit()
        return True

    def _persist_edit(self):
        """Store one completed mask gesture as its own undo step."""
        if self._editing is None or not self.path:
            return False
        clip_id, mask_id = self._editing
        clip = self.overlay.session._find_clip(clip_id)
        if clip is None:
            return False
        frame = self.overlay.local_frame(clip)
        changed = self.overlay.session.set_clip_mask_path(
            clip_id, mask_id, self.path, frame=frame)
        if changed:
            self.sync_from_session()
        return changed

    def sync_from_session(self):
        """Reload the edited mask after undo/redo or a playhead change."""
        if self._editing is None or self._drag is not None:
            return False
        clip_id, mask_id = self._editing
        clip = self.overlay.session._find_clip(clip_id)
        mask = MASK.find(clip, mask_id) if clip is not None else None
        if mask is None:
            return False
        frame = self.overlay.local_frame(clip)
        self.path = copy.deepcopy(MASK.project_path(clip, mask, frame,
                                                    self.overlay.project_size()))
        self.selected = {index for index in self.selected
                         if index < len(self.path.get("points") or ())}
        self.overlay.widget.update()
        return True

    def commit(self):
        if not self.path:
            return True
        points = PATHS.points(self.path)
        if len(points) < 2:
            return self.cancel(finish=False)
        closed = bool(self.path.get("closed"))
        result = None
        if self._editing is not None:
            result = self._persist_edit()
        elif self.mode == "shape":
            result = self.overlay.session.add_path_shape_clip(points, closed=closed)
        elif self.mode == "mask":
            clip = self.overlay.primary()
            if clip is not None:
                result = self.overlay.session.add_clip_mask(
                    clip.id, points, closed=closed, mode="add",
                    frame=self.overlay.session.playhead)
        else:
            clip = self.overlay.primary()
            if clip is not None:
                result = self.overlay.session.set_motion_path(
                    clip.id, points, closed=closed, enabled=True)
        self.path = None
        self._editing = None
        self.selected = set()
        self._drag = None
        self.overlay.mode = "select"
        self.overlay.widget.setCursor(Qt.CursorShape.ArrowCursor)
        self.overlay.widget.update()
        if result is not None:
            self.overlay._notify()
            if callable(self.overlay.on_mode_change):
                self.overlay.on_mode_change("select")
        return True

    def cancel(self, finish=False):
        if self.overlay.mode != "pen" and self.path is None:
            return False
        if finish and self.path and len(self.path.get("points") or ()) >= 2:
            return self.commit()
        self.path = None
        self._editing = None
        self.selected = set()
        self._drag = None
        self.overlay.mode = "select"
        self.overlay.widget.setCursor(Qt.CursorShape.ArrowCursor)
        self.overlay.widget.update()
        if callable(self.overlay.on_mode_change):
            self.overlay.on_mode_change("select")
        return True

    def delete_selected(self):
        if not self.path or not self.selected:
            return False
        keep = [point for index, point in enumerate(self.path.get("points") or ())
                if index not in self.selected]
        self.path = PATHS.normalise({"points": keep,
                                     "closed": self.path.get("closed", False)})
        self.selected = set()
        self.overlay.widget.update()
        self._persist_edit()
        return True

    def select_all(self):
        if not self.path:
            return False
        self.selected = set(range(len(self.path.get("points") or ())))
        self.overlay.widget.update()
        return True

    def toggle_smooth(self):
        """Convert selected points between corner and smooth handles."""
        if not self.path or not self.selected:
            return False
        points = list(self.path.get("points") or ())
        for index in sorted(self.selected):
            if not 0 <= index < len(points):
                continue
            point = points[index]
            point["smooth"] = not bool(point.get("smooth"))
            if point["smooth"] and point.get("in") == [0, 0] and point.get("out") == [0, 0]:
                previous = points[max(0, index - 1)]
                following = points[min(len(points) - 1, index + 1)]
                dx = (following["x"] - previous["x"]) * 0.22
                dy = (following["y"] - previous["y"]) * 0.22
                point["out"] = [dx, dy]
                point["in"] = [-dx, -dy]
            elif not point["smooth"]:
                point["in"] = [0.0, 0.0]
                point["out"] = [0.0, 0.0]
        self.path = PATHS.normalise({"points": points,
                                     "closed": self.path.get("closed", False)})
        self.overlay.widget.update()
        self._persist_edit()
        return True

    # ------------------------------------------------------------------- paint
    def paint(self, painter):
        self.paint_masks(painter)
        if not self.path:
            return
        points = self.path.get("points") or ()
        if not points:
            return
        widgets = [self._widget_point(point) for point in points]
        curve = [self.overlay.to_widget(*self._project((x, y)))
                 for x, y in PATHS.sampled(self.path, steps=20)]
        if len(curve) >= 2:
            painter.save()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(tokens.COLORS["panel"]), 4.0))
            painter.drawPolyline(QPolygonF(curve))
            painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 2.0,
                                Qt.PenStyle.SolidLine if self.path.get("closed")
                                else Qt.PenStyle.DashLine))
            painter.drawPolyline(QPolygonF(curve))
            painter.restore()
        for index, point in enumerate(points):
            centre = widgets[index]
            selected = index in self.selected
            painter.save()
            painter.setPen(QPen(QColor(tokens.COLORS["accent"]), 1.2))
            painter.setBrush(QBrush(QColor(tokens.COLORS["panel"])))
            hovered = index == self._hover_index
            radius = 5.6 if hovered else (4.5 if selected else 3.5)
            painter.drawRect(QRectF(centre.x() - radius, centre.y() - radius,
                                    radius * 2.0, radius * 2.0))
            if selected:
                for side in ("in", "out"):
                    handle = point.get(side) or (0.0, 0.0)
                    if abs(handle[0]) + abs(handle[1]) < 0.01:
                        continue
                    target = self.overlay.to_widget(*self._project(
                        (point["x"] + handle[0], point["y"] + handle[1])))
                    painter.setPen(QPen(QColor(tokens.COLORS["muted"]), 1.0))
                    painter.drawLine(centre, target)
                    painter.setBrush(QBrush(QColor(tokens.COLORS["accent"])))
                    painter.drawEllipse(target, 2.6, 2.6)
            painter.restore()
        first = widgets[0]
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(tokens.COLORS["success"]), 1.2))
        painter.drawEllipse(first, HIT, HIT)
        painter.restore()

    def paint_masks(self, painter):
        clip = self.overlay.primary()
        if clip is None or not hasattr(clip, "transform_meta"):
            return
        from ..model import mask as MASK
        frame = self.overlay.local_frame(clip)
        palette = (tokens.COLORS["accent"], tokens.COLORS["warning"],
                   tokens.COLORS["success"], tokens.COLORS["red"])
        for index, mask in enumerate(MASK.at(clip, frame, self.overlay.project_size())):
            if not mask.get("show_outline", True):
                continue
            values = PATHS.sampled(mask.get("path") or {}, steps=20)
            if len(values) < 2:
                continue
            points = [self.overlay.to_widget(x, y) for x, y in values]
            colour = QColor(palette[index % len(palette)])
            painter.save()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 150), 3.2))
            painter.drawPolyline(QPolygonF(points))
            active = self._editing == (clip.id, int(mask.get("id", 0)))
            painter.setPen(QPen(colour, 2.4 if active else 1.5,
                                Qt.PenStyle.SolidLine if active else Qt.PenStyle.DashLine))
            painter.drawPolyline(QPolygonF(points))
            painter.restore()


def _segment_distance(point, start, end):
    """Distance from a point to a line segment, in widget pixels."""
    dx, dy = end.x() - start.x(), end.y() - start.y()
    if abs(dx) + abs(dy) < 1e-9:
        return (point - start).manhattanLength()
    t = ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    projection = QPointF(start.x() + dx * t, start.y() + dy * t)
    return (point - projection).manhattanLength()
