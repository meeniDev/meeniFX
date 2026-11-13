"""UI-independent timeline data model, edit operations and undo/redo stack.

All positions are integer frames. Nothing in this module imports a GUI toolkit.
"""

import copy
import itertools
import math
import os

from .animation import clamp_gain_db, db_to_gain
from .keyframes import AnimatedValue
from .presets import split_instances
from .transform import default_transform
from .transition import (Transition, normalise_audio_curve, normalise_kind,
                         normalise_video_curve, video_weight)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
FAR = 10 ** 9
# Sources and timelines rarely share a frame rate, so a linked pair can drift by up to one
# source frame after a trim. Under this many timeline frames they still count as in sync.
SYNC_TOLERANCE = 1.5


def timecode_text(frame, fps):
    """HH:MM:SS:FF for status lines; the timeline widget has the painting equivalent."""
    fps = max(1, int(round(fps)))
    seconds, frames = divmod(max(0, int(frame)), fps)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def clone_effects(effects):
    """Deep copy of a clip's effect list so copies never share dicts or params."""
    return copy.deepcopy(effects)


def split_animated(values, frame):
    """Split {name: number|AnimatedValue} at a clip-local frame (used by clip splits)."""
    left, right = {}, {}
    for name, value in (values or {}).items():
        if isinstance(value, AnimatedValue):
            left_value, right_value = value.split_at(frame)
            left[name], right[name] = left_value, right_value
        else:
            left[name] = right[name] = copy.deepcopy(value)
    return left, right


def split_text(text, frame):
    """Split a text clip's settings at a clip-local frame.

    Text keyframes are held values, so each side keeps the words that were visible while
    it plays; numeric typography parameters split like any other animated channel.
    """
    if not text:
        return None, None
    left = copy.deepcopy({key: value for key, value in text.items() if key != "keys"})
    right = copy.deepcopy(left)
    for name in list(left):
        if isinstance(left[name], AnimatedValue):
            left[name], right[name] = left[name].split_at(frame)
    keys = {int(time): str(value) for time, value in (text.get("keys") or {}).items()}
    left_keys = {time: value for time, value in keys.items() if time < frame}
    right_keys = {time - frame: value for time, value in keys.items() if time >= frame}
    left["keys"] = left_keys
    right["keys"] = right_keys
    if left_keys and not right_keys:
        right["text"] = left_keys[max(left_keys)]
    elif right_keys and not left_keys:
        left["text"] = right_keys[min(right_keys)]
    return left, right


def split_transform_meta(meta, frame):
    """Split the motion-path handles, shifting the right half's frames."""
    left, right = {}, {}
    for key, value in (meta or {}).items():
        if key != "path" or not isinstance(value, dict):
            left[key] = copy.deepcopy(value)
            right[key] = copy.deepcopy(value)
            continue
        left_path, right_path = {}, {}
        for text, handles in value.items():
            try:
                at = int(text)
            except (TypeError, ValueError):
                continue
            if at < frame:
                left_path[text] = copy.deepcopy(handles)
            elif at > frame:
                right_path[str(at - frame)] = copy.deepcopy(handles)
        left["path"], right["path"] = left_path, right_path
    return left, right


class Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)
        return slot

    def disconnect(self, slot):
        if slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class Clip:
    """One timeline clip.

    A clip plays exactly one stream. `stream` is "video" or "audio": the video stack only
    looks at video clips and the mixer only listens to audio ones, so a video file with a
    sound track becomes two clips that never play the same audio twice. Clips that came
    from one source and should move together share `link_id` (0 = unlinked).
    """

    def __init__(self, clip_id, name, source_path, track, timeline_start, duration, source_in=0, effects=None,
                 source_out=None, source_fps=None, timeline_fps=30, speed=1.0, reverse=False, loop=False,
                 freeze=False, source_offset=0, source_min=None, source_max=None, volume=1.0,
                 fade_in=0, fade_out=0, fade_in_curve=0.0, fade_out_curve=0.0,
                 link_id=0, stream=None, transform=None, transform_meta=None, text=None,
                 shape=None, gain=0.0, presets=None, blend="normal", adjustment=False,
                 composition_id="", temporal=None):
        self.id = clip_id
        self.name = name
        self.source_path = source_path
        self.track = track
        self.timeline_start = int(timeline_start)
        self.duration = int(duration)
        self.source_in = int(source_in)
        self.source_fps = float(source_fps or timeline_fps)
        self.timeline_fps = float(timeline_fps)
        self.source_out = int(source_out if source_out is not None else self.source_in + duration)
        self.source_min = int(self.source_in if source_min is None else source_min)
        self.source_max = int(self.source_out if source_max is None else source_max)
        self.speed = max(0.1, min(10.0, float(speed)))
        self.reverse = bool(reverse)
        self.loop = bool(loop)
        self.freeze = bool(freeze)
        self.source_offset = max(0.0, float(source_offset))
        self.volume = max(0.0, min(2.0, float(volume)))
        # Audio gain in dB (number or AnimatedValue), the same shape as every other
        # animatable channel. It is the *clip's* gain: a linked video/audio pair keeps
        # its picture opacity and its sound gain completely independent.
        self.gain = gain if isinstance(gain, AnimatedValue) else clamp_gain_db(gain)
        # Fades are stored per clip: length in frames plus one curve number each, so a
        # later animation can drive them without a second data shape.
        self.fade_in = max(0, int(fade_in))
        self.fade_out = max(0, int(fade_out))
        self.fade_in_curve = clamp_curve(fade_in_curve)
        self.fade_out_curve = clamp_curve(fade_out_curve)
        self.clamp_fades("in")
        # each entry: {"effect_id": str, "enabled": bool, "params": {name: value}}
        self.effects = effects if effects is not None else []
        # Persistent video/audio pairing: two clips from one file share this id.
        self.link_id = int(link_id or 0)
        self.stream = stream or ("audio" if (track is not None and track.type == "audio") else "video")
        # Transform channels (numbers or AnimatedValue) and the motion-path handles.
        # A plain dict keeps them the same shape as effect parameters, so keyframes,
        # undo snapshots and the project file need no second storage concept.
        self.transform = copy.deepcopy(transform) if transform else default_transform()
        self.transform_meta = copy.deepcopy(transform_meta) if transform_meta else {}
        # Generated text clips keep their words, styling and text keyframes here; a
        # normal media clip leaves it None. The renderer decides from this, not from the
        # file extension, so moving a text clip never needs a media entry.
        self.text = copy.deepcopy(text) if text else None
        # Generated vector shape clips keep their geometry and colours here; a media clip
        # leaves it None. Like the text dict, it is what tells the renderer to draw.
        self.shape = copy.deepcopy(shape) if shape else None
        # Animation preset instances (see meenifx.model.presets): a layer on top of the
        # transform and keyframes above, never a replacement for them.
        self.presets = copy.deepcopy(presets) if presets else []
        # How this layer is mixed with the tracks below it (see processing.blend). The
        # stored key is validated so a hand-edited project can never crash the compositor.
        from ..processing.blend import normalise_mode
        self.blend = normalise_mode(blend)
        # An adjustment layer carries no picture: its effects run on everything already
        # composited below it. It is a normal clip otherwise (trim, fades, keyframes).
        self.adjustment = bool(adjustment)
        # A nested composition clip points at a reusable definition by stable id. It
        # carries no copied media; the renderer resolves the definition every time.
        self.composition_id = str(composition_id or "")
        # Native temporal rendering overrides. None means "inherit" from the track and
        # then the composition; a concrete bool/mode is a per-clip override.
        self.temporal = {"motion_blur": None, "frame_blend": None}
        if isinstance(temporal, dict):
            if temporal.get("motion_blur") in (True, False, None):
                self.temporal["motion_blur"] = temporal.get("motion_blur")
            mode = temporal.get("frame_blend")
            if mode in (None, "off", "mix", "flow"):
                self.temporal["frame_blend"] = mode

    @property
    def is_composition(self):
        return bool(self.composition_id)

    @property
    def is_text(self):
        return isinstance(self.text, dict) and bool(self.text)

    @property
    def is_shape(self):
        return isinstance(self.shape, dict) and bool(self.shape)

    def clamp_fades(self, first="in"):
        """Keep both fades inside the clip and out of each other's way.

        `first` decides which fade keeps its length when the clip got shorter, so a
        trim of the left edge shortens the fade-in and a trim of the right edge the fade-out.
        """
        self.fade_in = max(0, min(int(self.fade_in), self.duration))
        self.fade_out = max(0, min(int(self.fade_out), self.duration))
        if self.fade_in + self.fade_out > self.duration:
            if first == "out":
                self.fade_in = max(0, self.duration - self.fade_out)
            else:
                self.fade_out = max(0, self.duration - self.fade_in)

    def gain_db_at(self, timeline_frame):
        """Evaluated gain in dB at an absolute timeline frame."""
        local = max(0, int(timeline_frame) - int(self.timeline_start))
        value = self.gain.value_at(local) if isinstance(self.gain, AnimatedValue) else self.gain
        return clamp_gain_db(0.0 if value is None else value)

    def gain_linear_at(self, timeline_frame):
        """Evaluated linear gain multiplier (0 at or below the silent floor)."""
        return db_to_gain(self.gain_db_at(timeline_frame))

    def fade_gain_at(self, timeline_frame):
        """Combined fade multiplier at a timeline frame (0..1)."""
        if self.fade_in <= 0 and self.fade_out <= 0:
            return 1.0
        frame = float(timeline_frame) + 0.5
        gain = 1.0
        if self.fade_in > 0:
            gain *= fade_gain((frame - self.timeline_start) / self.fade_in, self.fade_in_curve)
        if self.fade_out > 0:
            gain *= fade_gain((self.end - frame) / self.fade_out, self.fade_out_curve)
        return max(0.0, min(1.0, gain))

    def source_position_at(self, timeline_frame):
        """Fractional source-frame position, for frame blending and optical flow.

        ``source_frame_at`` deliberately truncates to the frame a normal playback grid
        samples. Frame Mix and Optical Flow need the sub-frame position between two
        adjacent source frames, so they read this value and interpolate there. The
        clip's trim, speed, loop and reverse rules are identical to source_frame_at.
        """
        if self.source_out <= self.source_in:
            return float(self.source_in)
        span = self.source_out - self.source_in
        if self.freeze:
            return float(min(self.source_out - 1, self.source_in))
        elapsed = max(0.0, float(timeline_frame) - self.timeline_start)
        source_delta = (self.source_offset + elapsed * self.source_fps
                        / max(1.0, self.timeline_fps) * self.speed)
        if self.loop:
            source_delta %= span
        else:
            source_delta = min(source_delta, max(0.0, span - 1.0))
        return self.source_out - 1.0 - source_delta if self.reverse else self.source_in + source_delta

    def source_frame_at(self, timeline_frame):
        """Map an absolute timeline frame to a source-frame index for preview and export."""
        return int(self.source_position_at(timeline_frame))

    @property
    def source_duration_timeline(self):
        """Timeline frames needed to play the source range once at this clip's speed."""
        if self.source_out <= self.source_in:
            return 1
        remaining = max(1.0, (self.source_out - self.source_in) - self.source_offset)
        return max(1, int(round(remaining * self.timeline_fps /
                                max(1e-9, self.source_fps * self.speed))))

    @property
    def end(self):
        return self.timeline_start + self.duration

    @property
    def kind(self):
        if self.track is not None and self.track.type == "audio":
            return "audio"
        if self.is_composition:
            return "video"
        ext = os.path.splitext(self.source_path or "")[1].lower()
        return "image" if ext in IMAGE_EXTENSIONS else "video"

    @property
    def carries_audio(self):
        """True when the mixer should play this clip's source."""
        return self.stream in ("audio", "both")

    def sync_key(self):
        """Timeline frame at which the clip's source frame 0 would sit.

        Two clips of the same media are in sync when their keys match: the same source
        moment then falls on the same timeline frame, which is the definition a linked
        video/audio pair has to satisfy. Non-integer source rates make this fractional, so
        callers compare with a small tolerance.
        """
        rate = max(1e-6, (self.source_fps / max(1e-9, self.timeline_fps)) * max(0.1, self.speed))
        base = self.source_out - 1 - self.source_offset if self.reverse else self.source_in + self.source_offset
        return self.timeline_start - base / rate

    def state(self):
        return (self.id, self.name, self.source_path, self.timeline_start, self.duration, self.source_in,
                self.source_out, self.source_fps, self.timeline_fps, self.speed, self.reverse, self.loop,
                self.freeze, self.source_offset, self.source_min, self.source_max, clone_effects(self.effects),
                self.volume, self.fade_in, self.fade_out, self.fade_in_curve, self.fade_out_curve,
                self.link_id, self.stream, copy.deepcopy(self.transform),
                copy.deepcopy(self.transform_meta), copy.deepcopy(self.text),
                copy.deepcopy(self.gain), copy.deepcopy(self.presets),
                self.blend, bool(self.adjustment), copy.deepcopy(getattr(self, "shape", None)),
                self.composition_id, copy.deepcopy(self.temporal))


