"""Turn the timeline into mixer segments. Runs on the UI thread, pure otherwise."""

import os
from collections import OrderedDict

import numpy as np

from ..constants import AUDIO_EXT, VIDEO_EXT
from ..model.keyframes import AnimatedValue
from . import dsp
from .effects import apply_chain, chain_signature, has_audio_effects
from .mix import Segment, fade_envelope

AUDIO_CAPABLE = tuple(AUDIO_EXT) + tuple(VIDEO_EXT)

# Processed sources, keyed by path + chain: rebuilding a filter chain is cheap next to
# decoding, but it must not happen on every seek. Eight entries is plenty for a project
# with a handful of treated clips and keeps the memory cost predictable.
_PROCESSED = OrderedDict()
_PROCESSED_LIMIT = 8


def wants_audio(path):
    """True when a file may carry an audio stream worth decoding."""
    return bool(path) and os.path.splitext(path)[1].lower() in AUDIO_CAPABLE


def clip_segment(clip, samples, rate, timeline_fps, gain, envelopes=()):
    """Map one clip onto its decoded samples, or None when it cannot contribute audio."""
    if samples is None or getattr(samples, "size", 0) == 0:
        return None
    if clip.freeze or clip.duration <= 0:
        return None
    span = clip.source_out - clip.source_in
    if span <= 0:
        return None
    source_fps = float(clip.source_fps or timeline_fps) or float(timeline_fps)
    if source_fps <= 0:
        return None
    if clip.reverse:
        base_frames = clip.source_out - 1 - clip.source_offset
        direction = -1
    else:
        base_frames = clip.source_in + clip.source_offset
        direction = 1
    fps = float(timeline_fps) or 1.0
    base_gain = max(0.0, float(gain))
    # Clip gain (dB, possibly animated) is part of the signal here, which is what makes
    # the inspector, the timeline envelope, playback and the audio export agree.
    gain_now = base_gain * float(clip.gain_linear_at(clip.timeline_start))
    points = ()
    if isinstance(getattr(clip, "gain", None), AnimatedValue):
        # ~15 automation samples per second: linear interpolation between them is well
        # inside a tenth of a dB anywhere on the curve, and playback stays cheap.
        step = max(1, int(round(fps / 15.0)))
        samples_per = []
        frame = int(clip.timeline_start)
        while frame < int(clip.end):
            samples_per.append((frame / fps,
                                base_gain * float(clip.gain_linear_at(frame))))
            frame += step
        last = max(int(clip.timeline_start), int(clip.end) - 1)
        samples_per.append((int(clip.end) / fps,
                            base_gain * float(clip.gain_linear_at(last))))
        points = tuple(samples_per)
    transition_in = ()
    transition_out = ()
    for start, end, curve, role in envelopes or ():
        if role == "in":
            transition_in = (start, end, curve)
        elif role == "out":
            transition_out = (start, end, curve)
    return Segment(
        start_seconds=clip.timeline_start / fps,
        end_seconds=clip.end / fps,
        source=samples,
        rate=int(rate),
        base_seconds=base_frames / source_fps,
        speed=float(clip.speed or 1.0),
        direction=direction,
        loop_start=clip.source_in / source_fps,
        loop_length=(span / source_fps) if clip.loop else 0.0,
        gain=1.0 if points else gain_now,
        gain_points=points,
        # Fade lengths travel with the segment; the mixer turns them into a per-sample ramp.
        fade_in_seconds=max(0.0, float(getattr(clip, "fade_in", 0))) / fps,
        fade_out_seconds=max(0.0, float(getattr(clip, "fade_out", 0))) / fps,
        fade_in_curve=float(getattr(clip, "fade_in_curve", 0.0)),
        fade_out_curve=float(getattr(clip, "fade_out_curve", 0.0)),
        transition_in=transition_in,
        transition_out=transition_out,
    )


