"""Animatable values: keyframes, interpolation and easing. Time is clip-local frames."""

from dataclasses import dataclass

from .easing import segment_progress


@dataclass
class Keyframe:
    time: int
    value: object
    interpolation: str = "linear"
    in_influence: float = 0.33
    out_influence: float = 0.33
    in_value_handle: object = None
    out_value_handle: object = None


class AnimatedValue:
    """A parameter value with sorted keyframes and linear interpolation."""

    def __init__(self, keyframes=None, base_value=None):
        self.keyframes = sorted(list(keyframes or []), key=lambda key: key.time)
        self.base_value = base_value

    def value_at(self, frame):
        if not self.keyframes:
            return self.base_value
        frame = float(frame)
        if frame <= self.keyframes[0].time:
            return self.keyframes[0].value
        if frame >= self.keyframes[-1].time:
            return self.keyframes[-1].value
        for index, (left, right) in enumerate(zip(self.keyframes, self.keyframes[1:])):
            if left.time <= frame <= right.time:
                if right.time == left.time:
                    return left.value
                ratio = (frame - left.time) / (right.time - left.time)
                if isinstance(left.value, bool) or not isinstance(left.value, (int, float)):
                    return left.value if ratio < 1.0 else right.value
                # The curve lives on the keyframe that starts the segment; the preview,
                # the exporter and the graph editor all read it through this one call.
                # Auto/Continuous Bezier also look at the neighbouring keys, which is
                # why the neighbours travel with the call.
                prev_key = self.keyframes[index - 1] if index > 0 else None
                next_key = self.keyframes[index + 2] if index + 2 < len(self.keyframes) else None
                progress = segment_progress(left, right, ratio, prev_key, next_key)
                value = left.value + (right.value - left.value) * progress
                return int(round(value)) if isinstance(left.value, int) else value
        return self.keyframes[-1].value

    def key_at(self, frame):
        return next((key for key in self.keyframes if key.time == int(frame)), None)

    def set_keyframe(self, frame, value):
        frame = int(frame)
        key = self.key_at(frame)
        if key is None:
            key = Keyframe(frame, value)
            self.keyframes.append(key)
            self.keyframes.sort(key=lambda item: item.time)
        else:
            key.value = value
        return key

    def split_at(self, frame):
        """Return independent left/right curves whose boundary evaluates like this curve."""
        frame = int(frame)
        boundary = self.value_at(frame)
        left = AnimatedValue(base_value=self.base_value)
        right = AnimatedValue(base_value=boundary)
        for key in self.keyframes:
            if key.time < frame:
                left.keyframes.append(key)
            elif key.time > frame:
                right.keyframes.append(Keyframe(
                    key.time - frame, key.value, key.interpolation, key.in_influence,
                    key.out_influence, key.in_value_handle, key.out_value_handle,
                ))
        left.set_keyframe(max(0, frame - 1), self.value_at(max(0, frame - 1)))
        right.set_keyframe(0, boundary)
        return left, right

    def remove_keyframe(self, frame):
        frame = int(frame)
        before = len(self.keyframes)
        self.keyframes[:] = [key for key in self.keyframes if key.time != frame]
        return len(self.keyframes) != before


def parameter_value_at(value, frame):
    """Evaluate animated parameters while leaving static values unchanged."""
    return value.value_at(frame) if isinstance(value, AnimatedValue) else value


def clip_animations(effects):
    """(effect_id, parameter, AnimatedValue) for every animated parameter of a clip."""
    rows = []
    for entry in effects or ():
        for name, value in entry.get("params", {}).items():
            if isinstance(value, AnimatedValue):
                rows.append((entry["effect_id"], name, value))
    return rows


def clip_keyframe_frames(effects):
    """Every clip-local frame that carries a keyframe, whichever parameter owns it."""
    frames = set()
    for _effect, _name, value in clip_animations(effects):
        frames.update(key.time for key in value.keyframes)
    return frames


def animation_value(clip, effect_id, name):
    """The AnimatedValue for one parameter, or None when it is not animated."""
    for entry in clip.effects:
        if entry.get("effect_id") != effect_id:
            continue
        value = entry.get("params", {}).get(name)
        if isinstance(value, AnimatedValue):
            return value
    return None


def keyframe_at(value, frame):
    """The keyframe of an AnimatedValue sitting exactly on `frame`, or None."""
    return value.key_at(frame) if isinstance(value, AnimatedValue) else None


def sample_value(value, start, step, count):
    """[(frame, value)] sampled from an AnimatedValue, for graph drawing."""
    frames = []
    for index in range(max(0, int(count))):
        frame = int(start) + index * max(1, int(step))
        frames.append((frame, parameter_value_at(value, frame)))
    return frames
