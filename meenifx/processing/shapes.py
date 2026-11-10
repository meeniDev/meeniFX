"""Draw a shape clip into a transparent BGRA layer with OpenCV.

A shape is described by numbers (kind, box, radius, sides...), never by a stored bitmap:
every frame re-draws it at the *render* resolution, so a 35 % preview and a 100 % export are
the same picture and nothing is ever upscaled. Drawing happens in a tile around the shape's
own bounds, at 2x supersampling for smooth edges, so a small shape on a 4K frame costs a
small fraction of drawing the whole frame.

The returned layer is full-frame and transparent, matching the text renderer: the
compositor's transform then moves/scales it like any other picture.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from PIL import ImageColor

from ..model import path as PATHS

_TILE_CACHE = {}
CACHE_LIMIT = 96
SUPERSAMPLE = 2.0


def _bgr(colour):
    """(b, g, r) from a "#rrggbb" string (names accepted too)."""
    text = str(colour or "#000000").strip()
    rgb = (0, 0, 0)
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 8:
            digits = digits[2:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        try:
            rgb = (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
        except ValueError:
            rgb = (0, 0, 0)
    else:
        try:
            rgb = ImageColor.getrgb(text)[:3]
        except ValueError:
            rgb = (0, 0, 0)
    return float(rgb[2]), float(rgb[1]), float(rgb[0])


def _rgba(colour):
    """(b, g, r, alpha 0..1) from #RGB, #RRGGBB or #AARRGGBB (Qt's HexArgb order)."""
    text = str(colour or "#000000").strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        alpha = 1.0
        if len(digits) == 8:
            try:
                alpha = int(digits[0:2], 16) / 255.0
            except ValueError:
                alpha = 1.0
            digits = digits[2:]
        if len(digits) == 6:
            try:
                red, green, blue = (int(digits[0:2], 16), int(digits[2:4], 16),
                                    int(digits[4:6], 16))
                return float(blue), float(green), float(red), alpha
            except ValueError:
                pass
    blue, green, red = _bgr(text)
    return blue, green, red, 1.0


def _gradient_planes(spec, width, height):
    """(b, g, r, alpha) float planes for a linear or radial multi-stop gradient.

    The alpha plane carries per-stop transparency (``#AARRGGBB``), so a gradient can fade
    out without a second opacity control; stop colours themselves are interpolated by the
    model before they reach the renderer.
    """
    stops = []
    for stop in spec.get("gradient_stops") or ():
        try:
            position = max(0.0, min(1.0, float(stop.get("position", 0.0)) / 100.0))
        except (TypeError, ValueError):
            position = 0.0
        stops.append((position, _rgba(stop.get("color"))))
    if len(stops) < 2:
        stops = [(0.0, _rgba(spec.get("fill_color"))),
                 (1.0, _rgba(spec.get("gradient_color") or spec.get("fill_color")))]
    stops.sort(key=lambda item: item[0])
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    nx, ny = xx / max(1.0, width - 1.0) - 0.5, yy / max(1.0, height - 1.0) - 0.5
    angle = math.radians(float(spec.get("gradient_angle", 0.0) or 0.0))
    scale = max(0.01, float(spec.get("gradient_scale", 100.0) or 100.0) / 100.0)
    offset = float(spec.get("gradient_offset", 0.0) or 0.0) / 100.0
    if str(spec.get("gradient_type") or "linear") == "radial":
        t = np.sqrt(nx * nx + ny * ny) * 2.0 * scale + offset
    else:
        t = (nx * math.cos(angle) + ny * math.sin(angle)) * scale + 0.5 + offset
    t = np.clip(t, 0.0, 1.0)
    planes = [np.zeros((height, width), dtype=np.float32) for _ in range(4)]
    for index in range(4):
        values = np.zeros((height, width), dtype=np.float32)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p1 <= p0:
                continue
            local = np.clip((t - p0) / (p1 - p0), 0.0, 1.0)
            values = np.where(t >= p0, c0[index] + (c1[index] - c0[index]) * local,
                              values)
        values = np.where(t >= stops[-1][0], stops[-1][1][index], values)
        planes[index] = values
    return planes


