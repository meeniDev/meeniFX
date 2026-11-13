"""Simple Choker: matte growth and contraction on shapes, text, PNGs and keyframes.

The effect is driven directly on real BGRA layers (so a clipped growth, a washed-out
interior or a dark fringe fails without a window) and through the engine for keyframes,
preview/export parity and the project file.
"""

import os
import sys
import time
import uuid

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from conftest import build_engine, frame_diff  # noqa: E402
from meenifx import thumbnails as THUMBS  # noqa: E402
from meenifx.effects import matte as MATTE  # noqa: E402
from meenifx.effects.matte import SimpleChokerEffect  # noqa: E402
from meenifx.engine import EditorEngine  # noqa: E402
from meenifx.model import shape as SHAPE  # noqa: E402
from meenifx.processing import shapes as SHAPE_RENDER  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = (320, 180)
DEFAULTS = {"choke": 0.0, "softness": 0.0, "quality": "balanced", "preserve_rgb": True}


def pump(seconds=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.004)


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module")
def window(app):
    MainWindow._load_demo_project = lambda self: None
    editor = MainWindow(app)
    editor.resize(1440, 900)
    editor.show()
    pump(0.2)
    yield editor
    editor.projects._set_clean()
    editor.close()


@pytest.fixture()
def editor(window, tmp_path):
    window.projects._set_clean()
    settings = ProjectSettings(name=f"Choke {uuid.uuid4().hex[:6]}", width=PROJECT[0],
                               height=PROJECT[1], fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    window.engine.session.set_playhead(0)
    pump(0.08)
    return window


@pytest.fixture(scope="module")
def engine():
    return build_engine()[0]


@pytest.fixture(autouse=True)
def _fresh_cache():
    MATTE.clear_cache()
    yield
    MATTE.clear_cache()


# ----------------------------------------------------------------------- helpers
def choker(engine=None, **params):
    effect = SimpleChokerEffect(engine)
    effect.param_scale = 1.0
    effect.local_frame = 0
    values = dict(DEFAULTS)
    values.update(params)
    effect.load_params(values)
    return effect


class _ShapeClip:
    """The only thing the shape model needs: a settings dict."""

    def __init__(self, kind="ellipse", **overrides):
        self.shape = SHAPE.blank(kind)
        self.shape.update(overrides)


def shape_layer(kind="ellipse", size=(320, 180), **overrides):
    overrides.setdefault("width", 150.0)
    overrides.setdefault("height", 100.0)
    spec = SHAPE.resolve(_ShapeClip(kind, **overrides), 0)
    return SHAPE_RENDER.render(spec, size[0], size[1], 1.0)


def choke(layer, **params):
    return choker(**params).apply_layer(layer)


def painted(mask, level=128):
    return int((mask >= level).sum())


def bbox(mask, level=128):
    rows = np.flatnonzero((mask >= level).any(axis=1))
    cols = np.flatnonzero((mask >= level).any(axis=0))
    assert rows.size and cols.size, "the layer has no visible pixels"
    return int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])


def band(mask):
    return int(((mask > 8) & (mask < 247)).sum())


# -------------------------------------------------------------------- the effect
def test_it_is_registered_as_a_matte_effect():
    effect = SimpleChokerEffect(None)
    assert SimpleChokerEffect in EditorEngine.EFFECT_TYPES
    assert effect.key == "simple_choker" and effect.name == "Simple Choker"
    assert effect.category == "Matte / Keying"
    assert effect.MEDIA_TYPES == ("video",)
    names = {spec.name for spec in effect.PARAMS}
    assert {"choke", "softness", "quality", "preserve_rgb"} <= names
    choke_spec = effect.spec("choke")
    assert choke_spec.unit.strip() == "px" and choke_spec.pixels
    assert choke_spec.minimum < 0.0 < choke_spec.maximum
    advanced = {spec.name for spec in effect.PARAMS if spec.advanced}
    assert {"quality", "preserve_rgb"} <= advanced, "technical controls stay folded"
    assert effect.NOTE and "alpha" in effect.NOTE.lower()


