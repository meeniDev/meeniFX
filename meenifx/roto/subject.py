"""Foreground subject segmentation for Roto Brush.

The preferred backend is the optional U^2-Net ONNX model. It predicts a salient-object
matte from the picture itself, so an ordinary room, street or garden needs no colour key.
Brush strokes are then used as corrections: green regions are forced into the matte, red
regions are forced out, and GrabCut is used only to fit those corrections to the local
pixels. GrabCut alone remains the Basic fallback.

Video frames receive the optical-flow warped previous matte as `prior`. The model is run
again on every frame, which keeps a moving subject's matte attached to the subject instead
of copying the base frame's shape for the whole clip.
"""

from __future__ import annotations

import cv2
import numpy as np

from .model import get_model, model_status
from .segment import (_border_mask, _colour_matte, _grabcut, finish_matte,
                      guided_filter, segment_frame)


def _component_masks(matte, threshold=0.45, minimum_ratio=0.14):
    binary = np.where(np.asarray(matte, np.float32) >= float(threshold), 255, 0).astype(np.uint8)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    if count <= 1:
        return []
    areas = [(int(stats[index, cv2.CC_STAT_AREA]), index) for index in range(1, count)]
    largest = max((area for area, _index in areas), default=0)
    if largest <= 0:
        return []
    keep = []
    for area, index in sorted(areas, reverse=True):
        if area < max(24, int(largest * float(minimum_ratio))):
            continue
        keep.append((area, index, centroids[index]))
    return keep


def choose_component(matte, point=None, threshold=0.45):
    """Keep the main subject, or the connected subject nearest a chosen point.

    With no point, meaningful components are retained. That keeps two pets or two people
    when the model sees both, while dropping tiny saliency specks. A chosen point narrows
    the result to that one component, which is the multiple-subject control.
    """
    matte = np.asarray(matte, np.float32)
    components = _component_masks(matte, threshold=threshold)
    if not components:
        return np.clip(matte, 0.0, 1.0)
    height, width = matte.shape[:2]
    binary = np.where(matte >= float(threshold), 255, 0).astype(np.uint8)
    count, labels, _stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    if point is None:
        chosen = [index for _area, index, _centre in components]
    else:
        x = int(round(float(point[0]) * max(0, width - 1)))
        y = int(round(float(point[1]) * max(0, height - 1)))
        label = int(labels[max(0, min(height - 1, y)), max(0, min(width - 1, x))])
        if label > 0:
            chosen = [label]
        else:
            # Clicking just outside a thin edge should still select the obvious subject.
            best = min(range(1, count),
                       key=lambda index: float(np.hypot(
                           centroids[index][0] - x, centroids[index][1] - y)),
                       default=0)
            chosen = [best] if best else [components[0][1]]
    result = np.zeros_like(matte)
    for index in chosen:
        result[labels == index] = matte[labels == index]
    return np.clip(result, 0.0, 1.0)


def _model_prior(frame, settings, prior=None, subject_point=None):
    model = get_model()
    if model is None:
        return None
    quality = str(settings.get("quality") or "balanced")
    saliency = model.predict(frame, quality)
    if saliency is None:
        return None
    if prior is not None:
        previous = prior
        if previous.shape[:2] != saliency.shape[:2]:
            previous = cv2.resize(previous, (saliency.shape[1], saliency.shape[0]),
                                  interpolation=cv2.INTER_LINEAR)
        # Flow has already moved the previous mask onto this frame. The model supplies
        # object appearance, the warped mask supplies temporal continuity.
        saliency = np.clip(saliency * 0.68 + np.asarray(previous, np.float32) * 0.32,
                           0.0, 1.0)
    return choose_component(saliency, subject_point)


