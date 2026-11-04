"""Transform, keyframe, easing, warp and persistence tests (headless, no real media)."""

import copy
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import build_engine, frame_diff, synthetic_frame  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import easing  # noqa: E402
from meenifx.model import transform as T  # noqa: E402
from meenifx.model.keyframes import AnimatedValue  # noqa: E402


def bright_column(image):
    """Column of the brightest pixel: a cheap, robust position probe."""
    grey = image.astype(np.float32).mean(axis=2)
    return float(np.unravel_index(int(np.argmax(grey)), grey.shape)[1])


def content_box(image, background=(0, 0, 0)):
    """Bounding box of pixels that differ from the background."""
    mask = np.any(np.abs(image.astype(np.int16) - np.array(background, np.int16)) > 6, axis=2)
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


# ------------------------------------------------------------------ transform math
def test_identity_transform_keeps_the_imported_look():
    engine, clip = build_engine()
    base, _ = engine.render_frame(0, 1.0)
    T.ensure_transform(clip)
    assert T.is_identity(clip, 0)
    assert frame_diff(base, synthetic_frame()) == 0.0


def test_position_moves_the_picture_in_project_pixels():
    engine, clip = build_engine()
    base, _ = engine.render_frame(0, 1.0)
    before = content_box(base)
    T.set_channel(clip, "position_x", 40.0)
    T.set_channel(clip, "position_y", -20.0)
    moved, _ = engine.render_frame(0, 1.0)
    after = content_box(moved)
    assert after is not None
    # The bright frame border makes the box measurable; the shift must be exact.
    assert abs((after[0] - before[0]) - 40) <= 1
    # The clip is frame-sized, so only the bottom edge can move up.
    assert abs((after[3] - before[3]) + 20) <= 1


def test_scale_rotation_opacity_and_flip_all_render():
    engine, clip = build_engine()
    T.set_channel(clip, "scale_x", 50.0)
    T.set_channel(clip, "scale_y", 50.0)
    half, _ = engine.render_frame(0, 1.0)
    corner = half[:20, :20]
    assert float(np.abs(corner.astype(np.int16)).mean()) < 4, "scaled-down clip must clear the corners"

    T.reset_transform(clip)
    T.set_channel(clip, "rotation", 90.0)
    rotated, _ = engine.render_frame(0, 1.0)
    assert frame_diff(rotated, half) > 1.0

    T.reset_transform(clip)
    plain, _ = engine.render_frame(0, 1.0)
    T.set_channel(clip, "opacity", 50.0)
    faded, _ = engine.render_frame(0, 1.0)
    assert float(faded.mean()) < float(plain.mean())

    T.reset_transform(clip)
    base, _ = engine.render_frame(0, 1.0)
    clip.transform["flip_h"] = True
    flipped, _ = engine.render_frame(0, 1.0)
    assert frame_diff(np.fliplr(base), flipped) < 1.0


def test_anchor_point_never_moves_the_picture_by_itself():
    engine, clip = build_engine()
    base, _ = engine.render_frame(0, 1.0)
    # Moving anchor and compensating position keeps every pixel where it was.
    state = T.evaluate(clip, 0)
    state.anchor_x, state.anchor_y = 30.0, -12.0
    T.set_channel(clip, "anchor_x", 30.0)
    T.set_channel(clip, "anchor_y", -12.0)
    T.set_channel(clip, "position_x", 30.0)
    T.set_channel(clip, "position_y", -12.0)
    same, _ = engine.render_frame(0, 1.0)
    assert frame_diff(base, same) < 1.0


# --------------------------------------------------------------------- keyframes
def test_position_keyframes_animate_and_render_at_the_right_time():
    engine, clip = build_engine()
    x = T.animate_channel(clip, "position_x", 0)
    x.set_keyframe(0, 0.0)
    x.set_keyframe(30, 90.0)
    engine.session.playhead = 15
    assert abs(T.evaluate(clip, 15).position_x - 45.0) < 1.5
    start, _ = engine.render_frame(0, 1.0)
    middle, _ = engine.render_frame(15, 1.0)
    assert content_box(middle)[0] - content_box(start)[0] > 30