def audible_clips(timeline):
    """(clip, gain) pairs that should be heard, honouring mute, solo and visibility."""
    solo_audio = any(track.solo for track in timeline.tracks if track.type == "audio")
    result = []
    for track in timeline.tracks:
        if track.muted:
            continue
        if track.type == "audio":
            if solo_audio and not track.solo:
                continue
        else:
            if not track.visible or solo_audio:
                continue
        for clip in track.clips:
            # A nested composition is audible through its own contents, not through a
            # source file: include it so build_segments can route the children.
            if not getattr(clip, "is_composition", False) and not clip.source_path:
                continue
            # A video clip that has a linked audio clip is silent itself: the sound lives on
            # the audio lane, so one source can never be heard twice. Clips loaded from older
            # projects carry the "both" stream and still play their own audio. A composition
            # clip is an exception: its stream is "video" (it has no file) but its children's
            # audio is exactly what the parent timeline should hear.
            if not getattr(clip, "is_composition", False) \
                    and not getattr(clip, "carries_audio", True):
                continue
            gain = max(0.0, float(getattr(clip, "volume", 1.0))) * max(0.0, float(getattr(track, "volume", 1.0)))
            result.append((clip, gain))
    return result


def clear_processed_cache():
    """Drop rebuilt sources (project close, or a test that reuses a synthetic buffer)."""
    _PROCESSED.clear()


def processed_samples(clip, samples, rate, resolver, fps):
    """The clip's samples with its audio effect chain applied (cached)."""
    if samples is None or resolver is None:
        return samples
    entries = getattr(clip, "effects", None) or ()
    if not has_audio_effects(entries, resolver):
        return samples
    # The cache entry keeps a reference to the decoded array, so its id() cannot be
    # reused by another audio buffer while the entry is alive - that is what makes it
    # safe to identify the source by identity instead of by path alone.
    key = (clip.source_path, int(rate), id(samples), int(getattr(samples, "shape", (0,))[0]),
           chain_signature(entries))
    cached = _PROCESSED.get(key)
    if cached is not None:
        _PROCESSED.move_to_end(key)
        return cached[1]
    processed = apply_chain(samples, rate, entries, resolver, fps=fps, start_frame=0)
    # A stack of effects must never hand a clipped signal to the encoder.
    if processed.size and dsp.peak_db(processed) > 0.0:
        processed = dsp.soft_clip(processed)
    _PROCESSED[key] = (samples, processed)
    while len(_PROCESSED) > _PROCESSED_LIMIT:
        _PROCESSED.popitem(last=False)
    return processed


def _remap_nested_segment(segment, offset, speed, parent_gain, clip, parent_fps, rate):
    """Rebase a composition's segment onto the parent timeline.

    The child's own gain automation, fades and transition windows are preserved and the
    parent composition clip's volume and fades are multiplied on top, so nested audio
    respects every level's mixer settings and is heard exactly once.
    """
    speed = max(1e-6, float(speed or 1.0))
    start = offset + segment.start_seconds / speed
    end = offset + segment.end_seconds / speed
    if end <= start:
        return None
    duration = end - start
    step = max(1.0 / 40.0, duration / 20000.0)
    times = np.arange(start, end + step * 0.5, step, dtype=np.float64)
    if times.size == 0:
        return None
    nested_times = offset + (times - offset) * speed
    base_gain = float(segment.gain)
    if segment.gain_points:
        base_gain = np.interp(nested_times,
                              np.asarray([point[0] for point in segment.gain_points],
                                         dtype=np.float64),
                              np.asarray([point[1] for point in segment.gain_points],
                                         dtype=np.float64))
    nested_fade = 1.0
    if segment.fade_in_seconds > 0 or segment.fade_out_seconds > 0:
        nested_fade = fade_envelope(nested_times, segment.start_seconds, segment.end_seconds,
                                    segment.fade_in_seconds, segment.fade_out_seconds,
                                    segment.fade_in_curve, segment.fade_out_curve)
    parent_fade = 1.0
    fade_in = max(0.0, float(getattr(clip, "fade_in", 0) or 0)) / float(parent_fps or 30.0)
    fade_out = max(0.0, float(getattr(clip, "fade_out", 0) or 0)) / float(parent_fps or 30.0)
    if fade_in > 0 or fade_out > 0:
        parent_fade = fade_envelope(times, start, end, fade_in, fade_out,
                                    float(getattr(clip, "fade_in_curve", 0.0) or 0.0),
                                    float(getattr(clip, "fade_out_curve", 0.0) or 0.0))
    # `base_gain` is a scalar unless the child had gain automation; broadcasting it over
    # the sampled times keeps both cases one array.
    values = (np.asarray(base_gain, dtype=np.float64) * np.ones_like(times)
              * nested_fade * parent_fade * float(parent_gain or 0.0))
    points = tuple((float(t), float(v)) for t, v in zip(times, values))
    return Segment(
        start_seconds=float(start), end_seconds=float(end), source=segment.source,
        rate=segment.rate, base_seconds=segment.base_seconds, speed=segment.speed,
        direction=segment.direction, loop_start=segment.loop_start,
        loop_length=segment.loop_length, gain=1.0, gain_points=points,
        fade_in_seconds=0.0, fade_out_seconds=0.0,
        transition_in=(tuple((bound + offset, end_, curve)
                             for bound, end_, curve in (segment.transition_in,))
                       if segment.transition_in else ()),
        transition_out=(tuple((bound + offset, end_, curve)
                              for bound, end_, curve in (segment.transition_out,))
                        if segment.transition_out else ()),
    )


