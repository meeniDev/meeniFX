"""Sample-accurate mixing math for timeline audio. Pure numpy, no Qt, no widgets.

A Segment is one clip's contribution over its timeline span. Segments are immutable
snapshots built on the UI thread and read from the audio callback, so playback never
touches the live timeline model.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Segment:
    """One clip's audio mapped from timeline seconds onto its source samples."""

    start_seconds: float
    end_seconds: float
    source: np.ndarray          # (frames, channels) float32
    rate: int                   # source sample rate
    base_seconds: float         # source time for the segment's first instant
    speed: float = 1.0
    direction: int = 1          # -1 for a reversed clip
    loop_start: float = 0.0     # source seconds
    loop_length: float = 0.0    # 0 disables looping
    gain: float = 1.0
    # Clip gain automation: ((timeline seconds, linear gain), ...) sampled from the
    # clip's gain keyframes. Empty = the constant `gain` above, which is exactly what a
    # static gain clip stores.
    gain_points: tuple = ()
    fade_in_seconds: float = 0.0
    fade_out_seconds: float = 0.0
    fade_in_curve: float = 0.0
    fade_out_curve: float = 0.0
    # Overlap transition windows in timeline seconds: (start, end, curve). They multiply
    # the clip's own fades and gain automation, so a crossfade adds to an existing fade
    # instead of replacing it.
    transition_in: tuple = ()
    transition_out: tuple = ()

    def source_seconds(self, timeline_seconds):
        """Map timeline seconds to source seconds (forward, reverse and loop aware)."""
        elapsed = (timeline_seconds - self.start_seconds) * self.speed * self.direction
        position = self.base_seconds + elapsed
        if self.loop_length > 0:
            position = self.loop_start + np.mod(position - self.loop_start, self.loop_length)
        return position


def transition_shape(progress, curve="equal_power"):
    """Vectorised incoming-gain curve for an overlap transition.

    Equal power uses sin(t*pi/2), so the outgoing cos(t*pi/2) and incoming sin(t*pi/2)
    weights keep summed power constant through the crossfade. Linear is the plain ramp.
    """
    x = np.clip(np.asarray(progress, dtype=np.float64), 0.0, 1.0)
    if str(curve or "").strip().lower() == "linear":
        return x
    return np.sin(x * np.pi / 2.0)


def fade_shape(progress, curve=0.0):
    """The clip fade curve, vectorised: 0 at the start of a fade, 1 at its end.

    Mirrors model.timeline.fade_gain, so the drawn curve, the video opacity and the
    audio envelope are the same function evaluated at different resolutions.
    """
    x = np.clip(np.asarray(progress, dtype=np.float64), 0.0, 1.0)
    a = max(-1.0, min(1.0, float(curve or 0.0)))
    return x + a * x * (1.0 - x) * (2.0 * x - 1.0)


def fade_envelope(times, start_seconds, end_seconds, fade_in, fade_out, curve_in=0.0, curve_out=0.0):
    """Per-sample fade weights for a clip span (float32, 0..1)."""
    weights = np.ones(len(times), dtype=np.float64)
    if fade_in > 0:
        weights *= fade_shape((np.asarray(times) - start_seconds) / fade_in, curve_in)
    if fade_out > 0:
        weights *= fade_shape((end_seconds - np.asarray(times)) / fade_out, curve_out)
    return weights.astype(np.float32)


def empty_block(count, channels):
    return np.zeros((max(0, int(count)), max(1, int(channels))), dtype=np.float32)


