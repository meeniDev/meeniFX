"""Turn the timeline into mixer segments. Runs on the UI thread, pure otherwise."""

import os
from collections import OrderedDict

from ..constants import AUDIO_EXT, VIDEO_EXT
from ..model.keyframes import AnimatedValue
from . import dsp
from .effects import apply_chain, chain_signature, has_audio_effects
from .mix import Segment

AUDIO_CAPABLE = tuple(AUDIO_EXT) + tuple(VIDEO_EXT)

# Processed sources, keyed by path + chain: rebuilding a filter chain is cheap next to
# decoding, but it must not happen on every seek. Eight entries is plenty for a project
# with a handful of treated clips and keeps the memory cost predictable.
_PROCESSED = OrderedDict()
_PROCESSED_LIMIT = 8


def wants_audio(path):
    """True when a file may carry an audio stream worth decoding."""
    return bool(path) and os.path.splitext(path)[1].lower() in AUDIO_CAPABLE


def clip_segment(clip, samples, rate, timeline_fps, gain):
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
            if not clip.source_path:
                continue
            # A video clip that has a linked audio clip is silent itself: the sound lives on
            # the audio lane, so one source can never be heard twice. Clips loaded from older
            # projects carry the "both" stream and still play their own audio.
            if not getattr(clip, "carries_audio", True):
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


def build_segments(timeline, audio_for, rate, resolver=None, only_ids=None):
    """Build mixer segments for every audible clip that already has decoded audio.

    `resolver` is the engine's effect registry (``{key: effect}``); with it, a clip's
    audio effect chain is applied to its samples before mixing, which is what makes the
    effects audible in playback and present in the exported audio. `only_ids` restricts
    the result to those clips, which is how Auto Captions analyses one clip or one track
    instead of the whole mix.
    """
    allowed = set(only_ids) if only_ids is not None else None
    segments = []
    for clip, gain in audible_clips(timeline):
        if allowed is not None and clip.id not in allowed:
            continue
        samples = audio_for(clip.source_path)
        samples = processed_samples(clip, samples, rate, resolver, timeline.fps)
        segment = clip_segment(clip, samples, rate, timeline.fps, gain)
        if segment is not None:
            segments.append(segment)
    return segments


def missing_audio_paths(timeline, audio_for):
    """Audible sources whose audio has not been decoded yet (drives background loads)."""
    pending = []
    for clip, _gain in audible_clips(timeline):
        if wants_audio(clip.source_path) and audio_for(clip.source_path) is None:
            if clip.source_path not in pending:
                pending.append(clip.source_path)
    return pending
