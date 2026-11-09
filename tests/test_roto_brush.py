"""Roto Brush: stroke hints, matte shaping, propagation, cache and project storage."""

import json

import cv2
import numpy as np
import pytest

from conftest import build_engine
from meenifx.project.container import read_container, write_container
from meenifx.project.io import Project, apply as apply_project, collect
from meenifx.project.settings import ProjectSettings
from meenifx.roto.cache import MaskCache, mask_key, mask_size
from meenifx.roto.propagate import frame_order, propagate_seed
from meenifx.roto.segment import (finish_matte, outline_contours, quality_settings,
                                  segment_frame)
from meenifx.roto.strokes import rasterise

WIDTH, HEIGHT = 320, 180
BACKGROUND = (44, 40, 38)
SUBJECT = (220, 150, 60)          # BGR


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto"))


def plate(cx=160, cy=90, radius=46, width=WIDTH, height=HEIGHT):
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:] = BACKGROUND
    cv2.circle(frame, (cx, cy), radius, SUBJECT, -1)
    return frame


def layer_from(frame):
    layer = np.zeros(frame.shape[:2] + (4,), np.uint8)
    layer[:, :, :3] = frame
    layer[:, :, 3] = 255
    return layer


def roto_entry(strokes=None, base_frame=0, **params):
    return {"effect_id": "roto_brush", "enabled": True, "params": dict(params),
            "base_frame": int(base_frame),
            "strokes": strokes if strokes is not None else [
                {"frame": int(base_frame), "points": [[0.5, 0.5]], "radius": 0.08,
                 "mode": "foreground"}]}


def runtime(engine, clip, item, local_frame=0, scale=1.0):
    effect = engine.runtime_effects["roto_brush"]
    effect.clip = clip
    effect.clip_entry = item
    effect.local_frame = int(local_frame)
    effect.param_scale = scale
    params = {spec.name: spec.default for spec in effect.PARAMS}
    params.update(item.get("params") or {})
    effect.load_params(params)
    return effect


def apply(engine, clip, layer, item, local_frame=0, scale=1.0):
    return runtime(engine, clip, item, local_frame, scale).apply_layer(layer.copy())


def test_stroke_segments_the_subject_without_painting_or_erasing_pixels():
    engine, clip = build_engine(duration=30)
    picture = plate()
    out = apply(engine, clip, layer_from(picture), roto_entry())
    assert out[90, 160, 3] > 250, "the subject under the green stroke stays opaque"
    assert out[8, 8, 3] < 8, "the background is removed by alpha, not by deleting it"
    # The pixels of the plate itself are untouched: transparency, not paint.
    np.testing.assert_array_equal(out[8, 8, :3], np.array(BACKGROUND, np.uint8))
    np.testing.assert_array_equal(out[90, 160, :3], np.array(SUBJECT, np.uint8))
    assert not np.any(np.all(out[:, :, :3] == np.array((0, 255, 0), np.uint8), axis=2)), \
        "green is a hint colour, never a painted pixel"


def test_background_stroke_refines_the_selection_instead_of_deleting_pixels():
    engine, clip = build_engine(duration=30)
    item = roto_entry(strokes=[
        {"frame": 0, "points": [[0.5, 0.5]], "radius": 0.12, "mode": "foreground"},
        {"frame": 0, "points": [[0.5, 0.5]], "radius": 0.07, "mode": "background"},
    ])
    out = apply(engine, clip, layer_from(plate()), item)
    assert out[90, 160, 3] < 8, "the red stroke removes its area from the matte"
    np.testing.assert_array_equal(out[90, 160, :3], np.array(SUBJECT, np.uint8))
    # The green stroke is 0.12 of the short edge (~22 px) and the red one ~13 px, so the
    # ring between them is still an explicit foreground hint.
    assert out[90, 143, 3] > 250, "the rest of the subject survives the correction"


def test_preview_modes_and_solid_colour_are_real_views():
    engine, clip = build_engine(duration=30)
    layer = layer_from(plate())
    images = {view: apply(engine, clip, layer, roto_entry(matte_view=view))
              for view in ("composite", "checkerboard", "alpha", "solid")}
    assert len({image.tobytes() for image in images.values()}) == 4, \
        "every preview mode must draw something different"
    red = apply(engine, clip, layer, roto_entry(matte_view="solid", view_r=240,
                                               view_g=10, view_b=10))
    green = apply(engine, clip, layer, roto_entry(matte_view="solid", view_r=10,
                                                 view_g=240, view_b=10))
    assert not np.array_equal(red, green)
    corner = (4, 4)
    assert int(red[corner][2]) > 200 and int(green[corner][1]) > 200, \
        "the solid preview uses the configured colour"


