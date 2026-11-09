"""Roto Brush interaction: dedicated mode, green/red strokes, outline, zoom alignment."""

import cv2
import numpy as np
import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QKeyEvent, QPainter

from conftest import build_engine
from meenifx.ui.preview import PreviewWidget
from meenifx.ui.preview_transform import PreviewOverlay

WIDTH, HEIGHT = 320, 180
SUBJECT = (220, 150, 60)          # BGR


class _Event:
    def __init__(self, position, modifiers=Qt.KeyboardModifier.NoModifier):
        self._position = QPointF(position)
        self._modifiers = modifiers

    def position(self):
        return self._position

    def button(self):
        return Qt.MouseButton.LeftButton

    def modifiers(self):
        return self._modifiers


def picture(cx=160, cy=90, radius=46):
    frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    frame[:] = (42, 40, 38)
    cv2.circle(frame, (cx, cy), radius, SUBJECT, -1)
    return frame


def build(qapp, monkeypatch, tmp_path, strokes=None):
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto"))
    engine, clip = build_engine(duration=30)
    frames = [picture() for _ in range(30)]
    engine.media.load(clip.source_path).frames = frames
    engine.session.set_selection({clip.id})
    entry = {"effect_id": "roto_brush", "enabled": True, "params": {},
             "base_frame": 0, "strokes": list(strokes or [])}
    clip.effects = [entry]

    view = PreviewWidget(quality_getter=lambda: 100)
    view.resize(480, 360)
    view.set_frame(frames[0], logical_size=(WIDTH, HEIGHT))
    overlay = PreviewOverlay(view, engine, engine.session)
    view.overlay = overlay
    overlay.mode = "roto"
    overlay.on_change = lambda: None
    return engine, clip, entry, view, overlay


def paint_image(overlay, width=480, height=360):
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    overlay.paint(painter)
    painter.end()
    return image


def count_colour(image, colour, tolerance=26):
    target = QColor(colour)
    hits = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            pixel = image.pixelColor(x, y)
            if (abs(pixel.red() - target.red()) <= tolerance
                    and abs(pixel.green() - target.green()) <= tolerance
                    and abs(pixel.blue() - target.blue()) <= tolerance):
                hits += 1
    return hits


def stroke_points(overlay, view):
    centre = view.frame_rect().center()
    return centre, QPointF(centre.x() + 26, centre.y() + 14)


def test_roto_mode_hides_the_transform_box_and_handles(qapp, monkeypatch, tmp_path):
    engine, clip, entry, view, overlay = build(qapp, monkeypatch, tmp_path)
    calls = []
    overlay._paint_clip = lambda *args, **kwargs: calls.append(1)
    paint_image(overlay)
    assert calls == [], "no bounding box, handles or rotation knob in Roto Brush mode"
    overlay.mode = "select"
    paint_image(overlay)
    assert calls, "the normal transform chrome returns when the mode is left"


def test_drag_paints_continuous_green_strokes_and_alt_paints_red(qapp, monkeypatch,
                                                                 tmp_path):
    engine, clip, entry, view, overlay = build(qapp, monkeypatch, tmp_path)
    start, end = stroke_points(overlay, view)
    assert overlay.mouse_press(_Event(start))
    for step in range(1, 7):
        point = QPointF(start.x() + (end.x() - start.x()) * step / 6.0,
                        start.y() + (end.y() - start.y()) * step / 6.0)
        assert overlay.mouse_move(_Event(point))
    assert overlay.mouse_release(_Event(end))
    strokes = engine.timeline.find_clip(clip.id).effects[0]["strokes"]
    assert len(strokes) == 1
    assert strokes[0]["frame"] == 0
    assert strokes[0]["mode"] == "foreground"
    assert len(strokes[0]["points"]) >= 3, "a drag is a stroke, not a click"

    assert overlay.mouse_press(_Event(QPointF(start.x() + 8, start.y() - 6),
                                      Qt.KeyboardModifier.AltModifier))
    overlay.mouse_move(_Event(QPointF(start.x() + 24, start.y() - 12),
                              Qt.KeyboardModifier.AltModifier))
    overlay.mouse_release(_Event(QPointF(start.x() + 24, start.y() - 12)))
    strokes = engine.timeline.find_clip(clip.id).effects[0]["strokes"]
    assert [stroke["mode"] for stroke in strokes] == ["foreground", "background"]

    engine.session.undo()
    assert len(engine.timeline.find_clip(clip.id).effects[0]["strokes"]) == 1
    engine.session.undo()
    assert engine.timeline.find_clip(clip.id).effects[0]["strokes"] == []
    engine.session.redo()
    assert len(engine.timeline.find_clip(clip.id).effects[0]["strokes"]) == 1


