"""Mesh deformation for Puppet Warp and the other warps.

Puppet Warp uses moving-least-squares rigid/affine deformation (Schaefer & Warren,
"A simple deform modifier to rigidify 2D shape manipulation"): every pin contributes a
weight that falls off with distance, and the deformation is solved on a coarse grid,
then interpolated to full resolution by ``cv2.remap``. That gives smooth, continuous
bending (never a translation or a scale) and stays fast enough to drag pins live.

Pins and mesh offsets use canvas-normalized coordinates (0..1 of the frame), so a
preview at half quality deforms exactly like the full-resolution export.
"""

from __future__ import annotations

from collections import OrderedDict

import cv2
import numpy as np

from ..preview.backend import gpu

_CACHE = OrderedDict()
_CACHE_LIMIT = 12


def _distance_field(grid, points):
    dx = grid[:, 0:1] - points[None, :, 0]
    dy = grid[:, 1:2] - points[None, :, 1]
    return dx * dx + dy * dy


def _mls_map(grid, pins_source, pins_target, stiffness=1.0):
    """Apply moving-least-squares affine deformation to `grid` points.

    Every control point contributes a weight that falls off with distance; the best
    affine fit to the control points is then applied to the point. This is what makes
    the deformation bend the picture instead of translating or scaling it.
    """
    exponent = max(0.05, float(stiffness))
    distance = _distance_field(grid, pins_source)
    weights = 1.0 / (np.power(distance, exponent) + 1e-6)
    weight_sum = np.maximum(weights.sum(axis=1), 1e-9)
    p_star = (weights @ pins_source) / weight_sum[:, None]
    q_star = (weights @ pins_target) / weight_sum[:, None]
    dp = pins_source[None, :, :] - p_star[:, None, :]
    dq = pins_target[None, :, :] - q_star[:, None, :]
    w = weights[:, :, None]
    a11 = (w * dp[:, :, 0:1] * dp[:, :, 0:1]).sum(axis=1) + 1e-6
    a12 = (w * dp[:, :, 0:1] * dp[:, :, 1:2]).sum(axis=1)
    a22 = (w * dp[:, :, 1:2] * dp[:, :, 1:2]).sum(axis=1) + 1e-6
    b11 = (w * dp[:, :, 0:1] * dq[:, :, 0:1]).sum(axis=1)
    b12 = (w * dp[:, :, 0:1] * dq[:, :, 1:2]).sum(axis=1)
    b21 = (w * dp[:, :, 1:2] * dq[:, :, 0:1]).sum(axis=1)
    b22 = (w * dp[:, :, 1:2] * dq[:, :, 1:2]).sum(axis=1)
    det = a11 * a22 - a12 * a12
    det = np.where(np.abs(det) < 1e-9, 1e-9, det)
    inv11, inv12, inv21, inv22 = a22 / det, -a12 / det, -a12 / det, a11 / det
    m11 = (inv11 * b11 + inv12 * b21).reshape(-1)
    m12 = (inv11 * b12 + inv12 * b22).reshape(-1)
    m21 = (inv21 * b11 + inv22 * b21).reshape(-1)
    m22 = (inv21 * b12 + inv22 * b22).reshape(-1)
    vx = grid[:, 0] - p_star[:, 0]
    vy = grid[:, 1] - p_star[:, 1]
    out_x = vx * m11 + vy * m21 + q_star[:, 0]
    out_y = vx * m12 + vy * m22 + q_star[:, 1]
    return out_x, out_y


def _mls_inverse_map(grid, pins_source, pins_target, stiffness=1.0, iterations=8):
    """Source positions for destination `grid` points: the inverse of the pin deformation.

    `cv2.remap` needs destination -> source, while the mesh solves source -> destination.
    A few fixed-point iterations invert the smooth MLS map accurately; solving the map
    with the pins swapped instead would leave a systematic global shift.
    """
    x = grid[:, 0].copy()
    y = grid[:, 1].copy()
    target_x, target_y = grid[:, 0], grid[:, 1]
    for _ in range(max(1, int(iterations))):
        current = np.stack([x, y], axis=1)
        map_x, map_y = _mls_map(current, pins_source, pins_target, stiffness)
        step_x = target_x - map_x
        step_y = target_y - map_y
        x = x + step_x * 0.85
        y = y + step_y * 0.85
        if float(np.abs(step_x).max()) < 0.05 and float(np.abs(step_y).max()) < 0.05:
            break
    return x, y


def clamp_pins(pins, margin=0.0):
    """Pins as float arrays, ignoring malformed entries; keeps the mesh solve stable."""
    source, target = [], []
    for pin in pins or ():
        try:
            src = pin["src"]
            source.append((float(src[0]), float(src[1])))
            target.append((float(pin["x"]), float(pin["y"])))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    if not source:
        return None, None
    src = np.array(source, dtype=np.float64)
    dst = np.array(target, dtype=np.float64)
    if margin:
        src = np.clip(src, margin, 1.0 - margin)
        dst = np.clip(dst, margin, 1.0 - margin)
    return src, dst


