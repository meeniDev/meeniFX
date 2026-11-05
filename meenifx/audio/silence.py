"""Silence detection for Remove Silences.

Pure numpy: the dialog analyses a decoded clip, draws the proposed cuts and only then asks
the session to edit the timeline. Nothing here touches media files, and the original audio
is never rewritten - the cuts are ordinary trims/splits that undo removes.
"""

from dataclasses import dataclass

import numpy as np

WINDOW = 0.02          # seconds per analysis window
HOP = 0.01


@dataclass
class Settings:
    threshold_db: float = -38.0
    min_silence: float = 0.45
    min_speech: float = 0.22
    pad_before: float = 0.10
    pad_after: float = 0.10
    keep_pause: float = 0.0        # seconds of each gap kept (0 = remove it completely)
    crossfade: bool = True
    ripple: bool = True
    min_retained: float = 0.0      # seconds kept between cuts; shorter fragments are swallowed


PRESETS = {
    "gentle": Settings(-42.0, 0.65, 0.18, 0.15, 0.18, 0.32, True, True, 0.12),
    "balanced": Settings(-38.0, 0.45, 0.22, 0.10, 0.10, 0.18, True, True, 0.10),
    "aggressive": Settings(-32.0, 0.30, 0.30, 0.05, 0.06, 0.0, True, True, 0.08),
}

PRESET_ORDER = (("Gentle", "gentle"), ("Balanced", "balanced"),
                ("Aggressive", "aggressive"), ("Custom", "custom"))


def envelope(samples, rate, window=WINDOW, hop=HOP):
    """(times, dBFS) of short windows: the level curve shown in the dialog."""
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if audio.size == 0:
        return np.zeros(0), np.zeros(0)
    rate = max(1, int(rate))
    size = max(1, int(round(window * rate)))
    step = max(1, int(round(hop * rate)))
    count = max(1, 1 + (audio.size - size) // step)
    starts = np.arange(count) * step
    block = np.lib.stride_tricks.sliding_window_view(audio, size)[::step][:count]
    rms = np.sqrt(np.mean(block.astype(np.float64) ** 2, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))
    times = (starts + size / 2.0) / float(rate)
    return times, db


def speech_regions(samples, rate, settings):
    """[(start, end)] seconds where the level stays above the threshold long enough."""
    times, db = envelope(samples, rate)
    if times.size == 0:
        return []
    loud = db >= float(settings.threshold_db)
    regions = []
    start = None
    for index, flag in enumerate(loud):
        # A window is the middle of its span; the region starts half a window earlier.
        edge = times[index] - WINDOW / 2.0
        if flag and start is None:
            start = edge
        elif not flag and start is not None:
            regions.append([start, edge + WINDOW])
            start = None
    if start is not None:
        regions.append([start, float(times[-1] + WINDOW / 2.0)])
    # Drop blips shorter than the minimum speech duration (clicks, room noise).
    regions = [region for region in regions if region[1] - region[0] >= settings.min_speech]
    merged = []
    for region in regions:
        if merged and region[0] - merged[-1][1] < 0.02:
            merged[-1][1] = max(merged[-1][1], region[1])
        else:
            merged.append(region)
    return [(max(0.0, start), max(0.0, end)) for start, end in merged]


def cut_plan(speech, duration, settings):
    """Gaps to remove, padding the speech, and never cutting inside a kept pause."""
    duration = max(0.0, float(duration))
    cuts = []
    cursor = 0.0
    for start, end in speech:
        start = min(duration, max(0.0, start - settings.pad_before))
        end = min(duration, max(start, end + settings.pad_after))
        if start - cursor > 0.001:
            gap_start, gap_end = cursor, start
            if gap_end - gap_start >= settings.min_silence:
                if settings.keep_pause > 0:
                    # Keep a natural breath before the next line; the rest of the gap goes.
                    gap_end = max(gap_start, gap_end - settings.keep_pause)
                if gap_end - gap_start > 0.02:
                    cuts.append((gap_start, gap_end))
        cursor = max(cursor, end)
    if duration - cursor > 0.001 and duration - cursor >= settings.min_silence:
        tail_end = duration - max(0.0, settings.keep_pause)
        if tail_end - cursor > 0.02:
            cuts.append((cursor, tail_end))
    return _enforce_min_retained(cuts, duration, settings.min_retained)


def _enforce_min_retained(cuts, duration, minimum):
    """Swallow kept fragments shorter than `minimum` seconds.

    A 20 ms fragment between two cuts is invisible to the viewer and leaves a clip too
    short to trim, so it is merged into the neighbouring cuts instead. The first and last
    retained spans are extended by moving the outer cut edges to the clip ends.
    """
    minimum = float(minimum or 0.0)
    if minimum <= 0.0 or not cuts:
        return list(cuts)
    # Compare with a hair of slack so a 0.2 s span does not count as shorter than 0.2 s
    # merely because of binary rounding in the seconds above.
    minimum = max(0.0, minimum - 1e-9)
    spans = [[float(first), float(last)] for first, last in cuts]
    changed = True
    while changed and spans:
        changed = False
        if 0.0 < spans[0][0] < minimum:
            spans[0][0] = 0.0
            changed = True
        if 0.0 < duration - spans[-1][1] < minimum:
            spans[-1][1] = duration
            changed = True
        index = 0
        while index < len(spans) - 1:
            if spans[index + 1][0] - spans[index][1] < minimum:
                spans[index][1] = max(spans[index][1], spans[index + 1][1])
                del spans[index + 1]
                changed = True
            else:
                index += 1
        spans = [span for span in spans if span[1] - span[0] > 0.02]
    return [(first, last) for first, last in spans]


def total_removed(cuts):
    return float(sum(end - start for start, end in cuts))