def _point_on(points, distance):
    """Point at an arc-length distance along a polyline."""
    walked = 0.0
    for first, second in zip(points, points[1:]):
        length = math.hypot(second[0] - first[0], second[1] - first[1])
        if walked + length >= distance or length < 1e-9:
            local = (distance - walked) / max(1e-9, length)
            return (first[0] + (second[0] - first[0]) * local,
                    first[1] + (second[1] - first[1]) * local)
        walked += length
    return points[-1] if points else (0.0, 0.0)


def _qt_stroke_mask(points, closed, weight, spec, width, height, dash_scale=1.0):
    """An antialiased vector stroke with real caps, joins, miter limit and dashes.

    Qt's QPainterPath stroker is already shipped with the UI, so the outline is computed by
    a real vector geometry engine instead of an OpenCV line approximation: round/bevel/
    miter joins and the miter limit are geometric, and dashes follow the path's arc length.
    QImage painting needs no QApplication and is safe off the GUI thread. Returns a 0..255
    coverage mask, or None when Qt is unavailable so the caller can fall back.
    """
    if weight <= 0 or len(points) < 2 or width < 1 or height < 1:
        return None
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

        caps = {"butt": Qt.PenCapStyle.FlatCap, "round": Qt.PenCapStyle.RoundCap,
                "square": Qt.PenCapStyle.SquareCap}
        joins = {"miter": Qt.PenJoinStyle.MiterJoin, "round": Qt.PenJoinStyle.RoundJoin,
                 "bevel": Qt.PenJoinStyle.BevelJoin}
        image = QImage(int(width), int(height), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(255, 255, 255, 255))
        pen.setWidthF(float(weight))
        pen.setCapStyle(caps.get(str(spec.get("cap") or "butt"), Qt.PenCapStyle.FlatCap))
        pen.setJoinStyle(joins.get(str(spec.get("stroke_join") or "miter"),
                                   Qt.PenJoinStyle.MiterJoin))
        try:
            pen.setMiterLimit(max(1.0, float(spec.get("stroke_miter_limit", 4.0) or 4.0)))
        except (TypeError, ValueError):
            pen.setMiterLimit(4.0)
        dash = max(0.0, float(spec.get("stroke_dash", 0.0) or 0.0)) * float(dash_scale)
        gap = max(0.0, float(spec.get("stroke_gap", 0.0) or 0.0)) * float(dash_scale)
        if dash > 0.05:
            unit = max(1e-3, float(weight))
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([max(0.01, dash / unit), max(0.01, gap / unit)])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(float(points[0][0]), float(points[0][1]))
        for point in points[1:]:
            path.lineTo(float(point[0]), float(point[1]))
        if closed:
            path.closeSubpath()
        painter.drawPath(path)
        painter.end()
        buffer = image.constBits()
        buffer.setsize(image.sizeInBytes())
        rows = np.frombuffer(buffer, np.uint8).reshape(image.height(), image.bytesPerLine())
        rows = rows[:, :image.width() * 4].reshape(image.height(), image.width(), 4)
        return np.ascontiguousarray(rows[:, :, 3])
    except Exception:        # Qt missing or a degenerate tile: the OpenCV path still works
        return None