# Fade shape presets. The stored truth is the number; the names are a convenience.
#  0 = linear, positive = slow start / fast end, -1 = fast start / slow end.
# The curve family g(x) = x + a*x*(1-x)*(2x-1) is monotonic for a in [-1, 1], hits
# 0 at the clip start and 1 at the end, and a = 1 is exactly smoothstep (ease in-out).
FADE_CURVES = (("Linear", 0.0), ("Fast", -0.55), ("Slow", 0.55), ("Smooth", 1.0), ("Sharp", -1.0))
FADE_CURVE_LIMIT = 1.0


def clamp_curve(curve):
    try:
        value = float(curve)
    except (TypeError, ValueError):
        return 0.0
    return max(-FADE_CURVE_LIMIT, min(FADE_CURVE_LIMIT, value))


def fade_gain(progress, curve=0.0):
    """Fade multiplier at `progress` (0 = silent/transparent, 1 = full)."""
    x = 0.0 if progress < 0 else (1.0 if progress > 1 else float(progress))
    a = clamp_curve(curve)
    return x + a * x * (1.0 - x) * (2.0 * x - 1.0)


def fade_curve_name(curve):
    """Nearest preset name, or 'Custom' once the user bends the curve by hand."""
    a = clamp_curve(curve)
    for name, value in FADE_CURVES:
        if abs(a - value) <= 0.02:
            return name
    return "Custom"


MARKER_COLORS = ("#ffa94d", "#ff6b6b", "#ffd166", "#6be0a6", "#7c9cff", "#c792ea", "#eaf2ff", "#9aa9ba")


class Marker:
    """A named, coloured point on the ruler. Frames are integer timeline positions."""

    __slots__ = ("id", "frame", "name", "color")

    def __init__(self, marker_id, frame, name="", color=MARKER_COLORS[0]):
        self.id = int(marker_id)
        self.frame = max(0, int(frame))
        self.name = str(name or "")
        self.color = color or MARKER_COLORS[0]

    def state(self):
        return (self.id, self.frame, self.name, self.color)

    def record(self):
        """JSON-friendly form used by the layout settings and presets."""
        return {"frame": self.frame, "name": self.name, "color": self.color}

    def __repr__(self):
        return f"Marker({self.id}, {self.frame}, {self.name!r}, {self.color})"


class Track:
    def __init__(self, track_id, track_type, name, height=None):
        self.id = track_id
        self.type = track_type
        self.name = name
        self.muted = False
        self.solo = False
        self.locked = False
        self.visible = True
        self.volume = 1.0
        # Native temporal defaults for every clip on this track. None inherits the
        # composition; a value overrides it until a clip supplies its own override.
        self.motion_blur = None
        self.frame_blend = None
        self.height = height or (64 if track_type == "video" else 48)
        self.clips = []

    def attrs(self):
        # height is view state, deliberately not part of undo snapshots
        return (self.name, self.muted, self.solo, self.locked, self.visible, self.volume,
                self.motion_blur, self.frame_blend)

    def sorted_clips(self):
        return sorted(self.clips, key=lambda c: c.timeline_start)


