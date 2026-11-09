"""Stroke-guided segmentation.

What this backend is
--------------------
OpenCV GrabCut seeded by the green/red brush strokes, blended with a colour-distance
model and refined along the painted Refine Edge strokes with a guided filter. It runs on
the CPU, needs no model download and is deterministic, which is why it ships as the
built-in fallback.

What it is NOT
--------------
It is not a learned video-segmentation model. There is no appearance memory, no learned
object prior and no temporal reasoning of its own; propagation comes from optical flow in
`propagate.py`. On a clear subject against a contrasting background the stroke workflow
works well; on hair, fur, motion blur or a crowded scene the matte will be noticeably
worse than a model such as SAM 2 would produce. The Refine Edge brush is an edge-aware
guided filter, not a matting network, and the UI/docs say so.

Model options evaluated (see docs/ROTO_BRUSH.md): SAM 2 / SAM 2.1 (Apache-2.0, needs
PyTorch and a 110-770 MB checkpoint, WSL recommended on Windows) and its ONNX exports
(portable runtime, same weights) are the realistic upgrade path. None is bundled: the
release build must stay a single Windows package with no multi-GB runtime, and shipping a
model we cannot test on this machine would be worse than an honest fallback.
"""

from __future__ import annotations

import cv2
import numpy as np

# Quality only changes the analysis resolution and GrabCut iterations; the stored matte
# is always resampled back to the mask size, so preview and export see the same shape.
QUALITY = {
    "draft": {"scale": 0.5, "iterations": 2, "band": 0.75},
    "balanced": {"scale": 1.0, "iterations": 4, "band": 1.0},
    "high": {"scale": 1.0, "iterations": 7, "band": 1.4},
}


def quality_settings(quality):
    return dict(QUALITY.get(str(quality or "balanced"), QUALITY["balanced"]))


def _border_mask(shape, thickness):
    mask = np.zeros(shape, np.uint8)
    thickness = max(1, int(thickness))
    mask[:thickness, :] = 255
    mask[-thickness:, :] = 255
    mask[:, :thickness] = 255
    mask[:, -thickness:] = 255
    return mask


