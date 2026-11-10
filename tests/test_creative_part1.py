"""Part 1 creative tools: paths, masks, mattes, motion and frame/pixel snapping."""

from types import SimpleNamespace

import numpy as np

from meenifx.engine import EditorEngine
from meenifx.model import mask as MASK
from meenifx.model import matte as MATTE
from meenifx.model import motion as MOTION
from meenifx.model import path as PATHS
from meenifx.model import shape as SHAPE
from meenifx.processing import masks as MASK_OPS
from meenifx.processing import shapes as SHAPE_OPS


def clip(**extra):
    values = {"id": 1, "duration": 120, "timeline_start": 0,
              "transform_meta": {}, "effects": [], "is_shape": False, "is_text": False}
    values.update(extra)
    return SimpleNamespace(**values)


def test_paths_interpolate_and_measure_bezier_geometry():
    path = PATHS.normalise({"points": [(0, 0), (100, 0), (100, 100)],
                            "closed": True})
    assert PATHS.bounds(path) == (0.0, 0.0, 100.0, 100.0)
    keyed = PATHS.set_key(path, 0, path)
    keyed = PATHS.set_key(keyed, 10, {"points": [(0, 20), (100, 20), (100, 100)],
                                      "closed": True})
    midway = PATHS.at(keyed, 5)
    assert abs(midway["points"][0]["y"] - 10.0) < 1e-6
    assert len(PATHS.sampled(path, steps=8)) > 8


def test_custom_path_shape_renders_fill_and_stroke():
    settings = SHAPE.blank("path")
    settings["path"] = PATHS.normalise({"points": [(80, 60), (240, 60), (160, 220)],
                                        "closed": True})
    source = clip(shape=settings)
    spec = SHAPE.resolve(source, 0)
    layer = SHAPE_OPS.render(spec, 320, 240, 1.0)
    assert layer.shape == (240, 320, 4)
    assert int(layer[:, :, 3].max()) > 0
    bounds = SHAPE_OPS.bounds(spec, 320, 240)
    assert bounds is not None and bounds[2] > 100 and bounds[3] > 100


def test_multiple_masks_combine_and_only_change_alpha():
    source = clip()
    MASK.add(source, [(0, 0), (60, 0), (60, 60), (0, 60)], mode="add")
    MASK.add(source, [(20, 20), (40, 20), (40, 40), (20, 40)], mode="subtract")
    layer = np.zeros((80, 80, 4), np.uint8)
    layer[:, :, :3] = (20, 40, 60)
    layer[:, :, 3] = 255
    MASK_OPS.apply(layer, MASK.at(source, 0), 1.0)
    assert layer[5, 5, 3] > 200
    assert layer[30, 30, 3] == 0
    assert tuple(layer[5, 5, :3]) == (20, 40, 60)


def test_alpha_matte_uses_the_source_alpha_without_changing_colour():
    source = clip(id=2, name="Matte")
    target = clip(id=1)
    MATTE.set_source(target, 2, "alpha")
    source_layer = np.zeros((40, 40, 4), np.uint8)
    source_layer[:, :, :3] = (10, 20, 30)
    source_layer[:, :, 3] = 128
    target_layer = np.zeros((40, 40, 4), np.uint8)
    target_layer[:, :, :3] = (200, 100, 50)
    target_layer[:, :, 3] = 255
    engine = SimpleNamespace(
        timeline=SimpleNamespace(all_clips=lambda: [source, target]),
        project_size=lambda: (40, 40),
        held_frame=lambda item, local: max(0, int(local)),
        track_type_of=lambda item: "video",
        _layer_for=lambda item, frame, scale: (source_layer, (0, 0, 40, 40)),
        apply_effects_layer=lambda layer, *args, **kwargs: layer,
    )
    result = EditorEngine._apply_matte(engine, target, target_layer, 0, 1.0)
    assert result is target_layer
    assert int(result[10, 10, 3]) == 128
    assert tuple(result[10, 10, :3]) == (200, 100, 50)


def test_motion_path_ping_pong_is_continuous_and_reverses():
    source = clip()
    MOTION.set_path(source, [(0, 0), (100, 0)], closed=False)
    settings = MOTION.ensure(source)
    settings["loop"] = "ping_pong"
    settings["speed"] = 1.0
    assert abs(MOTION.progress_at(source, 25, 100) - 0.25) < 1e-6
    assert abs(MOTION.progress_at(source, 75, 100) - 0.75) < 1e-6
    assert abs(MOTION.progress_at(source, 125, 100) - 0.75) < 1e-6
    position, _angle = MOTION.position_at(source, 50, (0, 0))
    assert position is not None and position[0] > 0


