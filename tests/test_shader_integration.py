"""Shaders through the existing project, UI, alpha-compositing and export paths."""
import copy
import cv2
import numpy as np
import pytest
from conftest import build_engine, synthetic_frame
from test_keyframe_ui import editor, pump
from test_keyframe_workflow import _roundtrip
from meenifx.model import channels as CH, looks as LOOKS
from meenifx.model.keyframes import AnimatedValue

KEYS = ("lens_warp", "page_curl")


@pytest.mark.parametrize("key", KEYS)
def test_project_duplicate_copy_presets_and_thumbnail(key, tmp_path):
    from meenifx import thumbnails
    from meenifx.effects import categories
    engine, clip = build_engine()
    runtime = engine.runtime_effects[key]
    animated = AnimatedValue()
    name = "amount" if key == "lens_warp" else "progress"
    animated.set_keyframe(0, 0)
    animated.set_keyframe(20, 70 if key == "lens_warp" else .6)
    clip.effects = [{"effect_id": key, "params": {name: animated, "backend": "cpu"}}]
    engine.session.set_selection({clip.id})
    engine.session.duplicate_selected()
    copies = engine.timeline.all_clips()
    assert len(copies) == 2 and all(c.effects[0]["effect_id"] == key for c in copies)
    restored, _ = _roundtrip(engine.timeline, tmp_path / f"{key}.mfxpf")
    for c in restored.all_clips():
        assert c.effects[0]["effect_id"] == key
        assert c.effects[0]["params"][name].value_at(20) == pytest.approx(70 if key == "lens_warp" else .6)
    engine.session.copy_effects([clip.id])
    assert engine.session.paste_effects([copies[-1].id])
    assert runtime.PRESETS and categories.category_for(key) == "Distortion"
    for label, values in runtime.PRESETS.values():
        assert label and all(runtime.spec(param) is not None for param in values)
    preview = thumbnails.render_effect(engine, key, 124, 84)
    assert preview.shape == (84, 124, 3)
    assert categories.details_for(runtime)["description"]
    look = LOOKS.normalise({"id": "test", "name": "Test", "stack": [
        {"effect_id": key, "params": next(iter(runtime.PRESETS.values()))[1]}]})
    assert LOOKS.stack_for(look)[0]["effect_id"] == key


@pytest.mark.parametrize("key", KEYS)
def test_masks_nested_compositions_adjustment_and_generated_layers(key):
    engine, media = build_engine(frame=synthetic_frame(160, 100))
    shape_id = engine.session.add_shape_clip("rectangle", start=0, duration=30, box=(20, 20, 100, 60))
    shape = engine.timeline.find_clip(shape_id)
    shape.effects = [{"effect_id": key, "params": {"backend": "cpu"}}]
    engine.session.add_clip_mask(shape_id, [(30, 25), (80, 25), (80, 70), (30, 70)], mode="subtract")
    masked = engine.render_frame(0)[1]
    assert masked.shape == (100, 160, 3)
    inner = engine.session.precompose("Shader Inner", [shape_id])
    assert inner
    nested = engine.render_frame(0)[1]
    assert np.abs(masked.astype(float) - nested.astype(float)).mean() < 1
    outer = engine.session.precompose("Shader Outer", [inner])
    assert outer
    assert np.abs(masked.astype(float) - engine.render_frame(0)[1].astype(float)).mean() < 1
    adjustment_id = engine.session.add_adjustment_clip(0, 30)
    engine.timeline.find_clip(adjustment_id).effects = [{"effect_id": key, "params": {"backend": "cpu"}}]
    adjusted = engine.render_frame(0)[1]
    assert not np.array_equal(nested, adjusted)
    text_id = engine.session.add_text_clip("Shader", 0, 30)
    engine.timeline.find_clip(text_id).effects = [{"effect_id": key, "params": {"backend": "cpu"}}]
    assert engine.render_frame(0)[1].dtype == np.uint8


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("fmt", ["png", "mp4", "avi", "gif"])
def test_actual_export_parity(key, fmt, tmp_path):
    from PIL import Image
    engine, clip = build_engine(frame=synthetic_frame(160, 100), fps=10, duration=4, frames=4)
    clip.effects = [{"effect_id": key, "params": {"backend": "cpu"}}]
    # All format writers consume this same frame. Codec/palette errors have separate tolerances.
    reference = engine.render_frame(3)[1]
    path = tmp_path / f"{key}.{fmt}"
    engine.export({"output_path": str(path), "format": fmt, "range_start": 0, "range_end": 4,
                   "use_source_fps": True, "use_source_size": True, "gif_colors": 256})
    if fmt == "gif":
        with Image.open(path) as gif:
            decoded = np.array(gif.convert("RGB"))[:, :, ::-1]
    elif fmt in ("mp4", "avi"):
        capture = cv2.VideoCapture(str(path))
        ok, decoded = capture.read()
        capture.release()
        assert ok
    else:
        decoded = cv2.imread(str(path))
    diff = np.abs(decoded.astype(float)-reference.astype(float)).mean()
    assert diff < (0.01 if fmt == "png" else 10), (fmt, diff)


def test_lens_handles_graph_keyframes_and_presets(editor, qapp):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from meenifx.model import transform as T
    window = editor
    clip = window.engine.timeline.find_clip(window._test_clip_id)
    clip.effects = [{"effect_id": "lens_warp", "params": {"backend": "cpu"}}]
    window.engine.session.set_selection({clip.id})
    window.effects_panel.refresh_properties()
    window._set_overlay_mode("transform")
    pump(qapp)
    overlay = window.overlay
    start = overlay.lens_handles(clip)["center"]
    end = start + QPointF(25, 12)
    for kind, point in ((QMouseEvent.Type.MouseButtonPress, start),
                        (QMouseEvent.Type.MouseMove, end),
                        (QMouseEvent.Type.MouseButtonRelease, end)):
        event = QMouseEvent(kind, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        {QMouseEvent.Type.MouseButtonPress: overlay.mouse_press,
         QMouseEvent.Type.MouseMove: overlay.mouse_move,
         QMouseEvent.Type.MouseButtonRelease: overlay.mouse_release}[kind](event)
    channel = CH.effect_channel("lens_warp", "center_x")
    assert CH.value_at(clip, channel, 0) > 50
    window.engine.session.undo()
    clip = window.engine.timeline.find_clip(clip.id)
    assert CH.value_at(clip, channel, 0) == 50
    amount_channel = CH.effect_channel("lens_warp", "amount")
    CH.set_value(clip, amount_channel, 0, 0, True)
    CH.set_value(clip, amount_channel, 80, 20, True)
    window.graph_editor.refresh()
    assert window.graph_editor.channel_meta(amount_channel) is not None
    window.effects_panel._apply_effect_preset(clip, clip.effects[0], "lens_warp", "soft_glass")
