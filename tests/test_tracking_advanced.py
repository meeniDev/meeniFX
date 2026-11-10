"""Advanced tracking: custom labels, connectors, tracked-region effects and upgrades.

These drive the shared overlay directly and the real tracker effects.  The hand landmark
test here uses a fake provider to isolate coordinate mapping; real MediaPipe inference is
covered separately by ``test_hand_landmarks_real.py``.
"""

import os
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import build_engine, frame_diff, synthetic_frame  # noqa: E402
from meenifx.effects import tracking_regions as REGIONS  # noqa: E402
from meenifx.effects import tracking_viz as VIZ  # noqa: E402
from meenifx.effects.tracking_viz import (Detection, TrackStabilizer, draw_tracking,  # noqa: E402
                                          format_label)


def boxes(count):
    return [Detection(box=(10 + index * 55, 20, 40, 50), track_id=index + 1)
            for index in range(count)]


def test_label_variables_combine_with_custom_text():
    detection = Detection(box=(20, 30, 40, 50), track_id=7, speed=12.4, confidence=0.87,
                          extra={"side": "Left"})
    text = format_label("Blob {id} | {x},{y} {width}x{height} {speed} {confidence} {side}",
                        detection, 0)
    assert text == "Blob 7 | 20,30 40x50 12 87 Left"
    assert format_label("No variables", detection, 0) == "No variables"
    assert format_label("Unknown {colour}", detection, 0) == "Unknown {colour}"


def test_label_typography_and_placement_change_the_picture():
    frame = synthetic_frame()
    detection = Detection(box=(100, 60, 60, 60), track_id=1)
    base = {"label_text": "Target 1", "show_box": False, "show_labels": True,
            "label_style": {"label_size": 22, "label_background_opacity": 70,
                            "label_padding": 6, "label_position": "above",
                            "label_anchor": "top_left", "label_align": "left",
                            "label_weight": "regular"}}
    plain = draw_tracking(frame, [detection], **base)
    larger = draw_tracking(frame, [detection],
                           **{**base, "label_style": {**base["label_style"],
                                                     "label_size": 40}})
    below = draw_tracking(frame, [detection],
                          **{**base, "label_style": {**base["label_style"],
                                                    "label_position": "below",
                                                    "label_align": "right"}})
    opaque = draw_tracking(frame, [detection],
                           **{**base, "label_style": {**base["label_style"],
                                                     "label_background_opacity": 100}})
    assert frame_diff(plain, larger) > 0.1, "font size is ignored"
    assert frame_diff(plain, below) > 0.1, "label placement is ignored"
    assert frame_diff(plain, opaque) > 0.1, "label background opacity is ignored"


def test_connector_centre_custom_curved_and_pulsing_modes_differ():
    frame = np.zeros((180, 320, 3), np.uint8)
    detections = boxes(3)
    common = dict(connector_distance=100, show_box=False)
    none = draw_tracking(frame, detections, connector_mode="none", **common)
    centre = draw_tracking(frame, detections, connector_mode="center", **common)
    custom = draw_tracking(frame, detections, connector_mode="custom",
                           custom_links="1-3", **common)
    assert frame_diff(none, centre) > 0.1, "connect-to-centre draws nothing"
    assert frame_diff(none, custom) > 0.1, "custom links draw nothing"
    assert frame_diff(centre, custom) > 0.1
    curved = draw_tracking(frame, detections, connector_mode="distance",
                           connector_curve=80, connector_color="#FF00FF",
                           connector_thickness=4, connector_opacity=100,
                           connector_glow=True, **common)
    straight = draw_tracking(frame, detections, connector_mode="distance",
                             connector_curve=0, connector_color="#FF00FF",
                             connector_thickness=4, connector_opacity=100, **common)
    assert frame_diff(curved, straight) > 0.1, "curve and glow are ignored"
    first = draw_tracking(frame, detections, connector_mode="nearest",
                          connector_pulses=True, frame_index=0, **common)
    later = draw_tracking(frame, detections, connector_mode="nearest",
                          connector_pulses=True, frame_index=4, **common)
    assert frame_diff(first, later) > 0.005, "pulses do not travel over time"


