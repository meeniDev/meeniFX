"""Focused regressions for the creative-workflow overhaul.

These cover the model and rendering contracts behind the generator, ASCII, media-picker
and alpha-inversion UI: real renders and real parameter storage, not the widgets alone.
The window-level mouse/keyboard acceptance flow lives in test_creative_workflow_gui.py.
"""

from __future__ import annotations

import cv2
import numpy as np

from conftest import build_engine, frame_diff
from meenifx.effects.base import ParamSpec
from meenifx.model import alpha as ALPHA
from meenifx.model import generators as GENERATORS
from meenifx.model import mask as MASK
from meenifx.model import matte as MATTE
from meenifx.model import path as PATHS
from meenifx.model import shape as SHAPE
from meenifx.processing import ascii_art as ASCII
from meenifx.processing import masks as MASK_OPS


# --------------------------------------------------------------------- generators
def bare_engine(width=320, height=180):
    engine, _clip = build_engine()
    engine.timeline.tracks.clear()
    engine.source_width, engine.source_height = width, height
    engine.session.project_size = (width, height)
    return engine


def test_every_generator_is_a_real_deterministic_picture():
    engine = bare_engine()
    ids = {}
    for index, key in enumerate(GENERATORS.ORDER):
        ids[key] = engine.session.add_generator_clip(key, start=index * 12, duration=10)
        assert ids[key] is not None
    frames = {}
    for index, key in enumerate(GENERATORS.ORDER):
        first, _ = engine.render_frame(index * 12, 1.0)
        second, _ = engine.render_frame(index * 12, 1.0)
        np.testing.assert_array_equal(first, second, err_msg=f"{key} is not deterministic")
        assert int(first.max()) > 8, f"{key} rendered a blank clip"
        frames[key] = first
    # Five generators must not be five copies of the same default picture.
    assert len({frame.tobytes() for frame in frames.values()}) == 5


def test_generator_settings_belong_to_each_clip_and_duplicates_are_independent():
    engine = bare_engine()
    first = engine.session.add_generator_clip("solid", start=0, duration=20)
    clip = engine.timeline.find_clip(first)
    SHAPE.ensure_shape(clip)["gen_color"] = "#FF0000"
    red = engine.render_frame(0, 1.0)[1]
    assert red[:, :, 2].mean() > 200 and red[:, :, 1].mean() < 30

    engine.session.set_selection({first})
    engine.session.duplicate_selected()
    duplicate = max(engine.timeline.all_clips(), key=lambda item: item.timeline_start)
    SHAPE.ensure_shape(duplicate)["gen_color"] = "#00FF00"
    green = engine.render_frame(duplicate.timeline_start, 1.0)[1]
    assert green[:, :, 1].mean() > 200 and green[:, :, 2].mean() < 30
    assert SHAPE.ensure_shape(engine.timeline.find_clip(first))["gen_color"] == "#FF0000"


def test_generator_clips_trim_split_and_keep_their_settings():
    engine = bare_engine()
    clip_id = engine.session.add_generator_clip("checkerboard", start=0, duration=30)
    clip = engine.timeline.find_clip(clip_id)
    data = SHAPE.ensure_shape(clip)
    data["gen_size"] = 12.0
    engine.session.set_clip_duration(clip_id, 20)
    assert engine.timeline.find_clip(clip_id).duration == 20

    engine.session.set_selection({clip_id})
    engine.session.split_clip_at(engine.timeline.find_clip(clip_id), 10)
    pieces = [item for item in engine.timeline.all_clips() if (item.shape or {}).get(
        "generator") == "checkerboard"]
    assert len(pieces) == 2
    for piece in pieces:
        assert SHAPE.ensure_shape(piece)["gen_size"] == 12.0
    engine.session.undo()
    assert len([item for item in engine.timeline.all_clips()
                if (item.shape or {}).get("generator") == "checkerboard"]) == 1