def _stroke_polyline(mask, path, weight, closed, spec, scale=1.0):
    """Stroke a sampled path with dashes, caps and true vector join geometry."""
    if weight <= 0 or len(path) < 2:
        return
    vector = _qt_stroke_mask([(float(point[0]), float(point[1])) for point in path],
                             closed, weight, spec, mask.shape[1], mask.shape[0],
                             dash_scale=scale)
    if vector is not None:
        np.maximum(mask, vector, out=mask)
        return
    points = np.round(np.array(path)).astype(np.int32)
    weight = max(1, int(round(weight)))
    dash = float(spec.get("stroke_dash", 0.0) or 0.0) * scale
    gap = float(spec.get("stroke_gap", 0.0) or 0.0) * scale
    if dash > 0.0:
        chain = list(path) + ([path[0]] if closed else [])
        total = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                    for a, b in zip(chain, chain[1:]))
        cursor = 0.0
        while cursor < total:
            end = min(total, cursor + max(1.0, dash))
            first, second = _point_on(chain, cursor), _point_on(chain, end)
            cv2.line(mask, (int(round(first[0])), int(round(first[1]))),
                     (int(round(second[0])), int(round(second[1]))), 255, weight,
                     lineType=cv2.LINE_AA)
            if str(spec.get("cap")) == "round":
                for point in (first, second):
                    cv2.circle(mask, (int(round(point[0])), int(round(point[1]))),
                               max(1, weight // 2), 255, -1, lineType=cv2.LINE_AA)
            cursor = end + max(0.0, gap)
        return
    cv2.polylines(mask, [points], closed, 255, weight, lineType=cv2.LINE_AA)
    if str(spec.get("stroke_join")) == "round" and not closed:
        for point in points[1:-1]:
            cv2.circle(mask, (int(point[0]), int(point[1])), max(1, weight // 2),
                       255, -1, lineType=cv2.LINE_AA)
    cap = str(spec.get("cap") or "butt")
    if not closed and cap == "round":
        for point in (points[0], points[-1]):
            cv2.circle(mask, (int(point[0]), int(point[1])), max(1, weight // 2),
                       255, -1, lineType=cv2.LINE_AA)
    elif not closed and cap == "square":
        half = weight / 2.0
        for point, previous in ((points[0], points[1]), (points[-1], points[-2])):
            dx, dy = point[0] - previous[0], point[1] - previous[1]
            length = max(1e-6, math.hypot(dx, dy))
            cv2.line(mask, (int(point[0]), int(point[1])),
                     (int(point[0] + dx / length * half), int(point[1] + dy / length * half)),
                     255, weight, lineType=cv2.LINE_AA)


def _rounded_path(x, y, w, h, radius, segments=12):
    """Points around a rounded rectangle, clockwise from the top-left corner."""
    radius = max(0.0, min(float(radius), min(w, h) / 2.0))
    if radius <= 0.01:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    points = []
    corners = ((x + radius, y + radius, math.pi, 1.5 * math.pi),
               (x + w - radius, y + radius, 1.5 * math.pi, 2.0 * math.pi),
               (x + w - radius, y + h - radius, 0.0, 0.5 * math.pi),
               (x + radius, y + h - radius, 0.5 * math.pi, math.pi))
    for cx, cy, start, end in corners:
        for step in range(segments + 1):
            angle = start + (end - start) * step / segments
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _ellipse_path(x, y, w, h, steps=64):
    cx, cy = x + w / 2.0, y + h / 2.0
    return [(cx + w / 2.0 * math.cos(2 * math.pi * i / steps),
             cy + h / 2.0 * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def _polygon_path(kind, x, y, w, h, spec):
    cx, cy = x + w / 2.0, y + h / 2.0
    rx, ry = w / 2.0, h / 2.0
    if kind == "polygon":
        sides = max(3, int(round(spec.get("sides", 6))))
        return [(cx + rx * math.cos(-math.pi / 2 + 2 * math.pi * i / sides),
                 cy + ry * math.sin(-math.pi / 2 + 2 * math.pi * i / sides))
                for i in range(sides)]
    if kind == "star":
        points = max(3, int(round(spec.get("points", 5))))
        inner = max(0.05, min(0.95, float(spec.get("inner_ratio", 0.5))))
        path = []
        for index in range(points * 2):
            scale = 1.0 if index % 2 == 0 else inner
            angle = -math.pi / 2 + math.pi * index / points
            path.append((cx + rx * scale * math.cos(angle), cy + ry * scale * math.sin(angle)))
        return path
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _arrow_masks(mask, start, end, thickness, spec):
    length = max(4.0, thickness * 3.2)
    half = max(2.0, thickness * 1.35)
    for enabled, tip, direction in ((spec.get("arrow_end"), end, 1.0),
                                    (spec.get("arrow_start"), start, -1.0)):
        if not enabled:
            continue
        base_x = tip[0] - direction * length
        triangle = np.array([[tip[0], tip[1]],
                             [base_x, tip[1] - half],
                             [base_x, tip[1] + half]], dtype=np.int32)
        cv2.fillPoly(mask, [triangle], 255, lineType=cv2.LINE_AA)


def _fill_path(mask, path):
    cv2.fillPoly(mask, [np.round(np.array(path)).astype(np.int32)], 255,
                 lineType=cv2.LINE_AA)


def _stroke_path(mask, path, weight, spec=None):
    """Stroke a closed outline with the exact join style and miter limit."""
    if weight <= 0 or len(path) < 2:
        return
    vector = _qt_stroke_mask([(float(point[0]), float(point[1])) for point in path],
                             True, weight, spec or {}, mask.shape[1], mask.shape[0])
    if vector is not None:
        np.maximum(mask, vector, out=mask)
        return
    points = np.round(np.array(path)).astype(np.int32)
    cv2.polylines(mask, [points], True, 255, max(1, int(round(weight))),
                  lineType=cv2.LINE_AA)


def _aligned_stroke(fill, path, thickness, align, spec):
    """An inside/outside stroke with the same geometric joins as a centred one.

    A centred stroke twice the width already covers exactly `thickness` on each side of
    the outline, so clipping it to the fill (inside) or to the outside (outside) gives
    the aligned band without the distance-transform approximation.
    """
    vector = _qt_stroke_mask([(float(x), float(y)) for x, y in path], True,
                             thickness * 2.0, spec, fill.shape[1], fill.shape[0])
    if vector is None:
        return _ring(fill, thickness, align)
    if align == "inside":
        return np.where(fill > 127, vector, 0).astype(np.uint8)
    return np.where(fill > 127, 0, vector).astype(np.uint8)


def _ring(fill, width, align):
    """A stroke of `width` entirely inside or entirely outside the filled path.

    Clipping a centred stroke to the fill would leave only half its width, which is why
    "inside" and "outside" are built from the signed distance to the outline instead: the
    band runs 0..width inward (inside) or outward (outside), with a one-pixel soft edge
    that the supersampled downscale turns into a smooth outline.
    """
    inside = cv2.distanceTransform((fill > 127).astype(np.uint8), cv2.DIST_L2, 5)
    outside = cv2.distanceTransform((fill <= 127).astype(np.uint8), cv2.DIST_L2, 5)
    signed = inside.astype(np.float32) - outside.astype(np.float32)
    if align == "inside":
        coverage = (np.clip(signed + 0.5, 0, 1)
                    * np.clip(width - signed + 0.5, 0, 1))
    else:
        coverage = (np.clip(-signed + 0.5, 0, 1)
                    * np.clip(width + signed + 0.5, 0, 1))
    return np.clip(coverage * 255.0, 0, 255).astype(np.uint8)


def _path_tile(spec, project_w, project_h, scale):
    """A custom Bezier path, cropped to its own bounds like the primitive shapes."""
    path = spec.get("path") or {}
    values = PATHS.sampled(path, steps=20)
    if len(values) < 2:
        # A newly added Custom Path has no points yet; show a centred triangle until the
        # user draws one, instead of a clip that renders completely invisible.
        cx, cy = float(project_w) / 2.0, float(project_h) / 2.0
        half_w = max(20.0, float(spec.get("width", 320.0))) / 2.0
        half_h = max(20.0, float(spec.get("height", 180.0))) / 2.0
        values = [(cx, cy - half_h), (cx + half_w, cy + half_h), (cx - half_w, cy + half_h)]
    stroke = float(spec.get("stroke_width", 0.0)) if spec.get("stroke_enabled") else 0.0
    margin = max(stroke, 1.0) * 2.0 + 6.0
    raw_x = [value[0] for value in values]
    raw_y = [value[1] for value in values]
    left = max(0.0, min(raw_x) - margin)
    top = max(0.0, min(raw_y) - margin)
    right = min(float(project_w), max(raw_x) + margin)
    bottom = min(float(project_h), max(raw_y) + margin)
    if right - left < 1.0 or bottom - top < 1.0:
        return None, (0, 0)
    tile_w = max(1, int(round((right - left) * scale)))
    tile_h = max(1, int(round((bottom - top) * scale)))
    supersample = SUPERSAMPLE
    big_w, big_h = max(1, int(tile_w * supersample)), max(1, int(tile_h * supersample))
    sx, sy = big_w / max(1e-6, right - left), big_h / max(1e-6, bottom - top)
    points = np.array([[((x - left) * sx), ((y - top) * sy)] for x, y in values], np.float32)
    points = np.round(points).astype(np.int32)
    closed = bool(path.get("closed")) or not PATHS.points(path)
    fill = np.zeros((big_h, big_w), dtype=np.uint8)
    stroke_mask = np.zeros((big_h, big_w), dtype=np.uint8)
    if closed and len(points) >= 3:
        cv2.fillPoly(fill, [points], 255, lineType=cv2.LINE_AA)
    thickness = stroke * (sx + sy) / 2.0
    if thickness > 0:
        _stroke_polyline(stroke_mask, [(point[0], point[1]) for point in points],
                         thickness, closed, spec, scale=(sx + sy) / 2.0)
    gradient = _gradient_planes(spec, tile_w, tile_h) if spec.get("gradient_enabled") else None
    fill_layer = (_resize(fill, tile_w, tile_h).astype(np.float32) / 255.0
                  * float(spec.get("fill_opacity", 1.0)))
    if gradient is not None:
        # Per-stop alpha (#AARRGGBB) makes a gradient fade; it multiplies the fill before
        # the source-over maths so a transparent stop really lets the clip through.
        fill_layer = fill_layer * gradient[3]
    stroke_layer = (_resize(stroke_mask, tile_w, tile_h).astype(np.float32) / 255.0
                    * float(spec.get("stroke_opacity", 1.0)))
    alpha = stroke_layer + fill_layer * (1.0 - stroke_layer)
    safe = np.maximum(alpha, 1e-6)
    b_f, g_f, r_f = _bgr(spec.get("fill_color"))
    b_s, g_s, r_s = _bgr(spec.get("stroke_color"))
    tile = np.zeros((tile_h, tile_w, 4), dtype=np.uint8)
    for index, (fill_c, stroke_c) in enumerate(((b_f, b_s), (g_f, g_s), (r_f, r_s))):
        if gradient is not None:
            fill_c = gradient[index]
        tile[:, :, index] = np.clip(
            (stroke_c * stroke_layer + fill_c * fill_layer * (1.0 - stroke_layer)) / safe,
            0, 255)
    tile[:, :, 3] = np.clip(alpha * 255.0, 0, 255)
    return tile, (int(round(left * scale)), int(round(top * scale)))


def _tile(spec, project_w, project_h, scale):
    """(BGRA uint8 tile, (left, top)) for one shape at one render scale."""
    if str(spec.get("kind") or "") == "path":
        return _path_tile(spec, project_w, project_h, scale)
    width = max(1.0, float(spec.get("width", 1.0)))
    height = max(1.0, float(spec.get("height", 1.0)))
    kind = str(spec.get("kind") or "rectangle")
    stroke = float(spec.get("stroke_width", 0.0)) if spec.get("stroke_enabled") else 0.0
    if kind == "line":
        stroke = max(0.5, stroke)         # a line is its stroke: it is never invisible
    margin = max(stroke, 1.0) * 2.0 + 6.0
    x = (project_w - width) / 2.0
    y = (project_h - height) / 2.0
    # The tile never leaves the frame: a shape can be wider than the project.
    left, top = max(0.0, x - margin), max(0.0, y - margin)
    right, bottom = min(project_w, x + width + margin), min(project_h, y + height + margin)
    if right - left < 1.0 or bottom - top < 1.0:
        return None, (0, 0)

    tile_w = max(1, int(round((right - left) * scale)))
    tile_h = max(1, int(round((bottom - top) * scale)))
    s = SUPERSAMPLE
    big_w, big_h = max(1, int(tile_w * s)), max(1, int(tile_h * s))
    # Project pixels -> supersampled tile pixels.
    px = big_w / max(1e-6, right - left)
    py = big_h / max(1e-6, bottom - top)
    lx, ly = (x - left) * px, (y - top) * py
    lw, lh = width * px, height * py
    thickness = stroke * (px + py) / 2.0

    fill = np.zeros((big_h, big_w), dtype=np.uint8)
    stroke_mask = np.zeros((big_h, big_w), dtype=np.uint8)
    align = str(spec.get("align") or "center")

    if kind == "line":
        cy = ly + lh / 2.0
        start_x, end_x = lx, lx + lw
        weight = max(1.0, thickness)
        # The cap style extends the stroke itself: pre-extending square caps here would
        # lengthen the line twice. The OpenCV fallback handles its own square caps.
        start, end = (start_x, cy), (end_x, cy)
        _stroke_polyline(stroke_mask, [start, end], weight, False, spec,
                         scale=(px + py) / 2.0)
        _arrow_masks(stroke_mask, start, end, weight, spec)
    else:
        if kind == "ellipse":
            path = _ellipse_path(lx, ly, lw, lh)
        elif kind == "rounded_rectangle":
            path = _rounded_path(lx, ly, lw, lh, float(spec.get("radius", 0.0)) * px)
        else:
            path = _polygon_path(kind, lx, ly, lw, lh, spec)
        _fill_path(fill, path)
        align = align or "center"
        if align in ("inside", "outside"):
            stroke_mask = _aligned_stroke(fill, path, thickness, align, spec)
        else:
            if float(spec.get("stroke_dash", 0.0) or 0.0) > 0.0:
                _stroke_polyline(stroke_mask, path, thickness, True, spec,
                                 scale=(px + py) / 2.0)
            else:
                _stroke_path(stroke_mask, path, thickness, spec)

    # Masks arrive in 0..255; compositing needs them in 0..1 or the source-over maths
    # multiplies by a negative (1 - 255) and the antialiased edge vanishes.
    gradient = _gradient_planes(spec, tile_w, tile_h) if spec.get("gradient_enabled") else None
    fill_layer = (_resize(fill, tile_w, tile_h).astype(np.float32) / 255.0
                  * float(spec.get("fill_opacity", 1.0)))
    if gradient is not None:
        fill_layer = fill_layer * gradient[3]
    stroke_layer = (_resize(stroke_mask, tile_w, tile_h).astype(np.float32) / 255.0
                    * float(spec.get("stroke_opacity", 1.0)))
    # The stroke is painted *over* the fill (an inside stroke lies on top of opaque fill;
    # the other order multiplied it by 1 - fill and made it disappear). Source-over in
    # premultiplied space, then un-premultiplied for OpenCV's BGRA.
    alpha = stroke_layer + fill_layer * (1.0 - stroke_layer)
    b_f, g_f, r_f = _bgr(spec.get("fill_color"))
    b_s, g_s, r_s = _bgr(spec.get("stroke_color"))
    safe = np.maximum(alpha, 1e-6)
    tile = np.zeros((tile_h, tile_w, 4), dtype=np.uint8)
    for index, (fill_c, stroke_c) in enumerate(((b_f, b_s), (g_f, g_s), (r_f, r_s))):
        if spec.get("gradient_enabled"):
            fill_c = gradient[index]
        tile[:, :, index] = np.clip(
            (stroke_c * stroke_layer + fill_c * fill_layer * (1.0 - stroke_layer)) / safe,
            0, 255)
    tile[:, :, 3] = np.clip(alpha * 255.0, 0, 255)
    return tile, (int(round(left * scale)), int(round(top * scale)))


def _resize(mask, width, height):
    if mask.shape[1] == width and mask.shape[0] == height:
        return mask
    interpolation = cv2.INTER_AREA if mask.shape[1] >= width else cv2.INTER_LINEAR
    return cv2.resize(mask, (width, height), interpolation=interpolation)


def bounds(spec, project_w, project_h):
    """(x, y, width, height) of the shape's visible geometry in project pixels.

    A custom path is measured from its sampled Bezier geometry, not from the primitive
    width/height box, so the preview's selection box follows the drawn path.
    """
    if str(spec.get("kind") or "") == "path":
        path = spec.get("path") or {}
        values = PATHS.sampled(path, steps=20)
        if not values:
            cx, cy = float(project_w) / 2.0, float(project_h) / 2.0
            half_w = max(20.0, float(spec.get("width", 320.0))) / 2.0
            half_h = max(20.0, float(spec.get("height", 180.0))) / 2.0
            values = [(cx, cy - half_h), (cx + half_w, cy + half_h), (cx - half_w, cy + half_h)]
        stroke = float(spec.get("stroke_width", 0.0)) if spec.get("stroke_enabled") else 0.0
        left, top = min(v[0] for v in values), min(v[1] for v in values)
        right, bottom = max(v[0] for v in values), max(v[1] for v in values)
        margin = max(stroke, 1.0) / 2.0
        return left - margin, top - margin, right - left + margin * 2.0, bottom - top + margin * 2.0
    # The rendered layer is intentionally full-frame, so the overlay cannot use the
    # picture's dimensions as the object's box. This is the actual drawn extent: the
    # shape's centred width/height box plus the stroke, cap or arrow that reaches beyond
    # it. It is pure geometry (no rasterisation), so painting the overlay stays cheap.
    project_w, project_h = max(1.0, float(project_w)), max(1.0, float(project_h))
    width = max(1.0, float(spec.get("width", 1.0)))
    height = max(1.0, float(spec.get("height", 1.0)))
    kind = str(spec.get("kind") or "rectangle")
    stroke = float(spec.get("stroke_width", 0.0)) if spec.get("stroke_enabled") else 0.0
    x = (project_w - width) / 2.0
    y = (project_h - height) / 2.0

    if kind == "line":
        # A line is drawn along the middle of its box, not over the box's height.
        thickness = max(0.5, stroke)
        centre_y = y + height / 2.0
        left, right = x, x + width
        if spec.get("cap") in ("round", "square"):
            left -= thickness / 2.0
            right += thickness / 2.0
        half = thickness / 2.0
        if spec.get("arrow_start") or spec.get("arrow_end"):
            # Arrowheads are wider than the line but stay inside its endpoints.
            half = max(half, thickness * 1.35)
        return left, centre_y - half, right - left, half * 2.0

    if stroke > 0.0:
        align = str(spec.get("align") or "center")
        grow = stroke if align == "outside" else (0.0 if align == "inside" else stroke / 2.0)
        x -= grow
        y -= grow
        width += grow * 2.0
        height += grow * 2.0
    return x, y, width, height


def _cache_key(spec, project_w, project_h, scale):
    def clean(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 4)
        if isinstance(value, dict):
            return tuple(sorted((str(key), clean(item)) for key, item in value.items()))
        if isinstance(value, (list, tuple)):
            return tuple(clean(item) for item in value)
        return value

    return (tuple(sorted((name, clean(value)) for name, value in spec.items())),
            round(project_w, 3), round(project_h, 3), round(scale, 4))


def clear_cache():
    _TILE_CACHE.clear()


def _view(width, height, crop):
    """(x, y, w, h) of the part of a render we actually need."""
    if not crop:
        return 0, 0, width, height
    try:
        x, y, w, h = (int(round(float(value))) for value in crop)
    except (TypeError, ValueError):
        return 0, 0, width, height
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    return x, y, max(1, min(width - x, w)), max(1, min(height - y, h))


def render(spec, width, height, scale=1.0, crop=None):
    """A BGRA uint8 layer with the shape drawn on transparency.

    `crop` selects a (x, y, w, h) window of the full render. A transformed generated
    layer is rasterised at the density it will occupy on screen, but only over the
    window that can reach the output: a 1000 % shape no longer allocates a 10x frame.
    """
    width, height = max(1, int(width)), max(1, int(height))
    view_x, view_y, canvas_w, canvas_h = _view(width, height, crop)
    canvas = np.zeros((canvas_h, canvas_w, 4), dtype=np.uint8)
    if spec is None:
        return canvas
    scale = max(0.02, float(scale))
    project_w = width / scale
    project_h = height / scale
    key = _cache_key(spec, project_w, project_h, scale)
    cached = _TILE_CACHE.get(key)
    if cached is None:
        cached = _tile(spec, project_w, project_h, scale)
        if len(_TILE_CACHE) >= CACHE_LIMIT:
            # Bounded: an animated shape must not grow the cache without end.
            _TILE_CACHE.clear()
        _TILE_CACHE[key] = cached
    tile, (left, top) = cached
    if tile is None:
        return canvas
    tile_h, tile_w = tile.shape[:2]
    src_x = max(0, view_x - int(left))
    src_y = max(0, view_y - int(top))
    dst_x = max(0, int(left) - view_x)
    dst_y = max(0, int(top) - view_y)
    span_w = min(tile_w - src_x, canvas_w - dst_x)
    span_h = min(tile_h - src_y, canvas_h - dst_y)
    if span_w > 0 and span_h > 0:
        canvas[dst_y:dst_y + span_h, dst_x:dst_x + span_w] = \
            tile[src_y:src_y + span_h, src_x:src_x + span_w]
    return canvas
