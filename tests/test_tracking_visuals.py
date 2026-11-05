"""Shared tracking visuals, presets, quality scaling and detection caching."""

import numpy as np
import pytest

from conftest import build_engine, synthetic_frame
from meenifx.effects.tracking_viz import (Detection, _connector_pairs, draw_tracking,
                                         resolve_style)
from test_keyframe_ui import editor, pump  # noqa: F401  (shared MainWindow fixture)


def boxes(count):
    return [Detection(box=(10 + index * 55, 20, 40, 50), track_id=index + 1)
            for index in range(count)]


def test_named_preset_overrides_manual_style_but_custom_keeps_it():
    manual = {"show_box": False, "show_labels": False, "connector_mode": "distance"}
    clean = resolve_style("clean", manual)
    assert clean["show_box"] is True
    assert clean["show_labels"] is True
    assert clean["connector_mode"] == "none"
    custom = resolve_style("custom", manual)
    assert custom["show_box"] is False
    assert custom["connector_mode"] == "distance"


def test_connector_links_are_capped_and_never_all_to_all():
    detections = boxes(8)
    nearest = _connector_pairs([d.box for d in detections], "nearest", 1000, 2)
    distance = _connector_pairs([d.box for d in detections], "distance", 1000, 1)
    assert 0 < len(nearest) <= 2 * len(detections)
    assert 0 < len(distance) <= len(detections)
    assert len({tuple(sorted(pair)) for pair in nearest}) == len(nearest)


def test_connector_range_filters_links():
    detections = boxes(3)
    far = _connector_pairs([d.box for d in detections], "distance", 5, 2)
    near = _connector_pairs([d.box for d in detections], "distance", 1000, 2)
    assert far == []
    assert near


def test_distance_mode_is_visibly_different_from_nearest():
    frame = np.zeros((180, 320, 3), np.uint8)
    detections = boxes(3)
    nearest = draw_tracking(frame, detections, connector_mode="nearest",
                            connector_distance=100, show_labels=False)
    distance = draw_tracking(frame, detections, connector_mode="distance",
                             connector_distance=100, show_labels=False)
    assert np.abs(nearest.astype(int) - distance.astype(int)).mean() > 0.05


def test_opacity_zero_is_only_the_original_picture():
    frame = synthetic_frame()
    result = draw_tracking(frame, boxes(2), opacity=0, show_labels=False,
                           connector_mode="none")
    assert np.array_equal(result, frame)


def test_random_colours_stay_stable_for_an_object():
    engine, _clip = build_engine(frame=synthetic_frame())
    effect = engine.runtime_effects["hand_tracker"]
    effect.color_mode.set("random")
    first = effect._resolve_draw_color(3)
    second = effect._resolve_draw_color(3)
    other = effect._resolve_draw_color(4)
    assert first == second
    assert first != other


def test_old_tracker_switches_still_load():
    engine, _clip = build_engine(frame=synthetic_frame())
    effect = engine.runtime_effects["hand_tracker"]
    effect.load_params({"draw_box": False, "draw_hull": False, "label_faces": False})
    assert effect.show_box.get() is False
    assert effect.show_contour.get() is False
    effect.load_params({"draw_mode": "contour"})
    assert effect.show_contour.get() is True
    assert effect.show_box.get() is False
    assert effect.show_circle.get() is False


def test_face_detection_is_cached_between_identical_frames():
    engine, _clip = build_engine(frame=synthetic_frame())
    face = engine.runtime_effects["face_tracker"]
    calls = []

    def detect(frame):
        calls.append(frame.shape)
        return [(40, 30, 60, 60)]

    face.detect_faces = detect
    face.param_scale = 1.0
    frame = synthetic_frame()
    face.apply(frame)
    face.apply(frame)
    assert len(calls) == 1
    face.confidence.set(70)
    face.apply(frame)
    assert len(calls) == 2
    face.clear_tracking_cache()
    face.apply(frame)
    assert len(calls) == 3


def test_quality_presets_reduce_only_the_working_resolution():
    engine, _clip = build_engine(frame=synthetic_frame())
    effect = engine.runtime_effects["blob_tracker"]
    big = np.zeros((1080, 1920, 3), np.uint8)
    sizes = {}
    for quality in ("fast", "balanced", "precise"):
        effect.tracking_quality.set(quality)
        work, factor = effect._detection_scale(big, 480, 720)
        sizes[quality] = (max(work.shape[:2]), factor)
    assert sizes["fast"][0] <= 480
    assert sizes["balanced"][0] <= 720
    assert sizes["precise"][0] == 1920
    assert sizes["fast"][1] < sizes["balanced"][1] < sizes["precise"][1]


def test_hand_tracker_detects_skin_regions_but_not_grey():
    engine, _clip = build_engine(frame=synthetic_frame())
    effect = engine.runtime_effects["hand_tracker"]
    effect.param_scale = 1.0
    skinned = synthetic_frame()
    found = effect._detect(skinned)
    assert found, "the synthetic skin patches should be detected"
    grey = np.full_like(skinned, 120)
    assert effect._detect(grey) == []


def test_choosing_a_tracking_look_writes_its_values_into_the_clip(editor, qapp):
    panel = editor.effects_panel
    panel.add_effect("face_tracker")
    pump(qapp, 0.25)
    row = next(item for item in panel._rows
               if item["kind"] == "choice" and item["name"] == "viz_preset")
    combo = row["combo"]
    index = combo.findData("neon")
    assert index >= 0
    clip = next(clip for clip in editor.engine.timeline.all_clips()
                if clip.id == row["clip_id"])
    panel._choice_picked(clip, row["entry"], row["spec"], combo, index)
    entry = next(effect for effect in clip.effects if effect["effect_id"] == "face_tracker")
    assert entry["params"]["viz_preset"] == "neon"
    assert entry["params"]["show_box"] is False
    assert entry["params"]["show_points"] is True
    assert entry["params"]["connector_mode"] == "nearest"


def test_face_blur_feather_changes_the_edge_without_changing_the_detection():
    engine, _clip = build_engine(frame=synthetic_frame())
    face = engine.runtime_effects["face_tracker"]
    face.detect_faces = lambda frame: [(40, 30, 60, 60)]
    face.param_scale = 1.0
    face.load_params({**face.defaults, "blur_faces": True, "blur_amount": 60,
                      "blur_feather": 0, "show_box": False, "show_contour": False,
                      "show_labels": False, "connector_mode": "none"})
    frame = synthetic_frame()
    hard = face.apply(frame)
    face.blur_feather.set(30)
    soft = face.apply(frame)
    assert np.abs(hard.astype(int) - soft.astype(int)).mean() > 0.05