def test_generator_shape_round_trips_through_project_serialization():
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine = bare_engine()
    clip_id = engine.session.add_generator_clip("fractal_noise", start=0, duration=12)
    data = SHAPE.ensure_shape(engine.timeline.find_clip(clip_id))
    data["gen_seed"] = 91.0
    data["gen_detail"] = 3.0
    sections = timeline_to_sections(engine.timeline, lambda _path: "")
    loaded = sections_to_timeline(sections, engine.timeline.fps, lambda _media: "")
    restored = loaded.find_clip(clip_id)
    assert restored is not None
    settings = SHAPE.ensure_shape(restored)
    assert settings["kind"] == "generator"
    assert settings["generator"] == "fractal_noise"
    assert settings["gen_seed"] == 91.0
    assert settings["gen_detail"] == 3.0


# -------------------------------------------------------------------------- ASCII
def _ascii_effect(params):
    from meenifx.effects.builtins import AsciiEffect

    class App:
        param_scale = 1.0

        def project_size(self):
            return (320, 180)

    effect = AsciiEffect(App())
    effect.load_params(params)
    return effect


def _layer():
    frame = np.zeros((180, 320, 4), np.uint8)
    frame[:, :, :3] = (50, 120, 200)
    frame[:, :, 3] = 255
    return frame


def test_ascii_presets_custom_sets_and_fonts_are_deliberate():
    outputs = {}
    for preset in ("ascii", "blocks", "binary", "unicode"):
        effect = _ascii_effect({"preset": preset, "cell_size": 10,
                                "background_mode": "transparent"})
        outputs[preset] = effect.apply_layer(_layer())
        assert int(outputs[preset][:, :, 3].max()) > 0
    assert len({out.tobytes() for out in outputs.values()}) >= 3

    duplicate = _ascii_effect({"preset": "custom", "charset": "##@", "cell_size": 10})
    out = duplicate.apply_layer(_layer())
    assert len(duplicate._plan()[0]["glyphs"]) == 2
    assert "duplicate" in duplicate.status_note().lower()

    empty = _ascii_effect({"preset": "custom", "charset": "   ", "cell_size": 10})
    assert "empty" in empty.status_note().lower()
    assert empty.apply_layer(_layer()).shape == (180, 320, 4)


def test_ascii_spacing_font_size_contrast_and_density_are_real_controls():
    source = _layer()
    base = _ascii_effect({"preset": "ascii", "cell_size": 10})
    wide = _ascii_effect({"preset": "ascii", "cell_size": 10,
                          "char_spacing": 80, "line_spacing": 40})
    tall = _ascii_effect({"preset": "ascii", "cell_size": 10, "font_size": 20})
    bright = _ascii_effect({"preset": "ascii", "cell_size": 10,
                            "brightness": 60, "invert_density": True})
    assert wide._plan()[0]["cell_w"] > base._plan()[0]["cell_w"]
    assert wide._plan()[0]["cell_h"] > base._plan()[0]["cell_h"]
    assert tall._plan()[0]["font_size"] == 20
    assert wide.apply_layer(source.copy()).tobytes() != base.apply_layer(source.copy()).tobytes()
    assert bright.apply_layer(source.copy()).tobytes() != base.apply_layer(source.copy()).tobytes()


def test_ascii_unsupported_glyphs_fall_back_with_a_warning():
    effect = _ascii_effect({"preset": "custom", "charset": "ab\U0001F600",
                            "font": "Consolas", "cell_size": 10})
    plan = effect._plan()[0]
    assert "\U0001F600" not in plan["glyphs"]
    assert "missing" in effect.status_note().lower()
    assert set(plan["glyphs"]).issubset({"a", "b", "?", "#", " "})


def test_ascii_keeps_combining_and_zwj_sequences_together():
    assert ASCII.graphemes("e\u0301x") == ["e\u0301", "x"]
    assert ASCII.graphemes("\U0001F469\u200D\U0001F4BB!") == [
        "\U0001F469\u200d\U0001f4bb", "!"]