def test_circular_brush_cursor_uses_the_active_colour(qapp, monkeypatch, tmp_path):
    _engine, _clip, _entry, _view, overlay = build(qapp, monkeypatch, tmp_path)
    overlay.roto_hover = QPointF(240, 180)
    overlay.roto_brush_size = 0.12
    green = count_colour(paint_image(overlay), QColor(64, 220, 120))
    overlay.set_roto_brush_mode("background")
    red = count_colour(paint_image(overlay), QColor(255, 82, 82))
    assert green > 8, "a green ring marks the brush"
    assert red > 8, "Alt/background mode shows a red ring"
    overlay.set_roto_brush_mode("refine")
    blue = count_colour(paint_image(overlay), QColor(120, 200, 255))
    assert blue > 8, "the Refine Edge brush has its own indicator"


def test_outline_tracks_the_subject_and_updates_after_a_correction(qapp, monkeypatch,
                                                                   tmp_path):
    engine, clip, entry, view, overlay = build(qapp, monkeypatch, tmp_path)
    start, end = stroke_points(overlay, view)
    overlay.mouse_press(_Event(start))
    overlay.mouse_move(_Event(end))
    overlay.mouse_release(_Event(end))
    entry = engine.timeline.find_clip(clip.id).effects[0]
    outline = overlay.roto_outline(clip, entry, 0)
    assert outline, "painting a stroke draws a segmentation outline"
    points = np.concatenate(outline, axis=0)
    assert float(points[:, 0].max() - points[:, 0].min()) < 0.7, \
        "the outline follows the disc, not the clip rectangle"
    before = float(points[:, 0].max() - points[:, 0].min())

    # Alt-paint across the subject's left boundary; the outer outline must be re-cut.
    boundary = overlay.to_widget(*overlay.roto_to_project(clip, (0.36, 0.34)))
    lower = overlay.to_widget(*overlay.roto_to_project(clip, (0.36, 0.66)))
    overlay.mouse_press(_Event(boundary, Qt.KeyboardModifier.AltModifier))
    overlay.mouse_move(_Event(lower, Qt.KeyboardModifier.AltModifier))
    overlay.mouse_release(_Event(lower))
    entry = engine.timeline.find_clip(clip.id).effects[0]
    overlay._roto_outline_cache.clear()
    corrected = np.concatenate(overlay.roto_outline(clip, entry, 0), axis=0)
    after = float(corrected[:, 0].max() - corrected[:, 0].min())
    assert after < before, "the outline updates when a red correction is painted"


def test_strokes_stay_aligned_when_the_preview_is_resized(qapp, monkeypatch, tmp_path):
    _engine, _clip, _entry, view, overlay = build(qapp, monkeypatch, tmp_path)
    aligned = []
    for width, height in ((320, 240), (800, 600)):
        view.resize(width, height)
        rect = view.frame_rect()
        centre = overlay.roto_picture_point(rect.center())
        assert centre is not None
        aligned.append(centre)
        project = overlay.roto_to_project(overlay.primary(), centre)
        back = overlay.to_widget(*project)
        assert abs(back.x() - rect.center().x()) < 2.0
        assert abs(back.y() - rect.center().y()) < 2.0
    assert abs(aligned[0][0] - aligned[1][0]) < 0.02
    assert abs(aligned[0][1] - aligned[1][1]) < 0.02
    assert 0.45 < aligned[0][0] < 0.55 and 0.45 < aligned[0][1] < 0.55


def _key(view, key, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
    view.keyPressEvent(event)
    return event


def test_workspace_shortcuts_switch_tools_and_exit(qapp, monkeypatch, tmp_path):
    _engine, _clip, _entry, view, overlay = build(qapp, monkeypatch, tmp_path)
    overlay.set_roto_brush_mode("refine")
    assert _key(view, Qt.Key.Key_B).isAccepted()
    assert overlay.roto_brush_mode == "foreground", "B selects the Roto Brush"

    start, end = stroke_points(overlay, view)
    overlay.mouse_press(_Event(start))
    overlay.mouse_move(_Event(end))
    overlay.mouse_release(_Event(end))
    _engine.session.undo()
    assert _engine.timeline.find_clip(_clip.id).effects[0]["strokes"] == []
    _key(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier
         | Qt.KeyboardModifier.ShiftModifier)
    assert len(_engine.timeline.find_clip(_clip.id).effects[0]["strokes"]) == 1

    assert _key(view, Qt.Key.Key_V).isAccepted()
    assert overlay.mode == "select", "V restores Selection and leaves painting"
    overlay.mode = "roto"
    assert _key(view, Qt.Key.Key_Escape).isAccepted()
    assert overlay.mode == "select", "Esc leaves the Roto workspace"


def test_done_restores_the_normal_tools(qapp, monkeypatch, tmp_path):
    _engine, _clip, _entry, view, overlay = build(qapp, monkeypatch, tmp_path)
    modes = []
    overlay.on_mode_change = modes.append
    overlay.roto_hover = QPointF(100, 100)
    assert overlay.exit_roto()
    assert overlay.mode == "select"
    assert overlay.roto_hover is None
    assert modes == ["select"]
    assert not overlay.exit_roto(), "Done is idempotent"
    calls = []
    overlay._paint_clip = lambda *args, **kwargs: calls.append(1)
    paint_image(overlay)
    assert calls, "transform handles are painted again after leaving Roto Brush"