def test_distance_labels_are_drawn_on_connectors():
    frame = np.zeros((180, 320, 3), np.uint8)
    plain = draw_tracking(frame, boxes(2), connector_mode="nearest",
                          connector_distance=100, connector_labels=False)
    labelled = draw_tracking(frame, boxes(2), connector_mode="nearest",
                             connector_distance=100, connector_labels=True)
    assert frame_diff(plain, labelled) > 0.02


def test_ids_and_speeds_stay_with_a_moving_object():
    stabilizer = TrackStabilizer()
    first = [Detection(box=(10, 10, 30, 30)), Detection(box=(200, 10, 30, 30))]
    stabilizer.update(first, frame=0, max_distance=50)
    ids = [item.track_id for item in first]
    assert len(set(ids)) == 2
    # The left object moves 8 px, the right one stays put.
    second = [Detection(box=(18, 10, 30, 30)), Detection(box=(200, 10, 30, 30))]
    stabilizer.update(second, frame=1, max_distance=50)
    assert [item.track_id for item in second] == ids, "an object changed identity"
    assert second[0].speed == pytest.approx(8.0)
    assert second[1].speed == pytest.approx(0.0)


def test_region_mask_expands_shrinks_and_feathers():
    box = (100, 50, 80, 60)
    plain = REGIONS.region_mask((180, 320, 3), box, expand=0, feather=0)
    grown = REGIONS.region_mask((180, 320, 3), box, expand=10, feather=0)
    shrunk = REGIONS.region_mask((180, 320, 3), box, expand=-10, feather=0)
    soft = REGIONS.region_mask((180, 320, 3), box, expand=0, feather=20)
    assert (grown > 0).sum() > (plain > 0).sum() > (shrunk > 0).sum()
    assert 0 < (soft > 0).sum() and soft.max() == 255
    assert (soft == 255).sum() < (plain == 255).sum(), "feather must soften the edge"


def test_region_effects_change_only_inside_the_mask():
    frame = synthetic_frame()
    before = frame.copy()
    mask = REGIONS.region_mask(frame.shape, (40, 30, 80, 80), expand=0, feather=0)
    out = REGIONS.apply_stack(frame.copy(), mask, [("invert", 100)])
    inside = mask > 0
    outside = mask == 0
    assert np.abs(out[inside].astype(int) - before[inside].astype(int)).mean() > 20
    assert np.array_equal(out[outside], before[outside]), "the effect leaked outside"


def _face_engine(monkeypatch):
    engine, clip = build_engine(frame=synthetic_frame())
    effect = engine.runtime_effects["face_tracker"]
    effect._backend = "yunet"
    effect.param_scale = 1.0
    monkeypatch.setattr(effect, "detect_faces", lambda frame: [(40, 30, 70, 70)])
    return engine, clip, effect


def test_face_blur_modes_are_distinct_and_real(monkeypatch):
    _engine, _clip, effect = _face_engine(monkeypatch)
    effect.blur_faces.set(True)
    effect.blur_amount.set(100)
    effect.blur_feather.set(0)
    frame = synthetic_frame()
    plain = frame.copy()
    images = {}
    for mode in ("gaussian", "box", "pixelate", "mosaic"):
        effect.blur_mode.set(mode)
        images[mode] = effect.apply(frame)
        assert frame_diff(plain, images[mode]) > 0.2, f"{mode} does not blur"
    assert frame_diff(images["gaussian"], images["pixelate"]) > 0.05
    assert frame_diff(images["pixelate"], images["mosaic"]) > 0.01