def build_segments(timeline, audio_for, rate, resolver=None, only_ids=None,
                   compositions=None, _stack=()):
    """Build mixer segments for every audible clip that already has decoded audio.

    `resolver` is the engine's effect registry (``{key: effect}``); with it, a clip's
    audio effect chain is applied to its samples before mixing, which is what makes the
    effects audible in playback and present in the exported audio. `only_ids` restricts
    the result to those clips, which is how Auto Captions analyses one clip or one track
    instead of the whole mix.

    A nested composition contributes its children's segments, shifted into the parent's
    time by the composition clip's position, trimmed by its source in-point and scaled by
    its speed. The composition clip is never also decoded as media, so its sound cannot
    play twice.
    """
    allowed = set(only_ids) if only_ids is not None else None
    stack = set(_stack or ())
    segments = []
    for clip, gain in audible_clips(timeline):
        if allowed is not None and clip.id not in allowed:
            continue
        if getattr(clip, "is_composition", False):
            library = compositions
            comp = library.get(clip.composition_id) if library is not None else None
            if comp is None or comp.id in stack:
                continue
            speed = float(getattr(clip, "speed", 1.0) or 1.0)
            if speed <= 0 or getattr(clip, "reverse", False):
                # A reversed or frozen nested mix would need its whole child mix reversed;
                # leaving it silent is honest, and the picture still renders.
                continue
            nested = build_segments(comp.timeline, audio_for, rate, resolver,
                                    None, library, stack | {comp.id})
            offset = clip.timeline_start / float(timeline.fps or 30.0)
            offset -= (float(clip.source_in) / float(comp.fps or timeline.fps or 30.0)) / speed
            for segment in nested:
                remapped = _remap_nested_segment(segment, offset, speed, gain, clip,
                                                 timeline.fps, rate)
                if remapped is not None:
                    segments.append(remapped)
            continue
        samples = audio_for(clip.source_path)
        samples = processed_samples(clip, samples, rate, resolver, timeline.fps)
        envelopes = timeline.audio_transition_envelopes(clip)
        segment = clip_segment(clip, samples, rate, timeline.fps, gain, envelopes)
        if segment is not None:
            segments.append(segment)
    return segments


def missing_audio_paths(timeline, audio_for, compositions=None, _stack=()):
    """Audible sources whose audio has not been decoded yet (drives background loads)."""
    pending = []
    stack = set(_stack or ())
    for clip, _gain in audible_clips(timeline):
        if getattr(clip, "is_composition", False):
            comp = compositions.get(clip.composition_id) if compositions is not None else None
            if comp is None or comp.id in stack:
                continue
            for path in missing_audio_paths(comp.timeline, audio_for, compositions,
                                            stack | {comp.id}):
                if path not in pending:
                    pending.append(path)
            continue
        if wants_audio(clip.source_path) and audio_for(clip.source_path) is None:
            if clip.source_path not in pending:
                pending.append(clip.source_path)
    return pending
