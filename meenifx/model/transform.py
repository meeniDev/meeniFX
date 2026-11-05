"""Clip transforms: the animatable geometry every visual clip carries.

The model is deliberately small and explicit so the preview, the inspector, the graph
editor and the exporter all speak the same language:

* ``position`` is where the clip's *anchor* lands, in project pixels, measured from
  the frame centre (0, 0 keeps the clip exactly where it was imported);
* ``anchor`` is the pivot, an offset in project pixels from the centre of the clip's
  fitted picture;
* ``scale`` is a percentage (100 = the size the picture was fitted to);
* ``rotation`` is in degrees, clockwise;
* ``opacity`` is a percentage;
* ``flip_h`` / ``flip_v`` are plain booleans (not animated).

Unevaluated values live in ``Clip.transform`` as numbers or ``AnimatedValue`` objects,
exactly like effect parameters, so keyframes, undo and the project file need no new
storage concept. ``Clip.transform_meta`` holds the spatial Bezier handles of the
motion path.
"""

from __future__ import annotations

import math

import numpy as np

from .easing import segment_progress
from .keyframes import AnimatedValue, Keyframe

TRANSFORM_CHANNELS = ("position_x", "position_y", "scale_x", "scale_y",
                      "rotation", "anchor_x", "anchor_y", "opacity")
# Flags are transform *behaviour* toggles, saved with the clip: they change how a drag
# behaves, never the picture. "scale_from_center" and "snap_rotation" are the inspector's
# equivalents of holding Alt / Shift, so the same gestures exist without a keyboard.
TRANSFORM_FLAGS = ("flip_h", "flip_v", "uniform", "scale_from_center", "snap_rotation")
DEFAULTS = {
    "position_x": 0.0, "position_y": 0.0,
    "scale_x": 100.0, "scale_y": 100.0,
    "rotation": 0.0, "anchor_x": 0.0, "anchor_y": 0.0,
    "opacity": 100.0,
}
# (label, unit, minimum, maximum, step, decimals)
SPECS = {
    "position_x": ("Position X", "px", -20000.0, 20000.0, 1.0, 2),
    "position_y": ("Position Y", "px", -20000.0, 20000.0, 1.0, 2),
    "scale_x": ("Scale X", "%", -1000.0, 1000.0, 1.0, 2),
    "scale_y": ("Scale Y", "%", -1000.0, 1000.0, 1.0, 2),
    "rotation": ("Rotation", "\u00b0", -3600.0, 3600.0, 1.0, 2),
    "anchor_x": ("Anchor X", "px", -20000.0, 20000.0, 1.0, 2),
    "anchor_y": ("Anchor Y", "px", -20000.0, 20000.0, 1.0, 2),
    "opacity": ("Opacity", "%", 0.0, 100.0, 1.0, 1),
}


def default_transform():
    """A transform that leaves the clip exactly as imported."""
    out = dict(DEFAULTS)
    out.update({"flip_h": False, "flip_v": False, "uniform": True,
                "scale_from_center": False, "snap_rotation": True})
    return out


def ensure_transform(clip):
    """Give a clip (or a loaded dict) a complete transform without touching values."""
    current = getattr(clip, "transform", None)
    if not isinstance(current, dict):
        current = default_transform()
        clip.transform = current
    for name, value in DEFAULTS.items():
        current.setdefault(name, value)
    flags = default_transform()
    for name in TRANSFORM_FLAGS:
        current.setdefault(name, flags[name])
    if not isinstance(getattr(clip, "transform_meta", None), dict):
        clip.transform_meta = {}
    return current


