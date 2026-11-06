"""Every effect parameter must change the picture, and animated ones must move.

The audit drives the real pipeline (`EditorEngine.render_frame`) with the ranges the
properties panel builds its controls from, so a control that writes a value the
effect never reads - or a range the effect ignores - fails here instead of looking
dead in the app.
"""

import numpy as np
import pytest

from conftest import build_engine, frame_diff, render, synthetic_frame

# Effects whose subject is transparency: they need a source with an alpha edge, so the audit
# feeds them a matted version of the synthetic frame instead of the fully opaque one.
ALPHA_SOURCE_EFFECTS = {"simple_choker"}


EFFECT_KEYS = ("mosaic", "ascii", "face_tracker", "hand_tracker", "blob_tracker")


def alpha_frame(width=320, height=180):
    """The synthetic picture behind a soft matte, so an alpha effect has an edge to move."""
    import cv2
    frame = cv2.cvtColor(synthetic_frame(width, height), cv2.COLOR_BGR2BGRA)
    mask = np.zeros((height, width), np.uint8)
    cv2.ellipse(mask, (width // 2, height // 2), (width // 3, height // 3), 0, 0, 360, 255, -1)
    frame[:, :, 3] = cv2.GaussianBlur(mask, (0, 0), 6.0)
    return frame


def _specs(kind="int"):
    from meenifx.engine import EditorEngine
    cases = []
    for effect_class in EditorEngine.EFFECT_TYPES:
        if "video" not in effect_class.MEDIA_TYPES:
            # Audio effects never touch a picture; their parameters are measured on the
            # rendered signal in tests/test_audio_effects.py instead.
            continue
        for spec in effect_class.PARAMS:
            if (kind == "numeric" and spec.kind in ("int", "float")) or spec.kind == kind:
                cases.append((effect_class.key, spec))
    return cases


NUMERIC_CASES = _specs("numeric")
BOOL_CASES = _specs("bool")
CHOICE_CASES = _specs("choice")

# Parameters whose two extremes are the same picture by definition: rotating the hue by
# -180 and +180 degrees is one half turn either way, and a directional smear at -180 and
# +180 degrees runs along the same line. Both are correct; the audit's "min and max must
# differ" rule cannot describe them, so they are checked in
# tests/test_effects_extended.py instead (hue turns the colour wheel, angle turns the
# smear).
SYMMETRIC_EXTREMES = {("color_correct", "hue"), ("directional_blur", "angle"),
                      ("motion_blur", "angle"),
                      # A duotone tint at -180° and +180° is the same hue; the tint
                      # wheel is covered directionally in tests/test_effects_new.py.
                      ("duotone", "shadow_hue"), ("duotone", "highlight_hue")}

# Switches that only mean something with the right input: keeping edges sharp needs a
# transparent layer, and following the animation needs a moving clip. Both have
# dedicated tests in tests/test_effects_extended.py.
# Effects whose parameters describe *time* rather than a single frame: the response
# time of a flash, the frame rate of a held picture, the spacing of echoes. One still
# frame cannot show them, so they are tested across frames in tests/test_effects_new.py.
TEMPORAL_CASES = {
    ("wave_warp", "speed"), ("exposure_flicker", "speed"),
    ("flash", "duration"), ("flash", "interval"), ("flash", "envelope"),
    ("posterize_time", "step"), ("posterize_time", "offset"),
    ("stutter", "interval"), ("stutter", "hold"), ("stutter", "offset"),
    ("echo", "count"), ("echo", "step"), ("echo", "decay"),
    ("echo", "offset_x"), ("echo", "offset_y"), ("echo", "blend"),
}

# Group cards draw nothing on their own: the Look card's Strength scales its stack, which
# is checked end-to-end in tests/test_looks.py.
GROUP_CARDS = {("look", "strength")}

CONTEXT_SWITCHES = {("gaussian_blur", "keep_edges"), ("motion_blur", "follow_motion"),
                    # Puppet Warp only deforms when the clip carries pins, and its mesh
                    # overlay is a preview drawing that is never exported.
                    ("puppet_warp", "mesh_density"), ("puppet_warp", "stiffness"),
                    ("puppet_warp", "show_mesh"),
                    # Connector count needs three detections to be visible; the synthetic
                    # tracking frames have only two.  Dedicated tests drive the shared
                    # drawing directly with a larger set.
                    ("face_tracker", "max_connections"),
                    ("hand_tracker", "max_connections"),
                    ("blob_tracker", "max_connections"),
                    # Quality presets only change the working resolution on a large frame;
                    # a 320x180 audit frame is already below every preset's limit.
                    ("face_tracker", "tracking_quality"),
                    ("hand_tracker", "tracking_quality"),
                    ("blob_tracker", "tracking_quality")}


def blob_frame(width=640, height=480):
    """Black background plus blobs sized so every area range has headroom."""
    import cv2
    frame = np.zeros((height, width, 3), np.uint8)
    for cx, cy, radius in ((40, 60, 10), (150, 80, 30), (300, 120, 60), (140, 330, 98)):
        cv2.circle(frame, (cx, cy), radius, (255, 255, 255), -1)
    frame[140:460, 420:620] = (250, 250, 250)   # one 400x320 region (area 128000)
    return frame


def _engine_for(effect_id, monkeypatch):
    frame = blob_frame() if effect_id == "blob_tracker" else synthetic_frame()
    if effect_id in ALPHA_SOURCE_EFFECTS:
        frame = alpha_frame()
    engine, clip = build_engine(frame=frame)
    if effect_id == "face_tracker":
        # Face detection needs a real face; the drawing parameters are what is audited.
        runtime = engine.runtime_effects["face_tracker"]
        monkeypatch.setattr(runtime, "detect_faces",
                            lambda frame: [(40, 30, 60, 60), (200, 110, 90, 90)])
    return engine, clip


def _base_params(engine, effect_id):
    return dict(engine.runtime_effects[effect_id].capture_params())


def _prepared_base(effect_id, name, params):
    """Parameters that must be open for a test value to be reachable."""
    base = dict(params)
    if name == "palette_text":
        base["color_mode"] = "palette"
    if effect_id == "blob_tracker":
        # Keep every blob reachable so min/max of one parameter is what changes.
        base.update(min_area=1, max_area=400000, max_blobs=80, threshold=150)
    if effect_id == "face_tracker" and name in ("blur_amount", "blur_feather"):
        # The blur controls are intentional no-ops until "Blur faces" is enabled.
        base["blur_faces"] = True
    if name in ("connector_mode", "connector_distance", "max_connections"):
        # Bring the synthetic detections inside the connector radius.  Their default
        # layout is deliberately spread out to exercise per-object drawing.
        base["connector_distance"] = 100
    if name == "connector_distance":
        base["connector_mode"] = "nearest"
    if effect_id == "motion_blur":
        # A still clip has no measured movement, so the shutter has nothing to smear:
        # give the effect an explicit manual smear to work with.
        base.update(follow_motion=False, length=40.0, angle=0.0)
    if effect_id == "mesh_warp":
        # The grid resolution only matters once a node is displaced.
        base.update(node_1_1_x=0.2, node_1_1_y=-0.15)
    return base


@pytest.mark.parametrize("effect_id,spec", NUMERIC_CASES, ids=[f"{k}.{s.name}" for k, s in NUMERIC_CASES])
def test_numeric_parameter_extremes_change_the_output(effect_id, spec, monkeypatch):
    if effect_id == "face_tracker" and spec.name == "confidence":
        pytest.skip("detector argument; covered by test_face_confidence_reaches_the_detector")
    if (effect_id, spec.name) in SYMMETRIC_EXTREMES:
        pytest.skip("min and max are the same picture; covered by tests/test_effects_extended.py")
    if (effect_id, spec.name) in CONTEXT_SWITCHES:
        pytest.skip("needs pins, transparency or a moving clip; covered by tests/test_effects_extended.py")
    if (effect_id, spec.name) in TEMPORAL_CASES:
        pytest.skip("frame-rate parameter; covered by tests/test_effects_new.py")
    if (effect_id, spec.name) in GROUP_CARDS:
        pytest.skip("group card; covered by tests/test_looks.py")
    engine, clip = _engine_for(effect_id, monkeypatch)
    base = _prepared_base(effect_id, spec.name, _base_params(engine, effect_id))
    low = spec.minimum
    high = spec.maximum
    if spec.name == "palette_text":
        low, high = "#ff0000", "#00ff00"
    image_low = render(engine, clip, effect_id, {**base, spec.name: low})
    image_high = render(engine, clip, effect_id, {**base, spec.name: high})
    diff = frame_diff(image_low, image_high)
    assert diff > 0.2, (f"{effect_id}.{spec.name} renders identically at {low} and {high} "
                        f"(diff {diff:.3f}) - the control is dead")


@pytest.mark.parametrize("effect_id,spec", BOOL_CASES, ids=[f"{k}.{s.name}" for k, s in BOOL_CASES])
def test_toggle_parameter_changes_the_output(effect_id, spec, monkeypatch):
    if (effect_id, spec.name) in CONTEXT_SWITCHES:
        pytest.skip("needs transparent input or a moving clip; covered by tests/test_effects_extended.py")
    if (effect_id, spec.name) in TEMPORAL_CASES:
        pytest.skip("frame-rate parameter; covered by tests/test_effects_new.py")
    engine, clip = _engine_for(effect_id, monkeypatch)
    base = _prepared_base(effect_id, spec.name, _base_params(engine, effect_id))
    off = render(engine, clip, effect_id, {**base, spec.name: False})
    on = render(engine, clip, effect_id, {**base, spec.name: True})
    assert frame_diff(off, on) > 0.2, f"{effect_id}.{spec.name} does nothing"


@pytest.mark.parametrize("effect_id,spec", CHOICE_CASES, ids=[f"{k}.{s.name}" for k, s in CHOICE_CASES])
def test_choice_parameter_changes_the_output(effect_id, spec, monkeypatch):
    if (effect_id, spec.name) in CONTEXT_SWITCHES:
        pytest.skip("needs a larger frame or more detections; covered by dedicated tests")
    if (effect_id, spec.name) in TEMPORAL_CASES:
        pytest.skip("frame-rate parameter; covered by tests/test_effects_new.py")
    engine, clip = _engine_for(effect_id, monkeypatch)
    base = _prepared_base(effect_id, spec.name, _base_params(engine, effect_id))
    images = [render(engine, clip, effect_id, {**base, spec.name: choice}) for choice in spec.choices]
    for index in range(1, len(images)):
        assert frame_diff(images[0], images[index]) > 0.2, (
            f"{effect_id}.{spec.name}={spec.choices[index]!r} draws the same as "
            f"{spec.choices[0]!r}")


def test_text_parameter_changes_the_output(monkeypatch):
    engine, clip = _engine_for("ascii", monkeypatch)
    base = _base_params(engine, "ascii")
    plain = render(engine, clip, "ascii", {**base, "charset": ""})
    hashes = render(engine, clip, "ascii", {**base, "charset": "#"})
    assert frame_diff(plain, hashes) > 0.2, "the character set is ignored"


@pytest.mark.parametrize("effect_id,name,low,high", [
    ("mosaic", "scale", 20, 90),
    ("ascii", "cell_size", 4, 40),
    ("hand_tracker", "thickness", 1, 12),
    ("blob_tracker", "thickness", 1, 12),
])
def test_animated_parameter_follows_its_keyframes(effect_id, name, low, high):
    from meenifx.model.keyframes import AnimatedValue, parameter_value_at

    big = effect_id in ("mosaic", "ascii")
    frame = blob_frame() if effect_id == "blob_tracker" else synthetic_frame(640, 360) if big else synthetic_frame()
    engine, clip = build_engine(frame=frame)
    animated = AnimatedValue()
    animated.set_keyframe(0, low)
    animated.set_keyframe(20, high)

    params = _prepared_base(effect_id, name, _base_params(engine, effect_id))
    params[name] = animated
    clip.effects = [{"effect_id": effect_id, "enabled": True, "params": params}]
    frames = {frame: engine.render_frame(frame)[1] for frame in (0, 10, 20)}

    midpoint = int(round((low + high) / 2))
    assert parameter_value_at(animated, 10) == pytest.approx(midpoint, abs=1)
    assert frame_diff(frames[0], frames[10]) > 0.2, "no change between the two keyframes"
    assert frame_diff(frames[10], frames[20]) > 0.2, "the animation stops before its last keyframe"

    static = _prepared_base(effect_id, name, _base_params(engine, effect_id))
    static[name] = midpoint
    reference = render(engine, clip, effect_id, static, frame_no=10)
    assert frame_diff(frames[10], reference) < 1.0


def test_keyframe_times_are_clip_local():
    """A clip that starts at frame 100 must evaluate keyframes at its own local time."""
    from meenifx.model.keyframes import AnimatedValue

    engine, clip = build_engine(frame=synthetic_frame(640, 360), start=100, duration=60)
    animated = AnimatedValue()
    animated.set_keyframe(0, 20)
    animated.set_keyframe(20, 90)
    params = _base_params(engine, "mosaic")
    params["scale"] = animated
    clip.effects = [{"effect_id": "mosaic", "enabled": True, "params": params}]

    at_local_ten = engine.render_frame(110)[1]
    static_55 = render(engine, clip, "mosaic", {**params, "scale": 55}, frame_no=110)
    static_90 = render(engine, clip, "mosaic", {**params, "scale": 90}, frame_no=110)
    assert frame_diff(at_local_ten, static_55) < 1.0, "frame 110 does not use clip-local frame 10"
    assert frame_diff(at_local_ten, static_90) > 0.2, "timeline frame 110 clamped to the last keyframe"


def test_parameter_table_matches_effect_defaults():
    """The control table and the effect's own defaults must agree."""
    engine, _clip = build_engine()
    for runtime in engine.effects:
        values = runtime.param_vars()
        for spec in runtime.PARAMS:
            assert spec.name in values, f"{runtime.key}.{spec.name} has no Value to edit"
            assert values[spec.name].get() == spec.default, (
                f"{runtime.key}.{spec.name} defaults to {values[spec.name].get()!r} "
                f"but its control table says {spec.default!r}")


def test_parameter_ranges_are_sane():
    engine, _clip = build_engine()
    for runtime in engine.effects:
        assert runtime.PARAMS, f"{runtime.key} publishes no parameters"
        for spec in runtime.PARAMS:
            assert spec.maximum > spec.minimum, f"{runtime.key}.{spec.name} has an empty range"
            if spec.kind in ("int", "float"):
                assert spec.minimum <= float(spec.default) <= spec.maximum
                assert spec.step > 0


def test_face_confidence_reaches_the_detector(monkeypatch):
    """Confidence is a detector argument (not a drawing parameter), so check the call."""
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "FaceDetectorYN"):
        pytest.skip("this OpenCV build has no YuNet detector")
    engine, _clip = build_engine()
    runtime = engine.runtime_effects["face_tracker"]
    if runtime._backend != "yunet":
        pytest.skip("face backend is not YuNet here")
    seen = {}

    class Recorder:
        def __init__(self, score):
            self.score = score

        def detect(self, frame):
            seen["score"] = self.score
            return True, np.zeros((0, 15), np.float32)

    def fake_create(model, backend, size, score, nms, top_k):
        return Recorder(score)

    monkeypatch.setattr(cv2, "FaceDetectorYN", type("FD", (), {"create": staticmethod(fake_create)}))
    runtime._detector = None
    runtime._detector_key = None
    runtime.confidence.set(20)
    runtime.detect_faces(synthetic_frame())
    assert seen["score"] == pytest.approx(0.20, abs=0.01)
    runtime.confidence.set(90)
    runtime.detect_faces(synthetic_frame())
    assert seen["score"] == pytest.approx(0.90, abs=0.01)


def test_export_uses_the_same_evaluation(tmp_path):
    """The exporter renders through render_frame(scale=1), keyframes included."""
    import cv2
    from meenifx.model.keyframes import AnimatedValue

    engine, clip = build_engine(frame=synthetic_frame(), duration=12)
    animated = AnimatedValue()
    animated.set_keyframe(0, 20)
    animated.set_keyframe(10, 90)
    clip.effects = [{"effect_id": "mosaic", "enabled": True,
                     "params": {**(_base_params(engine, "mosaic")), "scale": animated}}]

    target = tmp_path / "frame.png"
    path, count, _fps, _size = engine.export({
        "output_path": str(target), "format": "png", "use_source_fps": True,
        "use_source_size": True, "range_start": 0, "range_end": 4,
    })
    written = cv2.imread(str(target))
    assert written is not None and count == 4
    assert frame_diff(written, engine.render_frame(3, 1.0)[1]) < 1.0


def test_pixel_parameters_scale_with_preview_quality():
    """Tile/cell sizes are logical pixels: they shrink with the preview scale."""
    import cv2
    engine, clip = build_engine(frame=synthetic_frame(640, 360), duration=30)
    runtime = engine.runtime_effects["mosaic"]
    params = _base_params(engine, "mosaic")
    params["scale"] = 30
    clip.effects = [{"effect_id": "mosaic", "enabled": True, "params": params}]

    runtime.param_scale = 1.0
    full_tiles = runtime.tile_size(640, 360)
    runtime.param_scale = 0.5
    half_tiles = runtime.tile_size(640, 360)
    runtime.param_scale = 0.35
    preview_tiles = runtime.tile_size(640, 360)
    assert abs(full_tiles[0] * 0.5 - half_tiles[0]) <= 1
    assert abs(full_tiles[1] * 0.5 - half_tiles[1]) <= 1
    assert abs(full_tiles[0] / full_tiles[1] - half_tiles[0] / half_tiles[1]) < 0.2
    assert abs(full_tiles[0] * 0.35 - preview_tiles[0]) <= 1

    full = engine.render_frame(5, 1.0)[1]
    preview = engine.render_frame(5, 0.35)[1]
    assert preview.shape[:2] == (round(full.shape[0] * 0.35), round(full.shape[1] * 0.35))
    # Tiles are whole pixels, so a 35% preview cannot land on exactly the same grid;
    # the picture must still be the same one, not a coarser mosaic.
    assert abs(float(full.mean()) - float(preview.mean())) < 4.0
    size = (32, 18)
    reference = cv2.resize(full, size, interpolation=cv2.INTER_AREA).astype(np.int16)
    coarse = cv2.resize(preview, size, interpolation=cv2.INTER_AREA).astype(np.int16)
    assert float(np.abs(reference - coarse).mean()) < 20.0
