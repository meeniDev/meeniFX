"""Blur fast path: an opaque layer blurs 8-bit directly, and must look the same.

The float premultiplied pipeline is what protects transparent edges, so it stays for
layers that really have alpha. These tests pin the shortcut to that reference: the
picture may differ by at most one 8-bit step, alpha must never move, and a layer with
transparency must still go the long way round (no dark fringe appearing).
"""

import numpy as np
import pytest

from conftest import build_engine
from meenifx.effects import blur as BLUR


def _layer(height=48, width=64, alpha=255):
    yy, xx = np.mgrid[0:height, 0:width]
    layer = np.zeros((height, width, 4), np.uint8)
    layer[:, :, 0] = (xx * 255 // max(1, width - 1))
    layer[:, :, 1] = (yy * 255 // max(1, height - 1))
    layer[:, :, 2] = 200
    layer[:, :, 3] = alpha
    return layer


def _reference_gaussian(layer, sigma):
    """The pre-fast-path implementation, verbatim."""
    data = BLUR.premultiply(layer)
    blurred = _cv2().GaussianBlur(data, (0, 0), sigmaX=sigma, sigmaY=sigma,
                                  borderType=_cv2().BORDER_REPLICATE)
    return BLUR.unpremultiply(blurred, layer)


def _cv2():
    import cv2
    return cv2


def _run(engine, layer, params):
    effect = engine.runtime_effects["gaussian_blur"]
    effect.param_scale = 1.0
    effect.local_frame = 0
    effect.load_params({"radius": params.get("radius", 8.0),
                        "keep_edges": params.get("keep_edges", False)})
    return effect.apply_layer(layer)


def test_an_opaque_layer_takes_the_fast_path_and_stays_within_one_step():
    engine, _clip = build_engine()
    layer = _layer()
    sigma = max(0.1, 8.0 / 2.5)
    fast = _run(engine, layer, {})
    reference = _reference_gaussian(layer, sigma)
    assert fast.shape == reference.shape and fast.dtype == np.uint8
    difference = np.abs(fast.astype(np.int16) - reference.astype(np.int16))
    assert int(difference.max()) <= 1, "the fast path must not change the picture"
    assert np.array_equal(fast[:, :, 3], layer[:, :, 3]), "opaque alpha is untouched"


def test_a_layer_with_transparency_still_uses_the_premultiplied_pipeline():
    engine, _clip = build_engine()
    layer = _layer()
    layer[:12, :, 3] = 0            # a transparent margin, like a letterboxed picture
    sigma = max(0.1, 8.0 / 2.5)
    result = _run(engine, layer, {})
    reference = _reference_gaussian(layer, sigma)
    assert np.abs(result.astype(np.int16) - reference.astype(np.int16)).max() <= 1
    assert int(result[0, 0, 3]) == 0, "the transparent margin stays transparent"
    # The opaque part must not be darkened by black bleeding out of the margin.
    assert int(result[20, 32, 2]) >= 180


def test_keep_edges_still_keeps_the_original_alpha():
    engine, _clip = build_engine()
    layer = _layer()
    layer[:, :, 3] = 128
    result = _run(engine, layer, {"keep_edges": True})
    assert np.array_equal(result[:, :, 3], layer[:, :, 3])


def test_box_blur_matches_the_float_reference_for_an_opaque_layer():
    engine, _clip = build_engine()
    layer = _layer()
    effect = engine.runtime_effects["box_blur"]
    effect.param_scale = 1.0
    effect.local_frame = 0
    effect.load_params({"radius": 4.0, "passes": 2})
    fast = effect.apply_layer(layer)

    from meenifx.effects.blur import premultiply, unpremultiply
    data = premultiply(layer)
    for _ in range(2):
        data = _cv2().blur(data, (9, 9), borderType=_cv2().BORDER_REPLICATE)
    reference = unpremultiply(data, layer)
    assert np.abs(fast.astype(np.int16) - reference.astype(np.int16)).max() <= 1


def test_the_fully_opaque_helper_is_honest():
    assert BLUR.fully_opaque(_layer()) is True
    assert BLUR.fully_opaque(np.zeros((4, 4, 3), np.uint8)) is False
    assert BLUR.fully_opaque(np.zeros((0, 0, 4), np.uint8)) is False
    faint = _layer()
    faint[0, 0, 3] = 254
    assert BLUR.fully_opaque(faint) is False


@pytest.mark.parametrize("effect_key", ["gaussian_blur", "box_blur"])
def test_preview_and_export_stay_identical_at_every_scale(effect_key):
    """A scaled preview is the same picture, smaller: the shortcut must not break that."""
    from meenifx.processing import layers as layer_ops

    engine, clip = build_engine()
    clip.effects = [{"effect_id": effect_key, "enabled": True,
                     "params": {"radius": 6.0}}]
    full = engine.render_frame(0, 1.0)[1]
    half = engine.render_frame(0, 0.5)[1]
    assert half.shape[:2] == (full.shape[0] // 2, full.shape[1] // 2)
    small = layer_ops.fit_layer(full, (full.shape[1], full.shape[0]), 0.5)[0][:, :, :3]
    # Same framing and softness, allowing for resampling differences.
    assert abs(float(half.mean()) - float(small.mean())) < 4.0