def test_ascii_transparent_and_solid_backgrounds_preserve_alpha():
    source = _layer()
    source[..., 3] = 128
    transparent = _ascii_effect({"preset": "ascii", "cell_size": 10,
                                 "background_mode": "transparent"})
    covered = transparent.apply_layer(source.copy())
    assert covered[..., 3].max() <= 128
    assert int((covered[..., 3] == 0).sum()) > 0

    solid = _ascii_effect({"preset": "ascii", "cell_size": 10,
                           "background_mode": "solid",
                           "background_color": "#112233"})
    filled = solid.apply_layer(source.copy())
    assert filled.shape == source.shape
    assert int(filled[..., 3].min()) == 255
    # #112233 is stored BGRA, so the pixel bytes are blue, green, red.
    background = np.all(filled[..., :3] == np.array([0x33, 0x22, 0x11], np.uint8),
                        axis=2)
    assert int(background.sum()) > 0, "solid background colour was not painted"


def test_ascii_opacity_zero_is_not_replaced_by_the_default():
    effect = _ascii_effect({"preset": "ascii", "cell_size": 10, "opacity": 0.0})
    out = effect.apply_layer(_layer())
    assert int(out[..., 3].max()) == 0


def test_dither_algorithms_and_editable_custom_palette_produce_distinct_output():
    from meenifx.effects.stylize import DitherEffect

    class App:
        param_scale = 1.0

        def project_size(self):
            return (320, 180)

    source = _layer()
    source[:, :, 0] = np.linspace(0, 255, 320, dtype=np.uint8)[None, :]
    source[:, :, 1] = np.linspace(0, 255, 180, dtype=np.uint8)[:, None]
    outputs = {}
    for algorithm in ("floyd_steinberg", "atkinson", "bayer2", "bayer4", "bayer8",
                      "jarvis_judice_ninke", "stucki", "sierra", "burkes"):
        effect = DitherEffect(App())
        effect.load_params({"algorithm": algorithm, "palette": "bw", "pixel_size": 1})
        outputs[algorithm] = effect.apply_layer(source.copy())
        assert outputs[algorithm].shape == source.shape
    assert len({out.tobytes() for out in outputs.values()}) >= 6

    effect = DitherEffect(App())
    effect.load_params({"palette": "custom", "pixel_size": 1,
                        "palette_text": "#FF0000,#00FF00,#0000FF"})
    custom = effect.apply_layer(source.copy())
    colours = {tuple(int(v) for v in pixel) for pixel in custom[..., :3].reshape(-1, 3)}
    assert len(colours) >= 3, colours
    # Hex inputs are sRGB and the renderer works in OpenCV BGR.
    assert (0, 0, 255) in colours and (0, 255, 0) in colours


def test_named_stylize_effects_and_editable_looks_are_registered():
    from meenifx.engine import EditorEngine
    from meenifx.model import looks as LOOKS

    engine = EditorEngine(30)
    required = ("pixel_art", "led_matrix", "hex_mosaic", "crosshatch", "line_art",
                "comic_dots", "scanlines", "vhs_noise", "retro_crt", "dither", "ascii")
    for key in required:
        assert key in engine.runtime_effects, key
        effect = engine.runtime_effects[key]
        assert effect.category in ("Stylize", "Warp")
        effect.param_scale = 0.5
        effect.local_frame = 0
        effect.load_params({})
        out = effect.apply_layer(_layer())
        assert out.shape == (180, 320, 4)
    LOOKS.load(force=True)
    expected = {"retro_game": "Retro Game", "newspaper_print": "Newspaper Print",
                "dithered_dream": "Dithered Dream", "crt_monitor": "CRT Monitor",
                "pixel_pop": "Pixel Pop", "comic_print": "Comic Print",
                "terminal_green": "Terminal Green", "vintage_computer": "Vintage Computer"}
    for look_id, name in expected.items():
        look = LOOKS.get(look_id)
        assert look is not None and look["name"] == name
        assert look["stack"], look_id
        for member in look["stack"]:
            assert member["effect_id"] in engine.runtime_effects, member


