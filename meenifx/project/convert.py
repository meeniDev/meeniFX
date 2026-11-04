"""Frame-rate conversion of a timeline so real durations stay the same (rounded to frames)."""

from ..model.keyframes import AnimatedValue
from ..model.timeline import Timeline


def convert_timeline_fps(timeline: Timeline, new_fps):
    """Rescale every frame position/length from timeline.fps to the whole-frame `new_fps` in place.

    Starts and ends are rounded separately so neighbouring clips stay neighbours.
    """
    old, new = timeline.fps, int(new_fps)
    if old == new:
        return
    r = new / old

    def s(v):
        return int(round(v * r))

    for track in timeline.tracks:
        for c in track.clips:
            start, end = s(c.timeline_start), s(c.end)
            c.timeline_start, c.duration = start, max(1, end - start)
            c.fade_in, c.fade_out = s(c.fade_in), s(c.fade_out)
            c.timeline_fps = float(new)
            c.clamp_fades("in")
            animated = list((c.transform or {}).values())
            for effect in c.effects:
                animated.extend((effect.get("params") or {}).values())
                for pin in effect.get("pins") or ():
                    animated.extend((pin.get("x"), pin.get("y")))
            for value in animated:
                if isinstance(value, AnimatedValue):
                    for k in value.keyframes:
                        k.time = s(k.time)
                    value.keyframes.sort(key=lambda k: k.time)
            for key, handles in (c.transform_meta.get("path") or {}).items():
                try:
                    at = int(key)
                except (TypeError, ValueError):
                    continue
                if str(s(at)) != key:
                    c.transform_meta["path"].pop(key, None)
                    c.transform_meta["path"][str(s(at))] = handles
    used = set()
    for m in sorted(timeline.markers, key=lambda x: x.frame):
        m.frame = s(m.frame)
        while m.frame in used:
            m.frame += 1
        used.add(m.frame)
    if timeline.loop_in is not None:
        timeline.loop_in = s(timeline.loop_in)
    if timeline.loop_out is not None:
        timeline.loop_out = s(timeline.loop_out)
    timeline.fps = new
    timeline.min_duration = new * 30
