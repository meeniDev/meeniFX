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
    engine.session.project_size = (160, 100)
    engine.timeline.tracks[0].clips.clear()
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
def test_actual_export_parity(key, fmt, tmp_path, record_property):
    from PIL import Image
    engine, clip = build_engine(frame=synthetic_frame(160, 100), fps=10, duration=4, frames=4)
    animated = AnimatedValue()
    animated.set_keyframe(0, 0)
    animated.set_keyframe(3, 80 if key == "lens_warp" else .65)
    parameter = "amount" if key == "lens_warp" else "progress"
    clip.effects = [{"effect_id": key, "params": {"backend": "cpu", parameter: animated}}]
    # All format writers consume this same frame. Codec/palette errors have separate tolerances.
    reference = engine.render_frame(3)[1]
    path = tmp_path / f"{key}.{fmt}"
    engine.export({"output_path": str(path), "format": fmt, "range_start": 0, "range_end": 4,
                   "use_source_fps": True, "use_source_size": True, "gif_colors": 256})
    if fmt == "gif":
        with Image.open(path) as gif:
            gif.seek(gif.n_frames - 1)
            decoded = np.array(gif.convert("RGB"))[:, :, ::-1]
    elif fmt in ("mp4", "avi"):
        capture = cv2.VideoCapture(str(path))
        decoded = None
        while True:
            ok, current = capture.read()
            if not ok:
                break
            decoded = current
        capture.release()
        assert decoded is not None
    else:
        decoded = cv2.imread(str(path))
    diff = np.abs(decoded.astype(float)-reference.astype(float)).mean()
    record_property("mean_absolute_error", float(diff))
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
    assert overlay.param(clip, "lens_warp", "center_x", 0, 50) == 50
    amount_channel = CH.effect_channel("lens_warp", "amount")
    CH.set_value(clip, amount_channel, 0, 0, True)
    CH.set_value(clip, amount_channel, 80, 20, True)
    window.graph_editor.refresh()
    assert window.graph_editor.channel_meta(amount_channel) is not None
    from PyQt6.QtWidgets import QComboBox
    combo = QComboBox()
    combo.addItem("Custom", None)
    combo.addItem("Soft Glass", "soft_glass")
    window.effects_panel._apply_effect_preset(clip, clip.effects[0], combo, 1)
    current = window.engine.timeline.find_clip(clip.id)
    assert CH.value_at(current, amount_channel, 0) == 35


def test_page_curl_reveals_lower_clip_without_a_second_transition_system():
    from meenifx.media.cache import MediaEntry
    engine, clip = build_engine(frame=synthetic_frame(160, 100), duration=4, frames=4)
    lower = np.full((100, 160, 3), (180, 30, 10), np.uint8)
    entry = MediaEntry("video", 4, 30)
    entry.frames = [lower.copy() for _ in range(4)]
    engine.media._entries["lower.png"] = entry
    track = engine.timeline.add_track("video")
    engine.timeline.tracks.remove(track)
    engine.timeline.tracks.append(track)
    engine.timeline.new_clip("Lower", "lower.png", track, 0, 4, source_out=4, source_fps=30)
    clip.effects = [{"effect_id": "page_curl", "params": {"progress": 1, "backend": "cpu"}}]
    assert np.array_equal(engine.render_frame(0)[1], lower)
    clip.effects[0]["params"]["progress"] = .4
    halfway = engine.render_frame(0)[1]
    assert np.any(np.all(halfway == lower, axis=2))
    assert not np.array_equal(halfway, lower)


def test_backside_clip_texture_and_self_reference_guard():
    engine, clip = build_engine()
    runtime = engine.runtime_effects["page_curl"]
    runtime.clip = clip
    runtime.local_frame = 7
    with pytest.raises(ValueError, match="another"):
        runtime.clip_texture(str(clip.id), (320, 180))
    source = engine.timeline.new_clip("Backside", clip.source_path, clip.track, 1000, 30,
                                      source_out=30, source_fps=30)
    runtime.load_params({"back_texture": "clip", "back_source": str(source.id)})
    layer = np.dstack((synthetic_frame(), np.full((180, 320), 255, np.uint8)))
    image = runtime.apply_layer(layer)
    assert image.shape == layer.shape and runtime.local_frame == 7
    engine._shader_texture_stack = {id(source)}
    with pytest.raises(ValueError, match="Circular"):
        runtime.clip_texture(str(source.id), (320, 180))


def test_original_names_and_stable_effect_ids():
    import re
    from meenifx.engine import EditorEngine
    engine = EditorEngine()
    assert engine.runtime_effects["page_curl"].name == "Page Curl"
    assert engine.runtime_effects["lens_warp"].name == "Lens Warp"
    assert engine.runtime_effects["kaleidoscope"].name == "Kaleidoscope"
    for runtime in engine.effects:
        assert not re.match(r"(?i)^cc(?:\s|_)", runtime.name)
        for item in (getattr(runtime, "PRESETS", {}) or {}).values():
            if isinstance(item, tuple) and isinstance(item[0], str):
                assert not re.match(r"(?i)^cc(?:\s|_)", item[0])


@pytest.mark.parametrize("key", KEYS)
def test_shader_chain_preserves_existing_tracked_region_effects(key, monkeypatch):
    from meenifx.effects.tracking_viz import Detection
    engine, clip = build_engine()
    monkeypatch.setattr(engine.runtime_effects["blob_tracker"], "_detect",
                        lambda _frame: [Detection(box=(20, 20, 60, 50))])
    shader = {"effect_id": key, "params": {"backend": "cpu"}}
    clip.effects = [shader]
    plain = engine.render_frame(0)[1]
    clip.effects = [{"effect_id": "blob_tracker", "params": {
        "region_effect_1": "invert", "region_amount_1": 100, "region_feather": 0,
        "show_box": False, "show_contour": False, "show_points": False,
        "show_labels": False, "show_numbers": False, "connector_mode": "none"}}, shader]
    tracked = engine.render_frame(0)[1]
    assert not np.array_equal(plain, tracked)
    assert np.array_equal(tracked, engine.render_frame(17)[1])