def test_every_preview_view_has_the_expected_alpha_or_background():
    engine, clip = build_engine(duration=30)
    layer = layer_from(plate())
    views = {view: apply(engine, clip, layer, roto_entry(matte_view=view))
             for view in ("original", "outline", "isolated", "alpha", "checkerboard",
                          "composite")}
    np.testing.assert_array_equal(views["original"], layer)
    np.testing.assert_array_equal(views["outline"], layer)

    alpha = views["alpha"]
    assert int(alpha[4, 4, :3].max()) == 0, "alpha matte is black where the background was"
    assert int(alpha[4, 4, 3]) == 255 and int(alpha[90, 160, 3]) == 255
    assert int(alpha[90, 160, :3].max()) == 255, "the subject is white in the matte"

    composite = views["composite"]
    assert int(composite[4, 4, 3]) == 0, "composite leaves the removed background transparent"
    assert int(composite[90, 160, 3]) == 255, "the subject stays opaque"
    np.testing.assert_array_equal(composite[4, 4, :3], layer[4, 4, :3]), \
        "the effect does not paint black or green over transparent pixels"

    checker = views["checkerboard"]
    assert int(checker[4, 4, 3]) == 255 and int(checker[4, 4, :3].max()) > 20
    assert not np.array_equal(checker[4, 4, :3], composite[4, 4, :3]), \
        "the checker is a diagnostic background, not a black fill"

    isolated = views["isolated"]
    assert int(isolated[4, 4, 3]) == 255 and int(isolated[4, 4, :3].max()) < 45
    assert int(isolated[90, 160, 3]) == 255

    # A diagnostic preview must never reach an export. The exporter sets this flag while
    # rendering, and the effect then evaluates Composite regardless of the popup state.
    effect = runtime(engine, clip, roto_entry(matte_view="alpha"))
    engine.exporting = True
    try:
        exported = effect.apply_layer(layer.copy())
    finally:
        engine.exporting = False
    assert int(exported[4, 4, 3]) == 0
    np.testing.assert_array_equal(exported[4, 4, :3], layer[4, 4, :3])


def test_underlying_clip_shows_through_the_removed_background():
    engine, clip = build_engine(frame=plate(), duration=30)
    lower_track = engine.timeline.add_track("video", index=len(engine.timeline.tracks))
    lower_path = "synthetic-roto-lower.png"
    from meenifx.media.cache import MediaEntry
    lower = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    lower[:] = (180, 40, 120)          # BGR magenta, unmistakable against black
    media = MediaEntry("video", 30, 30.0)
    media.frames = [lower.copy() for _ in range(30)]
    engine.media._entries[lower_path] = media
    engine.timeline.new_clip("lower", lower_path, lower_track, 0, 30, source_out=30,
                             source_fps=30.0)
    clip.effects = [roto_entry()]
    _raw, processed = engine.render_frame(0, 1.0)
    np.testing.assert_array_equal(processed[8, 8], lower[8, 8])
    assert int(processed[90, 160].max()) > 150, "the subject still covers the lower clip"


def test_export_writes_the_composite_and_never_the_alpha_preview(tmp_path):
    engine, clip = build_engine(frame=plate(), duration=30)
    from meenifx.media.cache import MediaEntry
    lower_track = engine.timeline.add_track("video", index=len(engine.timeline.tracks))
    lower_path = "synthetic-roto-lower-export.png"
    lower = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    lower[:] = (180, 40, 120)
    media = MediaEntry("video", 30, 30.0)
    media.frames = [lower.copy() for _ in range(30)]
    engine.media._entries[lower_path] = media
    engine.timeline.new_clip("lower", lower_path, lower_track, 0, 30, source_out=30,
                             source_fps=30.0)
    # A diagnostic view is selected, but the exporter must render the real composite.
    clip.effects = [roto_entry(matte_view="alpha")]
    settings = {"output_path": str(tmp_path / "roto.png"), "format": "png",
                "range_start": 0, "range_end": 1, "use_source_size": True,
                "use_source_fps": True}
    written, count, _fps, size = engine.export(settings)
    assert count == 1 and size == (WIDTH, HEIGHT)
    exported = cv2.imread(written)
    assert exported is not None
    np.testing.assert_array_equal(exported[8, 8], lower[8, 8]), \
        "the removed background must show the lower clip, not the black alpha preview"
    assert int(exported[90, 160].max()) > 150, "the subject is still in the exported frame"


