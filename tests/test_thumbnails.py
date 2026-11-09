"""Effect thumbnails: a real render of the effect, the shipped reference, and the cache."""

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import ROOT, build_engine  # noqa: E402
from meenifx import thumbnails as T  # noqa: E402

SIZE = (140, 96)


@pytest.fixture(scope="module")
def engine():
    return build_engine()[0]


def _base(settings=None):
    settings = settings or T.PreviewSettings(background="checker", quality="medium")
    return T._alpha_over(T.background_layer(*SIZE, settings.background),
                         T.reference_layer(*SIZE, settings))


def _diff(image):
    base = _base()
    return float(np.abs(image.astype(int) - base.astype(int)).mean())


def _wait(structure, seconds=20.0):
    from PyQt6.QtWidgets import QApplication
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if structure():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------------------- reference
def test_the_reference_is_figure_cafe_beside_main():
    path = T.default_reference()
    assert path is not None, "figure_cafe.png must ship beside main.py"
    assert os.path.basename(path) == "figure_cafe.png"
    assert os.path.dirname(os.path.abspath(path)) == ROOT
    reference, custom = T.reference_path(T.PreviewSettings())
    assert reference == path and not custom


def test_the_reference_keeps_its_transparency_and_aspect(qapp):
    layer = T.reference_layer(*SIZE)
    assert layer is not None and layer.shape[:2] == (SIZE[1], SIZE[0])
    alpha = layer[:, :, 3]
    assert alpha.max() == 255, "the picture itself is opaque"
    assert alpha[0, 0] == 0 and alpha[-1, -1] == 0, "the margin around it is transparent"

    rows = np.where(alpha.any(axis=1))[0]
    columns = np.where(alpha.any(axis=0))[0]
    drawn = (columns[-1] - columns[0] + 1, rows[-1] - rows[0] + 1)

    source = T._load_image(T.default_reference(), T._mtime(T.default_reference()), 1000)
    opaque = source[:, :, 3].any(axis=1)
    source_rows = np.where(opaque)[0]
    source_columns = np.where(source[:, :, 3].any(axis=0))[0]
    source_ratio = ((source_columns[-1] - source_columns[0] + 1)
                    / float(source_rows[-1] - source_rows[0] + 1))
    assert abs((drawn[0] / float(drawn[1])) - source_ratio) < 0.03, "no stretching"


def test_the_background_setting_changes_the_surface(qapp, engine):
    checker = T.render_entries(engine, [], *SIZE, T.PreviewSettings(background="checker"))
    dark = T.render_entries(engine, [], *SIZE, T.PreviewSettings(background="dark"))
    light = T.render_entries(engine, [], *SIZE, T.PreviewSettings(background="light"))
    assert np.abs(checker[:4, :4].astype(int) - dark[:4, :4].astype(int)).max() > 6
    assert light[0, 0].mean() > 150, "the light surface is a real choice"
    assert checker[0, 0].mean() > 8, "the empty corner is the board, never pure black"


# -------------------------------------------------------------------------- effects
def test_every_visual_effect_renders_a_real_change(engine):
    """No placeholder previews: each card's picture must come from the effect itself."""
    identical = []
    for effect in engine.effects:
        key = type(effect).key
        if key in T.ICON_ONLY or key == "look":
            continue
        image = T.render_effect(engine, key, *SIZE)
        assert image is not None, f"{key} produced no thumbnail"
        if _diff(image) < 0.5:
            identical.append(key)
    assert identical == [], f"these effects look like the plain reference: {identical}"


def test_effects_that_cannot_show_on_a_still_use_an_icon(qapp, engine):
    audio = {type(effect).key for effect in engine.effects if "audio" in effect.media_types()}
    assert audio and audio <= T.ICON_ONLY
    assert {"posterize_time", "stutter", "echo", "face_tracker", "hand_tracker"} <= T.ICON_ONLY


def test_a_style_renders_its_whole_stack_and_follows_its_strength(engine):
    from meenifx.model import looks as LOOKS

    look = next(item for item in LOOKS.all_looks() if len(item["stack"]) >= 2)
    full = T.render_entries(engine, T.style_entries(look, 100.0), *SIZE, T.PreviewSettings())
    assert _diff(full) > 1.0, f"{look['name']} has to show its stack's combined result"
    off = T.render_entries(engine, T.style_entries(look, 0.0), *SIZE, T.PreviewSettings())
    assert _diff(off) < 0.5, "the group strength at 0 % must leave the reference alone"


# ------------------------------------------------------------------------ animations
def test_every_animation_family_renders_a_real_preview(engine):
    from meenifx.model import animation_families as FAMILIES

    missing, identical = [], []
    for family in FAMILIES.families():
        for media in ("clip", "text"):
            if media not in (family.get("media") or []):
                continue
            entry = T.animation_entry(family, media)
            image = (T.render_entries(engine, [entry], *SIZE, T.PreviewSettings())
                     if entry else None)
            if image is None:
                missing.append((family["key"], media))
                continue
            if media == "clip" and _diff(image) < 0.5:
                identical.append((family["key"], media))
    assert missing == [], f"no thumbnail for {missing}"
    assert identical == [], f"these animations look like the plain reference: {identical}"


def test_a_text_animation_previews_its_letters(engine):
    from meenifx.model import animation_families as FAMILIES

    family = next(item for item in FAMILIES.families()
                  if "text" in item["media"] and "clip" not in item["media"])
    settings = T.PreviewSettings(background="dark")
    image = T.render_entries(engine, [T.animation_entry(family, "text")], *SIZE, settings)
    assert image is not None
    reference = _base(settings)
    assert float(np.abs(image.astype(int) - reference.astype(int)).mean()) > 2.0, \
        "a text animation must show text, not the untouched picture"
    assert float(image.max()) > 180, "the glyphs are visible"


