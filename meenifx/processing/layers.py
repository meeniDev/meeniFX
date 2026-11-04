"""Layer rendering: fit a source into the project frame, warp it, composite it.

The exporter, the preview and the thumbnails all go through these functions, so a
transform can never look right in one place and wrong in another.

Layers are BGRA at the render size (project size x preview scale). The alpha channel
carries the picture's own transparency, so rotation, scaling and corner-pin warps
composite correctly over lower tracks instead of painting black rectangles.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from ..preview.backend import gpu


def to_bgra(frame):
    """BGRA view of a frame (2-D grey, BGR or BGRA input)."""
    if frame is None:
        return None
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGRA)
    elif frame.shape[2] == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    return frame


def render_size(project_size, scale):
    pw, ph = max(1, int(project_size[0])), max(1, int(project_size[1]))
    scale = max(0.02, min(1.0, float(scale)))
    return max(1, int(round(pw * scale))), max(1, int(round(ph * scale)))


def ideal_rect(project_size, image_size):
    """(x, y, w, h) of the letterboxed picture before rounding to whole pixels.

    The same fit as :func:`meenifx.model.transform.fit_rect`, kept in float so the layer
    can round it outward: a letterbox narrower than a pixel must not become a one-pixel
    transparent line along an edge.
    """
    pw, ph = max(1, int(project_size[0])), max(1, int(project_size[1]))
    iw, ih = max(1, int(image_size[0])), max(1, int(image_size[1]))
    ratio = min(pw / float(iw), ph / float(ih))
    nw, nh = iw * ratio, ih * ratio
    return (pw - nw) / 2.0, (ph - nh) / 2.0, nw, nh


def fit_layer(source, project_size, scale=1.0):
    """(BGRA canvas at render size, (x, y, w, h) of the picture inside it).

    Uses the same letterbox math as the engine's original fit, then scales it for the
    preview, so lowering the preview quality never changes the framing. The picture's box
    is rounded outward, so it covers its ideal area exactly: a sub-pixel letterbox is
    absorbed (0.1 % stretch, invisible) instead of leaving a one-pixel transparent rim that
    a warp would smear into a black band at export resolution.
    """
    width, height = render_size(project_size, scale)
    src = to_bgra(source)
    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    if src is None or src.size == 0:
        return canvas, (0, 0, 0, 0)
    ih, iw = src.shape[:2]
    fx, fy, fw, fh = ideal_rect(project_size, (iw, ih))
    rx, ry, rw, rh = fx * scale, fy * scale, fw * scale, fh * scale
    if (iw, ih) == (width, height) and (fx, fy, fw, fh) == (0.0, 0.0, float(width), float(height)):
        canvas[:, :, :] = src
        return canvas, (0, 0, width, height)
    left = max(0, min(width - 1, int(math.floor(rx + 1e-6))))
    top = max(0, min(height - 1, int(math.floor(ry + 1e-6))))
    right = max(left + 1, min(width, int(math.ceil(rx + rw - 1e-6))))
    bottom = max(top + 1, min(height, int(math.ceil(ry + rh - 1e-6))))
    tw, th = right - left, bottom - top
    interpolation = cv2.INTER_AREA if (iw > tw or ih > th) else cv2.INTER_LINEAR
    resized = gpu.resize(src, (tw, th), interpolation=interpolation)
    canvas[top:top + th, left:left + tw] = resized
    return canvas, (float(left), float(top), float(tw), float(th))


def apply_transform(layer, rect, state, project_size, scale=1.0):
    """Warp a fitted layer by a transform state; returns a new BGRA canvas."""
    if state.is_identity():
        return layer
    height, width = layer.shape[:2]
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return layer
    # The layer is already at render scale, so its pixels are render pixels: only the
    # translation has to be scaled, the linear part is the same in both spaces.
    pw, ph = float(project_size[0]), float(project_size[1])
    import math
    theta = math.radians(state.rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    sx = state.scale_x / 100.0 * (-1.0 if state.flip_h else 1.0)
    sy = state.scale_y / 100.0 * (-1.0 if state.flip_v else 1.0)
    if abs(sx) < 1e-4:
        sx = 1e-4 if sx >= 0 else -1e-4
    if abs(sy) < 1e-4:
        sy = 1e-4 if sy >= 0 else -1e-4
    center = np.array([(pw - 1.0) / 2.0, (ph - 1.0) / 2.0])
    target = center + np.array([state.position_x, state.position_y])
    anchor = center + np.array([state.anchor_x, state.anchor_y])
    linear = np.array([[cos_t * sx, -sin_t * sy], [sin_t * sx, cos_t * sy]], dtype=np.float64)
    offset = target - linear @ anchor
    matrix = np.array([[linear[0, 0], linear[0, 1], offset[0] * scale],
                       [linear[1, 0], linear[1, 1], offset[1] * scale]], dtype=np.float64)
    return gpu.warp_affine(layer, matrix, (width, height), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


def layer_alpha(layer):
    """Float alpha (0..1) of a BGRA layer."""
    return layer[:, :, 3].astype(np.float32) / 255.0


def composite(canvas, layer, opacity=1.0):
    """Alpha-composite `layer` over a float32 BGR `canvas` in place; returns it."""
    alpha = layer_alpha(layer) * float(max(0.0, opacity))
    if not alpha.any():
        return canvas
    rgb = layer[:, :, :3].astype(np.float32)
    a = alpha[:, :, None]
    canvas *= (1.0 - a)
    canvas += rgb * a
    return canvas


def flatten(canvas, background_bgr=(0, 0, 0)):
    """Float canvas -> uint8 BGR, optionally composited over a solid background."""
    out = np.clip(canvas, 0, 255)
    return out.astype(np.uint8)


def to_bgr(layer):
    """Drop alpha: BGRA -> BGR (for effects that only understand colour)."""
    return np.ascontiguousarray(layer[:, :, :3])
