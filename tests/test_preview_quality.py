"""Preview quality must never change framing - only sharpness."""

import numpy as np
import pytest

from conftest import synthetic_frame

COLORS = [(0, 0, 255), (0, 255, 255), (0, 255, 0), (255, 255, 0),
          (255, 0, 0), (255, 0, 255), (255, 128, 0), (128, 0, 255)]
LOGICAL_W, LOGICAL_H = 320, 180


def _bars():
    frame = np.zeros((LOGICAL_H, LOGICAL_W, 3), np.uint8)
    for index, color in enumerate(COLORS):
        frame[:, index * 40:(index + 1) * 40] = color
    return frame


def _nearest(rgb):
    bgr = [(c[2], c[1], c[0]) for c in COLORS]
    return min(range(len(bgr)), key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, bgr[i])))


def _render_view(view):
    from PyQt6.QtGui import QImage
    image = QImage(view.width(), view.height(), QImage.Format.Format_RGB32)
    image.fill(0)
    view.render(image)
    return image


@pytest.fixture()
def view(qapp):
    from meenifx.ui.preview import PreviewWidget
    widget = PreviewWidget(quality_getter=lambda: 100)
    widget.resize(800, 600)
    widget.show()
    qapp.processEvents()
    return widget


@pytest.mark.parametrize("quality", [100, 75, 50, 35, 10])
@pytest.mark.parametrize("zoom", [None, 1.0, 2.0])
def test_framing_is_identical_at_every_quality(view, quality, zoom):
    """The eight colour bars stay in their eight slots whatever the render scale."""
    import cv2
    source = _bars()
    render_w = max(1, round(LOGICAL_W * quality / 100))
    render_h = max(1, round(LOGICAL_H * quality / 100))
    render = cv2.resize(source, (render_w, render_h), interpolation=cv2.INTER_AREA)
    view.set_frame(render, frame_key="bars", logical_size=(LOGICAL_W, LOGICAL_H))
    view.set_zoom(zoom)
    view.pan_x = view.pan_y = 0
    view._clamp_pan()
    image = _render_view(view)
    rect = view.frame_rect()
    assert (rect.width(), rect.height()) == pytest.approx((LOGICAL_W * view.zoom, LOGICAL_H * view.zoom))
    y = int(round(rect.center().y()))
    seen = [_nearest(image.pixelColor(int(rect.left() + rect.width() * (index + 0.5) / 8), y).getRgb()[:3])
            for index in range(8)]
    assert seen == list(range(8))
    assert view._image.width() == render_w
    assert view._frame_size == (LOGICAL_W, LOGICAL_H)


def test_panning_stays_in_logical_coordinates(view):
    """Pan offsets are logical pixels, so they cannot shift when quality changes."""
    import cv2
    source = _bars()
    view.set_frame(source, frame_key="bars", logical_size=(LOGICAL_W, LOGICAL_H))
    view.set_zoom(2.0)
    view.pan_x, view.pan_y = 60, -40
    view._clamp_pan()
    rect_before = view.frame_rect()
    for quality in (75, 50, 35, 10):
        render = cv2.resize(source, (max(1, round(LOGICAL_W * quality / 100)),
                                     max(1, round(LOGICAL_H * quality / 100))),
                            interpolation=cv2.INTER_AREA)
        view.set_frame(render, frame_key="bars", logical_size=(LOGICAL_W, LOGICAL_H))
        assert view.frame_rect() == rect_before


def test_auto_quality_recovers_after_fast_frames():
    """Auto used to degrade forever: its climb-back branch compared against 1.0."""
    from types import SimpleNamespace

    from meenifx.model.values import Value
    from meenifx.ui.main_window import MainWindow

    window = SimpleNamespace(
        _quality_label="Auto", _auto_scale=0.125, _interacting=lambda: False,
        engine=SimpleNamespace(playback_fps=Value(30)),
        _show_quality_hint=lambda message: None,
    )
    window.preview_scale = lambda: window._auto_scale
    adapt = MainWindow._adapt_preview_quality
    adapt(window, 1.0)
    assert window._auto_scale == 0.25
    adapt(window, 1.0)
    assert window._auto_scale == 0.5
    adapt(window, 1000.0)
    assert window._auto_scale == 0.25


def test_engine_render_size_follows_quality():
    from conftest import build_engine
    engine, _clip = build_engine(frame=synthetic_frame())
    assert engine.render_size(1.0) == (LOGICAL_W, LOGICAL_H)
    assert engine.render_size(0.35) == (round(LOGICAL_W * 0.35), round(LOGICAL_H * 0.35))
    full = engine.render_frame(0, 1.0)[1]
    preview = engine.render_frame(0, 0.35)[1]
    assert full.shape[:2] == (LOGICAL_H, LOGICAL_W)
    assert preview.shape[:2] == (round(LOGICAL_H * 0.35), round(LOGICAL_W * 0.35))
    assert engine.project_size() == (LOGICAL_W, LOGICAL_H)