def test_a_picture_family_never_borrows_a_text_variant():
    from meenifx.model import animation_families as FAMILIES

    clip_only = next(item for item in FAMILIES.families()
                     if set(item["media"]) == {"clip"})
    assert T.animation_entry(clip_only, "clip") is not None
    # The browser only asks for a media the family has, but a fallback must still be a
    # picture variant: a text-only entrance must never stand in for a clip one.
    entry = T.animation_entry(clip_only, "text")
    assert entry["animation"]["preset"]["category"].startswith("clip")


# ---------------------------------------------------------------------------- cache
def test_the_cache_renders_once_then_serves_the_image_from_memory(qapp, engine, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "cache_folder", lambda: str(tmp_path))
    cache = T.ThumbnailCache(engine)
    cache.settings = T.PreviewSettings()
    entry = [T.preview_entry(next(e for e in engine.effects if type(e).key == "glow"))]

    assert cache.image("effect", "glow", {}, entry, *SIZE) is None, "a miss starts a job"
    assert _wait(lambda: cache.image("effect", "glow", {}, entry, *SIZE) is not None)
    image = cache.image("effect", "glow", {}, entry, *SIZE)
    assert image is not None and not image.isNull() and image.width() == SIZE[0]
    assert os.listdir(tmp_path), "the thumbnail is cached on disk too"
    cache.clear()
    assert cache.image("effect", "glow", {}, entry, *SIZE) is None
    assert _wait(lambda: cache.image("effect", "glow", {}, entry, *SIZE) is not None)


def test_the_animation_cache_key_follows_the_media(qapp, engine):
    import json

    from meenifx.model import animation_families as FAMILIES

    family = next(item for item in FAMILIES.families()
                  if {"clip", "text"} <= set(item["media"]))
    cache = T.ThumbnailCache(engine)
    picture = json.dumps([T.animation_entry(family, "clip")], sort_keys=True, default=str)
    words = json.dumps([T.animation_entry(family, "text")], sort_keys=True, default=str)
    assert picture != words
    assert cache.key("animation", family["key"], picture, *SIZE, 0) != \
        cache.key("animation", family["key"], words, *SIZE, 0)


def test_replacing_the_reference_changes_the_cache_key(qapp, engine, tmp_path, monkeypatch):
    import cv2

    monkeypatch.setattr(T, "cache_folder", lambda: str(tmp_path))
    custom = tmp_path / "custom.png"
    assert cv2.imwrite(str(custom), np.full((40, 60, 3), 90, np.uint8))
    default_settings = T.PreviewSettings()
    cache = T.ThumbnailCache(engine)
    cache.settings = default_settings
    first = cache.key("effect", "glow", {}, *SIZE, 0)
    cache.settings = T.PreviewSettings(image_mode="custom", custom_path=str(custom))
    assert cache.key("effect", "glow", {}, *SIZE, 0) != first
    layer = T.reference_layer(*SIZE, cache.settings)
    assert layer is not None and layer[:, :, 3].max() == 255, "the custom image is used"


def test_a_missing_custom_image_falls_back_to_the_shipped_one(qapp, tmp_path):
    missing = T.PreviewSettings(image_mode="custom", custom_path=str(tmp_path / "gone.png"))
    assert T.reference_path(missing)[0] == T.default_reference()
    unreadable = tmp_path / "broken.png"
    unreadable.write_text("not an image", encoding="utf-8")
    assert T.reference_path(T.PreviewSettings(image_mode="custom",
                                              custom_path=str(unreadable)))[0] == T.default_reference()


def test_preview_settings_persist(qapp, tmp_path):
    from PyQt6.QtCore import QSettings

    store = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    chosen = T.PreviewSettings(image_mode="custom", custom_path=str(tmp_path / "p.png"),
                               background="light", quality="high")
    chosen.save(store)
    loaded = T.PreviewSettings.load(store)
    assert (loaded.image_mode, loaded.custom_path, loaded.background, loaded.quality) == \
           ("custom", str(tmp_path / "p.png"), "light", "high")
    assert loaded.scale() == 3


# ------------------------------------------------------------- audio and time
def test_audio_effects_get_waveform_illustrations_not_the_cafe_picture(qapp, engine):
    from PyQt6.QtGui import QImage

    images = {}
    for key in sorted(T.AUDIO_EFFECTS):
        image = T.audio_preview(key, *SIZE, T.PreviewSettings(background="dark"))
        assert isinstance(image, QImage) and not image.isNull(), key
        images[key] = image
    assert images["audio_gain"] != images["audio_eq"]
    assert images["audio_lowpass"] != images["audio_highpass"]
    reference = T.reference_preview(*SIZE, T.PreviewSettings(background="dark"))
    assert reference is not None and images["audio_gain"] != reference


def test_temporal_effects_generate_a_representative_frame_sequence(qapp, engine):
    settings = T.PreviewSettings(background="dark")
    for key in sorted(T.TEMPORAL_EFFECTS):
        frames = T.temporal_preview_frames(engine, key, *SIZE, settings)
        assert len(frames) >= 6, key
        assert all(frame.shape[:2] == (SIZE[1], SIZE[0]) for frame in frames), key
        assert any(np.abs(frames[index].astype(int) - frames[index - 1].astype(int)).mean() > 0.1
                   for index in range(1, len(frames))), key