@pytest.mark.parametrize("kind", ["rectangle", "rounded_rectangle", "ellipse", "polygon", "star"])
def test_zero_choke_returns_the_layer_untouched(kind):
    layer = shape_layer(kind)
    out = choke(layer, choke=0.0, softness=0.0)
    assert out is layer, "a neutral choke must not even copy the picture"
    assert np.array_equal(out, layer)


def test_zero_choke_keeps_partial_alpha_exactly():
    layer = shape_layer("ellipse")
    layer[:, :, 3] = (layer[:, :, 3].astype(np.float32) * 0.45).astype(np.uint8)
    out = choke(layer, choke=0.0, softness=0.0)
    assert np.array_equal(out[:, :, 3], layer[:, :, 3])


def test_positive_choke_shrinks_the_visible_matte():
    layer = shape_layer("ellipse")
    before = bbox(layer[:, :, 3])
    out = choke(layer, choke=8.0)
    after = bbox(out[:, :, 3])
    assert painted(out[:, :, 3]) < painted(layer[:, :, 3])
    assert abs((after[0] - before[0]) - 8) <= 1, (before, after)
    assert abs((before[2] - after[2]) - 8) <= 1, (before, after)
    assert int(out[90, 160, 3]) == 255, "a choke moves the edge, it does not fade the fill"


def test_negative_choke_grows_the_visible_matte():
    layer = shape_layer("ellipse")
    before = bbox(layer[:, :, 3])
    out = choke(layer, choke=-8.0)
    after = bbox(out[:, :, 3])
    assert painted(out[:, :, 3]) > painted(layer[:, :, 3])
    assert abs((before[0] - after[0]) - 8) <= 1, (before, after)
    assert abs((after[2] - before[2]) - 8) <= 1, (before, after)
    assert abs((before[1] - after[1]) - 8) <= 1, (before, after)
    assert abs((after[3] - before[3]) - 8) <= 1, (before, after)


@pytest.mark.parametrize("kind", SHAPE.KINDS)
def test_choke_moves_every_shape_edge(kind):
    # A 1 px erosion can legitimately erase the default 2 px line entirely.
    # Use a substantial stroke when asserting that some interior must survive.
    layer = shape_layer(kind, width=150.0, height=90.0, stroke_width=8.0)
    before = painted(layer[:, :, 3])
    shrunk = choke(layer, choke=1.0)
    grown = choke(layer, choke=-4.0)
    assert 0 < painted(shrunk[:, :, 3]) < before, kind
    assert painted(grown[:, :, 3]) > before, kind


def test_a_fractional_choke_keeps_an_antialiased_edge():
    out = choke(shape_layer("ellipse"), choke=3.5)
    assert band(out[:, :, 3]) > 100, "a fractional choke must not leave a hard 0/255 step"
    assert painted(out[:, :, 3]) > 0


def test_partially_transparent_interiors_keep_their_opacity():
    layer = shape_layer("ellipse")
    layer[:, :, 3] = (layer[:, :, 3].astype(np.float32) * 0.55).astype(np.uint8)
    inside = int(layer[90, 160, 3])
    assert 135 <= inside <= 145, inside
    out = choke(layer, choke=6.0)
    assert int(out[90, 160, 3]) == inside, "the visible interior must keep its 55 % alpha"
    assert int(out[90, 100, 3]) == inside, "the fill behind the new edge is unchanged"
    assert int(out[90, 89, 3]) < inside * 0.4, "the edge itself moved inward"


def test_edge_softness_widens_the_transition_band():
    crisp = choke(shape_layer("ellipse"), choke=6.0, softness=0.0)[:, :, 3]
    soft = choke(shape_layer("ellipse"), choke=6.0, softness=10.0)[:, :, 3]
    assert band(soft) > band(crisp) * 1.8, (band(crisp), band(soft))
    assert band(soft) > 800


