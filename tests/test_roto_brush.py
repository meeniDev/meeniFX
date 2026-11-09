"""Roto Brush: paint strokes, default seed, diagnostic views and project persistence."""

import copy
import os

import numpy as np

from conftest import build_engine
from meenifx.project.container import read_container, write_container
from meenifx.project.io import Project, apply as apply_project, collect
from meenifx.project.settings import ProjectSettings


def plate(background=(64, 177, 0), foreground=(120, 150, 210), size=(320, 180)):
    width, height = size
    layer = np.zeros((height, width, 4), np.uint8)
    layer[:, :, :3] = background
    layer[:, :, 3] = 255
    layer[36:145, 72:248, :3] = foreground
    return layer


def strokes():
    return [
        {"points": [[0.5, 0.20], [0.5, 0.80]], "radius": 0.10, "mode": "foreground"},
        {"points": [[0.05, 0.10], [0.95, 0.10]], "radius": 0.08, "mode": "background"},
    ]


def runtime(engine, entry, scale=1.0, **params):
    effect = engine.runtime_effects["roto_brush"]
    values = dict(effect.defaults)
    values.update(params)
    effect.param_scale = scale
    effect.local_frame = 0
    effect.clip_entry = entry
    effect.load_params(values)
    return effect


def apply(engine, layer, entry=None, **params):
    entry = entry or {"effect_id": "roto_brush", "enabled": True, "params": {},
                      "strokes": strokes()}
    return runtime(engine, entry, **params).apply_layer(layer.copy())


def test_painted_foreground_and_background_masks_produce_a_cutout():
    engine, _clip = build_engine()
    out = apply(engine, plate())
    assert out[10, 10, 3] == 0, "the painted background clears"
    assert out[90, 150, 3] > 250, "the painted subject stays opaque"
    assert int(out[90, 150, 0]) > 100, "the subject colour is retained"


def test_background_stroke_removes_its_painted_area():
    engine, _clip = build_engine()
    entry = {"effect_id": "roto_brush", "enabled": True, "params": {},
             "strokes": [
                 {"points": [[0.5, 0.2], [0.5, 0.8]], "radius": 0.10, "mode": "foreground"},
                 {"points": [[0.50, 0.50]], "radius": 0.08, "mode": "background"},
             ]}
    out = apply(engine, plate(), entry)
    assert out[90, 160, 3] < 20, "the background stroke punches a hole"
    assert out[90, 110, 3] > 250, "the rest of the subject remains"


def test_default_seed_is_visible_and_can_be_disabled():
    engine, _clip = build_engine()
    entry = {"effect_id": "roto_brush", "enabled": True, "params": {}, "strokes": []}
    seeded = apply(engine, plate(), entry, auto_seed=True)
    empty = apply(engine, plate(), entry, auto_seed=False)
    assert seeded[90, 160, 3] > 250
    assert seeded[10, 10, 3] == 0
    assert np.array_equal(empty, plate())


def test_matte_views_and_zero_transparency():
    engine, _clip = build_engine()
    layer = plate()
    entry = {"effect_id": "roto_brush", "enabled": True, "params": {},
             "strokes": strokes()}
    alpha = apply(engine, layer, entry, matte_view="alpha")
    mono = apply(engine, layer, entry, matte_view="black_white")
    clear = apply(engine, layer, entry, transparency=0.0)
    assert alpha[90, 150, 3] == 255
    assert int(alpha[90, 150, 0]) > 200
    assert set(np.unique(mono[:, :, 0])).issubset({0, 255})
    assert np.array_equal(clear, layer)


def test_preview_scale_keeps_the_brush_geometry():
    engine, _clip = build_engine()
    layer = plate()
    entry = {"effect_id": "roto_brush", "enabled": True, "params": {},
             "strokes": strokes()}
    full = apply(engine, layer, entry, scale=1.0)
    half = apply(engine, layer, entry, scale=0.5)
    import cv2
    resized = cv2.resize(half, (full.shape[1], full.shape[0]), interpolation=cv2.INTER_AREA)
    assert abs(float(resized[:, :, 3].mean()) - float(full[:, :, 3].mean())) < 8.0


def test_brush_strokes_survive_project_round_trip(tmp_path):
    engine, clip = build_engine(frame=plate()[:, :, :3], duration=30)
    entry = {"effect_id": "roto_brush", "enabled": True, "params": {}, "strokes": strokes()}
    clip.effects = [entry]
    project = Project(ProjectSettings.from_preset("720p 30", "Roto"), None)
    path = str(tmp_path / "roto.mfxpf")
    write_container(path, collect(engine, project))
    loaded = read_container(path)
    fresh, _other = build_engine(frame=plate()[:, :, :3], duration=30)
    fresh.timeline.tracks.clear()
    apply_project(fresh, loaded, path)
    restored = fresh.timeline.find_clip(clip.id)
    assert restored is not None
    assert restored.effects[0]["strokes"] == strokes()
