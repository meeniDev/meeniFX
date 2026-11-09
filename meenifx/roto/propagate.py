"""Temporal mask propagation with optical flow.

The mask is carried to the next frame by warping it along a dense Farneback flow field,
then refined from that frame's own pixels with a GrabCut pass seeded by the warped mask
(probable foreground/background bands, not a hard threshold). The mask therefore follows
the subject instead of being copied frame to frame; when the subject moves slowly and the
background is distinct this tracks well, and when it does not the user paints a correction
stroke on the bad frame.

This is classical propagation, not a learned video-segmentation model: there is no object
memory, so fast motion, occlusion and motion blur still need corrections. It runs on the
CPU at a bounded analysis size so the preview and the timeline stay responsive.
"""

from __future__ import annotations

import cv2
import numpy as np

from .segment import guided_filter

ANALYSIS_EDGE = 480


def _gray(frame):
    if frame is None:
        return None
    if frame.ndim == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame


def _analysis_size(shape, max_edge=ANALYSIS_EDGE):
    height, width = shape[:2]
    factor = min(1.0, float(max_edge) / max(1.0, float(max(width, height))))
    return max(8, int(round(width * factor))), max(8, int(round(height * factor)))


def frame_order(start, end, seed, direction):
    """The source frames to analyse, moving outward from the seed.

    Forward and backward never cross the seed; `all` does the forward pass first and then
    the backward one, so one Run analyses the whole clip from a single base frame.
    """
    start, end, seed = int(start), int(end), int(seed)
    if end < start:
        start, end = end, start
    forward = [frame for frame in range(seed + 1, end + 1)]
    backward = [frame for frame in range(seed - 1, start - 1, -1)]
    if direction == "backward":
        return backward
    if direction == "forward":
        return forward
    return forward + backward


def warp_matte(matte, flow):
    """Move a matte along a dense flow field (pixels per frame).

    The flow is forward (previous frame -> this frame): it says where each previous pixel
    went. Rebuilding this frame's matte therefore samples the *previous* matte at
    ``position - flow``; adding it would shift the mask against the motion, which makes a
    moving subject lose its matte within a few frames.
    """
    height, width = matte.shape[:2]
    grid_x, grid_y = np.meshgrid(np.arange(width, dtype=np.float32),
                                 np.arange(height, dtype=np.float32))
    map_x = grid_x - flow[:, :, 0]
    map_y = grid_y - flow[:, :, 1]
    return cv2.remap(matte, map_x, map_y, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def _refine(warped, rgb, settings):
    """Re-fit the warped mask to this frame's pixels."""
    if not bool(settings.get("refine", True)):
        return cv2.GaussianBlur(warped, (0, 0), 1.0)
    height, width = warped.shape[:2]
    mask = np.full((height, width), cv2.GC_PR_BGD, np.uint8)
    mask[warped > 0.50] = cv2.GC_PR_FGD
    mask[warped > 0.90] = cv2.GC_FGD
    mask[warped < 0.10] = cv2.GC_BGD
    if not np.any(mask == cv2.GC_FGD) or not np.any(mask == cv2.GC_BGD):
        return warped
    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    iterations = max(1, min(3, int(settings.get("iterations", 2))))
    try:
        cv2.grabCut(rgb, mask, None, background_model, foreground_model, iterations,
                    cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return warped
    refined = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.float32)
    blended = np.clip(refined * 0.85 + warped * 0.15, 0.0, 1.0)
    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    guided = guided_filter(gray, blended, radius=max(2, int(min(width, height) * 0.02)),
                           epsilon=1e-3)
    return np.clip(guided * 0.6 + blended * 0.4, 0.0, 1.0)


def propagate_seed(seed_matte, seed_frame, frames, read_frame, settings,
                   on_mask=None, should_stop=None, on_progress=None,
                   mask_refiner=None):
    """Carry `seed_matte` across `frames` (in order, excluding the seed).

    `read_frame(source_frame)` must return the BGR frame for a source index. When
    `mask_refiner(frame, warped, rgb)` is supplied, it runs the learned subject model on
    every frame using the flow-warped matte as its temporal prior. Without it the classic
    GrabCut propagation remains available as the Basic fallback.
    """
    seed_frame = int(seed_frame)
    frames = [int(frame) for frame in frames if int(frame) != seed_frame]
    if not frames:
        return []
    previous_gray = _gray(read_frame(seed_frame))
    previous_matte = np.asarray(seed_matte, np.float32)
    total = len(frames)
    done = []
    for index, frame in enumerate(frames):
        if should_stop is not None and should_stop():
            break
        rgb = read_frame(frame)
        if rgb is None or previous_gray is None or previous_matte is None:
            break
        gray = _gray(rgb)
        height, width = gray.shape[:2]
        work_w, work_h = _analysis_size(gray.shape)
        small_previous = cv2.resize(previous_gray, (work_w, work_h),
                                    interpolation=cv2.INTER_AREA)
        small_gray = cv2.resize(gray, (work_w, work_h), interpolation=cv2.INTER_AREA)
        flow = cv2.calcOpticalFlowFarneback(small_previous, small_gray, None,
                                            0.5, 4, 21, 3, 5, 1.1, 0)
        small_matte = cv2.resize(previous_matte, (work_w, work_h),
                                 interpolation=cv2.INTER_LINEAR)
        warped = np.clip(warp_matte(small_matte, flow), 0.0, 1.0)
        if mask_refiner is not None:
            refined = mask_refiner(frame, warped, rgb)
            if refined is None:
                break
            matte = np.asarray(refined, np.float32)
            if matte.shape[:2] != (height, width):
                matte = cv2.resize(matte, (width, height), interpolation=cv2.INTER_LINEAR)
        else:
            small_rgb = cv2.resize(rgb, (work_w, work_h), interpolation=cv2.INTER_AREA)
            refined = _refine(warped, small_rgb, settings)
            matte = cv2.resize(refined, (width, height), interpolation=cv2.INTER_LINEAR)
        matte = np.clip(matte, 0.0, 1.0).astype(np.float32)
        if on_mask is not None:
            on_mask(frame, matte)
        done.append(frame)
        previous_gray, previous_matte = gray, matte
        if on_progress is not None:
            on_progress(index + 1, total)
    return done
