"""Real MediaPipe hand-landmark inference over licensed photographs.

These are not fake-provider tests.  They require the Tasks model fetched by
``tools/fetch_hand_model.py`` and the supported MediaPipe/OpenCV environment.
Variants deliberately include conditions the model may fail; the test records the
observed detections instead of asserting accuracy that was never measured.
"""

from __future__ import annotations

import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import build_engine  # noqa: E402
from meenifx.effects.builtins import HandTrackerEffect  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "hands")
SHOTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "artifacts", "hand-tracking")
MODEL = HandTrackerEffect.model_path()


def _read(name):
    image = cv2.imread(os.path.join(FIXTURES, name))
    assert image is not None, f"missing fixture {name}"
    return image


def _effect(image):
    engine, _clip = build_engine(frame=image)
    effect = engine.runtime_effects["hand_tracker"]
    effect.param_scale = 1.0
    effect.max_hands.set(2)
    effect.hand_confidence.set(30)
    return effect


def _describe(detections):
    return [(len(item.points), item.extra.get("side", "")) for item in detections or []]


@pytest.fixture
def effects():
    """Build effects and close MediaPipe before pytest's GC/unconfigure phase."""
    created = []

    def make(image):
        effect = _effect(image)
        created.append(effect)
        return effect

    yield make
    for effect in created:
        effect.close()


def _save(make, name, image, detections):
    os.makedirs(SHOTS, exist_ok=True)
    effect = make(image)
    output = effect.draw_tracking(image.copy(), detections, label="Hand")
    path = os.path.join(SHOTS, name)
    cv2.imwrite(path, output)
    return path


pytestmark = pytest.mark.skipif(
    not MODEL, reason="hand_landmarker.task is absent; run tools/fetch_hand_model.py")


def test_real_single_hand_returns_21_landmarks_and_a_side(effects):
    image = _read("human_hand.jpg")
    effect = effects(image)
    detections = effect._detect_landmarks(image)
    print("single hand:", _describe(detections))
    assert detections, "MediaPipe found no hand in the one-hand fixture"
    assert all(len(item.points) == 21 for item in detections)
    assert all(item.extra.get("side") in ("Left", "Right") for item in detections)
    saved = _save(effects, "single_hand.png", image, detections)
    assert os.path.getsize(saved) > 0


def test_real_two_hands_return_two_21_point_skeletons_and_sides(effects):
    image = _read("two_hands.jpg")
    effect = effects(image)
    detections = effect._detect_landmarks(image)
    sides = _describe(detections)
    print("two hands:", sides)
    assert len(detections) == 2, f"expected both hands, got {sides}"
    assert all(len(item.points) == 21 for item in detections)
    assert {item.extra.get("side") for item in detections} == {"Left", "Right"}
    saved = _save(effects, "two_hands.png", image, detections)
    assert os.path.getsize(saved) > 0


def test_real_max_hands_limits_the_two_hand_fixture(effects):
    image = _read("two_hands.jpg")
    effect = effects(image)
    effect.max_hands.set(1)
    assert len(effect._detect_landmarks(image)) == 1
    effect.max_hands.set(2)
    assert len(effect._detect_landmarks(image)) == 2


def test_lighting_motion_and_frame_entry_are_recorded(effects):
    image = _read("two_hands.jpg")
    height, width = image.shape[:2]
    variants = {
        "dark": np.clip(image.astype(np.float32) * 0.45, 0, 255).astype(np.uint8),
        "bright": np.clip(image.astype(np.float32) * 1.35, 0, 255).astype(np.uint8),
        "moved": cv2.warpAffine(image, np.float32([[1, 0, -180], [0, 1, 40]]), (width, height)),
        "entering": cv2.warpAffine(image, np.float32([[1, 0, -560], [0, 1, 0]]), (width, height)),
        "leaving": cv2.warpAffine(image, np.float32([[1, 0, 560], [0, 1, 0]]), (width, height)),
        "out_of_frame": cv2.warpAffine(image, np.float32([[1, 0, 2000], [0, 1, 0]]), (width, height)),
    }
    results = {}
    for name, variant in variants.items():
        # Record every outcome: these are observed model results, not accuracy claims.
        effect = effects(variant)
        detections = effect._detect_landmarks(variant)
        results[name] = _describe(detections)
        if detections:
            _save(effects, f"{name}.png", variant, detections)
        assert all(len(item.points) == 21 for item in detections or [])
    print("variants:", results)
    assert results["moved"], "a small translation should still find a hand"
    assert not results["out_of_frame"], "a hand shifted fully out of frame must not be reported"
