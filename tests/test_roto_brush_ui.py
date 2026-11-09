"""The Roto Brush preview gesture writes a real stroke and one undo step."""

import numpy as np
from PyQt6.QtCore import QPointF, Qt

from conftest import build_engine
from meenifx.ui.preview import PreviewWidget
from meenifx.ui.preview_transform import PreviewOverlay


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


def test_roto_brush_drag_writes_one_stroke_and_undoes_it(qapp):
    engine, clip = build_engine(duration=30)
    engine.session.set_selection({clip.id})
    entry = {"effect_id": "roto_brush", "enabled": True, "params": {}, "strokes": []}
    clip.effects = [entry]

    view = PreviewWidget(quality_getter=lambda: 100)
    view.resize(400, 300)
    view.set_frame(np.zeros((180, 320, 3), dtype=np.uint8), logical_size=(320, 180))
    overlay = PreviewOverlay(view, engine, engine.session)
    overlay.mode = "roto"
    overlay.on_change = lambda: None

    rect = view.frame_rect()
    start = rect.center()
    end = QPointF(start.x() + 36, start.y() + 18)
    assert overlay.mouse_press(_Event(start))
    assert overlay.mouse_move(_Event(end))
    assert overlay.mouse_release(_Event(end))

    restored = engine.timeline.find_clip(clip.id)
    strokes = restored.effects[0]["strokes"]
    assert len(strokes) == 1
    assert len(strokes[0]["points"]) >= 2
    assert strokes[0]["mode"] == "foreground"
    assert 0.0 <= strokes[0]["points"][0][0] <= 1.0

    engine.session.undo()
    assert engine.timeline.find_clip(clip.id).effects[0]["strokes"] == []