def test_roto_cutout_composites_inside_a_nested_composition():
    from meenifx.media.cache import MediaEntry
    from meenifx.project.settings import ProjectSettings
    engine, clip = build_engine(frame=plate(), duration=30)
    engine.apply_project_settings(ProjectSettings(name="Nested roto", width=WIDTH,
                                                  height=HEIGHT, fps=30.0))
    lower_track = engine.timeline.add_track("video", index=len(engine.timeline.tracks))
    lower_path = "synthetic-roto-nested-lower.png"
    lower = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    lower[:] = (180, 40, 120)
    media = MediaEntry("video", 30, 30.0)
    media.frames = [lower.copy() for _ in range(30)]
    engine.media._entries[lower_path] = media
    lower_clip = engine.timeline.new_clip("lower", lower_path, lower_track, 0, 30,
                                          source_out=30, source_fps=30.0)
    clip.effects = [roto_entry()]
    nested_id = engine.session.precompose("Roto nested", [clip.id, lower_clip.id])
    assert nested_id is not None
    nested = engine.timeline.find_clip(nested_id)
    assert nested is not None and nested.is_composition
    _raw, frame = engine.render_frame(0, 1.0)
    assert frame.shape[:2] == (HEIGHT, WIDTH)
    np.testing.assert_array_equal(frame[8, 8], lower[8, 8]), \
        "the cutout reveals the lower clip inside the nested composition"
    assert int(frame[90, 160].max()) > 150, "the subject survives the nested render"


def test_refine_edge_stroke_changes_the_soft_boundary():
    height, width = 120, 160
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:] = BACKGROUND
    cv2.circle(frame, (80, 60), 38, SUBJECT, -1)
    frame = cv2.GaussianBlur(frame, (0, 0), 2.5)
    foreground, background, _refine = rasterise(
        [{"frame": 0, "points": [[0.5, 0.5]], "radius": 0.12, "mode": "foreground"}],
        width, height, (0, 0, width, height))
    settings = dict(quality_settings("balanced"), refine=True, tolerance=30.0,
                    search_radius=18.0)
    plain = segment_frame(frame, foreground, background, None, settings)
    # The refine stroke crosses the subject's left and right soft edges.
    _fg, _bg, refine = rasterise(
        [{"frame": 0, "points": [[0.18, 0.5], [0.82, 0.5]], "radius": 0.05,
          "mode": "refine"}], width, height, (0, 0, width, height))
    refined = segment_frame(frame, foreground, background, refine, settings)
    assert plain is not None and refined is not None
    difference = np.abs(plain - refined)
    assert float(difference.mean()) > 0.001, \
        "the Refine Edge brush must really change the matte"
    assert float(difference.max()) > 0.05, "the change is a real edge change"
    columns = np.nonzero(difference.max(axis=0) > 0.05)[0]
    assert columns.min() < width * 0.35 and columns.max() > width * 0.65, \
        "the refinement lands on the boundary the brush crossed"


def test_every_matte_control_changes_the_matte():
    yy, xx = np.mgrid[0:120, 0:160]
    disc = np.clip((46.0 - np.hypot(xx - 80.0, yy - 60.0)) / 5.0 + 0.5, 0.0, 1.0)
    noise = disc.copy()
    noise[::7, ::5] = 1.0 - noise[::7, ::5]
    base = {"smooth": 0.0, "feather": 0.0, "choke": 0.0, "edge_contrast": 0.0}
    reference = finish_matte(disc, base)

    smooth = finish_matte(noise, dict(base, smooth=4.0))
    assert float(np.abs(smooth - finish_matte(noise, base)).mean()) > 0.001
    feather = finish_matte(disc, dict(base, feather=8.0))
    soft = lambda matte: int(((matte > 0.05) & (matte < 0.95)).sum())
    assert soft(feather) > soft(reference), "feather widens the ramp"
    grow = finish_matte(disc, dict(base, choke=6.0))
    assert grow.mean() > reference.mean(), "positive Expand grows the matte"
    choke = finish_matte(disc, dict(base, choke=-6.0))
    assert choke.mean() < reference.mean(), "negative Shrink eats into the matte"
    contrast = finish_matte(disc, dict(base, edge_contrast=90.0))
    assert soft(contrast) < soft(reference), "edge contrast steepens the ramp"
    for value in (smooth, feather, choke, grow, contrast):
        assert value.dtype == np.float32 and value.min() >= 0.0 and value.max() <= 1.0