def test_ease_presets_produce_real_curves():
    bounce = [easing.ease_out_bounce(t / 100.0) for t in range(101)]
    elastic = [easing.ease_out_elastic(t / 100.0) for t in range(101)]
    spring = [easing.ease_spring(t / 100.0) for t in range(101)]
    assert abs(bounce[0]) < 1e-9 and abs(bounce[-1] - 1.0) < 1e-9
    # Bounce really bounces: it comes back down before settling.
    assert any(bounce[i] > bounce[i + 1] for i in range(1, 100))
    assert abs(elastic[-1] - 1.0) < 1e-9 and any(value > 1.0 for value in elastic)
    assert abs(spring[-1] - 1.0) < 1e-9


def test_keyframe_curve_is_identical_in_preview_and_export_scale():
    engine, clip = build_engine()
    x = T.animate_channel(clip, "position_x", 0)
    x.set_keyframe(0, 0.0)
    x.set_keyframe(24, 60.0)
    x.keyframes[0].interpolation = "fn:bounce_out"
    full, _ = engine.render_frame(12, 1.0)
    quarter, _ = engine.render_frame(12, 0.5)
    upscaled = np.array(__import__("cv2").resize(quarter, (full.shape[1], full.shape[0]),
                                                 interpolation=__import__("cv2").INTER_LINEAR))
    assert frame_diff(full, upscaled) < 6.0
    # And the value the exporter uses is the same one the preview shows.
    assert abs(T.evaluate(clip, 12).position_x - x.value_at(12)) < 1e-6


# ------------------------------------------------------------------------- warps
def test_puppet_warp_deforms_and_is_stable_without_pins():
    engine, clip = build_engine()
    clip.effects = [{"effect_id": "puppet_warp", "enabled": True, "params": {},
                     "pins": [{"id": 1, "src": [0.25, 0.25], "x": 0.25, "y": 0.25}]}]
    still, _ = engine.render_frame(0, 1.0)
    base, _ = engine.render_frame(0, 1.0)
    assert frame_diff(still, base) < 0.5, "a pin that has not moved must not deform anything"
    pin = clip.effects[0]["pins"][0]
    pin["x"], pin["y"] = 0.45, 0.45
    warped, _ = engine.render_frame(0, 1.0)
    assert frame_diff(warped, base) > 2.0
    # Deformation is local: the pinned area moves far more than the opposite corner.
    near = frame_diff(warped[20:110, 40:130], base[20:110, 40:130])
    far = frame_diff(warped[120:, 220:], base[120:, 220:])
    assert near > 10.0 and far < near * 0.8, (near, far)


def test_puppet_warp_animates_through_the_shared_keyframe_evaluator():
    engine, clip = build_engine()
    pin = {"id": 1, "src": [0.3, 0.3], "x": AnimatedValue(base_value=0.3), "y": AnimatedValue(base_value=0.3)}
    pin["x"].set_keyframe(0, 0.3)
    pin["x"].set_keyframe(30, 0.6)
    pin["y"].set_keyframe(0, 0.3)
    pin["y"].set_keyframe(30, 0.6)
    clip.effects = [{"effect_id": "puppet_warp", "enabled": True, "params": {}, "pins": [pin]}]
    first, _ = engine.render_frame(0, 1.0)
    middle, _ = engine.render_frame(15, 1.0)
    assert frame_diff(first, middle) > 1.0
    channel = CH.pin_channel("puppet_warp", 1, "x")
    assert abs(CH.value_at(clip, channel, 15) - 0.45) < 1e-6
    assert CH.is_animated(clip, channel)


def test_corner_pin_mesh_warp_skew_perspective_and_distortion_change_the_picture():
    engine, clip = build_engine()
    base, _ = engine.render_frame(0, 1.0)
    cases = [
        ("corner_pin", {"corner_tl_x": 0.15, "corner_tl_y": 0.05}),
        ("perspective", {"tilt_x": 25.0}),
        ("mesh_warp", {"node_1_1_x": 0.12, "node_1_1_y": -0.08}),
        ("skew", {"skew_x": 12.0}),
        ("distortion", {"amount": 60.0}),
    ]
    for effect_id, params in cases:
        clip.effects = [{"effect_id": effect_id, "enabled": True, "params": dict(params)}]
        changed, _ = engine.render_frame(0, 1.0)
        assert frame_diff(base, changed) > 0.5, effect_id
    clip.effects = [{"effect_id": "corner_pin", "enabled": True, "params": {}}]
    unchanged, _ = engine.render_frame(0, 1.0)
    assert frame_diff(base, unchanged) < 1.0, "default corner pin is an identity"