def test_face_sprite_keeps_its_transparency(monkeypatch, tmp_path):
    _engine, _clip, effect = _face_engine(monkeypatch)
    sprite = np.zeros((40, 40, 4), np.uint8)
    sprite[:, :, 2] = 255
    sprite[:, :, 3] = 255
    sprite[:20, :20, 3] = 0
    path = tmp_path / "badge.png"
    assert cv2.imwrite(str(path), sprite)
    effect.sprite_path.set(str(path))
    effect.sprite_scale.set(100)
    effect.sprite_opacity.set(100)
    frame = synthetic_frame()
    out = effect.apply(frame)
    # The transparent quadrant is scaled with the sprite, so sample the opaque half.
    assert out[60, 80, 2] > frame[60, 80, 2], "the sprite was not composited"
    assert np.array_equal(out[45, 45], frame[45, 45]), \
        "a transparent sprite pixel must not paint over the picture"


def test_face_smoothing_averages_boxes_over_time(monkeypatch):
    _engine, _clip, effect = _face_engine(monkeypatch)
    effect.face_smoothing.set(50)
    first = effect._smooth_faces([(0, 0, 40, 40)])
    second = effect._smooth_faces([(100, 0, 40, 40)])
    assert first[0][0] == 0
    assert 0 < second[0][0] < 100, "smoothing must lag the jump"


def test_face_custom_stack_parses_and_renders(monkeypatch):
    _engine, _clip, effect = _face_engine(monkeypatch)
    assert REGIONS.parse_stack("blur:60, pixelate:40, nonsense") == [("blur", 60.0),
                                                                    ("pixelate", 40.0)]
    effect.custom_effects.set("invert:100")
    frame = synthetic_frame()
    out = effect.apply(frame)
    assert frame_diff(out, frame) > 0.2


def test_tracker_region_effects_run_inside_detections(monkeypatch):
    _engine, _clip, effect = _face_engine(monkeypatch)
    effect.region_effect_1.set("invert")
    effect.region_amount_1.set(100)
    effect.region_feather.set(0)
    frame = synthetic_frame()
    out = effect.apply(frame)
    assert frame_diff(out, frame) > 1.0
    # Far from the face the inverse must not have touched the picture.
    assert np.array_equal(out[0:20, 0:20], frame[0:20, 0:20])


class _FakeLandmarks:
    def __init__(self, points):
        self.landmark = [SimpleNamespace(x=x / 320.0, y=y / 180.0) for x, y in points]


class _FakeResults:
    def __init__(self):
        points = [(60 + index * 2, 60 + (index % 5) * 4) for index in range(21)]
        other = [(200 + index * 2, 70 + (index % 5) * 4) for index in range(21)]
        self.multi_hand_landmarks = [_FakeLandmarks(points), _FakeLandmarks(other)]
        self.multi_handedness = [
            [SimpleNamespace(classification=[SimpleNamespace(label="Left", score=0.92)])],
            [SimpleNamespace(classification=[SimpleNamespace(label="Right", score=0.88)])],
        ]


class _FakeHandBackend:
    def process(self, _rgb):
        return _FakeResults()


def test_hand_landmarks_build_21_point_skeletons_and_side_labels(monkeypatch):
    engine, _clip = build_engine(frame=synthetic_frame())
    effect = engine.runtime_effects["hand_tracker"]
    effect.param_scale = 1.0
    effect._hands = _FakeHandBackend()
    frame = synthetic_frame()
    found = effect._detect_landmarks(frame)
    assert len(found) == 2
    assert all(len(item.points) == 21 for item in found)
    assert [item.extra["side"] for item in found] == ["Left", "Right"]
    effect.show_labels.set(True)
    effect.label_text.set("Hand {side}")
    effect.show_points.set(True)
    effect.show_skeleton.set(True)
    out = effect.apply(frame)
    assert frame_diff(out, frame) > 0.3, "landmarks, skeleton and labels are missing"
    assert effect._landmark_error == ""