def fit_rect(project_size, image_size):
    """(x, y, w, h) of the letterboxed picture at scale 1.0, matching the old fit path."""
    pw, ph = max(1, int(project_size[0])), max(1, int(project_size[1]))
    iw, ih = max(1, int(image_size[0])), max(1, int(image_size[1]))
    if (iw, ih) == (pw, ph):
        return 0.0, 0.0, float(pw), float(ph)
    ratio = min(pw / float(iw), ph / float(ih))
    nw, nh = max(1, int(iw * ratio)), max(1, int(ih * ratio))
    return float((pw - nw) // 2), float((ph - nh) // 2), float(nw), float(nh)


def _number(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


class TransformState:
    """Numbers a single frame of a clip transform evaluates to."""

    __slots__ = ("position_x", "position_y", "scale_x", "scale_y", "rotation",
                 "anchor_x", "anchor_y", "opacity", "flip_h", "flip_v")

    def __init__(self, **values):
        for name in self.__slots__:
            setattr(self, name, values.get(name, DEFAULTS.get(name, False)))

    def __repr__(self):
        return ("TransformState(" + ", ".join(f"{n}={getattr(self, n):.2f}"
                                              for n in self.__slots__) + ")")

    def is_identity(self):
        return (abs(self.position_x) < 1e-6 and abs(self.position_y) < 1e-6
                and abs(self.scale_x - 100.0) < 1e-6 and abs(self.scale_y - 100.0) < 1e-6
                and abs(self.rotation % 360.0) < 1e-6
                and abs(self.anchor_x) < 1e-6 and abs(self.anchor_y) < 1e-6
                and not self.flip_h and not self.flip_v)


def _position_keys(clip):
    """The (left, right) keyframe pairs of the position channels, or (None, None)."""
    transform = ensure_transform(clip)
    x_value = transform.get("position_x")
    y_value = transform.get("position_y")
    if not isinstance(x_value, AnimatedValue) or not isinstance(y_value, AnimatedValue):
        return None, None
    return x_value, y_value


def path_handles(clip):
    """{frame: {"in": [dx, dy], "out": [dx, dy]}} of the motion path, created empty."""
    meta = ensure_transform(clip)
    handles = clip.transform_meta.setdefault("path", {})
    if not isinstance(handles, dict):
        clip.transform_meta["path"] = handles = {}
    return handles


def _frame_key(frame):
    return str(int(frame))


def set_path_handle(clip, frame, side, offset):
    """Store one spatial handle (project pixels from the keyframe point)."""
    handles = path_handles(clip)
    entry = handles.setdefault(_frame_key(frame), {})
    entry[side] = [float(offset[0]), float(offset[1])]


def path_handle(clip, frame, side):
    entry = path_handles(clip).get(_frame_key(frame))
    if not isinstance(entry, dict):
        return None
    value = entry.get(side)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    return None


def clear_path_handles(clip, frame=None):
    handles = path_handles(clip)
    if frame is None:
        handles.clear()
    else:
        handles.pop(_frame_key(frame), None)


def _axis_at(value, frame):
    """One position axis: an animated axis evaluates, a static one just reads.

    The text animation presets key only the axis they move, so one axis can be an
    `AnimatedValue` while the other is still a plain number.
    """
    if isinstance(value, AnimatedValue) and value.keyframes:
        return _number(value.value_at(frame))
    return _number(value, 0.0)


def position_at(clip, frame):
    """Evaluated (x, y) of the clip position, curved through the motion path handles."""
    transform = ensure_transform(clip)
    x_value = transform.get("position_x")
    y_value = transform.get("position_y")
    if not isinstance(x_value, AnimatedValue) or not x_value.keyframes:
        x = _number(x_value, 0.0)
        y = _number(y_value, 0.0)
        return x, y
    x_keys = x_value.keyframes
    frame = float(frame)
    if frame <= x_keys[0].time or frame >= x_keys[-1].time:
        return _number(x_value.value_at(frame)), _axis_at(y_value, frame)
    for left, right in zip(x_keys, x_keys[1:]):
        if not (left.time <= frame <= right.time):
            continue
        if right.time == left.time:
            return _number(left.value), _axis_at(y_value, left.time)
        progress = segment_progress(left, right, (frame - left.time) / (right.time - left.time))
        k0 = (float(left.value), _axis_at(y_value, left.time))
        k1 = (float(right.value), _axis_at(y_value, right.time))
        out_handle = path_handle(clip, left.time, "out")
        in_handle = path_handle(clip, right.time, "in")
        if out_handle or in_handle:
            out_handle = out_handle or (0.0, 0.0)
            in_handle = in_handle or (0.0, 0.0)
            c0 = (k0[0] + out_handle[0], k0[1] + out_handle[1])
            c1 = (k1[0] + in_handle[0], k1[1] + in_handle[1])
            t = progress
            mt = 1.0 - t
            x = (mt ** 3 * k0[0] + 3 * mt * mt * t * c0[0] + 3 * mt * t * t * c1[0] + t ** 3 * k1[0])
            y = (mt ** 3 * k0[1] + 3 * mt * mt * t * c0[1] + 3 * mt * t * t * c1[1] + t ** 3 * k1[1])
            return x, y
        x = k0[0] + (k1[0] - k0[0]) * progress
        y = k0[1] + (k1[1] - k0[1]) * progress
        return x, y
    return _number(x_value.value_at(frame)), _axis_at(y_value, frame)


def evaluate(clip, frame):
    """The clip's transform at a clip-local frame."""
    transform = ensure_transform(clip)
    position_x, position_y = position_at(clip, frame)
    values = {}
    for name in ("scale_x", "scale_y", "rotation", "anchor_x", "anchor_y", "opacity"):
        stored = transform.get(name)
        value = stored.value_at(frame) if isinstance(stored, AnimatedValue) else stored
        values[name] = _number(value, DEFAULTS[name])
    values["position_x"], values["position_y"] = position_x, position_y
    values["flip_h"] = bool(transform.get("flip_h"))
    values["flip_v"] = bool(transform.get("flip_v"))
    return TransformState(**values)


def is_identity(clip, frame):
    """True when nothing needs warping: the clip can take the old, fastest path."""
    transform = ensure_transform(clip)
    if all(not isinstance(transform.get(name), AnimatedValue) for name in TRANSFORM_CHANNELS):
        return evaluate(clip, frame).is_identity()
    return evaluate(clip, frame).is_identity()


def opacity_at(clip, frame):
    transform = ensure_transform(clip)
    stored = transform.get("opacity")
    value = stored.value_at(frame) if isinstance(stored, AnimatedValue) else stored
    return max(0.0, min(100.0, _number(value, 100.0))) / 100.0


def matrix(state, project_size, image_size):
    """2x3 affine mapping source pixels to project pixels for a transform state."""
    pw, ph = float(project_size[0]), float(project_size[1])
    _x, _y, nw, nh = fit_rect(project_size, image_size)
    # Centre the fitted rectangle on the frame's *pixel* centre (pw-1)/2, which is the
    # same convention the anchor and position use. fit_rect() floors the letterbox
    # margins to whole pixels for the renderer, and that half-pixel bias would show
    # up as a clip that snaps half a pixel off the frame centre.
    x0 = (pw - 1.0 - nw) / 2.0
    y0 = (ph - 1.0 - nh) / 2.0
    iw, ih = max(1.0, float(image_size[0])), max(1.0, float(image_size[1]))
    # Source pixels -> the fitted rectangle.
    layer = np.array([[nw / iw, 0.0, x0],
                      [0.0, nh / ih, y0],
                      [0.0, 0.0, 1.0]], dtype=np.float64)
    # Fitted coordinates -> transformed project coordinates.
    theta = math.radians(state.rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    sx = state.scale_x / 100.0 * (-1.0 if state.flip_h else 1.0)
    sy = state.scale_y / 100.0 * (-1.0 if state.flip_v else 1.0)
    rotate_scale = np.array([[cos_t * sx, -sin_t * sy],
                             [sin_t * sx, cos_t * sy]], dtype=np.float64)
    center = np.array([(pw - 1.0) / 2.0, (ph - 1.0) / 2.0])
    anchor = np.array([state.anchor_x, state.anchor_y])
    target = center + np.array([state.position_x, state.position_y])
    offset = target - rotate_scale @ (center + anchor)
    # A zero scale would collapse the matrix; keep a hair of it so the inverse exists.
    if abs(sx) < 1e-4:
        rotate_scale[0, 0] = rotate_scale[0, 0] or 1e-4
    if abs(sy) < 1e-4:
        rotate_scale[1, 1] = rotate_scale[1, 1] or 1e-4
    transform = np.array([[rotate_scale[0, 0], rotate_scale[0, 1], offset[0]],
                          [rotate_scale[1, 0], rotate_scale[1, 1], offset[1]],
                          [0.0, 0.0, 1.0]], dtype=np.float64)
    return transform @ layer


def render_matrix(state, project_size, image_size, scale=1.0):
    """matrix() scaled for a preview render scale (single source for preview and export)."""
    full = matrix(state, project_size, image_size)
    scale = float(scale)
    if abs(scale - 1.0) < 1e-9:
        return full[:2].copy()
    zoom = np.array([[scale, 0.0, 0.0], [0.0, scale, 0.0], [0.0, 0.0, 1.0]])
    return (zoom @ full)[:2].copy()


def invert(point, state, project_size, image_size):
    """Project-space point -> source-pixel point (undoes transform and fit)."""
    full = matrix(state, project_size, image_size)
    try:
        inverse = np.linalg.inv(full)
    except np.linalg.LinAlgError:
        return None
    vector = inverse @ np.array([float(point[0]), float(point[1]), 1.0])
    return float(vector[0]), float(vector[1])


def forward(point, state, project_size, image_size):
    """Source-pixel point -> project-space point."""
    full = matrix(state, project_size, image_size)
    vector = full @ np.array([float(point[0]), float(point[1]), 1.0])
    return float(vector[0]), float(vector[1])


def content_corners(state, project_size, image_size):
    """The transformed picture rectangle as four project-space corners (TL, TR, BR, BL)."""
    x0, y0, nw, nh = fit_rect(project_size, image_size)
    iw, ih = max(1.0, float(image_size[0])), max(1.0, float(image_size[1]))
    corners = [(0.0, 0.0), (iw, 0.0), (iw, ih), (0.0, ih)]
    return [forward(corner, state, project_size, image_size) for corner in corners]


def frame_size(clip, media_entry):
    """(width, height) of a clip's own picture, for the fit rectangle."""
    from ..media.cache import entry_size
    return entry_size(media_entry) or (1920, 1080)


def reset_transform(clip):
    """Identity transform, keeping flip and uniform toggles (anchor included)."""
    ensure_transform(clip)
    current = clip.transform
    for name, value in DEFAULTS.items():
        current[name] = value
    clip.transform_meta.pop("path", None)


def center_in_frame(clip):
    """Put the anchor back on the frame centre without changing size."""
    ensure_transform(clip)["position_x"] = 0.0
    ensure_transform(clip)["position_y"] = 0.0


def scale_to(clip, percent):
    ensure_transform(clip)["scale_x"] = float(percent)
    ensure_transform(clip)["scale_y"] = float(percent)


def fit_frame(clip):
    """The imported fit: 100 % scale, centred (the default look)."""
    transform = ensure_transform(clip)
    transform["scale_x"] = transform["scale_y"] = 100.0
    transform["position_x"] = transform["position_y"] = 0.0
    transform["rotation"] = 0.0


def fill_frame(clip, project_size, image_size):
    """Scale so the picture covers the whole frame (cropping what overflows)."""
    pw, ph = project_size
    _x, _y, nw, nh = fit_rect(project_size, image_size)
    ratio = max(pw / max(1e-6, nw), ph / max(1e-6, nh)) * 100.0
    transform = ensure_transform(clip)
    transform["scale_x"] = transform["scale_y"] = float(ratio)
    transform["position_x"] = transform["position_y"] = 0.0


# 3x3 anchor presets: (key, label, fx, fy) with -1/0/1 in fitted-picture space.
ANCHOR_PRESETS = (
    ("top_left", "Top Left", -1, -1), ("top_center", "Top Center", 0, -1),
    ("top_right", "Top Right", 1, -1), ("center_left", "Center Left", -1, 0),
    ("center", "Center", 0, 0), ("center_right", "Center Right", 1, 0),
    ("bottom_left", "Bottom Left", -1, 1), ("bottom_center", "Bottom Center", 0, 1),
    ("bottom_right", "Bottom Right", 1, 1),
)
ANCHOR_LABELS = {key: label for key, label, _fx, _fy in ANCHOR_PRESETS}


def anchor_for(preset, project_size, image_size):
    """(x, y) anchor offset for a preset, in project pixels from the picture centre."""
    entry = next((row for row in ANCHOR_PRESETS if row[0] == preset), None)
    if entry is None:
        return 0.0, 0.0
    _key, _label, fx, fy = entry
    _x, _y, nw, nh = fit_rect(project_size, image_size)
    return fx * nw / 2.0, fy * nh / 2.0


def set_anchor(clip, anchor_x, anchor_y, frame=0, auto_key=False,
               project_size=(1920, 1080), image_size=(1920, 1080), keep_position=True):
    """Move the anchor, compensating the position so the picture does not jump.

    Positions are written through :func:`set_channel`, so an animated channel gets a
    keyframe at `frame` (Auto-Keyframe) and a static one shifts as a whole - the same
    rules the preview drag and the inspector use.
    """
    frame = max(0, int(frame or 0))
    # The anchor can be animated itself (an anchor drag under Auto-Keyframe), so read it
    # through evaluate(): the stored channel may be an AnimatedValue, not a number.
    state = evaluate(clip, frame)
    old_x, old_y = state.anchor_x, state.anchor_y
    new_x, new_y = float(anchor_x), float(anchor_y)
    if keep_position:
        # matrix() maps a fitted point p to target + RS*(p - centre - anchor), so shifting
        # the anchor by d moves the picture by -RS*d. Add RS*d to the position to undo it.
        theta = math.radians(state.rotation)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        sx = state.scale_x / 100.0 * (-1.0 if state.flip_h else 1.0)
        sy = state.scale_y / 100.0 * (-1.0 if state.flip_v else 1.0)
        dx, dy = new_x - old_x, new_y - old_y
        offset_x = cos_t * sx * dx - sin_t * sy * dy
        offset_y = sin_t * sx * dx + cos_t * sy * dy
        set_channel(clip, "position_x", state.position_x + offset_x, frame, auto_key)
        set_channel(clip, "position_y", state.position_y + offset_y, frame, auto_key)
    set_channel(clip, "anchor_x", new_x, frame, auto_key)
    set_channel(clip, "anchor_y", new_y, frame, auto_key)
    return new_x, new_y


def set_channel(clip, name, value, frame=None, auto_key=False):
    """Write one transform channel, honouring animation and the Auto-Keyframe rule.

    Returns True when the write created or updated a keyframe. When the channel is
    animated but there is no keyframe at `frame` and Auto-Keyframe is off, the whole
    animation shifts by the delta: the curve keeps its shape and nothing is silently
    keyframed.
    """
    transform = ensure_transform(clip)
    stored = transform.get(name)
    frame = int(frame or 0)
    if isinstance(stored, AnimatedValue):
        key = stored.key_at(frame)
        if key is not None:
            key.value = _number(value, key.value)
            return True
        if auto_key:
            stored.set_keyframe(frame, float(value))
            return True
        delta = float(value) - float(stored.value_at(frame))
        for key in stored.keyframes:
            key.value = float(key.value) + delta
        return False
    if auto_key:
        animated = AnimatedValue(base_value=stored)
        if frame > 0:
            animated.set_keyframe(0, float(stored))
        animated.set_keyframe(int(frame), float(value))
        transform[name] = animated
        if name in ("position_x", "position_y"):
            other = "position_y" if name == "position_x" else "position_x"
            if not isinstance(transform.get(other), AnimatedValue):
                other_value = _number(transform.get(other), 0.0)
                other_animated = AnimatedValue(base_value=other_value)
                if frame > 0:
                    other_animated.set_keyframe(0, other_value)
                other_animated.set_keyframe(int(frame), other_value)
                transform[other] = other_animated
        return True
    transform[name] = float(value)
    return False


def set_position(clip, x, y, frame=None, auto_key=False):
    """Move the clip; keeps X and Y keyframes on the same frames."""
    changed = set_channel(clip, "position_x", x, frame, auto_key)
    changed = set_channel(clip, "position_y", y, frame, auto_key) or changed
    return changed


def animate_channel(clip, name, frame):
    """Turn a static transform channel into an animated one with a key at `frame`."""
    transform = ensure_transform(clip)
    stored = transform.get(name)
    if isinstance(stored, AnimatedValue):
        return stored
    current = _number(stored, DEFAULTS[name])
    animated = AnimatedValue(base_value=current)
    animated.set_keyframe(int(frame), current)
    transform[name] = animated
    if name in ("position_x", "position_y"):
        other = "position_y" if name == "position_x" else "position_x"
        if not isinstance(transform.get(other), AnimatedValue):
            other_value = _number(transform.get(other), 0.0)
            other_animated = AnimatedValue(base_value=other_value)
            other_animated.set_keyframe(int(frame), other_value)
            transform[other] = other_animated
    return transform[name]


def unanimate_channel(clip, name, frame):
    """Freeze a channel at the value it has at `frame` and drop its keys."""
    transform = ensure_transform(clip)
    stored = transform.get(name)
    if isinstance(stored, AnimatedValue):
        transform[name] = float(stored.value_at(frame))


def keyframe_time(clip, name, frame):
    transform = ensure_transform(clip)
    stored = transform.get(name)
    return stored.key_at(int(frame)) is not None if isinstance(stored, AnimatedValue) else False


def key_times(clip, name):
    transform = ensure_transform(clip)
    stored = transform.get(name)
    return [key.time for key in stored.keyframes] if isinstance(stored, AnimatedValue) else []


def toggle_keyframe(clip, name, frame):
    """Diamond button: add a key at `frame`, or remove the one already there."""
    frame = int(frame)
    transform = ensure_transform(clip)
    stored = transform.get(name)
    if not isinstance(stored, AnimatedValue):
        animate_channel(clip, name, frame)
        return True
    if stored.remove_keyframe(frame):
        return False
    stored.set_keyframe(frame, float(stored.value_at(frame)))
    return True