def test_frame_pixel_mapping_round_trips_at_zoom_levels(qapp):
    from PyQt6.QtWidgets import QApplication
    from meenifx.ui.timeline_widget import TimelinePanel
    engine = EditorEngine(30)
    panel = TimelinePanel(engine, None)
    canvas = panel.canvas
    canvas.scroll_x = 0.0
    for ppf in (0.37, 1.0, 1.5, 8.0):
        canvas.ppf = ppf
        for frame in (0, 1, 24, 47, 100, 479):
            assert canvas.frame_at(canvas.x_at_exact(frame)) == frame
    QApplication.processEvents()
    panel.deleteLater()


def test_session_mask_matte_and_motion_edits_are_undoable():
    from meenifx.model import transform as T
    engine = EditorEngine(30)
    session = engine.session
    session.project_size = (640, 360)
    first_id = session.add_shape_clip("rectangle", box=(100, 100, 180, 100))
    second_id = session.add_shape_clip("ellipse", box=(360, 100, 100, 100))
    assert first_id and second_id
    mask_id = session.add_clip_mask(first_id, [(100, 100), (280, 100), (280, 200)])
    first = engine.timeline.find_clip(first_id)
    assert mask_id and MASK.masks(first)
    session.set_clip_matte(first_id, second_id, "alpha")
    assert MATTE.ensure(first)["source_id"] == second_id
    session.set_motion_path(first_id, [(0, 0), (80, 40)], enabled=True)
    assert MOTION.ensure(first)["enabled"]
    assert T.position_at(first, 60) != (0.0, 0.0)
    session.undo()
    first = engine.timeline.find_clip(first_id)
    assert not MOTION.ensure(first)["enabled"]
    session.undo()
    first = engine.timeline.find_clip(first_id)
    assert MATTE.ensure(first)["source_id"] == 0
    session.undo()
    first = engine.timeline.find_clip(first_id)
    assert not MASK.masks(first)


def test_masks_mattes_and_motion_survive_project_sections_round_trip():
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections
    engine = EditorEngine(30)
    session = engine.session
    session.project_size = (640, 360)
    first_id = session.add_shape_clip("rectangle", box=(80, 80, 180, 100))
    second_id = session.add_shape_clip("ellipse", box=(320, 80, 100, 100))
    session.add_clip_mask(first_id, [(100, 100), (240, 100), (240, 180)], mode="subtract")
    session.set_clip_matte(first_id, second_id, "luma_inverted")
    session.set_motion_path(first_id, [(0, 0), (70, 30)], enabled=True)
    sections = timeline_to_sections(engine.timeline, lambda path: 0)
    restored = sections_to_timeline(sections, 30.0, lambda media_id: "")
    first = restored.find_clip(first_id)
    assert MASK.masks(first)[0]["mode"] == "subtract"
    assert MATTE.ensure(first)["mode"] == "luma_inverted"
    assert MATTE.ensure(first)["source_id"] == second_id
    assert MOTION.ensure(first)["enabled"]
    assert len(MOTION.ensure(first)["path"]["points"]) == 2


def test_engine_alpha_matte_affects_the_export_compositor():
    engine = EditorEngine(30)
    engine.source_width = engine.source_height = 64
    engine.session.project_size = (64, 64)
    session = engine.session
    first = session.add_shape_clip("rectangle", box=(0, 0, 64, 64))
    source = session.add_shape_clip("ellipse", box=(0, 0, 64, 64))
    source_clip = engine.timeline.find_clip(source)
    source_clip.track.visible = False
    session.set_clip_matte(first, source, "alpha")
    image = engine.compose_frame(0, 1.0, with_alpha=True)
    assert image is not None and image.shape[2] == 4
    assert image[32, 32, 3] > 200
    assert image[0, 0, 3] < 16


def test_creative_metadata_invalidates_preview_caches():
    from meenifx.preview.invalidation import clip_digest, timeline_digests
    engine = EditorEngine(30)
    session = engine.session
    session.project_size = (640, 360)
    first_id = session.add_shape_clip("rectangle", box=(100, 100, 180, 100))
    second_id = session.add_shape_clip("ellipse", box=(320, 100, 100, 100))
    first = engine.timeline.find_clip(first_id)
    second = engine.timeline.find_clip(second_id)
    before = clip_digest(first)
    session.add_clip_mask(first_id, [(100, 100), (280, 100), (280, 200)])
    assert clip_digest(first) != before
    before = clip_digest(first)
    session.set_clip_matte(first_id, second_id, "alpha")
    assert clip_digest(first) != before
    before = clip_digest(first)
    session.set_motion_path(first_id, [(0, 0), (50, 20)])
    assert clip_digest(first) != before
    before = timeline_digests(engine.timeline)[first_id][0]
    second.shape["fill_color"] = "#00FF00"
    assert timeline_digests(engine.timeline)[first_id][0] != before