def test_outline_follows_the_subject_boundary_not_the_clip_rectangle():
    picture = plate()
    foreground, background, _refine = rasterise(
        [{"frame": 0, "points": [[0.5, 0.5]], "radius": 0.08, "mode": "foreground"}],
        WIDTH, HEIGHT, (0, 0, WIDTH, HEIGHT))
    matte = segment_frame(picture, foreground, background, None,
                          dict(quality_settings("balanced"), tolerance=30.0))
    shapes = outline_contours(matte)
    assert shapes, "a segmented frame must produce an outline"
    points = np.concatenate(shapes, axis=0)
    assert len(points) >= 6, "a circle is approximated, not a 4-corner rectangle"
    span_x = float(points[:, 0].max() - points[:, 0].min())
    span_y = float(points[:, 1].max() - points[:, 1].min())
    assert span_x < 0.6 and span_y < 0.8, "the outline hugs the disc, not the frame"
    assert outline_contours(None) == []


def test_propagation_follows_the_moving_subject_both_ways():
    count = 34
    radius = 30
    frames = [plate(cx=40 + index * 6, radius=radius) for index in range(count)]
    seed = 17
    foreground, background, _refine = rasterise(
        [{"frame": seed,
          "points": [[frames[seed].shape[1] and (40 + seed * 6) / WIDTH, 0.5]],
          "radius": 0.075, "mode": "foreground"}],
        WIDTH, HEIGHT, (0, 0, WIDTH, HEIGHT))
    settings = dict(quality_settings("draft"), refine=True, tolerance=30.0,
                    search_radius=22.0)
    seed_matte = segment_frame(frames[seed], foreground, background, None, settings)
    assert seed_matte is not None

    def truth(index):
        mask = np.zeros((HEIGHT, WIDTH), np.uint8)
        cv2.circle(mask, (40 + index * 6, 90), radius, 255, -1)
        return mask > 0

    def iou(matte, index):
        predicted = matte > 0.5
        expected = truth(index)
        return float((predicted & expected).sum() / max(1, (predicted | expected).sum()))

    def run(direction):
        stored = {}
        done = propagate_seed(
            seed_matte, seed, frame_order(0, count - 1, seed, direction),
            lambda index: frames[index], settings,
            on_mask=lambda index, matte: stored.__setitem__(index, matte))
        return stored, done

    forward, done_forward = run("forward")
    assert 20 in done_forward and len(done_forward) >= 8
    for offset in (2, 6, 10):
        assert iou(forward[seed + offset], seed + offset) > 0.8, \
            "the mask must travel with the subject, not stay on the seed frame"
    assert float(np.abs(forward[seed + 8] - seed_matte).mean()) > 0.02, \
        "propagation must not reuse the static base matte"

    backward, done_backward = run("backward")
    assert 12 in done_backward and len(done_backward) >= 8
    for offset in (2, 6, 10):
        assert iou(backward[seed - offset], seed - offset) > 0.8