def _smoothstep(edge0, edge1, value):
    width = max(1e-5, float(edge1) - float(edge0))
    t = np.clip((value - float(edge0)) / width, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _box(image, radius):
    radius = max(1, int(radius))
    return cv2.blur(image, (radius * 2 + 1, radius * 2 + 1), borderType=cv2.BORDER_REFLECT)


def guided_filter(guide, source, radius=6, epsilon=1e-3):
    """Edge-aware smoothing of `source` towards the gradients of `guide` (0..1 float).

    This is the honest part of the Refine Edge brush: it makes the matte follow an edge
    that the coarse mask blurred, but it cannot invent real alpha for hair the way a
    matting model can.
    """
    guide = np.asarray(guide, np.float32)
    source = np.asarray(source, np.float32)
    radius = max(1, int(radius))
    mean_guide = _box(guide, radius)
    mean_source = _box(source, radius)
    variance = _box(guide * guide, radius) - mean_guide * mean_guide
    covariance = _box(guide * source, radius) - mean_guide * mean_source
    a = covariance / (variance + float(epsilon))
    b = mean_source - a * mean_guide
    return _box(a, radius) * guide + _box(b, radius)


def _colour_matte(frame_bgr, foreground, background, tolerance):
    """Soft matte from LAB distance to the colours the strokes sampled."""
    lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    fg_pixels = lab[foreground > 127]
    if fg_pixels.size == 0:
        return None
    bg_pixels = lab[background > 127]
    if bg_pixels.size == 0:
        border = _border_mask(lab.shape[:2], 2)
        bg_pixels = lab[border > 0]
    fg_mean = fg_pixels.mean(axis=0)
    bg_mean = bg_pixels.mean(axis=0) if bg_pixels.size else np.array([128.0, 128.0, 128.0])
    d_fg = np.linalg.norm(lab - fg_mean, axis=2)
    d_bg = np.linalg.norm(lab - bg_mean, axis=2)
    margin = max(1.0, float(tolerance))
    return _smoothstep(-margin, margin, d_bg - d_fg)


def _grabcut(frame_bgr, foreground, background, refine, settings):
    height, width = frame_bgr.shape[:2]
    sure_fg = foreground > 127
    sure_bg = background > 127
    if not sure_fg.any():
        return None
    if not sure_bg.any():
        sure_bg = _border_mask((height, width), max(1, min(height, width) // 60)) > 0
    search = max(1, int(round(float(settings.get("search_radius", 24)))))
    search = max(1, int(round(search * float(settings.get("band", 1.0)))))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (search * 2 + 1, search * 2 + 1))
    probable_fg = cv2.dilate(foreground, kernel) > 127
    probable_bg = cv2.dilate(background, kernel) > 127
    mask = np.full((height, width), cv2.GC_PR_BGD, np.uint8)
    mask[probable_bg] = cv2.GC_PR_BGD
    mask[probable_fg] = cv2.GC_PR_FGD
    mask[refine > 127] = cv2.GC_PR_FGD
    mask[sure_bg] = cv2.GC_BGD
    mask[sure_fg] = cv2.GC_FGD
    if not np.any(mask == cv2.GC_FGD) or not np.any(mask == cv2.GC_BGD):
        return None
    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(frame_bgr, mask, None, background_model, foreground_model,
                    max(1, int(settings.get("iterations", 4))), cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None
    keep = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.float32)
    # The painted foreground is a hint the user gave explicitly; never let GrabCut
    # discard it, or a stroke would appear to do nothing.
    keep[sure_fg] = 1.0
    keep[sure_bg] = 0.0
    return keep


def segment_frame(frame_bgr, foreground, background, refine=None, settings=None):
    """A soft matte (float32 0..1) for one frame and its brush hints, or None.

    `foreground`, `background` and `refine` are uint8 masks in the frame's pixels.
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return None
    settings = dict(settings or {})
    height, width = frame_bgr.shape[:2]
    if refine is None:
        refine = np.zeros((height, width), np.uint8)
    foreground = np.where(foreground > 127, 255, 0).astype(np.uint8)
    background = np.where(background > 127, 255, 0).astype(np.uint8)
    if not np.any(foreground > 0):
        return None

    tolerance = float(settings.get("tolerance", 28.0))
    colour = _colour_matte(frame_bgr, foreground, background, tolerance)
    grab = _grabcut(frame_bgr, foreground, background, refine, settings) \
        if settings.get("refine", True) else None
    if grab is not None and colour is not None:
        matte = np.clip(grab * (0.62 + 0.38 * colour), 0.0, 1.0)
    elif grab is not None:
        matte = grab
    elif colour is not None:
        matte = colour
    else:
        return None
    # The stroke cores stay solid even when the two models disagree about them.
    matte[foreground > 127] = 1.0
    matte[background > 127] = 0.0

    refine_pixels = refine > 127
    if np.any(refine_pixels):
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        guided = guided_filter(gray, matte, radius=max(2, int(round(min(width, height) * 0.02))),
                               epsilon=1e-3)
        band = float(settings.get("search_radius", 24))
        weight = cv2.GaussianBlur(refine_pixels.astype(np.float32), (0, 0),
                                  sigmaX=max(1.0, band / 3.0)) 
        weight = np.clip(weight * 1.4, 0.0, 1.0)
        matte = matte * (1.0 - weight) + np.clip(guided, 0.0, 1.0) * weight
        matte[foreground > 127] = 1.0
        matte[background > 127] = 0.0
    return np.clip(matte, 0.0, 1.0).astype(np.float32)


def finish_matte(matte, settings, scale=1.0):
    """Smooth, contrast, choke and feather, the controls that reshape the matte.

    Everything here runs on the *cached* raw matte, so changing a slider re-shapes every
    analysed frame without segmenting again. Pixel values are multiplied by `scale` so a
    half-quality preview looks like the full-quality export.
    """
    if matte is None:
        return None
    matte = np.asarray(matte, np.float32)
    scale = max(0.02, float(scale or 1.0))

    smooth = max(0.0, float(settings.get("smooth", 0.0))) * scale
    if smooth >= 1.0:
        size = int(round(smooth)) * 2 + 1
        size = min(31, max(3, size))
        matte = cv2.medianBlur((np.clip(matte, 0.0, 1.0) * 255).astype(np.uint8), size)
        matte = matte.astype(np.float32) / 255.0

    contrast = max(0.0, min(100.0, float(settings.get("edge_contrast", 0.0))))
    if contrast > 0.01:
        steepness = 1.0 + contrast / 100.0 * 11.0
        matte = 1.0 / (1.0 + np.exp(-steepness * (matte - 0.5)))
        # Keep the ramp anchored: 0 stays 0 and 1 stays 1 at every steepness.
        low = 1.0 / (1.0 + np.exp(steepness * 0.5))
        high = 1.0 / (1.0 + np.exp(-steepness * 0.5))
        matte = (matte - low) / max(1e-6, high - low)
        matte = np.clip(matte, 0.0, 1.0)

    choke = float(settings.get("choke", 0.0)) * scale
    amount = int(round(abs(choke)))
    if amount > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                           (min(61, amount * 2 + 1),) * 2)
        u8 = (np.clip(matte, 0.0, 1.0) * 255).astype(np.uint8)
        u8 = cv2.erode(u8, kernel) if choke > 0 else cv2.dilate(u8, kernel)
        matte = u8.astype(np.float32) / 255.0

    feather = max(0.0, float(settings.get("feather", 0.0))) * scale
    if feather > 0.01:
        matte = cv2.GaussianBlur(np.clip(matte, 0.0, 1.0), (0, 0),
                                 sigmaX=feather / 3.0, sigmaY=feather / 3.0,
                                 borderType=cv2.BORDER_REPLICATE)
    return np.clip(matte, 0.0, 1.0).astype(np.float32)


def decontaminate(frame_bgr, matte, amount):
    """Pull the background's colour out of the matte's soft edge.

    A green-screen edge or a bright background leaves a coloured fringe on the subject.
    The edge band is recoloured from the subject's own interior colour, which is what the
    control promises; it is not a physical unmixing solve.
    """
    strength = max(0.0, min(100.0, float(amount))) / 100.0
    if strength <= 0.005 or frame_bgr is None or matte is None:
        return frame_bgr
    matte = np.clip(np.asarray(matte, np.float32), 0.0, 1.0)
    if matte.shape[:2] != frame_bgr.shape[:2]:
        matte = cv2.resize(matte, (frame_bgr.shape[1], frame_bgr.shape[0]),
                           interpolation=cv2.INTER_LINEAR)
    band = (matte > 0.02) & (matte < 0.98)
    if not np.any(band):
        return frame_bgr
    # The replacement colour must come from the subject itself. Blurring the whole frame
    # merely re-averages the fringe; extrapolating the *solid interior* into the band is
    # what actually removes a green-screen or bright-background rim.
    solid = (matte >= 0.95).astype(np.uint8)
    if not np.any(solid):
        solid = (matte >= 0.5).astype(np.uint8)
    solid = cv2.erode(solid, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    if not np.any(solid):
        return frame_bgr
    sigma = max(2.0, min(frame_bgr.shape[:2]) * 0.02)
    cover = cv2.GaussianBlur(solid.astype(np.float32), (0, 0), sigmaX=sigma, sigmaY=sigma)
    colour_sum = cv2.GaussianBlur(frame_bgr.astype(np.float32) * solid[:, :, None].astype(np.float32),
                                  (0, 0), sigmaX=sigma, sigmaY=sigma)
    subject_colour = colour_sum / np.maximum(cover, 1e-4)[:, :, None]
    weight = cv2.GaussianBlur(band.astype(np.float32), (0, 0), sigmaX=max(1.0, sigma * 0.5))
    weight = (np.clip(weight * strength * 1.6, 0.0, 1.0)[:, :, None]
              * band[:, :, None])
    mixed = frame_bgr.astype(np.float32) * (1.0 - weight) + subject_colour * weight
    return np.clip(mixed, 0.0, 255.0).astype(np.uint8)


def outline_contours(matte, threshold=0.5, max_shapes=8, epsilon=0.004):
    """Normalised outline polylines for a matte: [(N, 2) float arrays in 0..1].

    The UI maps these through the clip transform, so the outline follows the actual
    segmented boundary instead of drawing a rectangle around the clip.
    """
    if matte is None or not np.size(matte):
        return []
    height, width = matte.shape[:2]
    binary = np.where(np.asarray(matte) >= float(threshold), 255, 0).astype(np.uint8)
    if not np.any(binary):
        return []
    contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:max_shapes]
    minimum = max(8, int(width * height * 0.00005))
    shapes = []
    for contour in contours:
        if cv2.contourArea(contour) < minimum:
            continue
        simplified = cv2.approxPolyDP(contour, epsilon * max(width, height), True)
        points = simplified.reshape(-1, 2).astype(np.float32)
        if len(points) < 3:
            continue
        points[:, 0] /= float(max(1, width - 1))
        points[:, 1] /= float(max(1, height - 1))
        shapes.append(points)
    return shapes
