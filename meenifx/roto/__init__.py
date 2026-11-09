"""Roto Brush backend: brush strokes, segmentation, mask cache and propagation.

The interactive model here is deliberately model-agnostic: a *segmenter* turns a frame
and a set of positive/negative brush hints into a soft matte, a *cache* keeps those
mattes per source frame, and *propagation* carries a mask to neighbouring frames.

The shipped segmenter is the classical OpenCV path (GrabCut seeded by the strokes plus
an edge-aware guided-filter refinement). It is honest about what it is: stroke-guided
classical segmentation, not a learned video-segmentation model. See `segment.py` for the
list of limitations and `docs/ROTO_BRUSH.md` for the evaluated model options.
"""

from .cache import MaskCache, mask_key, mask_size
from .segment import (decontaminate, finish_matte, guided_filter, outline_contours,
                      segment_frame)
from .strokes import (MODES, interpolate, rasterise, stroke_mode, stroke_points,
                      stroke_radius, strokes_for_frame)

__all__ = [
    "MaskCache", "mask_key", "mask_size",
    "decontaminate", "finish_matte", "guided_filter", "outline_contours", "segment_frame",
    "MODES", "interpolate", "rasterise", "stroke_mode", "stroke_points", "stroke_radius",
    "strokes_for_frame",
]