def test_cache_round_trip_freeze_and_key_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto"))
    engine, clip = build_engine(duration=30)
    cache = MaskCache()
    item = roto_entry()
    key = mask_key(item)
    size = mask_size(WIDTH, HEIGHT)
    cache.configure(clip, key, size)
    matte = np.zeros((size[1], size[0]), np.float32)
    cv2.circle(matte, (size[0] // 2, size[1] // 2), 20, 1.0, -1)
    cache.store(clip, 0, matte, key, size)
    stored = cache.load(clip, 0, key)
    assert stored is not None and stored.shape == matte.shape
    assert float(np.abs(stored - matte).mean()) < 0.01
    assert cache.summary(clip, key) == (1, False)
    assert cache.set_frozen(clip, True) and cache.summary(clip, key) == (1, True)
    assert cache.frozen(clip)
    assert cache.load(clip, 0, "another-key") is None, "stale keys never show masks"

    changed = dict(item, strokes=[{"frame": 0, "points": [[0.4, 0.4]], "radius": 0.1,
                                  "mode": "foreground"}])
    assert mask_key(changed) != key
    cache.configure(clip, mask_key(changed), size)
    assert cache.summary(clip, mask_key(changed)) == (0, False), \
        "a changed stroke key resets the folder instead of reusing old masks"
    cache.clear(clip)
    assert cache.load(clip, 0, key) is None


def test_frames_without_a_mask_render_unchanged():
    engine, clip = build_engine(duration=30)
    item = roto_entry(base_frame=0)
    picture = plate()
    # The clip plays source frame 10 here and has no strokes and no cache for it: the
    # effect must leave the frame alone rather than paste the base matte over it.
    out = apply(engine, clip, layer_from(picture), item, local_frame=10)
    np.testing.assert_array_equal(out, layer_from(picture))
    effect = runtime(engine, clip, item, local_frame=10)
    assert effect.raw_matte(picture, 10) is None
    assert "Propagate Forward" in effect.last_note


def test_edge_colour_cleanup_reaches_the_soft_edge_only():
    engine, clip = build_engine(duration=30)
    # A bright background and a dark subject produce a strong contaminated fringe. The
    # matte is placed on that fringe through the real cache, so the test measures the
    # composite path the user sees rather than a hand-made matte.
    picture = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    picture[:, :] = (250, 250, 250)
    cv2.circle(picture, (160, 90), 50, (40, 40, 40), -1)
    picture = cv2.GaussianBlur(picture, (0, 0), 3.0)
    item = roto_entry()
    size = mask_size(WIDTH, HEIGHT)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    matte = np.clip((54.0 - np.hypot(xx - 160.0, yy - 90.0)) / 8.0 + 0.5, 0.0, 1.0)
    MaskCache().store(clip, 0, matte.astype(np.float32), mask_key(item), size)
    plain = apply(engine, clip, layer_from(picture), item)
    cleaned = apply(engine, clip, layer_from(picture), roto_entry(decontaminate=100.0))
    difference = np.abs(plain.astype(np.int16) - cleaned.astype(np.int16))
    assert difference[:, :, :3].max() > 10, "edge colour cleanup must reach the pixels"
    assert difference[:, :, 3].max() == 0, "it recolours the edge, it does not move alpha"
    assert int((difference[:, :, :3].max(axis=2) > 4).sum()) < WIDTH * HEIGHT / 3, \
        "only the edge band changes, not the whole frame"


def test_preview_scale_keeps_the_shaped_matte_geometry():
    engine, clip = build_engine(duration=30)
    layer = layer_from(plate())
    item = roto_entry(feather=8.0, choke=4.0, edge_contrast=40.0)
    full = apply(engine, clip, layer, item, scale=1.0)
    small = cv2.resize(layer, (WIDTH // 2, HEIGHT // 2), interpolation=cv2.INTER_AREA)
    half = apply(engine, clip, small, item, scale=0.5)
    upscaled = cv2.resize(half, (WIDTH, HEIGHT), interpolation=cv2.INTER_LINEAR)
    difference = np.abs(full[:, :, 3].astype(np.int16) - upscaled[:, :, 3].astype(np.int16))
    assert float(difference.mean()) < 6.0, \
        "a half-quality preview must shape the matte like the full-quality export"


def test_project_round_trip_keeps_roto_metadata_and_no_masks(tmp_path):
    engine, clip = build_engine(duration=30)
    item = roto_entry(base_frame=7, tolerance=41.0)
    item["strokes"].append({"frame": 12, "points": [[0.3, 0.4]], "radius": 0.06,
                            "mode": "background"})
    item["propagation"] = {"base_frame": 7, "analysed": [8, 9], "range": [0, 29]}
    item["cache"] = {"key": "abc123", "frames": 3, "frozen": False}
    clip.effects = [item]
    project = Project(ProjectSettings.from_preset("720p 30", "Roto"), None)
    path = str(tmp_path / "roto.mfxpf")
    payload = collect(engine, project)
    write_container(path, payload)
    text = json.dumps(payload.sections, default=str)
    assert "base_frame" in text and "propagation" in text and "strokes" in text
    assert "iVBOR" not in text and "data:image" not in text, \
        "masks are files, never base64 inside the project JSON"
    assert len(text) < 200000

    loaded = read_container(path)
    fresh, _other = build_engine(duration=30)
    fresh.timeline.tracks.clear()
    apply_project(fresh, loaded, path)
    restored = fresh.timeline.find_clip(clip.id)
    assert restored is not None
    entry = restored.effects[0]
    assert entry["base_frame"] == 7
    assert entry["strokes"] == item["strokes"]
    assert entry["propagation"] == item["propagation"]
    assert entry["cache"] == item["cache"]
