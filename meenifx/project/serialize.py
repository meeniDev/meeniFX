"""Timeline <-> container sections. Clips, effects and keyframes use compact columnar arrays.

Format 3 adds clip transforms and puppet-warp pins. Both use the *same* keyframe table
as effect parameters: every animated value is stored once as an "owner" row plus its
keyframes, and the owner's `kind` says where it belongs ("effect", "transform" or
"pin"). Nothing animatable is duplicated into a second storage format, and unknown
interpolation names survive a save/load round trip untouched.
"""

import copy
import itertools

from ..model import shape as shape_model
from ..model import text as text_model
from ..model import transform as transforms
from ..model.composition import Composition, CompositionLibrary
from ..model.keyframes import AnimatedValue, Keyframe
from ..model.timeline import Clip, Marker, Timeline, Track

CLIP_COLUMNS = ("id", "track", "media", "name", "start", "dur", "src_in", "src_out", "src_min", "src_max",
                "src_fps", "tl_fps", "speed", "reverse", "loop", "freeze", "offset", "volume",
                "fade_in", "fade_out", "fade_in_curve", "fade_out_curve", "stream", "link",
                "transform", "meta", "text", "shape", "gain", "presets", "blend", "adjust",
                "composition")
KF_FIELDS = ("in_influence", "out_influence", "in_value_handle", "out_value_handle")
PIN_KEYS = ("id", "src", "locked")
RESERVED_EFFECT_KEYS = ("effect_id", "enabled", "params", "pins")


def _split_animated(mapping):
    """({static values}, {name: AnimatedValue}) of a transform or parameter dict."""
    static, animated = {}, {}
    for name, value in mapping.items():
        if isinstance(value, AnimatedValue):
            animated[name] = value
        else:
            static[name] = value
    return static, animated


def timeline_to_sections(timeline, media_id_for):
    """`media_id_for(path)` returns the media-library id of a source path."""
    tracks = [{"id": t.id, "type": t.type, "name": t.name, "muted": t.muted, "solo": t.solo,
               "locked": t.locked, "visible": t.visible, "volume": t.volume, "height": t.height}
              for t in timeline.tracks]
    max_clip = 0
    cols = {k: [] for k in CLIP_COLUMNS}
    eff = {"clip": [], "effect_id": [], "enabled": [], "params": [], "pins": [], "data": []}
    owners, kinds = [], []
    kf = {"owners": owners, "kinds": kinds, "owner": [], "time": [], "value": [], "interp": [],
          "in_influence": [], "out_influence": [], "in_value_handle": [], "out_value_handle": []}
    interps, interp_index = [], {}
    effect_row = 0
    clip_index = 0

    def add_owner(kind, row, name, animated):
        owners.append([row, name, animated.base_value])
        kinds.append(kind)
        owner = len(owners) - 1
        for key in animated.keyframes:
            kf["owner"].append(owner)
            kf["time"].append(key.time)
            kf["value"].append(key.value)
            idx = interp_index.setdefault(key.interpolation, len(interp_index))
            if idx == len(interps):
                interps.append(key.interpolation)
            kf["interp"].append(idx)
            for field in KF_FIELDS:
                kf[field].append(getattr(key, field))

    for track_index, track in enumerate(timeline.tracks):
        for c in track.clips:
            max_clip = max(max_clip, c.id)
            transforms.ensure_transform(c)
            transform_static, transform_animated = _split_animated(c.transform)
            for name, animated in transform_animated.items():
                add_owner("transform", clip_index, name, animated)
            text_column = None
            if c.is_text:
                settings = text_model.ensure_text(c)
                text_column, text_animated = _split_animated(
                    {name: value for name, value in settings.items() if name != "keys"})
                text_column["keys"] = {str(int(frame)): str(words)
                                       for frame, words in (settings.get("keys") or {}).items()}
                for name, animated in text_animated.items():
                    add_owner("text", clip_index, name, animated)
            shape_column = None
            if c.is_shape:
                settings = shape_model.ensure_shape(c)
                shape_column, shape_animated = _split_animated(
                    {name: value for name, value in settings.items()
                     if name not in shape_model.COLOR_FIELDS})
                for name in shape_model.COLOR_FIELDS:
                    value = settings.get(name)
                    if isinstance(value, dict):
                        # Held keys, in the same {"keys": {frame: value}} shape the loader
                        # and the rest of the model use - a bare mapping would load empty.
                        shape_column[name] = {"keys": {
                            str(int(frame)): str(colour)
                            for frame, colour in (value.get("keys") or {}).items()}}
                    else:
                        shape_column[name] = value
                for name, animated in shape_animated.items():
                    add_owner("shape", clip_index, name, animated)
            gain_static = c.gain
            if isinstance(gain_static, AnimatedValue):
                # Animated gain is stored once, as an owner row named "gain" on the clip.
                add_owner("gain", clip_index, "gain", gain_static)
                gain_static = gain_static.base_value if gain_static.base_value is not None else 0.0
            for key, val in zip(CLIP_COLUMNS, (
                    c.id, track_index, media_id_for(c.source_path) if c.source_path else "", c.name,
                    c.timeline_start, c.duration, c.source_in, c.source_out, c.source_min, c.source_max,
                    c.source_fps, c.timeline_fps, c.speed, c.reverse, c.loop, c.freeze, c.source_offset,
                    c.volume, c.fade_in, c.fade_out, c.fade_in_curve, c.fade_out_curve,
                    c.stream, c.link_id, transform_static, c.transform_meta or {}, text_column,
                    shape_column, gain_static, copy.deepcopy(getattr(c, "presets", []) or []),
                    getattr(c, "blend", "normal"), bool(getattr(c, "adjustment", False)),
                    getattr(c, "composition_id", ""))):
                cols[key].append(val)
            for effect in c.effects:
                static, animated = _split_animated(effect.get("params", {}))
                for name, value in animated.items():
                    add_owner("effect", effect_row, name, value)
                pins = []
                for pin in effect.get("pins") or ():
                    stored = {key: pin[key] for key in PIN_KEYS if key in pin}
                    for axis in ("x", "y"):
                        value = pin.get(axis)
                        if isinstance(value, AnimatedValue):
                            add_owner("pin", effect_row, f"{pin.get('id')}.{axis}", value)
                        elif value is not None:
                            stored[axis] = float(value)
                    pins.append(stored)
                extra = {key: value for key, value in effect.items() if key not in RESERVED_EFFECT_KEYS}
                eff["clip"].append(c.id)
                eff["effect_id"].append(effect.get("effect_id"))
                eff["enabled"].append(bool(effect.get("enabled", True)))
                eff["params"].append(static)
                eff["pins"].append(pins)
                eff["data"].append(extra)
                effect_row += 1
            clip_index += 1
    kf["interp_names"] = interps
    markers = {"markers": [{"id": m.id, "frame": m.frame, "name": m.name, "color": m.color}
                           for m in timeline.markers],
               "loop_in": timeline.loop_in, "loop_out": timeline.loop_out,
               "loop_enabled": bool(timeline.loop_enabled), "loop_at_end": bool(timeline.loop_at_end)}
    return {"tracks": {"tracks": tracks, "counters": dict(timeline._counters), "max_clip": max_clip},
            "clips": cols, "effects": eff, "keyframes": kf, "markers": markers}


def _keyframes_per_owner(sections):
    kf = sections["keyframes"]
    interps = kf.get("interp_names", [])
    per_owner = [[] for _ in kf["owners"]]
    for i, owner in enumerate(kf["owner"]):
        name = interps[kf["interp"][i]] if kf["interp"][i] < len(interps) else "linear"
        per_owner[owner].append(Keyframe(
            kf["time"][i], kf["value"][i], name, kf["in_influence"][i],
            kf["out_influence"][i], kf["in_value_handle"][i], kf["out_value_handle"][i]))
    return per_owner