def _hinted_refine(frame, base, foreground, background, refine, settings):
    height, width = base.shape[:2]
    sure_fg = foreground > 127
    sure_bg = background > 127
    if not np.any(sure_fg) and not np.any(sure_bg):
        return np.clip(base, 0.0, 1.0)
    mask = np.full((height, width), cv2.GC_PR_BGD, np.uint8)
    mask[base > 0.22] = cv2.GC_PR_FGD
    mask[base > 0.78] = cv2.GC_FGD
    if not np.any(base > 0.08):
        mask[_border_mask((height, width), max(1, min(height, width) // 60)) > 0] = cv2.GC_BGD
    mask[sure_fg] = cv2.GC_FGD
    mask[sure_bg] = cv2.GC_BGD
    if not np.any(mask == cv2.GC_FGD) or not np.any(mask == cv2.GC_BGD):
        return np.clip(base, 0.0, 1.0)
    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    iterations = 2 if str(settings.get("quality")) == "draft" else 3
    try:
        cv2.grabCut(frame, mask, None, background_model, foreground_model, iterations,
                    cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return np.clip(base, 0.0, 1.0)
    fitted = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.float32)
    result = np.clip(fitted * 0.55 + base * 0.45, 0.0, 1.0)
    result[sure_fg] = 1.0
    result[sure_bg] = 0.0
    return result


def _edge_refine(frame, matte, settings, refine=None):
    height, width = matte.shape[:2]
    quality = str(settings.get("quality") or "balanced")
    radius = {"draft": 2, "balanced": 4, "high": 7}.get(quality, 4)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    refined = guided_filter(gray, np.clip(matte, 0.0, 1.0), radius=radius, epsilon=1e-3)
    refined = np.clip(refined, 0.0, 1.0)
    if refine is not None and np.any(refine > 127):
        edge = guided_filter(gray, np.clip(matte, 0.0, 1.0),
                             radius=max(3, int(round(min(height, width) * 0.015))),
                             epsilon=5e-4)
        weight = cv2.GaussianBlur((refine > 127).astype(np.float32), (0, 0),
                                  sigmaX=max(1.0, float(settings.get("search_radius", 24)) / 3.0))
        weight = np.clip(weight * 1.5, 0.0, 1.0)
        refined = refined * (1.0 - weight) + edge * weight
    return np.clip(refined, 0.0, 1.0).astype(np.float32)


def segment_subject(frame_bgr, foreground=None, background=None, refine=None,
                    settings=None, subject_point=None, prior=None):
    """(matte, backend) for one frame.

    `backend` is "u2net" when the learned model produced the matte, or "basic" when the
    explicitly labelled GrabCut fallback was used. Brush hints are applied to either.
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return None, "basic"
    settings = dict(settings or {})
    height, width = frame_bgr.shape[:2]
    if foreground is None:
        foreground = np.zeros((height, width), np.uint8)
    if background is None:
        background = np.zeros((height, width), np.uint8)
    if refine is None:
        refine = np.zeros((height, width), np.uint8)
    backend_request = str(settings.get("backend") or "auto")
    status = model_status()
    use_model = backend_request != "basic" and status.available

    if not use_model:
        matte = segment_frame(frame_bgr, foreground, background, refine, settings)
        return matte, "basic"

    base = _model_prior(frame_bgr, settings, prior=prior, subject_point=subject_point)
    if base is None:
        matte = segment_frame(frame_bgr, foreground, background, refine, settings)
        return matte, "basic"

    # Automatic mode has no strokes. In that case the model matte is already the answer;
    # running GrabCut would only erode the soft edge it produced.
    if np.any(foreground > 127) or np.any(background > 127):
        base = _hinted_refine(frame_bgr, base, foreground, background, refine, settings)
    else:
        base[foreground > 127] = 1.0
        base[background > 127] = 0.0

    matte = _edge_refine(frame_bgr, base, settings, refine=refine)
    matte[foreground > 127] = 1.0
    matte[background > 127] = 0.0
    return np.clip(matte, 0.0, 1.0).astype(np.float32), "u2net"


def basic_fallback_note(settings):
    """A short UI note when the caller asked for the model but it is absent."""
    request = str((settings or {}).get("backend") or "auto")
    status = model_status()
    if request == "basic":
        return "Basic mode: GrabCut plus optical flow. No learned subject model."
    if status.available:
        return ""
    if request == "model":
        return "The AI model is not installed. Basic GrabCut is being used instead."
    return "AI model not installed. Basic GrabCut is active until it is downloaded."