class Timeline:
    def __init__(self, fps=30):
        self.fps = int(fps)
        self.tracks = []  # display order, top to bottom
        self.markers = []  # Marker objects, always sorted by frame
        # The loop region is the only range in the timeline: frames, plus its toggle.
        self.loop_in = None
        self.loop_out = None
        self.loop_enabled = False
        # Without a loop region, playback wraps at the end of the last clip unless
        # this is off (then it stops there).
        self.loop_at_end = True
        # Native temporal rendering (not an effect): the master motion-blur switch and
        # the frame-blending mode every track/clip inherits unless it overrides them.
        self.motion_blur = False
        self.motion_blur_shutter = 180.0
        self.motion_blur_phase = 0.0
        self.motion_blur_samples = 8
        self.frame_blending = "off"
        self.min_duration = self.fps * 30
        self._clip_ids = itertools.count(1)
        self._track_ids = itertools.count(1)
        self._marker_ids = itertools.count(1)
        self._link_ids = itertools.count(1)
        self._counters = {"video": 0, "audio": 0}
        # Transition records for overlapping pairs on one track. They hold settings only:
        # the overlap window is derived from the live clips, so moving or trimming a clip
        # can never leave a stale duration behind.
        self.transitions = []

    def adopt(self, other):
        """Take over another timeline's whole state in place, so every holder of this object stays valid."""
        self.__dict__.update(other.__dict__)

    # ---- markers ---------------------------------------------------------
    def marker_frames(self):
        return [m.frame for m in self.markers]

    def marker_at(self, frame, exclude=()):
        """Marker sitting exactly on frame (markers are one per frame)."""
        frame = int(frame)
        for marker in self.markers:
            if marker.frame == frame and marker.id not in exclude:
                return marker
        return None

    def marker_by_id(self, marker_id):
        return next((m for m in self.markers if m.id == marker_id), None)

    def markers_in_range(self, start, end):
        lo, hi = sorted((int(start), int(end)))
        return [m for m in self.markers if lo <= m.frame <= hi]

    def next_marker(self, frame, direction=1, exclude=()):
        """Nearest marker strictly after (direction 1) or before (-1) frame."""
        frame = int(frame)
        candidates = [m.frame for m in self.markers
                      if m.id not in exclude and ((m.frame > frame) if direction > 0 else (m.frame < frame))]
        if not candidates:
            return None
        return min(candidates) if direction > 0 else max(candidates)

    def add_marker(self, frame, name="", color=None):
        """Create a marker, or return the one already on that frame instead of stacking."""
        frame = max(0, int(frame))
        existing = self.marker_at(frame)
        if existing is not None:
            return existing
        marker = Marker(next(self._marker_ids), frame, name, color or MARKER_COLORS[0])
        self.markers.append(marker)
        self.markers.sort(key=lambda m: (m.frame, m.id))
        return marker

    def remove_marker(self, marker_id):
        marker = self.marker_by_id(marker_id)
        if marker is None:
            return False
        self.markers.remove(marker)
        return True

    def set_marker_frame(self, marker_id, frame):
        """Move a marker; landing on another marker merges them (the other one wins)."""
        marker = self.marker_by_id(marker_id)
        if marker is None:
            return None
        marker.frame = max(0, int(frame))
        clash = self.marker_at(marker.frame, exclude=(marker_id,))
        if clash is not None:
            self.markers.remove(marker)
            return clash
        self.markers.sort(key=lambda m: (m.frame, m.id))
        return marker

    def set_marker_name(self, marker_id, name):
        marker = self.marker_by_id(marker_id)
        if marker is not None:
            marker.name = str(name or "")
        return marker

    def set_marker_color(self, marker_id, color):
        marker = self.marker_by_id(marker_id)
        if marker is not None and color:
            marker.color = str(color)
        return marker

    def clear_markers(self):
        self.markers = []

    def marker_records(self):
        return [m.record() for m in self.markers]

    def set_marker_records(self, records):
        """Load markers from JSON-friendly dicts (layout settings or a preset)."""
        self.markers = []
        used_frames = set()
        for record in records or ():
            try:
                frame = max(0, int(record.get("frame", 0)))
            except (AttributeError, TypeError, ValueError):
                continue
            if frame in used_frames:
                continue
            used_frames.add(frame)
            marker = Marker(next(self._marker_ids), frame, record.get("name", ""),
                            record.get("color") or MARKER_COLORS[0])
            self.markers.append(marker)
        self.markers.sort(key=lambda m: (m.frame, m.id))
        return self.markers

    # ---- structure -------------------------------------------------------
    @property
    def duration(self):
        return max((c.end for t in self.tracks for c in t.clips), default=0)

    def project_range(self):
        """Whole project as a half-open (start, end) frame range."""
        return 0, max(0, self.duration)

    def loop_range(self):
        """(start, end) of the loop region, or None when the region is empty."""
        if self.loop_in is None or self.loop_out is None:
            return None
        lo, hi = int(min(self.loop_in, self.loop_out)), int(max(self.loop_in, self.loop_out))
        return (lo, hi) if hi > lo else None

    def play_range(self):
        """Range playback runs over: the enabled loop region, else the whole project."""
        region = self.loop_range()
        if self.loop_enabled and region is not None:
            return region
        return (0, max(1, self.duration))

    def add_track(self, track_type, name=None, index=None):
        self._counters[track_type] += 1
        track = Track(next(self._track_ids), track_type, name or f"{track_type.capitalize()} {self._counters[track_type]}")
        if index is None:
            index = 0 if track_type == "video" else len(self.tracks)  # newest video track is the top layer
        self.tracks.insert(max(0, min(index, len(self.tracks))), track)
        return track

    def remove_track(self, track):
        if track in self.tracks:
            self.tracks.remove(track)
        self.prune_transitions()

    def lane_index(self, track):
        """Index of a track among the tracks of its own type (0 = leftmost/nearest in header list)."""
        lanes = self.tracks_of_type(track.type)
        return lanes.index(track) if track in lanes else -1

    def tracks_of_type(self, track_type):
        return [t for t in self.tracks if t.type == track_type]

    def new_clip(self, name, source_path, track, start, duration, source_in=0, effects=None, source_out=None,
                 source_fps=None, speed=1.0, reverse=False, loop=False, freeze=False, source_offset=0,
                 source_min=None, source_max=None, volume=1.0, fade_in=0, fade_out=0,
                 fade_in_curve=0.0, fade_out_curve=0.0, link_id=0, stream=None,
                 transform=None, transform_meta=None, text=None, gain=0.0, presets=None,
                 blend="normal", adjustment=False, shape=None, composition_id=""):
        clip = Clip(next(self._clip_ids), name, source_path, track, start, duration, source_in, effects,
                    source_out, source_fps, self.fps, speed, reverse, loop, freeze,
                    source_offset, source_min, source_max, volume, fade_in, fade_out,
                    fade_in_curve, fade_out_curve, link_id, stream, transform, transform_meta, text,
                    shape, gain, presets, blend, adjustment, composition_id)
        track.clips.append(clip)
        return clip

    # ---- video/audio links -----------------------------------------------
    def new_link_id(self):
        return next(self._link_ids)

    def link_group(self, link_id):
        """Every clip that shares a link id, in timeline order."""
        if not link_id:
            return []
        return [c for c in self.all_clips() if c.link_id == link_id]

    def linked_pairs(self, clip):
        """The other members of `clip`'s link group (empty when it is unlinked)."""
        if clip is None or not clip.link_id:
            return []
        return [c for c in self.link_group(clip.link_id) if c.id != clip.id]

    def link_anchor(self, link_id):
        """Member the group is measured against: the earliest start, then the lowest id."""
        group = self.link_group(link_id)
        return min(group, key=lambda c: (c.timeline_start, c.id)) if group else None

    def link_offset(self, clip):
        """Frames between a clip and its group anchor (0 when in perfect sync)."""
        anchor = self.link_anchor(clip.link_id) if clip is not None and clip.link_id else None
        if anchor is None or anchor is clip:
            return 0.0
        return clip.sync_key() - anchor.sync_key()

    def link_is_synced(self, clip, tolerance=SYNC_TOLERANCE):
        """True when every member plays the same source moment on the same timeline frame."""
        group = self.link_group(clip.link_id) if clip is not None else []
        if len(group) < 2:
            return True
        keys = [c.sync_key() for c in group]
        return max(keys) - min(keys) <= tolerance

    def assign_link(self, ids, link_id=None):
        """Make `ids` one group (existing ids are merged into their groups' new owner)."""
        clips = [c for c in (self.find_clip(i) for i in ids) if c is not None]
        if len(clips) < 2:
            return 0
        link_id = link_id or self.new_link_id()
        for clip in clips:
            clip.link_id = link_id
        return link_id

    def clear_link(self, ids):
        """Drop the relationship for `ids` (and for members left alone in the same group)."""
        for cid in ids:
            clip = self.find_clip(cid)
            if clip is None or not clip.link_id:
                continue
            clip.link_id = 0
        return True


    # ---- overlap transitions ---------------------------------------------
    def transition_between(self, first_id, second_id):
        """The record for a pair of clips, in either direction (None when there is none)."""
        pair = {int(first_id), int(second_id)}
        for transition in getattr(self, "transitions", []) or ():
            if {transition.out_id, transition.in_id} == pair:
                return transition
        return None

    def ordered_pair(self, first, second):
        """(outgoing, incoming) by timeline order, with the clip id breaking ties."""
        if (first.timeline_start, first.id) <= (second.timeline_start, second.id):
            return first, second
        return second, first

    def transition_window(self, transition):
        """(start, end) frames of the pair's actual overlap, or None when it is gone."""
        if transition is None:
            return None
        outgoing = self.find_clip(transition.out_id)
        incoming = self.find_clip(transition.in_id)
        if outgoing is None or incoming is None or outgoing is incoming:
            return None
        start = max(outgoing.timeline_start, incoming.timeline_start)
        end = min(outgoing.end, incoming.end)
        return (start, end) if end > start else None

    def transition_compatible(self, first, second):
        """True when a crossfade makes sense for this pair of clips on the same track."""
        if first is None or second is None or first is second:
            return False
        if first.track is None or second.track is None or first.track is not second.track:
            return False
        if first.track.type == "video":
            # Adjustment layers have no picture of their own; a crossfade would have
            # nothing to blend. Nested compositions, text, shapes, images and video do.
            return not getattr(first, "adjustment", False) and not getattr(second, "adjustment", False)
        return first.track.type == "audio"

    def add_transition(self, first_id, second_id, kind="crossfade",
                       curve=None, audio_curve=None):
        """Create (or return the existing) transition for a clip pair.

        An existing record keeps its curve: dropping clips into an overlap again must not
        silently reset a curve the user chose.
        """
        first = self.find_clip(first_id)
        second = self.find_clip(second_id)
        if not self.transition_compatible(first, second):
            return None
        outgoing, incoming = self.ordered_pair(first, second)
        existing = self.transition_between(outgoing.id, incoming.id)
        if existing is not None:
            return existing
        transition = Transition(outgoing.id, incoming.id, normalise_kind(kind),
                                normalise_video_curve(curve),
                                normalise_audio_curve(audio_curve))
        self.transitions.append(transition)
        return transition

    def remove_transition(self, first_id, second_id):
        """Drop one record (a pair may legitimately overlap without a transition)."""
        transition = self.transition_between(first_id, second_id)
        if transition is None:
            return False
        self.transitions.remove(transition)
        return True

    def prune_transitions(self):
        """Forget records whose clips, track, order or overlap no longer exist.

        Called after every structural edit. This is what makes removing an overlap (by
        moving, trimming, deleting, splitting or rippling a clip) remove its transition
        instead of leaving a reference to something that is no longer there.
        """
        kept = []
        for transition in getattr(self, "transitions", []) or ():
            outgoing = self.find_clip(transition.out_id)
            incoming = self.find_clip(transition.in_id)
            if not self.transition_compatible(outgoing, incoming):
                continue
            first, second = self.ordered_pair(outgoing, incoming)
            if first.id != transition.out_id or second.id != transition.in_id:
                transition.out_id, transition.in_id = first.id, second.id
            if self.transition_window(transition) is None:
                continue
            kept.append(transition)
        changed = len(kept) != len(getattr(self, "transitions", []) or ())
        self.transitions = kept
        return changed

    def transition_at(self, track, frame_no):
        """The transition whose overlap covers `frame_no` on `track`, or None."""
        for transition in getattr(self, "transitions", []) or ():
            outgoing = self.find_clip(transition.out_id)
            if outgoing is None or outgoing.track is not track:
                continue
            window = self.transition_window(transition)
            if window is not None and window[0] <= frame_no < window[1]:
                return transition
        return None

    def video_layers_at(self, track, frame_no):
        """[(clip, weight, group)] painted on one video track at `frame_no`.

        A single clip is the ordinary case. When two clips overlap and carry a
        transition record, both are returned with complementary weights and a shared
        group key, so the compositor can blend two fully rendered pictures instead of
        dropping one of them. Overlaps without a record keep the historical behaviour
        (the earliest-starting clip wins) so old projects render exactly as before.
        """
        active = [clip for clip in track.sorted_clips()
                  if (clip.timeline_start <= frame_no < clip.end
                      and (clip.source_path or clip.is_text or clip.is_shape
                           or clip.is_composition or getattr(clip, "adjustment", False)))]
        if not active:
            return []
        if len(active) == 1:
            return [(active[0], 1.0, None)]
        active_ids = {clip.id for clip in active}
        for transition in getattr(self, "transitions", []) or ():
            if transition.out_id not in active_ids or transition.in_id not in active_ids:
                continue
            window = self.transition_window(transition)
            if window is None or not (window[0] <= frame_no < window[1]):
                continue
            if len(active) != 2:
                # Three or more clips over one frame is ambiguous in this version; fall
                # back to the old single-clip result rather than inventing a mix.
                break
            incoming = self.find_clip(transition.in_id)
            outgoing = self.find_clip(transition.out_id)
            progress = (frame_no - window[0]) / float(window[1] - window[0])
            weight = video_weight(progress, transition.curve)
            group = (track.id, transition.out_id, transition.in_id)
            return [(outgoing, 1.0 - weight, group), (incoming, weight, group)]
        return [(active[0], 1.0, None)]

    def audio_partners(self, clip):
        """Audio clips linked to a video clip (link group first, then a legacy pair)."""
        if clip is None or clip.track is None or clip.track.type != "video":
            return []
        if clip.link_id:
            return [other for other in self.link_group(clip.link_id)
                    if other.id != clip.id and other.track is not None
                    and other.track.type == "audio"]
        return [other for other in self.all_clips()
                if other.id != clip.id and other.track is not None
                and other.track.type == "audio" and other.source_path == clip.source_path
                and other.timeline_start == clip.timeline_start]

    def audio_transition_envelopes(self, clip):
        """[(start_seconds, end_seconds, curve, role)] for one audio clip.

        These ride on the clip's mixer segment, so volume automation, fades and effects
        are applied exactly as before and the crossfade multiplies on top of them.
        """
        envelopes = []
        if clip is None or clip.track is None or clip.track.type != "audio":
            return envelopes
        fps = float(self.fps or 30)
        seen = set()

        def add(window, transition, role):
            start = max(window[0], clip.timeline_start)
            end = min(window[1], clip.end)
            if end <= start or (role, start, end) in seen:
                return
            seen.add((role, start, end))
            envelopes.append((start / fps, end / fps, transition.audio_curve, role))

        for transition in getattr(self, "transitions", []) or ():
            if clip.id not in (transition.out_id, transition.in_id):
                continue
            window = self.transition_window(transition)
            if window is not None:
                add(window, transition, "out" if clip.id == transition.out_id else "in")
        # One-sided audio: the picture pair has only one linked sound. There is no audio
        # pair record to read, so follow the picture transition and fade that lone clip;
        # otherwise its full-level sound would cut abruptly when the clip ends.
        for transition in getattr(self, "transitions", []) or ():
            if clip.id in (transition.out_id, transition.in_id):
                continue
            outgoing = self.find_clip(transition.out_id)
            incoming = self.find_clip(transition.in_id)
            if (outgoing is None or incoming is None or outgoing.track is None
                    or outgoing.track.type != "video"):
                continue
            out_audio = self.audio_partners(outgoing)
            in_audio = self.audio_partners(incoming)
            if (bool(out_audio) == bool(in_audio)) or clip not in (*out_audio, *in_audio):
                continue
            window = self.transition_window(transition)
            if window is not None:
                add(window, transition, "out" if clip in out_audio else "in")
        return envelopes

    def create_auto_transitions(self, clip_ids=None):
        """Record transitions for the overlaps a drop just produced.

        `clip_ids` limits the pairs to the clips that moved (None means every overlap),
        so unrelated clips that happened to overlap already are not reinterpreted. Audio
        partners of the pair get their own record over their actual overlap, which is
        what keeps a linked video/audio pair's sound in step with the picture.
        """
        moved = {int(cid) for cid in (clip_ids or ())}
        self.prune_transitions()
        created = []
        for track in self.tracks:
            if track.locked:
                continue
            clips = track.sorted_clips()
            for index, outgoing in enumerate(clips):
                for incoming in clips[index + 1:]:
                    if incoming.timeline_start >= outgoing.end:
                        continue
                    if moved and outgoing.id not in moved and incoming.id not in moved:
                        continue
                    if not self.transition_compatible(outgoing, incoming):
                        continue
                    start = max(outgoing.timeline_start, incoming.timeline_start)
                    end = min(outgoing.end, incoming.end)
                    if end <= start:
                        continue
                    if any(other is not outgoing and other is not incoming
                           and other.timeline_start < end and other.end > start
                           for other in clips):
                        # A three-way overlap has no single unambiguous transition.
                        continue
                    transition = self.add_transition(outgoing.id, incoming.id)
                    if transition is not None:
                        created.append(transition)
        for transition in list(created):
            outgoing = self.find_clip(transition.out_id)
            incoming = self.find_clip(transition.in_id)
            if outgoing is None or incoming is None or outgoing.track.type != "video":
                continue
            for audio_out in self.audio_partners(outgoing):
                for audio_in in self.audio_partners(incoming):
                    if audio_out.track is not audio_in.track or audio_out.track.locked:
                        continue
                    start = max(audio_out.timeline_start, audio_in.timeline_start)
                    end = min(audio_out.end, audio_in.end)
                    if end <= start:
                        continue
                    audio_transition = self.add_transition(audio_out.id, audio_in.id)
                    if audio_transition is not None and audio_transition not in created:
                        created.append(audio_transition)
        return created

    def find_clip(self, clip_id):
        for track in self.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return clip
        return None

    def all_clips(self):
        return [c for t in self.tracks for c in t.clips]

    def set_fade(self, clip_id, edge, frames, curve=None):
        """Set one fade in frames (and optionally its curve); returns the applied length.

        A fade can never be longer than the clip, and fade-in + fade-out can never
        overlap: the second fade is capped by whatever the first one already owns.
        """
        clip = self.find_clip(clip_id)
        if clip is None:
            return 0
        other = clip.fade_out if edge == "in" else clip.fade_in
        limit = max(0, clip.duration - other)
        frames = max(0, min(int(frames), limit))
        if edge == "in":
            clip.fade_in = frames
            if curve is not None:
                clip.fade_in_curve = clamp_curve(curve)
        else:
            clip.fade_out = frames
            if curve is not None:
                clip.fade_out_curve = clamp_curve(curve)
        return frames

    def set_fade_curve(self, clip_id, edge, curve):
        clip = self.find_clip(clip_id)
        if clip is None:
            return None
        value = clamp_curve(curve)
        if edge == "in":
            clip.fade_in_curve = value
        else:
            clip.fade_out_curve = value
        return value

    def first_free_start(self, track, start, duration):
        start = max(0, int(start))
        for clip in track.sorted_clips():
            if clip.end <= start:
                continue
            if clip.timeline_start >= start + duration:
                break
            start = clip.end
        return start

    # ---- snapshots -------------------------------------------------------
    def snapshot(self):
        tracks = tuple((t, t.attrs(), tuple(c.state() for c in t.clips)) for t in self.tracks)
        # A composition library is part of the edit state too: creating a pre-composition
        # must undo as one action, including the definition it created. Composition
        # timelines deliberately have no library back-reference, so this cannot recurse.
        comps = self.compositions.snapshot() if getattr(self, "compositions", None) else None
        transitions = tuple(t.state() for t in getattr(self, "transitions", []) or ())
        return (tracks, tuple(m.state() for m in self.markers), self.loop_in,
                self.loop_out, comps, transitions,
                (self.motion_blur, self.motion_blur_shutter, self.motion_blur_phase,
                 self.motion_blur_samples, self.frame_blending))

    def restore(self, snap):
        tracks, markers, loop_in, loop_out = snap[:4]
        if len(snap) > 4 and getattr(self, "compositions", None) is not None:
            self.compositions.restore(snap[4])
        if len(snap) > 5:
            self.transitions = [Transition(*state) for state in snap[5]]
        else:
            # A pre-transition snapshot has no records; clearing is what an undo of the
            # first-ever transition must restore.
            self.transitions = []
        if len(snap) > 6:
            (self.motion_blur, self.motion_blur_shutter, self.motion_blur_phase,
             self.motion_blur_samples, self.frame_blending) = snap[6]
        self.tracks = []
        for track, attrs, clips in tracks:
            (track.name, track.muted, track.solo, track.locked, track.visible,
             track.volume) = attrs[:6]
            if len(attrs) > 6:
                track.motion_blur, track.frame_blend = attrs[6], attrs[7]
            track.clips = []
            for (cid, name, path, start, dur, src_in, src_out, src_fps, timeline_fps,
                 speed, reverse, loop, freeze, source_offset, source_min, source_max, effects, volume,
                 fade_in, fade_out, fade_in_curve, fade_out_curve, link_id, stream,
                 transform, transform_meta, text, gain, presets, blend, adjustment,
                 shape, composition_id, *rest) in clips:
                temporal = rest[0] if rest else None
                # Everything from `shape` on goes by name: a generated shape sits between
                # text and gain in the constructor and a positional call would shift it.
                track.clips.append(Clip(cid, name, path, track, start, dur, src_in, clone_effects(effects),
                                        src_out, src_fps, timeline_fps, speed, reverse, loop, freeze,
                                        source_offset, source_min, source_max, volume,
                                        fade_in, fade_out, fade_in_curve, fade_out_curve,
                                        link_id, stream, transform, transform_meta, text,
                                        shape=shape, gain=gain, presets=presets, blend=blend,
                                        adjustment=adjustment, composition_id=composition_id,
                                        temporal=temporal))
            self.tracks.append(track)
        self.markers = [Marker(mid, frame, name, color) for mid, frame, name, color in markers]
        self.markers.sort(key=lambda m: (m.frame, m.id))
        self._marker_ids = itertools.count(max((m.id for m in self.markers), default=0) + 1)
        # Links live on the clips, so only the id counter has to move past what is left.
        self._link_ids = itertools.count(max((c.link_id for c in self.all_clips()), default=0) + 1)
        self.loop_in = loop_in
        self.loop_out = loop_out

    # ---- edit operations (all operate on an absolute snapshot) -----------
    @staticmethod
    def _split_animated_params(effects, frame):
        """Split clip-local animated values so each new clip keeps its own curve."""
        left_effects, right_effects = clone_effects(effects), clone_effects(effects)
        for left_entry, right_entry in zip(left_effects, right_effects):
            for name, value in left_entry["params"].items():
                if isinstance(value, AnimatedValue):
                    left_value, right_value = value.split_at(frame)
                    left_entry["params"][name] = left_value
                    right_entry["params"][name] = right_value
            # Puppet pins are animated too; a split must not hand both halves one curve.
            left_pins, right_pins = left_entry.get("pins") or [], right_entry.get("pins") or []
            for left_pin, right_pin in zip(left_pins, right_pins):
                for axis in ("x", "y"):
                    stored = left_pin.get(axis)
                    if isinstance(stored, AnimatedValue):
                        left_value, right_value = stored.split_at(frame)
                        left_pin[axis], right_pin[axis] = left_value, right_value
        return left_effects, right_effects

    def _target_track(self, track, offset):
        same = self.tracks_of_type(track.type)
        index = same.index(track) + offset
        if 0 <= index < len(same):
            return same[index]
        return None

    @staticmethod
    def _intersect(a, b):
        out = []
        for lo1, hi1 in a:
            for lo2, hi2 in b:
                lo, hi = max(lo1, lo2), min(hi1, hi2)
                if lo <= hi:
                    out.append((lo, hi))
        return sorted(out)

    def _valid_deltas(self, clip, target, others):
        """Intervals of delta for which `clip` fits in a gap of `others` on `target`."""
        spans = []
        prev_end = 0
        for other in others:
            lo = prev_end - clip.timeline_start
            hi = other.timeline_start - clip.duration - clip.timeline_start
            if lo <= hi:
                spans.append((lo, hi))
            prev_end = max(prev_end, other.end)
        spans.append((prev_end - clip.timeline_start, FAR))
        return spans

    def apply_move(self, snap, ids, delta, track_delta, mode):
        """Restore `snap`, then move clips `ids` by delta frames / track_delta lanes.

        mode "snap" keeps clips out of occupied space; mode "push" pushes neighbours right.
        Returns the (delta, track_delta) actually applied.
        """
        self.restore(snap)
        moving = [c for c in (self.find_clip(i) for i in ids) if c is not None]
        if not moving:
            return 0, 0
        moving_ids = {c.id for c in moving}

        targets = {}
        for clip in moving:
            target = self._target_track(clip.track, track_delta) if track_delta else clip.track
            if target is None or target.locked:
                track_delta = 0
                targets = {}
                break
            targets[clip.id] = target
        if not targets:
            targets = {c.id: c.track for c in moving}

        def others_on(track):
            return sorted((c for c in track.clips if c.id not in moving_ids), key=lambda c: c.timeline_start)

        min_start = min(c.timeline_start for c in moving)
        delta = max(delta, -min_start)

        if mode == "snap":
            for attempt in (0, 1):
                valid = [(-FAR, FAR)]
                for clip in moving:
                    valid = self._intersect(valid, self._valid_deltas(clip, targets[clip.id], others_on(targets[clip.id])))
                if valid:
                    break
                targets = {c.id: c.track for c in moving}
                track_delta = 0
            if not valid:
                valid = [(0, 0)]
            delta = min(
                (min(max(delta, lo), hi) for lo, hi in valid),
                key=lambda d: abs(d - delta),
            )

        for clip in moving:
            clip.timeline_start += delta
        for clip in moving:
            target = targets[clip.id]
            if target is not clip.track:
                clip.track.clips.remove(clip)
                target.clips.append(clip)
                clip.track = target

        if mode == "push":
            for track in {t for t in targets.values()}:
                self._push_neighbours(track, moving_ids)
        self.prune_transitions()
        return delta, track_delta

    def move_preview(self, ids, delta, lane_shift, mode="snap", fixed_tracks=(),
                     allow_overlap=None):
        """Where clips would land for a drag, without mutating the model.

        Returns (placements, valid): placements is [(clip, target_track, start_frame)].
        Every position is derived from the clip's stored start, never from a partially
        applied move, so a drag cannot feed back on itself. Lane shifts clamp to the
        nearest lane of the clip's own type, so video never lands on an audio lane.

        `fixed_tracks` names clips that may slide in time but must keep their track: a
        linked video/audio pair follows horizontally while the pointer only re-lanes the
        clip actually being dragged, which is how AE behaves.
        """
        moving = [c for c in (self.find_clip(i) for i in ids) if c is not None]
        if not moving:
            return [], False
        moving_ids = {c.id for c in moving}
        pinned = {int(cid) for cid in fixed_tracks or ()}
        delta = max(int(delta), -min(c.timeline_start for c in moving))
        placements = []
        valid = True
        for clip in moving:
            if clip.id in pinned:
                target = clip.track
            else:
                lanes = self.tracks_of_type(clip.track.type)
                index = max(0, min(len(lanes) - 1, self.lane_index(clip.track) + int(lane_shift))) if lanes else 0
                target = lanes[index] if lanes else clip.track
            start = max(0, clip.timeline_start + delta)
            placements.append((clip, target, start))
            if target.locked:
                valid = False
        if allow_overlap is None:
            allow_overlap = mode == "crossfade"
        if mode != "push" and not allow_overlap:
            for clip, target, start in placements:
                for other in target.clips:
                    if other.id in moving_ids or other is clip:
                        continue
                    if start < other.end and start + clip.duration > other.timeline_start:
                        valid = False
                        break
        elif allow_overlap:
            # Overlapping a locked track, an adjustment layer or a spot where three clips
            # would cover the same frame is refused; two ordinary clips are a crossfade.
            for clip, target, start in placements:
                if target.locked:
                    valid = False
                    break
                for other in target.clips:
                    if other.id in moving_ids or other is clip:
                        continue
                    if not (start < other.end and start + clip.duration > other.timeline_start):
                        continue
                    if not self.transition_compatible(clip, other):
                        valid = False
                        break
                if not valid:
                    break
            if valid:
                for target in {target for _clip, target, _start in placements}:
                    spans = [(start, start + clip.duration)
                             for clip, lane, start in placements if lane is target]
                    spans += [(other.timeline_start, other.end)
                              for other in target.clips if other.id not in moving_ids]
                    if self._overlap_depth(spans) > 2:
                        valid = False
                        break
        return placements, valid

    @staticmethod
    def _overlap_depth(spans):
        """Largest number of spans covering one frame (adjacent spans do not overlap)."""
        events = []
        for start, end in spans:
            if end > start:
                events.append((start, 1))
                events.append((end, -1))
        depth = deepest = 0
        for _frame, delta in sorted(events, key=lambda event: (event[0], event[1])):
            depth += delta
            deepest = max(deepest, depth)
        return deepest

    def apply_placements(self, placements, mode="snap"):
        """Apply placements produced by move_preview; the drag's single model mutation."""
        moving_ids = {clip.id for clip, _track, _start in placements}
        for clip, _target, start in placements:
            clip.timeline_start = start
        for clip, target, _start in placements:
            if target is not clip.track:
                if clip in clip.track.clips:
                    clip.track.clips.remove(clip)
                target.clips.append(clip)
                clip.track = target
        if mode == "push":
            for track in {target for _clip, target, _start in placements}:
                self._push_neighbours(track, moving_ids)
        self.prune_transitions()

    @staticmethod
    def _push_neighbours(track, fixed_ids):
        fixed = sorted((c for c in track.clips if c.id in fixed_ids), key=lambda c: c.timeline_start)
        cursor = 0
        for other in sorted((c for c in track.clips if c.id not in fixed_ids), key=lambda c: c.timeline_start):
            start = max(other.timeline_start, cursor)
            for moved in fixed:
                if start < moved.end and start + other.duration > moved.timeline_start:
                    start = moved.end
            other.timeline_start = start
            cursor = start + other.duration

    def trim_bounds(self, clip, edge):
        """(lowest, highest) delta a trim of `edge` may use for this clip."""
        neighbours = [c for c in clip.track.clips if c is not clip]
        stretchable = clip.kind == "image"
        if edge == "left":
            prev_end = max((c.end for c in neighbours if c.end <= clip.timeline_start), default=0)
            low = prev_end - clip.timeline_start
            if not stretchable and not clip.loop and not clip.freeze:
                available_before = clip.source_out - clip.source_min if clip.reverse \
                    else clip.source_in - clip.source_min
                # A clip whose in-point sits before source_min has no headroom at all; the
                # bound may never become positive, which would trim the clip forwards.
                low = max(low, -int(max(0, available_before) * clip.timeline_fps /
                                    max(1.0, clip.source_fps * clip.speed)))
            return low, clip.duration - 1
        next_start = min((c.timeline_start for c in neighbours if c.timeline_start >= clip.end), default=FAR)
        high = next_start - clip.end
        return 1 - clip.duration, max(high, 1 - clip.duration)

    def _trim_clip(self, clip, edge, delta):
        """Move one edge of one clip by `delta` frames, remapping its source range."""
        stretchable = clip.kind == "image"
        if edge == "left":
            if not stretchable and not clip.freeze and delta != 0:
                consumed = delta * clip.source_fps / max(1.0, clip.timeline_fps) * clip.speed
                if clip.loop:
                    clip.source_offset = (clip.source_offset + consumed) % max(1, clip.source_out - clip.source_in)
                else:
                    whole = int(math.floor(abs(consumed)))
                    if delta > 0:
                        if clip.reverse:
                            clip.source_out = max(clip.source_in + 1, clip.source_out - whole)
                        else:
                            clip.source_in = min(clip.source_out - 1, clip.source_in + whole)
                    elif clip.reverse:
                        clip.source_out = min(clip.source_max, clip.source_out + whole)
                    else:
                        clip.source_in = max(clip.source_min, clip.source_in - whole)
                    clip.source_offset = 0.0
            clip.timeline_start += delta
            clip.duration -= delta
            return delta
        clip.duration += delta
        if not stretchable and not clip.freeze:
            source_frames_needed = clip.source_offset + max(1.0, clip.duration * clip.source_fps /
                                                            max(1.0, clip.timeline_fps) * clip.speed)
            available = max(1.0, (clip.source_out - clip.source_min) if clip.reverse
                            else (clip.source_max - clip.source_in))
            clip.loop = source_frames_needed > available
            if clip.loop:
                clip.source_in, clip.source_out = clip.source_min, clip.source_max
            else:
                used = min(available, int(math.ceil(source_frames_needed)))
                if clip.reverse:
                    clip.source_in = max(clip.source_min, clip.source_out - used)
                else:
                    clip.source_out = min(clip.source_max, clip.source_in + used)
        return delta

    def apply_trim(self, snap, clip_id, edge, delta, source_length=None, also=()):
        """Restore `snap`, then trim one edge of a clip (and of its linked partners).

        Linked clips take the same delta, clamped to what every member can accept, so a
        pair keeps its source alignment instead of sliding out of sync on the first trim.
        Returns the applied delta.
        """
        self.restore(snap)
        clips = []
        for cid in (clip_id, *tuple(also or ())):
            clip = self.find_clip(cid)
            if clip is not None and all(clip is not other for other in clips):
                clips.append(clip)
        if not clips:
            return 0
        low, high = -FAR, FAR
        for clip in clips:
            member_low, member_high = self.trim_bounds(clip, edge)
            low, high = max(low, member_low), min(high, member_high)
        if low > high:
            low = high = 0
        delta = max(low, min(delta, high))
        for clip in clips:
            self._trim_clip(clip, edge, delta)
            clip.clamp_fades("in" if edge == "left" else "out")
        self.prune_transitions()
        return delta

    def apply_stretch(self, snap, clip_id, edge, target_frame, also=()):
        """Set a clip edge and speed, taking its linked members with it.

        `also` holds the link partners. They end up on the same start frame, the same
        duration and the same speed, so a video/audio pair stays frame-synced instead of
        the picture changing length while its sound keeps the old length. The stretch is
        clamped by the nearest neighbours on *every* member's own track, and a member on
        a locked track is never touched. Returns the applied speed.
        """
        self.restore(snap)
        clip = self.find_clip(clip_id)
        if clip is None:
            return None
        members = [clip]
        for cid in also or ():
            other = self.find_clip(cid)
            if (other is not None and other is not clip and other not in members
                    and other.link_id == clip.link_id and not other.track.locked):
                members.append(other)
        old_start, old_end = clip.timeline_start, clip.end
        if edge == "right":
            next_start = FAR
            for member in members:
                for other in member.track.clips:
                    if other is not member and other.timeline_start >= old_end:
                        next_start = min(next_start, other.timeline_start)
            new_start = old_start
            new_end = max(old_start + 1, min(int(target_frame), next_start))
        else:
            prev_end = 0
            for member in members:
                for other in member.track.clips:
                    if other is not member and other.end <= old_start:
                        prev_end = max(prev_end, other.end)
            new_start = max(prev_end, min(old_end - 1, int(target_frame)))
            new_end = old_end
        duration = max(1, new_end - new_start)
        base_duration = max(1.0, (clip.source_out - clip.source_in) *
                            clip.timeline_fps / max(1.0, clip.source_fps))
        speed = max(0.1, min(10.0, base_duration / duration))
        for member in members:
            member.timeline_start = new_start
            member.duration = duration
            # One shared speed keeps link_is_synced() true for the pair.
            member.speed = speed
            member.loop = False
            member.clamp_fades("in" if edge == "left" else "out")
        self.prune_transitions()
        return speed

    def split_clip(self, clip_id, frame):
        clip = self.find_clip(clip_id)
        if clip is None or not (clip.timeline_start < frame < clip.end):
            return None
        left_len = frame - clip.timeline_start
        left_effects, right_effects = self._split_animated_params(clip.effects, left_len)
        left_transform, right_transform = split_animated(clip.transform, left_len)
        left_meta, right_meta = split_transform_meta(clip.transform_meta, left_len)
        left_text, right_text = split_text(clip.text, left_len)
        # Gain automation splits like any other animated value, so the left piece keeps
        # the fade it played and the right piece starts where the split landed.
        left_gain, right_gain = split_animated({"gain": clip.gain}, left_len)
        left_presets, right_presets = split_instances(clip.presets, left_len, left_len,
                                                      clip.duration - left_len)
        right = self.new_clip(
            clip.name, clip.source_path, clip.track, frame, clip.duration - left_len,
            clip.source_in, right_effects, clip.source_out, clip.source_fps,
            clip.speed, clip.reverse, clip.loop, clip.freeze, clip.source_offset,
            clip.source_min, clip.source_max, clip.volume,
            link_id=clip.link_id, stream=clip.stream, transform=right_transform,
            transform_meta=right_meta, text=right_text, gain=right_gain["gain"],
            presets=right_presets, shape=copy.deepcopy(clip.shape),
            composition_id=clip.composition_id,
        )
        right.source_offset = clip.source_offset + left_len * clip.source_fps / max(1.0, clip.timeline_fps) * clip.speed
        clip.effects = left_effects
        clip.presets = left_presets
        clip.transform = left_transform
        clip.transform_meta = left_meta
        clip.gain = left_gain["gain"]
        if left_text:
            clip.text = left_text
        clip.duration = left_len
        # A split keeps the fade-in on the left piece and the fade-out on the right one;
        # both are clamped to their new length (a split inside a fade shortens it).
        right.fade_out = min(clip.fade_out, right.duration)
        right.fade_out_curve = clip.fade_out_curve
        right.fade_in = 0
        clip.fade_in = min(clip.fade_in, clip.duration)
        clip.fade_out = 0
        clip.clamp_fades("in")
        right.clamp_fades("out")
        self.prune_transitions()
        return right.id

    # ---- clipboard -------------------------------------------------------
    def clip_record(self, clip):
        lanes = self.tracks_of_type(clip.track.type)
        return {
            "name": clip.name, "source_path": clip.source_path, "type": clip.track.type,
            "lane": lanes.index(clip.track), "start": clip.timeline_start, "duration": clip.duration,
            "source_in": clip.source_in, "source_out": clip.source_out, "source_fps": clip.source_fps,
            "source_min": clip.source_min, "source_max": clip.source_max,
            "speed": clip.speed, "reverse": clip.reverse, "loop": clip.loop, "freeze": clip.freeze,
            "source_offset": clip.source_offset, "effects": clone_effects(clip.effects),
            "volume": clip.volume, "fade_in": clip.fade_in, "fade_out": clip.fade_out,
            "fade_in_curve": clip.fade_in_curve, "fade_out_curve": clip.fade_out_curve,
            "link": clip.link_id, "stream": clip.stream,
            "transform": copy.deepcopy(clip.transform),
            "transform_meta": copy.deepcopy(clip.transform_meta),
            "text": copy.deepcopy(clip.text),
            "shape": copy.deepcopy(getattr(clip, "shape", None)),
            "gain": copy.deepcopy(clip.gain),
            "presets": copy.deepcopy(getattr(clip, "presets", [])),
            "composition_id": getattr(clip, "composition_id", ""),
            "blend": getattr(clip, "blend", "normal"),
            "adjustment": bool(getattr(clip, "adjustment", False)),
        }

    def paste_records(self, records, at_frame, anchor_track=None, mode="snap"):
        """Create copies of clipboard records starting at `at_frame`.

        Relative spacing and lane offsets are preserved. If `anchor_track` is given the
        earliest record lands on it. Occupied space is resolved per overlap `mode`:
        "snap" tries the same time on neighbouring tracks, then the next free spot;
        "push" pushes the existing neighbours to the right. Returns the new clip ids.
        """
        if not records:
            return []
        records = sorted(records, key=lambda r: (r["start"], r["lane"]))
        first = records[0]
        shift = 0
        if anchor_track is not None and anchor_track.type == first["type"]:
            shift = self.tracks_of_type(first["type"]).index(anchor_track) - first["lane"]
        delta = max(0, int(at_frame)) - first["start"]

        def lane_track(rec, extra):
            lanes = self.tracks_of_type(rec["type"])
            if not lanes:
                lanes = [self.add_track(rec["type"])]
            return lanes[max(0, min(len(lanes) - 1, rec["lane"] + shift + extra))]

        def temp_clip(rec, d):
            return Clip(0, rec["name"], rec["source_path"], None, rec["start"] + d, rec["duration"],
                        rec.get("source_in", 0), source_out=rec.get("source_out"),
                        source_fps=rec.get("source_fps", self.fps), timeline_fps=self.fps,
                        speed=rec.get("speed", 1.0), reverse=rec.get("reverse", False),
                        loop=rec.get("loop", False), freeze=rec.get("freeze", False),
                        source_offset=rec.get("source_offset", 0), source_min=rec.get("source_min"),
                        source_max=rec.get("source_max"), volume=rec.get("volume", 1.0),
                        fade_in=rec.get("fade_in", 0), fade_out=rec.get("fade_out", 0),
                        fade_in_curve=rec.get("fade_in_curve", 0.0),
                        fade_out_curve=rec.get("fade_out_curve", 0.0),
                        transform=rec.get("transform"), transform_meta=rec.get("transform_meta"),
                        text=rec.get("text"), shape=rec.get("shape"), gain=rec.get("gain", 0.0))

        def valid_for(extra):
            valid = [(-FAR, FAR)]
            for rec in records:
                target = lane_track(rec, extra)
                if target.locked:
                    return []
                others = target.sorted_clips()
                valid = self._intersect(valid, self._valid_deltas(temp_clip(rec, 0), target, others))
                if not valid:
                    return []
            return valid

        chosen_extra = 0
        if mode == "snap":
            def exact(valid):
                return any(lo <= delta <= hi for lo, hi in valid)

            found = None
            max_extra = max(len(self.tracks_of_type(r["type"])) for r in records)
            for extra in [0] + [s * k for k in range(1, max_extra + 1) for s in (1, -1)]:
                if exact(valid_for(extra)):
                    found = extra
                    break
            if found is not None:
                chosen_extra = found
            else:
                valid = valid_for(0)
                candidates = [max(lo, delta) for lo, hi in valid if hi >= delta]
                if candidates:
                    delta = min(candidates)

        new_ids = []
        touched = set()
        # Pasted copies of a linked pair stay a pair, under a fresh link id.
        link_map = {}
        for rec in records:
            target = lane_track(rec, chosen_extra)
            clip = self.new_clip(rec["name"], rec["source_path"], target, rec["start"] + delta, rec["duration"],
                                 rec.get("source_in", 0), clone_effects(rec["effects"]),
                                 rec.get("source_out"), rec.get("source_fps"), rec.get("speed", 1.0),
                                 rec.get("reverse", False), rec.get("loop", False), rec.get("freeze", False),
                                 rec.get("source_offset", 0), rec.get("source_min"), rec.get("source_max"),
                                 rec.get("volume", 1.0), rec.get("fade_in", 0), rec.get("fade_out", 0),
                                 rec.get("fade_in_curve", 0.0), rec.get("fade_out_curve", 0.0),
                                 stream=rec.get("stream"), gain=rec.get("gain", 0.0),
                                 transform=rec.get("transform"),
                                 transform_meta=rec.get("transform_meta"),
                                 composition_id=rec.get("composition_id", ""),
                                 blend=rec.get("blend", "normal"),
                                 adjustment=rec.get("adjustment", False))
            clip.presets = copy.deepcopy(rec.get("presets") or [])
            clip.text = copy.deepcopy(rec.get("text"))
            clip.shape = copy.deepcopy(rec.get("shape"))
            clip.source_offset = rec.get("source_offset", 0)
            old_link = rec.get("link") or 0
            if old_link:
                clip.link_id = link_map.setdefault(old_link, self.new_link_id())
            new_ids.append(clip.id)
            touched.add(target)
        if mode == "push":
            for track in touched:
                self._push_neighbours(track, set(new_ids))
        self.prune_transitions()
        return new_ids

    # ---- placement -------------------------------------------------------
    def default_lane(self, kind):
        """Lane new media lands on when no track is targeted (video: lowest, audio: first)."""
        lanes = self.tracks_of_type(kind)
        if not lanes:
            return None
        return lanes[-1] if kind == "video" else lanes[0]

    def lane_below(self, track, kind):
        """First track of `kind` displayed below `track` (None when there is none)."""
        if track is None or track not in self.tracks:
            return None
        for candidate in self.tracks[self.tracks.index(track) + 1:]:
            if candidate.type == kind:
                return candidate
        return None

    def first_free_start_all(self, lanes, start, duration):
        """First frame where *every* lane in `lanes` has room (keeps a pair aligned)."""
        start = max(0, int(start))
        lanes = [lane for lane in lanes if lane is not None]
        for _guard in range(4096):
            moved = False
            for lane in lanes:
                nxt = self.first_free_start(lane, start, duration)
                if nxt > start:
                    start, moved = nxt, True
            if not moved:
                break
        return start

    def entry_duration(self, entry):
        """Timeline length of an import entry, clamped to the source range it has."""
        duration = max(1, int(entry.get("duration") or 1))
        source_out, source_fps = entry.get("source_out"), entry.get("source_fps")
        ext = os.path.splitext(entry.get("path") or "")[1].lower()
        if entry.get("type") != "audio" and ext not in IMAGE_EXTENSIONS and source_out is not None and source_fps:
            span = max(1, int(source_out) - int(entry.get("source_in", 0)))
            duration = min(duration, max(1, int(round(span * self.fps / max(1.0, float(source_fps))))))
        return duration

    def plan_media(self, entries, start=None, track=None, find_free=False, create=True):
        """Where a media drop lands: (placements, ok, reason) without reordering clips.

        A video entry flagged `audio` produces two placements - the video clip and an audio
        clip on the lane directly below it - sharing one link key, both starting on the same
        frame. Nothing is written to the model when `create` is False, so the drag preview
        can use exactly the same computation as the drop.
        """
        entries = [entry for entry in (entries or ()) if entry]
        if not entries:
            return [], True, ""
        if track is not None and track not in self.tracks:
            track = None          # the drop target was deleted while the file decoded
        placements = []
        cursor = start
        for index, entry in enumerate(entries):
            kind = "audio" if entry.get("type") == "audio" else "video"
            duration = self.entry_duration(entry)
            partner_wanted = bool(entry.get("audio")) and kind == "video"
            lane = self._choose_lane(kind, track, cursor, duration, find_free, create)
            if lane is not None and lane.locked:
                return [], False, f"{lane.name} is locked"
            partner = None
            if partner_wanted:
                partner = self.lane_below(lane, "audio")
                if partner is None:
                    partner = self.default_lane("audio")
                if partner is not None and partner.locked:
                    return [], False, f"{partner.name} is locked"
            if create:
                if lane is None:
                    lane = self.add_track(kind)
                if partner_wanted and partner is None:
                    index_below = (self.tracks.index(lane) + 1) if lane in self.tracks else 0
                    partner = self.add_track("audio", index=index_below)
            begin = max((c.end for c in lane.clips), default=0) if (cursor is None and lane is not None) \
                else (0 if cursor is None else int(cursor))
            begin = self.first_free_start_all([lane, partner], begin, duration)
            placements.append({"name": entry.get("name") or os.path.basename(entry.get("path") or ""),
                               "path": entry.get("path") or "", "track": lane, "start": begin,
                               "duration": duration, "source_in": int(entry.get("source_in", 0)),
                               "source_out": entry.get("source_out"),
                               "source_fps": entry.get("source_fps"), "stream": "audio" if kind == "audio" else "video",
                               "link": None})
            if partner_wanted:
                placements.append({"name": placements[-1]["name"], "path": placements[-1]["path"],
                                   "track": partner, "start": begin, "duration": duration,
                                   "source_in": placements[-1]["source_in"],
                                   "source_out": placements[-1]["source_out"],
                                   "source_fps": placements[-1]["source_fps"], "stream": "audio",
                                   "link": f"drop{len(placements)}"})
                placements[-2]["link"] = placements[-1]["link"]
            cursor = begin + duration
        return placements, True, ""

    def _choose_lane(self, kind, reference, cursor, duration, find_free, create):
        """Lane a new clip of `kind` should land on, creating one only when asked to."""
        lanes = self.tracks_of_type(kind)
        if reference is not None and reference.type == kind:
            return reference
        if reference is not None and lanes:
            position = self.tracks.index(reference)
            return min(lanes, key=lambda t: abs(self.tracks.index(t) - position))
        if find_free and cursor is not None:
            for lane in (reversed(lanes) if kind == "video" else lanes):
                if not lane.locked and self.first_free_start(lane, cursor, duration) == cursor:
                    return lane
        lane = self.default_lane(kind)
        if lane is None and create:
            lane = self.add_track(kind)
        return lane

    def apply_plan(self, placements):
        """Create the clips a plan describes (one link id per linked pair)."""
        ids, links = [], {}
        for placement in placements or ():
            target = placement.get("track")
            if target is None or target not in self.tracks:
                continue
            clip = self.new_clip(placement["name"], placement["path"], target, placement["start"],
                                 max(1, int(placement["duration"])), placement.get("source_in", 0),
                                 source_out=placement.get("source_out"),
                                 source_fps=placement.get("source_fps"),
                                 volume=placement.get("volume", 1.0),
                                 stream=placement.get("stream"))
            key = placement.get("link")
            if key is not None:
                clip.link_id = links.setdefault(key, self.new_link_id())
            ids.append(clip.id)
        return ids

    def place_media(self, entries, start=None, track=None, find_free=False):
        """Place imported media one after another. Each entry: dict(name, path, duration, type).

        A track of the wrong type is replaced by the nearest matching one (created if none).
        With `find_free` and no track, each clip goes on the first track with room. An entry
        flagged `audio` also gets its linked audio clip, or nothing is inserted at all.
        """
        placements, ok, _reason = self.plan_media(entries, start, track, find_free)
        if not ok or not placements:
            return []
        return self.apply_plan(placements)

    def place_clips(self, entries, start=None, track=None, find_free=False):
        """Backwards-compatible name for place_media (imports, paste and tests use it)."""
        return self.place_media(entries, start, track, find_free)

    def delete_clips(self, ids, ripple=False):
        ids = set(ids)
        for track in self.tracks:
            removed = [c for c in track.clips if c.id in ids]
            if not removed:
                continue
            track.clips = [c for c in track.clips if c.id not in ids]
            if ripple:
                for clip in track.clips:
                    clip.timeline_start -= sum(r.duration for r in removed if r.timeline_start < clip.timeline_start)
        self.prune_transitions()


class CommandStack:
    """Undo/redo via before/after snapshots; each entry is one labelled user action."""

    def __init__(self, timeline, limit=200):
        self.timeline = timeline
        self.limit = limit
        self._undo = []
        self._redo = []
        self.changed = Signal()

    def commit(self, label, before):
        after = self.timeline.snapshot()
        if after == before:
            return False
        self._undo.append((label, before, after))
        del self._undo[:-self.limit]
        self._redo.clear()
        self.changed.emit()
        return True

    def run(self, label, action):
        before = self.timeline.snapshot()
        result = action()
        self.commit(label, before)
        return result

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def undo(self):
        if not self._undo:
            return None
        entry = self._undo.pop()
        self._redo.append(entry)
        self.timeline.restore(entry[1])
        self.changed.emit()
        return entry[0]

    def redo(self):
        if not self._redo:
            return None
        entry = self._redo.pop()
        self._undo.append(entry)
        self.timeline.restore(entry[2])
        self.changed.emit()
        return entry[0]