def test_ascii_preview_and_png_export_match(tmp_path):
    engine, clip = build_engine()
    params = dict(engine.runtime_effects["ascii"].capture_params())
    params.update({"preset": "ascii", "cell_size": 8, "color_mode": "source"})
    clip.effects = [{"effect_id": "ascii", "enabled": True, "params": params}]
    preview = engine.render_frame(0, 1.0)[1]
    path, count, _fps, _size = engine.export({
        "output_path": str(tmp_path / "ascii-parity.png"),
        "format": "png", "use_source_fps": True, "use_source_size": True,
        "range_start": 0, "range_end": 1,
    })
    exported = cv2.imread(path)
    assert count == 1 and exported is not None
    assert frame_diff(exported, preview) < 1.0


# -------------------------------------------------------------------------- media
def test_media_parameters_are_typed_and_point_at_real_assets():
    from meenifx.effects.builtins import AsciiEffect, FaceTrackerEffect
    from meenifx.effects.distort_extra import DisplacementMapEffect
    from meenifx.effects.lut import LutEffect
    from meenifx.effects.page_curl import PageCurlEffect

    specs = {}
    for effect in (FaceTrackerEffect, DisplacementMapEffect, LutEffect, PageCurlEffect):
        specs.update({spec.name: spec for spec in effect.PARAMS if spec.name in
                      ("sprite_path", "map_path", "lut_file", "back_path")})
    assert set(specs) == {"sprite_path", "map_path", "lut_file", "back_path"}
    assert all(spec.kind == "media" for spec in specs.values())
    assert specs["lut_file"].media_kind == "lut"
    assert all(specs[name].media_kind == "image"
               for name in ("sprite_path", "map_path", "back_path"))
    # Ordinary words must stay ordinary text.
    assert next(spec for spec in AsciiEffect.PARAMS if spec.name == "charset").kind == "text"


def test_media_picker_shows_project_media_filename_replace_and_clear(qapp, tmp_path):
    from meenifx.project.io import Project
    from meenifx.ui.media_picker import MediaPicker

    picture = np.zeros((24, 32, 3), np.uint8)
    picture[:] = (20, 120, 220)
    asset = tmp_path / "tést image.png"
    assert cv2.imwrite(str(asset), picture)
    project = Project()
    project.library.add(str(asset))
    picker = MediaPicker("", project_provider=lambda: project, label="sprite image")
    try:
        assert "No asset" in picker.name.text()
        picker.set_path(str(asset))
        assert picker.path() == str(asset)
        assert asset.name in picker.name.text()
        assert not picker.thumb.pixmap().isNull()
        assert picker.clear.isEnabled()
        menu = picker.choose.menu()
        labels = [action.text() for action in menu.actions()]
        assert any("Browse" in text for text in labels)
        media_menu = next(action.menu() for action in menu.actions() if action.menu())
        assert any(asset.name == action.text() for action in media_menu.actions())
        picker.clear.click()
        assert picker.path() == ""
        assert not picker.clear.isEnabled()
    finally:
        picker.close()


def test_media_picker_persists_the_selection_through_the_effect_panel(qapp, tmp_path):
    from meenifx.ui.panels import EffectsPanel

    engine, clip = build_engine()
    engine.session.set_selection({clip.id})
    panel = EffectsPanel(engine)
    try:
        panel.add_effect("face_tracker")
        panel.refresh_properties()
        row = next(item for item in panel._rows if item.get("name") == "sprite_path")
        asset = tmp_path / "sprite.png"
        assert cv2.imwrite(str(asset), np.full((16, 16, 3), 90, np.uint8))
        row["picker"].set_path(str(asset), emit=True)
        entry = next(item for item in clip.effects
                     if item.get("effect_id") == "face_tracker")
        assert entry["params"]["sprite_path"] == str(asset)
        panel.refresh_properties()
        assert next(item for item in panel._rows
                    if item.get("name") == "sprite_path")["picker"].path() == str(asset)
        engine.session.undo()
        assert not (next(item for item in engine.timeline.find_clip(clip.id).effects
                         if item.get("effect_id") == "face_tracker")
                    ["params"].get("sprite_path"))
    finally:
        panel.close()