def render(segments, start_seconds, count, rate, channels=2):
    """Render `count` interleaved-free float32 frames starting at `start_seconds`."""
    out = empty_block(count, channels)
    if count <= 0 or not segments:
        return out
    times = start_seconds + np.arange(count, dtype=np.float64) / float(rate)
    for segment in segments:
        first = int(np.floor((segment.start_seconds - start_seconds) * rate))
        last = int(np.ceil((segment.end_seconds - start_seconds) * rate))
        first, last = max(0, first), min(count, last)
        if last <= first:
            continue
        source = segment.source
        if source is None or source.size == 0:
            continue
        if source.ndim == 1:
            source = source[:, None]
        frames = source.shape[0]
        position = np.asarray(segment.source_seconds(times[first:last]), dtype=np.float64) * segment.rate
        position = np.clip(position, 0.0, frames - 1)
        index = np.floor(position).astype(np.int64)
        frac = (position - index).astype(np.float32)
        nxt = np.minimum(index + 1, frames - 1)
        gain = float(segment.gain)
        # Fades and gain automation both ramp inside the segment per sample, so playback,
        # scrub bursts and the audio export all get the exact same click-free envelope.
        weights = None
        if segment.fade_in_seconds > 0 or segment.fade_out_seconds > 0:
            weights = fade_envelope(times[first:last], segment.start_seconds, segment.end_seconds,
                                    segment.fade_in_seconds, segment.fade_out_seconds,
                                    segment.fade_in_curve, segment.fade_out_curve)
        if segment.gain_points:
            points = segment.gain_points
            auto = np.interp(times[first:last],
                             np.asarray([point[0] for point in points], dtype=np.float64),
                             np.asarray([point[1] for point in points], dtype=np.float64))
            weights = auto if weights is None else weights * auto
            # The sampled automation already carries the clip gain, so the constant gain
            # must not be applied a second time.
            gain = 1.0
        # Overlap crossfades ride on top of everything else. The outgoing clip follows
        # 1 - shape and the incoming clip shape, so the pair sums to a click-free motion
        # and a one-sided pair (only one clip has sound) still fades predictably.
        for window, incoming in ((segment.transition_out, False),
                                 (segment.transition_in, True)):
            if not window:
                continue
            try:
                window_start, window_end, curve = window
            except (TypeError, ValueError):
                continue
            if window_end <= window_start:
                continue
            progress = ((times[first:last] - float(window_start))
                        / float(window_end - window_start))
            shape = transition_shape(progress, curve)
            if not incoming:
                if str(curve or "").strip().lower() == "linear":
                    shape = 1.0 - shape
                else:
                    # cos(t*pi/2) = sqrt(1 - sin(t*pi/2)^2): the pair keeps summed power.
                    shape = np.sqrt(np.clip(1.0 - shape * shape, 0.0, 1.0))
            weights = shape if weights is None else weights * shape
        if weights is not None:
            weights = (weights * gain).astype(np.float32)
        else:
            weights = None
        for channel in range(channels):
            column = source[:, min(channel, source.shape[1] - 1)]
            sample = column[index] * (1.0 - frac) + column[nxt] * frac
            out[first:last, channel] += sample * (gain if weights is None else weights)
    return out


def peak_of(block):
    """Peak absolute level of a rendered block (0..n)."""
    if block.size == 0:
        return 0.0
    return float(np.max(np.abs(block)))


def to_pcm16(block):
    """Convert a float block to little-endian interleaved int16 bytes."""
    clipped = np.clip(block, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def resample(samples, source_rate, target_rate):
    """Linear resample of (frames, channels) float32 audio."""
    if samples is None or samples.size == 0 or source_rate == target_rate:
        return samples
    frames = samples.shape[0]
    if frames == 0:
        return samples
    duration = frames / float(source_rate)
    target_frames = max(1, int(round(duration * target_rate)))
    positions = np.linspace(0.0, frames - 1, target_frames, dtype=np.float64)
    index = np.floor(positions).astype(np.int64)
    frac = (positions - index).astype(np.float32)
    nxt = np.minimum(index + 1, frames - 1)
    if samples.ndim == 1:
        return (samples[index] * (1.0 - frac) + samples[nxt] * frac).astype(np.float32)
    out = np.empty((target_frames, samples.shape[1]), dtype=np.float32)
    for channel in range(samples.shape[1]):
        column = samples[:, channel]
        out[:, channel] = column[index] * (1.0 - frac) + column[nxt] * frac
    return out


def to_stereo(samples):
    """Normalise any decoded array to (frames, 2) float32."""
    if samples is None:
        return None
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    if data.shape[1] == 1:
        return np.repeat(data, 2, axis=1)
    if data.shape[1] > 2:
        return data[:, :2]
    return data