def test_growing_the_edge_does_not_add_a_dark_rim():
    layer = shape_layer("ellipse")
    layer[:, :, 0] = 30
    layer[:, :, 1] = 30
    layer[:, :, 2] = 230                      # BGR red
    out = choke(layer, choke=-10.0)
    grown = (out[:, :, 3] > 200) & (layer[:, :, 3] == 0)
    assert int(grown.sum()) > 500, "the matte did not grow"
    ring = out[grown][:, :3].astype(np.float32)
    assert ring.mean(axis=0)[2] > 180, "the new ring took the shape's red, not black"
    assert ring.mean(axis=0)[2] - ring.mean(axis=0)[0] > 120


def test_fully_opaque_footage_is_left_alone_and_explained():
    layer = np.dstack([np.full((90, 160, 3), 120, np.uint8),
                       np.full((90, 160), 255, np.uint8)])
    effect = choker(choke=12.0)
    out = effect.apply_layer(layer)
    assert np.array_equal(out, layer)
    note = effect.last_note.lower()
    assert "alpha" in note and "matte" in note


def test_a_fully_transparent_layer_is_left_alone():
    layer = np.zeros((40, 40, 4), np.uint8)
    effect = choker(choke=4.0)
    assert np.array_equal(effect.apply_layer(layer), layer)


def test_edge_quality_selects_the_distance_field_accuracy():
    assert MATTE._QUALITY["fast"][1] < MATTE._QUALITY["balanced"][1]
    assert MATTE._QUALITY["precise"][0] == cv2.DIST_MASK_5
    layer = shape_layer("ellipse")
    boxes = [bbox(choke(layer, choke=6.0, quality=name)[:, :, 3]) for name in MATTE._QUALITY]
    for box in boxes[1:]:
        assert max(abs(a - b) for a, b in zip(boxes[0], box)) <= 2, boxes


def test_repeated_renders_reuse_the_cached_edge():
    layer = shape_layer("ellipse")
    effect = choker(choke=6.0)
    first = effect.apply_layer(layer)
    second = effect.apply_layer(layer)
    assert np.array_equal(first, second)
    assert len(MATTE._CACHE) == 1, "the same matte and parameters must hit the cache"
    changed = layer.copy()
    changed[:, :, 3] = 0
    changed[60:120, 100:220, 3] = 255
    effect.apply_layer(changed)
    assert len(MATTE._CACHE) == 2, "different matte pixels must not reuse the old edge"


def test_keyframed_choke_changes_the_edge_over_time(engine):
    from meenifx.model.keyframes import AnimatedValue

    curve = AnimatedValue()
    curve.set_keyframe(0, 0.0)
    curve.set_keyframe(20, 12.0)
    layer = shape_layer("ellipse")
    entries = [{"effect_id": "simple_choker", "enabled": True,
                "params": {"choke": curve, "softness": 0.0, "quality": "balanced",
                           "preserve_rgb": True}}]
    start = engine.apply_effects_layer(layer.copy(), entries, 0, 1.0)
    end = engine.apply_effects_layer(layer.copy(), entries, 20, 1.0)
    assert painted(end[:, :, 3]) < painted(start[:, :, 3]) - 500


def test_the_browser_thumbnail_shows_a_choked_matte(engine):
    settings = THUMBS.PreviewSettings(background="checker", quality="medium")
    reference = THUMBS.reference_layer(140, 96, settings)
    assert reference is not None
    plain = THUMBS._alpha_over(THUMBS.background_layer(140, 96, settings.background),
                               THUMBS._alpha_demo_layer(reference))
    image = THUMBS.render_effect(engine, "simple_choker", 140, 96, settings)
    assert image is not None and image.shape == (96, 140, 3)
    assert frame_diff(image, plain) > 1.0, "the card must show the edge moving"