# -------------------------------------------------------------------------- alpha
def test_clip_alpha_inversion_is_undoable_and_single():
    engine, clip = build_engine()
    before = engine.compose_frame(0, 1.0, with_alpha=True)
    assert int(before[..., 3].min()) == 255
    engine.session.set_clip_alpha_invert(clip.id, True)
    inverted = engine.compose_frame(0, 1.0, with_alpha=True)
    assert int(inverted[..., 3].max()) == 0
    engine.session.undo()
    assert int(engine.compose_frame(0, 1.0, with_alpha=True)[..., 3].min()) == 255
    engine.session.redo()
    assert int(engine.compose_frame(0, 1.0, with_alpha=True)[..., 3].max()) == 0


def test_matte_inversion_uses_the_legacy_mode_once():
    engine, clip = build_engine(duration=10)
    source = engine.timeline.find_clip(clip.id)
    target = engine.timeline.new_clip("target", source.source_path, source.track, 0, 10,
                                      source_out=10, source_fps=30.0)
    # Pin the layer coverage to the source alpha rather than a sub-rectangle.
    MATTE.ensure(target).update({"source_id": source.id, "mode": "alpha",
                                 "enabled": True, "invert": False})
    layer = np.zeros((180, 320, 4), np.uint8)
    layer[..., 3] = 255
    normal = engine._apply_matte(target, layer.copy(), 0, 1.0)
    assert int(normal[..., 3].min()) == 255
    MATTE.ensure(target)["invert"] = True
    inverted = engine._apply_matte(target, layer.copy(), 0, 1.0)
    assert int(inverted[..., 3].max()) == 0

    # A legacy alpha_inverted matte remains inverted when opened, and the explicit
    # invert control flips the already-inverted result exactly once.
    MATTE.ensure(target).update({"mode": "alpha_inverted", "invert": False})
    legacy = engine._apply_matte(target, layer.copy(), 0, 1.0)
    assert int(legacy[..., 3].max()) == 0
    MATTE.ensure(target)["invert"] = True
    explicit = engine._apply_matte(target, layer.copy(), 0, 1.0)
    assert int(explicit[..., 3].min()) == 255


def test_mask_inversion_affects_only_intermediate_coverage():
    engine, clip = build_engine()
    mask_id = engine.session.add_clip_mask(clip.id, [(60, 40), (250, 40),
                                                     (250, 140), (60, 140)])
    mask = MASK.find(clip, mask_id)
    layer = np.zeros((180, 320, 4), np.uint8)
    layer[..., :3] = (12, 80, 190)
    layer[..., 3] = 255
    mask["invert"] = False
    normal = MASK_OPS.apply(layer.copy(), MASK.at(clip, 0, engine.session.project_size),
                            1.0)
    mask["invert"] = True
    inverted = MASK_OPS.apply(layer.copy(), MASK.at(clip, 0, engine.session.project_size),
                              1.0)
    assert int(normal[..., 3].min()) == 0 and int(normal[..., 3].max()) == 255
    assert int(inverted[..., 3].min()) == 0 and int(inverted[..., 3].max()) == 255
    np.testing.assert_array_equal(normal[..., 3].astype(np.uint16)
                                  + inverted[..., 3].astype(np.uint16), 255)
    # Inversion changes coverage, never the picture's colour channels.
    np.testing.assert_array_equal(normal[..., :3], inverted[..., :3])


def test_clip_alpha_invert_is_serialized_with_transform_metadata():
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = build_engine()
    engine.session.set_clip_alpha_invert(clip.id, True)
    sections = timeline_to_sections(engine.timeline, lambda _path: "")
    loaded = sections_to_timeline(sections, engine.timeline.fps, lambda _media: "")
    restored = loaded.find_clip(clip.id)
    assert restored is not None and ALPHA.inverted(restored)