def sections_to_timeline(sections, fps, path_for_media):
    """Build a Timeline. `path_for_media(media_id)` returns the resolved source path."""
    timeline = Timeline(fps)
    tdata = sections["tracks"]
    for t in tdata["tracks"]:
        track = Track(t["id"], t["type"], t["name"], t.get("height"))
        track.muted, track.solo = bool(t.get("muted")), bool(t.get("solo"))
        track.locked, track.visible = bool(t.get("locked")), bool(t.get("visible", True))
        track.volume = float(t.get("volume", 1.0))
        timeline.tracks.append(track)
    timeline._counters.update({k: int(v) for k, v in tdata.get("counters", {}).items() if k in timeline._counters})
    track_ids = [t["id"] for t in tdata["tracks"]]
    timeline._track_ids = itertools.count(max(track_ids, default=0) + 1)

    # ---- effect rows, with their static params, pins and extra data
    eff, kf = sections["effects"], sections["keyframes"]
    rows = [{"effect_id": eff["effect_id"][i], "enabled": eff["enabled"][i],
             "params": dict(eff["params"][i])}
            for i in range(len(eff["clip"]))]
    extras = eff.get("data") or [{} for _ in rows]
    pins_column = eff.get("pins") or [[] for _ in rows]
    for row, extra, pins in zip(rows, extras, pins_column):
        if isinstance(extra, dict) and extra:
            row.update(extra)
        if pins:
            row["pins"] = [dict(pin) for pin in pins]

    owners = kf["owners"]
    kinds = kf.get("kinds") or ["effect"] * len(owners)
    per_owner = _keyframes_per_owner(sections)
    transform_owners = []
    text_owners = []
    shape_owners = []
    gain_owners = []
    for owner, (row, name, base) in enumerate(owners):
        kind = kinds[owner] if owner < len(kinds) else "effect"
        keys = per_owner[owner]
        if kind == "effect" and 0 <= row < len(rows):
            rows[row]["params"][name] = AnimatedValue(keys, base)
        elif kind == "pin" and 0 <= row < len(rows):
            pin_id, _sep, axis = str(name).rpartition(".")
            for pin in rows[row].get("pins") or ():
                if str(pin.get("id")) == pin_id:
                    pin[axis] = AnimatedValue(keys, base)
                    break
        elif kind == "transform":
            transform_owners.append((int(row), name, AnimatedValue(keys, base)))
        elif kind == "text":
            text_owners.append((int(row), name, AnimatedValue(keys, base)))
        elif kind == "shape":
            shape_owners.append((int(row), name, AnimatedValue(keys, base)))
        elif kind == "gain":
            gain_owners.append((int(row), name, AnimatedValue(keys, base)))

    by_clip = {}
    for clip_id, row in zip(eff["clip"], rows):
        by_clip.setdefault(clip_id, []).append(row)

    c = sections["clips"]
    count = len(c["id"])
    streams = {"stream": c.get("stream") or ["both"] * count,
               "link": c.get("link") or [0] * count}
    transform_column = c.get("transform") or [{} for _ in range(count)]
    meta_column = c.get("meta") or [{} for _ in range(count)]
    text_column = c.get("text") or [None for _ in range(count)]
    shape_column = c.get("shape") or [None for _ in range(count)]
    transform_by_clip = {}
    for clip_index, name, animated in transform_owners:
        transform_by_clip.setdefault(clip_index, {})[name] = animated
    text_by_clip = {}
    for clip_index, name, animated in text_owners:
        text_by_clip.setdefault(clip_index, {})[name] = animated
    shape_by_clip = {}
    for clip_index, name, animated in shape_owners:
        shape_by_clip.setdefault(clip_index, {})[name] = animated
    gain_by_clip = {}
    for clip_index, _name, animated in gain_owners:
        gain_by_clip[clip_index] = animated
    gain_column = c.get("gain") or [0.0 for _ in range(count)]
    presets_column = c.get("presets") or [None for _ in range(count)]
    blend_column = c.get("blend") or ["normal" for _ in range(count)]
    adjust_column = c.get("adjust") or [False for _ in range(count)]
    composition_column = c.get("composition") or ["" for _ in range(count)]
    for i in range(count):
        track = timeline.tracks[c["track"][i]]
        path = path_for_media(c["media"][i]) if c["media"][i] else ""
        clip = Clip(c["id"][i], c["name"][i], path, track, c["start"][i], c["dur"][i], c["src_in"][i],
                    by_clip.get(c["id"][i], []), c["src_out"][i], c["src_fps"][i], c["tl_fps"][i], c["speed"][i],
                    c["reverse"][i], c["loop"][i], c["freeze"][i], c["offset"][i], c["src_min"][i],
                    c["src_max"][i], c["volume"][i], c["fade_in"][i], c["fade_out"][i],
                    c["fade_in_curve"][i], c["fade_out_curve"][i],
                    link_id=int(streams["link"][i] or 0), stream=streams["stream"][i] or None,
                    transform=transform_column[i] or None, transform_meta=meta_column[i] or None,
                    text=text_column[i] or None, shape=shape_column[i] or None,
                    gain=gain_column[i],
                    presets=presets_column[i] or None, blend=blend_column[i],
                    adjustment=bool(adjust_column[i]),
                    composition_id=composition_column[i] or "")
        track.clips.append(clip)
        if i in gain_by_clip:
            clip.gain = gain_by_clip[i]
        # Animated transform channels belong to the i-th clip written by the saver.
        for name, animated in transform_by_clip.get(i, {}).items():
            clip.transform[name] = animated
        if clip.text:
            # Text keyframes are held strings, so their frames come back from JSON as
            # strings and have to be integers again.
            settings = text_model.ensure_text(clip)
            settings["keys"] = {int(frame): str(words)
                                for frame, words in (settings.get("keys") or {}).items()}
            for name, animated in text_by_clip.get(i, {}).items():
                settings[name] = animated
        if clip.shape:
            # Shape colour keys come back from the container with string frames.
            settings = shape_model.ensure_shape(clip)
            for name in shape_model.COLOR_FIELDS:
                value = settings.get(name)
                if isinstance(value, dict):
                    settings[name] = {"keys": {int(frame): str(colour) for frame, colour
                                               in (value.get("keys") or {}).items()}}
            for name, animated in shape_by_clip.get(i, {}).items():
                settings[name] = animated

    timeline._clip_ids = itertools.count(max(tdata.get("max_clip", 0), max(c["id"], default=0)) + 1)
    timeline._link_ids = itertools.count(max((clip.link_id for clip in timeline.all_clips()), default=0) + 1)

    # Posterize Time used to store "Step" (frames per hold). Convert it with the project's
    # own rate here, so an older file holds exactly the frames it always did - animated
    # steps included - and the Frame Rate control shows the rate the picture is using.
    from ..effects.temporal import migrate_timeline
    migrate_timeline(timeline)

    m = sections["markers"]
    timeline.markers = [Marker(r["id"], r["frame"], r.get("name", ""), r.get("color")) for r in m["markers"]]
    timeline.markers.sort(key=lambda x: (x.frame, x.id))
    timeline._marker_ids = itertools.count(max((x.id for x in timeline.markers), default=0) + 1)
    timeline.loop_in, timeline.loop_out = m.get("loop_in"), m.get("loop_out")
    timeline.loop_enabled = bool(m.get("loop_enabled"))
    timeline.loop_at_end = bool(m.get("loop_at_end", True))
    return timeline


def compositions_to_sections(library, media_id_for):
    """Serialize every reusable composition, including its own full timeline."""
    rows = []
    for comp in library.all() if library is not None else ():
        rows.append({
            "id": comp.id,
            "name": comp.name,
            "width": comp.width,
            "height": comp.height,
            "fps": comp.fps,
            "background": list(comp.background),
            "duration": int(comp.declared_duration),
            "timeline": timeline_to_sections(comp.timeline, media_id_for),
        })
    return {"compositions": rows}


def sections_to_compositions(sections, path_for_media):
    """Build a CompositionLibrary from the project's compositions section."""
    library = CompositionLibrary()
    for row in (sections or {}).get("compositions") or ():
        try:
            comp = Composition(row["id"], row.get("name"), row.get("width", 1920),
                               row.get("height", 1080), row.get("fps", 30),
                               tuple(row.get("background") or (0, 0, 0)),
                               row.get("duration", 0))
            if row.get("timeline"):
                comp.timeline = sections_to_timeline(row["timeline"], comp.fps,
                                                     path_for_media)
            library.add(comp)
        except (KeyError, TypeError, ValueError):
            # A damaged composition row must not prevent the rest of the project from
            # opening; a missing reference simply renders as a missing nested clip.
            continue
    return library