# ------------------------------------------------------------------ persistence
def test_project_roundtrip_keeps_transform_keys_pins_and_easing(tmp_path=None):
    import tempfile
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = build_engine()
    clip.transform["scale_x"] = 140.0
    angle = T.animate_channel(clip, "rotation", 0)
    angle.set_keyframe(0, 0.0)
    angle.set_keyframe(20, 45.0)
    angle.keyframes[0].interpolation = "fn:bounce_out"
    angle.keyframes[0].out_value_handle = (0.12, 0.72)
    T.set_path_handle(clip, 0, "out", (20.0, -10.0))
    clip.effects = [{"effect_id": "puppet_warp", "enabled": True,
                     "params": {"stiffness": 1.4}, "mesh_density": 18,
                     "pins": [{"id": 1, "src": [0.4, 0.6], "x": 0.5, "y": 0.7, "locked": False},
                              {"id": 2, "src": [0.7, 0.2], "x": 0.7, "y": 0.2, "locked": True}]}]

    sections = timeline_to_sections(engine.timeline, lambda path: "media-1")
    copy_sections = copy.deepcopy(sections)
    restored = sections_to_timeline(copy_sections, 30, lambda media_id: "synthetic-effect-test.png")
    rebuilt = restored.all_clips()[0]

    assert rebuilt.transform["scale_x"] == 140.0
    animated = rebuilt.transform["rotation"]
    assert isinstance(animated, AnimatedValue)
    assert [key.time for key in animated.keyframes] == [0, 20]
    assert animated.keyframes[0].interpolation == "fn:bounce_out"
    assert tuple(animated.keyframes[0].out_value_handle) == (0.12, 0.72)
    assert T.path_handle(rebuilt, 0, "out") == (20.0, -10.0)
    pins = rebuilt.effects[0]["pins"]
    assert [pin["id"] for pin in pins] == [1, 2]
    assert pins[1]["locked"] is True
    assert pins[0]["x"] == 0.5
    assert rebuilt.effects[0]["mesh_density"] == 18, "non-param effect data must survive"
    assert rebuilt.effects[0]["params"]["stiffness"] == 1.4


def test_format_two_projects_still_load():
    from meenifx.project.container import migrate

    sections = {"clips": {"id": [1], "transform_absent": True},
                "effects": {"clip": [], "effect_id": []},
                "keyframes": {"owners": [], "owner": []}}
    migrated = migrate(copy.deepcopy(sections), 2)
    assert migrated["clips"]["transform"] == [{}]
    assert migrated["clips"]["meta"] == [{}]


# ----------------------------------------------------------------- undo/session
def test_drag_is_one_undo_step():
    engine, clip = build_engine()
    stack = engine.session.stack
    engine.session.begin_gesture()
    for step in range(30):
        T.set_channel(clip, "position_x", float(step * 2))
    assert engine.session.commit_gesture("Move clip")
    assert stack.can_undo
    depth = len(stack._undo)
    engine.session.undo()
    assert engine.timeline.find_clip(clip.id).transform["position_x"] == 0.0
    engine.session.redo()
    assert engine.timeline.find_clip(clip.id).transform["position_x"] == 58.0
    assert len(stack._undo) == depth


def test_auto_keyframe_creates_keys_only_when_enabled():
    engine, clip = build_engine()
    engine.session.auto_keyframe.set(False)
    T.animate_channel(clip, "opacity", 0)
    engine.session.set_playhead(10)
    CH.set_value(clip, "transform.opacity", 40.0, 10, auto_key=False)
    keys = CH.key_times(clip, "transform.opacity")
    assert keys == [0], "Auto-Keyframe off must not invent a keyframe"
    engine.session.auto_keyframe.set(True)
    CH.set_value(clip, "transform.opacity", 60.0, 20, auto_key=True)
    assert CH.key_times(clip, "transform.opacity") == [0, 20]