def test_layer_alpha_inversion_preserves_partial_coverage_order():
    layer = np.zeros((8, 8, 4), np.uint8)
    layer[..., 3] = np.array([0, 64, 128, 192, 255, 255, 0, 128], np.uint8)
    original = layer[..., 3].copy()
    ALPHA.apply(layer, force=True)
    np.testing.assert_array_equal(layer[..., 3], 255 - original)
    ALPHA.apply(layer, force=True)
    np.testing.assert_array_equal(layer[..., 3], original)


def test_roto_propagation_stops_after_cancellation_without_losing_finished_frames():
    from meenifx.roto.propagate import propagate_seed

    source = np.full((48, 64, 3), 90, np.uint8)
    writes = []
    cancelled = []

    def on_mask(frame, matte):
        writes.append((int(frame), matte.shape))
        cancelled.append(True)

    done = propagate_seed(np.ones((48, 64), np.float32), 0, [0, 1, 2, 3, 4],
                          lambda _frame: source, {},
                          on_mask=on_mask, should_stop=lambda: bool(cancelled))
    assert done == [1]
    assert len(writes) == 1, "a finished mask must stay written when analysis is stopped"


def test_legacy_isolation_effects_stay_registered_and_serialize():
    """Old Roto Brush/Smart Key nodes are hidden from discovery, never stripped on open."""
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = build_engine(duration=12)
    clip.effects = [
        {"effect_id": "roto_brush", "enabled": True,
         "params": {"backend": "basic", "subject": "auto", "matte_view": "composite"},
         "strokes": [{"frame": 0, "points": [[0.2, 0.4], [0.3, 0.5]],
                       "radius": 0.05, "mode": "foreground"}],
         "base_frame": 0},
        {"effect_id": "smart_key", "enabled": True,
         "params": {"subject": "person", "matte_view": "composite"}},
    ]
    registered = {type(effect).key for effect in engine.effects}
    assert {"roto_brush", "smart_key"} <= registered
    sections = timeline_to_sections(engine.timeline, lambda _path: "")
    loaded = sections_to_timeline(sections, engine.timeline.fps, lambda _media: "")
    restored = loaded.find_clip(clip.id)
    assert restored is not None
    entries = {entry["effect_id"]: entry for entry in restored.effects}
    assert entries["roto_brush"]["strokes"][0]["mode"] == "foreground"
    assert entries["roto_brush"]["base_frame"] == 0
    assert entries["smart_key"]["params"]["subject"] == "person"


def test_png_alpha_export_keeps_preview_colours_and_partial_edges(tmp_path):
    """The 4-channel PNG must be straight BGRA, not an RGBA/BGRA swap.

    This regressed once: the compositor returns BGRA, while the image writer treated the
    same buffer as RGBA. Alpha was correct but red and blue were exchanged, which is
    exactly the kind of preview/export mismatch a visual session catches.
    """
    engine = bare_engine()
    clip_id = engine.session.add_generator_clip("solid", start=0, duration=8)
    clip = engine.timeline.find_clip(clip_id)
    SHAPE.ensure_shape(clip).update(gen_color="#FF0000", gen_opacity=100.0)
    mask_id = engine.session.add_clip_mask(
        clip.id, [(0, 0), (170, 0), (170, 180), (0, 180)], closed=True)
    engine.session.update_clip_mask(clip.id, mask_id, {"feather": 6.0})
    preview = engine.compose_frame(0, 1.0, with_alpha=True)
    assert preview is not None
    assert preview[90, 30, 2] > 200 and preview[90, 30, 0] < 30, "preview is BGRA red"
    assert np.any((preview[..., 3] > 0) & (preview[..., 3] < 255)), "preview has a soft edge"

    path = tmp_path / "alpha parity.png"
    engine.export({"output_path": str(path), "format": "png", "alpha": True,
                   "use_source_size": True, "range_start": 0, "range_end": 1})
    exported = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert exported is not None and exported.shape[2] == 4
    np.testing.assert_array_equal(exported, preview)
    assert int(exported[..., 3].min()) == 0 and int(exported[..., 3].max()) == 255