# ------------------------------------------------------------------ real layers
def test_text_layers_can_be_choked(editor):
    window = editor
    clip_id = window.add_text_clip()
    assert clip_id is not None
    clip = window.engine.timeline.find_clip(clip_id)
    pump(0.1)
    plain = window.engine.render_frame(0, 1.0)[1]
    clip.effects = [{"effect_id": "simple_choker", "enabled": True,
                     "params": {"choke": 4.0, "softness": 1.0}}]
    choked = window.engine.render_frame(0, 1.0)[1]
    assert frame_diff(plain, choked) > 0.3, "the choker did nothing to a text layer"


def test_a_transparent_png_can_be_choked(editor, tmp_path):
    window = editor
    path = str(tmp_path / "badge.png")
    picture = np.zeros((120, 200, 4), np.uint8)
    picture[:, :, :3] = (60, 180, 60)
    cv2.ellipse(picture, (100, 60), (70, 40), 0, 0, 360, (60, 180, 60, 255), -1)
    assert cv2.imwrite(path, picture)
    from meenifx.project.media import media_type_for
    project = window.projects.project
    item = project.library.add(path, 2.0, 200, 120, 30.0, media_type=media_type_for(path))
    window.project_panel.rebuild(force=True)
    clips = window.insert_media_items([item], frame=0)
    assert clips, "the PNG was not inserted"
    clip = window.engine.timeline.find_clip(clips[0])
    window.engine.session.set_selection({clip.id})
    pump(0.12)
    plain = window.engine.render_frame(0, 1.0)[1]
    clip.effects = [{"effect_id": "simple_choker", "enabled": True,
                     "params": {"choke": 8.0, "softness": 2.0}}]
    choked = window.engine.render_frame(0, 1.0)[1]
    assert frame_diff(plain, choked) > 0.5, "the PNG's alpha edge was not choked"


def test_preview_and_export_agree_on_the_choked_edge(editor):
    window = editor
    window.engine.session.set_playhead(0)
    clip_id = window.engine.session.add_shape_clip("ellipse", box=(60, 40, 200, 120))
    clip = window.engine.timeline.find_clip(clip_id)
    plain = window.engine.render_frame(0, 1.0)[1]
    clip.effects = [{"effect_id": "simple_choker", "enabled": True,
                     "params": {"choke": 8.0, "softness": 2.0}}]
    full = window.engine.render_frame(0, 1.0)[1]
    preview = window.engine.render_frame(0, 0.5)[1]
    reduced = cv2.resize(full, (preview.shape[1], preview.shape[0]),
                         interpolation=cv2.INTER_AREA)
    assert frame_diff(reduced, preview) < 6.0, "the preview and the export disagree"
    assert frame_diff(plain, full) > 1.0, "the effect never reached the exported frame"


def test_the_choker_survives_save_and_reopen(editor):
    window = editor
    clip_id = window.engine.session.add_shape_clip("ellipse", box=(60, 40, 200, 120))
    clip = window.engine.timeline.find_clip(clip_id)
    clip.effects = [{"effect_id": "simple_choker", "enabled": True,
                     "params": {"choke": 9.0, "softness": 3.0, "quality": "precise",
                                "preserve_rgb": False}}]
    path = window.projects.project.path
    assert window.projects.save()
    pump(0.1)
    assert window.projects.close_project()
    pump(0.1)
    assert window.projects.open_path(path)
    pump(0.25)
    reloaded = window.engine.timeline.find_clip(clip_id)
    assert reloaded is not None and reloaded.is_shape
    entry = next(item for item in reloaded.effects if item["effect_id"] == "simple_choker")
    assert abs(float(entry["params"]["choke"]) - 9.0) < 0.01
    assert abs(float(entry["params"]["softness"]) - 3.0) < 0.01
    assert entry["params"]["quality"] == "precise"
    assert bool(entry["params"]["preserve_rgb"]) is False
