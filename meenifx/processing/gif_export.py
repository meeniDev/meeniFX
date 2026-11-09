"""GIF timing and encoding helpers.

GIF stores each frame delay in centiseconds.  The exporter must therefore work in
centiseconds from the first frame, not convert a per-frame floating-point duration in
isolation: rounding each frame separately accumulates drift and makes a five second
animation play for a different length of time.

Timing here always comes from the timeline duration passed in by the engine.  Encoding
time and rendering speed are deliberately not part of the calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

import numpy as np
from PIL import Image


# A 2 cs delay is the shortest delay that common browsers and desktop viewers render at
# the intended speed.  Many viewers turn 0-1 cs into 10 cs, which is what made the old
# zero-delay GIFs play roughly three times too slowly.
MIN_DELAY_CS = 2
MAX_RELIABLE_FPS = 100.0 / MIN_DELAY_CS


@dataclass(frozen=True)
class GifTiming:
    """The frame plan for one GIF export."""

    requested_fps: float
    effective_fps: float
    frame_count: int
    delays_cs: tuple[int, ...]
    duration_seconds: float
    requested_duration_seconds: float
    clamped: bool


def plan_timing(duration_seconds, requested_fps, min_delay_cs=MIN_DELAY_CS) -> GifTiming:
    """Plan frame count and centisecond delays for a timeline duration.

    ``duration_seconds`` is the intended playback duration.  The returned delays sum to
    the closest whole number of centiseconds; any rounding remainder is spread across
    the frames instead of being applied independently to every frame.
    """
    try:
        duration = max(0.001, float(duration_seconds))
    except (TypeError, ValueError):
        duration = 0.001
    try:
        requested = max(0.001, float(requested_fps))
    except (TypeError, ValueError):
        requested = 30.0

    minimum = max(1, int(min_delay_cs))
    total_cs = max(1, int(round(duration * 100.0)))
    desired_frames = max(1, int(round(duration * requested)))

    # Keep every stored delay at or above the viewer-safe minimum.  This caps GIF output
    # at 50 fps while preserving the timeline duration: frames are resampled, not slowed.
    frame_count = min(desired_frames, max(1, total_cs // minimum))
    total_cs = max(total_cs, frame_count * minimum)

    # Bresenham-style cumulative boundaries distribute the fractional remainder over
    # the whole sequence.  For example, 5 s at 24 fps becomes a 4/5 cs pattern whose sum
    # is exactly 500 cs, and 23.976 fps keeps its 4/5 cs pattern without drifting.
    boundaries = [(index * total_cs) // frame_count for index in range(frame_count + 1)]
    delays = tuple(boundaries[index + 1] - boundaries[index]
                   for index in range(frame_count))
    actual_duration = total_cs / 100.0
    return GifTiming(
        requested_fps=requested,
        effective_fps=frame_count / actual_duration,
        frame_count=frame_count,
        delays_cs=delays,
        duration_seconds=actual_duration,
        requested_duration_seconds=duration,
        clamped=frame_count < desired_frames,
    )


def sample_source_indices(indices, frame_count):
    """Choose deterministic source frames for the planned GIF frame count.

    ``indices`` is the engine's timeline-derived render sequence (including speed and
    reverse).  Each output frame represents a timestamp in that sequence, so sampling is
    based on elapsed playback position rather than on wall-clock rendering time.
    """
    source = list(indices or ())
    if not source:
        return []
    count = max(1, int(frame_count))
    if count == 1:
        return [source[0]]
    last = len(source) - 1
    # Output frame i starts at i / count of the source sequence.  Rounding to the nearest
    # source frame keeps keyframes and moving objects aligned without accumulating error.
    return [source[min(last, int(round(index * len(source) / count)))]
            for index in range(count)]


def loop_parameter(mode="forever", custom=2):
    """Translate the UI loop mode into Pillow's Netscape loop value.

    ``None`` means no loop extension, which is a single play in viewers.  ``0`` means
    forever, and a positive value is the number of additional loops stored in the GIF.
    """
    if isinstance(mode, int):
        return max(0, mode)
    value = str(mode or "forever").strip().lower()
    if value == "once":
        return None
    if value == "custom":
        try:
            return max(1, int(custom))
        except (TypeError, ValueError):
            return 1
    return 0


def write_gif(path, frames, delays_cs, *, loop=0, colors=256, dither="floyd"):
    """Write BGR frames as a GIF with exact per-frame centisecond delays.

    Frames are quantised explicitly so the colour-quality and dithering controls are
    real encoder settings rather than cosmetic options.  The file is written through a
    temporary neighbour and replaced only after a complete encode.
    """
    frames = list(frames or ())
    delays = [max(1, int(delay)) for delay in delays_cs or ()]
    if not frames:
        raise ValueError("No GIF frames to write.")
    if len(frames) != len(delays):
        raise ValueError("GIF frame and delay counts do not match.")

    try:
        palette_size = max(2, min(256, int(colors)))
    except (TypeError, ValueError):
        palette_size = 256
    dither_mode = (Image.Dither.FLOYDSTEINBERG
                   if str(dither or "floyd").lower() in {"floyd", "floyd-steinberg", "diffusion"}
                   else Image.Dither.NONE)

    images = []
    for frame in frames:
        array = np.asarray(frame)
        if array.ndim == 2:
            rgb = np.repeat(array[:, :, None], 3, axis=2)
        else:
            rgb = array[:, :, 2::-1]  # BGR -> RGB, dropping alpha if present
        image = Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8), "RGB")
        images.append(image.quantize(colors=palette_size, method=Image.Quantize.MEDIANCUT,
                                     dither=dither_mode))

    kwargs = {
        "save_all": True,
        "append_images": images[1:],
        "duration": [delay * 10 for delay in delays],  # Pillow takes milliseconds
        "disposal": 2,
        "optimize": False,
    }
    if loop is not None:
        kwargs["loop"] = int(loop)

    temporary = str(path) + ".tmp.gif"
    try:
        images[0].save(temporary, format="GIF", **kwargs)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass
