"""Roto Brush backend: brush strokes, segmentation, mask cache and propagation.

A *segmenter* turns a frame and a set of positive/negative brush hints into a soft matte,
a *cache* keeps those mattes per source frame, and *propagation* carries a mask to
neighbouring frames.

The preferred segmenter is U^2-Net (Apache-2.0 ONNX weights, downloaded on demand, CPU
inference). The classical OpenCV path (GrabCut seeded by the strokes plus guided-filter
edge refinement) remains as an explicitly labelled Basic fallback. Neither path performs
learned video-object memory; propagation uses optical flow and a per-frame model pass.
See `docs/ROTO_BRUSH.md` for the model choice and remaining limitations.
"""

from .cache import MaskCache, mask_key, mask_size
from .model import (DEFAULT_MODEL, MODEL_VERSION, U2NET, download_model, get_model,
                    model_path, model_signature, model_status)
from .segment import (decontaminate, finish_matte, guided_filter, outline_contours,
                      segment_frame)
from .subject import basic_fallback_note, choose_component, segment_subject
from .strokes import (MODES, interpolate, rasterise, stroke_mode, stroke_points,
                      stroke_radius, strokes_for_frame)

__all__ = [
    "MaskCache", "mask_key", "mask_size",
    "DEFAULT_MODEL", "MODEL_VERSION", "U2NET", "download_model", "get_model",
    "model_path", "model_signature", "model_status", "basic_fallback_note",
    "choose_component", "segment_subject",
    "decontaminate", "finish_matte", "guided_filter", "outline_contours", "segment_frame",
    "MODES", "interpolate", "rasterise", "stroke_mode", "stroke_points", "stroke_radius",
    "strokes_for_frame",
]