def _anchors(x, y, width, height):
    """Fixed control points on the border of the surface that holds the picture.

    Moving-least-squares is underdetermined with one or two pins (a single point would
    collapse the whole picture onto it). Pinning that border to itself makes the
    deformation local to the pins, which is what a puppet-warp mesh has to do.
    """
    points = []
    for fx in (0.0, 0.5, 1.0):
        for fy in (0.0, 0.5, 1.0):
            if (fx, fy) == (0.5, 0.5):
                continue
            points.append((x + fx * max(0.0, width - 1.0), y + fy * max(0.0, height - 1.0)))
    return np.array(points, dtype=np.float64)


def puppet_maps(width, height, pins, stiffness=1.0, density=24, cache=None, bounds=None):
    """(map_x, map_y) int8-safe float maps warping the frame by the pins.

    `bounds` is the picture rectangle ``(x, y, w, h)`` inside the layer. The layer is the
    whole frame, so a letterboxed picture has transparent margins; deforming only the
    picture and sampling only inside it keeps those margins out of the result. Without
    that, a clamped sample on the picture edge repeats the transparent margin and smears
    a wide black band into the picture - the export then shows the seam the preview's
    lower resolution hides.
    """
    src, dst = clamp_pins(pins)
    if src is None or len(src) == 0:
        return None, None
    x, y, box_w, box_h = _bounds(bounds, width, height)
    key = (int(width), int(height), round(float(stiffness), 3), int(density),
           (round(x, 3), round(y, 3), round(box_w, 3), round(box_h, 3)),
           tuple(np.round(src, 5).ravel()), tuple(np.round(dst, 5).ravel()))
    store = cache if cache is not None else _CACHE
    if not hasattr(store, "move_to_end"):
        store = _CACHE
    found = store.get(key)
    if found is not None:
        store.move_to_end(key)
        return found
    step = max(4, int(round(min(width, height) / max(4, int(density)))))
    xs = np.arange(0, width + step, step, dtype=np.float64)
    ys = np.arange(0, height + step, step, dtype=np.float64)
    gx, gy = np.meshgrid(xs, ys)
    grid = np.stack([gx.ravel(), gy.ravel()], axis=1)
    src_px = src * np.array([width, height], dtype=np.float64)
    dst_px = dst * np.array([width, height], dtype=np.float64)
    anchors = _anchors(x, y, box_w, box_h)
    src_px = np.vstack([src_px, anchors])
    dst_px = np.vstack([dst_px, anchors])
    map_x, map_y = _mls_inverse_map(grid, src_px, dst_px, stiffness)
    map_x = map_x.reshape(gx.shape).astype(np.float32)
    map_y = map_y.reshape(gx.shape).astype(np.float32)
    map_x = cv2.resize(map_x, (width, height), interpolation=cv2.INTER_LINEAR)
    map_y = cv2.resize(map_y, (width, height), interpolation=cv2.INTER_LINEAR)
    # A deformation must bend the picture, never tear a seam into it: every sample clamps
    # to the picture border, so the edge pixel repeats instead of the transparent margin.
    np.clip(map_x, x, x + box_w - 1.0, out=map_x)
    np.clip(map_y, y, y + box_h - 1.0, out=map_y)
    if (x, y, box_w, box_h) != (0.0, 0.0, float(width), float(height)):
        # Outside the picture the layer is untouched (the letterbox margin stays as it is).
        grid_x = np.arange(width, dtype=np.float32)[None, :]
        grid_y = np.arange(height, dtype=np.float32)[:, None]
        outside = ((grid_x < x) | (grid_x > x + box_w - 1.0)
                   | (grid_y < y) | (grid_y > y + box_h - 1.0))
        map_x = np.where(outside, grid_x, map_x).astype(np.float32)
        map_y = np.where(outside, grid_y, map_y).astype(np.float32)
    result = (map_x, map_y)
    store[key] = result
    store.move_to_end(key)
    while len(store) > _CACHE_LIMIT:
        store.popitem(last=False)
    return result


def apply_puppet(layer, pins, stiffness=1.0, density=24, cache=None, bounds=None):
    """Deform a BGRA layer by puppet pins; returns a new layer.

    `bounds` is the picture rectangle inside the layer (see :func:`puppet_maps`).
    """
    if layer is None or not pins:
        return layer
    height, width = layer.shape[:2]
    map_x, map_y = puppet_maps(width, height, pins, stiffness, density, cache, bounds)
    if map_x is None:
        return layer
    return gpu.remap(layer, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


def _bounds(bounds, width, height):
    """Clamp an optional picture rectangle to the layer; the whole layer by default."""
    if not bounds:
        return 0.0, 0.0, float(width), float(height)
    try:
        x, y, box_w, box_h = (float(value) for value in bounds)
    except (TypeError, ValueError):
        return 0.0, 0.0, float(width), float(height)
    if box_w < 1 or box_h < 1:
        return 0.0, 0.0, float(width), float(height)
    x = max(0.0, min(float(width) - 1.0, x))
    y = max(0.0, min(float(height) - 1.0, y))
    box_w = max(1.0, min(float(width) - x, box_w))
    box_h = max(1.0, min(float(height) - y, box_h))
    return x, y, box_w, box_h


def mesh_displacement(width, height, rows, cols, offsets):
    """Dense interpolation of a control grid of normalized (dx, dy) offsets."""
    rows, cols = max(2, int(rows)), max(2, int(cols))
    grid = np.zeros((rows, cols, 2), dtype=np.float32)
    for row in range(rows):
        for col in range(cols):
            key = (row, col)
            if key in offsets:
                grid[row, col] = offsets[key]
    dense = cv2.resize(grid, (width, height), interpolation=cv2.INTER_CUBIC)
    return dense[:, :, 0] * width, dense[:, :, 1] * height


def apply_mesh(layer, rows, cols, offsets):
    """Grid warp: control-point offsets pushed through a smooth interpolation."""
    if layer is None or not offsets:
        return layer
    height, width = layer.shape[:2]
    dx, dy = mesh_displacement(width, height, rows, cols, offsets)
    xs, ys = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
    map_x = xs - dx
    map_y = ys - dy
    return gpu.remap(layer, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


def apply_distortion(layer, amount, center=(0.5, 0.5), radius=0.6):
    """Bulge (positive) or pinch (negative) around a normalized centre."""
    if layer is None or abs(float(amount)) < 1e-6:
        return layer
    height, width = layer.shape[:2]
    cx, cy = float(center[0]) * width, float(center[1]) * height
    radius = max(0.02, float(radius)) * max(width, height)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    dx, dy = xs - cx, ys - cy
    distance = np.sqrt(dx * dx + dy * dy)
    falloff = np.clip(1.0 - distance / radius, 0.0, 1.0)
    strength = float(amount) / 100.0
    scale = 1.0 + strength * falloff * falloff
    scale = np.where(distance > 1e-6, scale, 1.0)
    map_x = cx + dx / np.maximum(scale, 1e-3)
    map_y = cy + dy / np.maximum(scale, 1e-3)
    return cv2.remap(layer, map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                     borderValue=(0, 0, 0, 0))


def apply_corner_pin(layer, corners):
    """Perspective warp pushing the four frame corners to `corners` (normalized)."""
    if layer is None or not corners or len(corners) != 4:
        return layer
    height, width = layer.shape[:2]
    source = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    target = np.array([[float(x) * (width - 1), float(y) * (height - 1)] for x, y in corners],
                      dtype=np.float32)
    if np.allclose(source, target, atol=0.5):
        return layer
    matrix = cv2.getPerspectiveTransform(source, target)
    return gpu.warp_perspective(layer, matrix, (width, height), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


def apply_perspective(layer, tilt_x=0.0, tilt_y=0.0, zoom=1.0):
    """Pinhole keystone: rotate the layer around its X/Y axes and project it back.

    Unlike a shear this really foreshortens the far edge, which is what a "perspective"
    transform means: the four projected corners feed the same warp as Corner Pin.
    """
    tilt_x = float(tilt_x)
    tilt_y = float(tilt_y)
    zoom = max(0.05, float(zoom))
    if abs(tilt_x) < 1e-3 and abs(tilt_y) < 1e-3 and abs(zoom - 1.0) < 1e-3:
        return layer
    if layer is None:
        return layer
    ax, ay = np.radians(tilt_x), np.radians(tilt_y)
    cos_x, sin_x = np.cos(ax), np.sin(ax)
    cos_y, sin_y = np.cos(ay), np.sin(ay)
    corners = []
    for nx, ny in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)):
        y1 = ny * cos_x
        z1 = ny * sin_x
        x2 = nx * cos_y - z1 * sin_y
        z2 = nx * sin_y + z1 * cos_y
        divisor = 1.0 + z2 * 1.2
        divisor = divisor if abs(divisor) > 0.08 else (0.08 if divisor >= 0 else -0.08)
        px = (x2 / divisor) * zoom + 0.5
        py = (y1 / divisor) * zoom + 0.5
        corners.append((px, py))
    return apply_corner_pin(layer, corners)


def apply_skew(layer, skew_x, skew_y):
    """Shear a layer around its centre."""
    if layer is None or (abs(float(skew_x)) < 1e-6 and abs(float(skew_y)) < 1e-6):
        return layer
    height, width = layer.shape[:2]
    tx = np.tan(np.radians(float(skew_x)))
    ty = np.tan(np.radians(float(skew_y)))
    matrix = np.array([[1.0, tx, -tx * height / 2.0],
                       [ty, 1.0, -ty * width / 2.0]], dtype=np.float32)
    return gpu.warp_affine(layer, matrix, (width, height), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
