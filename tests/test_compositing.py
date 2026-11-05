"""Compositing: blend modes, effect Mix and adjustment layers.

Blend modes are checked against known pixel values (not against the implementation's
own formula), the preview and the export are compared through the one compositor, and
an adjustment layer is proven to touch only the layers below it and only while it plays.
"""

import sys

import numpy as np
import pytest

from conftest import frame_diff

from meenifx.engine import EditorEngine
from meenifx.media.cache import MediaEntry
from meenifx.model.timeline import Timeline
from test_keyframe_ui import editor  # noqa: F401  (shared MainWindow fixture)

from meenifx.processing import blend as B

BASE = 64
TOP = 192

# (mode, expected channel value) for a solid 64 backdrop and a solid 192 source.
KNOWN = (
    ("normal", 192.0),
    ("add", 255.0),
    ("screen", 207.8),
    ("multiply", 48.2),
    ("overlay", 96.4),
    ("hard_light", 160.6),
    ("soft_light", 88.3),
    ("difference", 128.0),
    ("darken", 64.0),
    ("lighten", 192.0),
    ("color_dodge", 255.0),
    ("color_burn", 1.3),
)


def solid_engine(bottom=BASE, top=TOP, size=(32, 18)):
    """Two stacked opaque clips, each a solid colour (BGR order unchanged)."""
    engine = EditorEngine(30)
    engine.source_width, engine.source_height = size
    layers = {}
    for name, value in (("bottom.png", bottom), ("top.png", top)):
        entry = MediaEntry("video", 60, 30)
        entry.frames = [np.full((size[1], size[0], 3), value, np.uint8) for _ in range(60)]
        engine.media._entries[name] = entry
        track = engine.timeline.add_track("video")
        layers[name] = engine.timeline.new_clip(name, name, track, 0, 60,
                                                source_out=60, source_fps=30)
    return engine, layers["bottom.png"], layers["top.png"]


def pixel(engine, frame=0, scale=1.0):
    return engine.render_frame(frame, scale)[1][0, 0].tolist()


def test_the_mode_list_and_labels_agree():
    assert tuple(key for key, _label in B.BLEND_LABELS) == B.BLEND_MODES
    assert [B.mode_label(key) for key in B.BLEND_MODES] == [label for _k, label in B.BLEND_LABELS]
    assert B.normalise_mode("Soft Light") == "soft_light"
    assert B.normalise_mode("nonsense") == "normal"


@pytest.mark.parametrize("mode,expected", KNOWN, ids=[mode for mode, _v in KNOWN])
def test_blend_modes_match_known_pixel_values(mode, expected):
    base = np.full((4, 4, 3), float(BASE), np.float32)
    top = np.full((4, 4, 3), float(TOP), np.float32)
    value = float(np.clip(B.blend_rgb(base, top, mode), 0, 255)[0, 0, 0])
    assert value == pytest.approx(expected, abs=0.5)


@pytest.mark.parametrize("mode,expected", KNOWN, ids=[mode for mode, _v in KNOWN])
def test_every_mode_renders_through_the_compositor(mode, expected):
    engine, _bottom, top = solid_engine()
    top.blend = mode
    value = pixel(engine)
    assert value == [pytest.approx(expected, abs=2.0)] * 3, f"{mode} rendered {value}"


def test_normal_is_the_old_composite():
    engine, bottom, top = solid_engine()
    assert pixel(engine) == [TOP] * 3
    engine.timeline.tracks[0].clips.remove(top)
    assert pixel(engine) == [BASE] * 3


def test_alpha_mixes_the_blended_colour_by_the_layer_coverage():
    engine, _bottom, top = solid_engine()
    top.blend = "screen"
    top.transform["opacity"] = 50
    screen = 255.0 - (255.0 - BASE) * (255.0 - TOP) / 255.0
    assert pixel(engine) == [pytest.approx(BASE + (screen - BASE) * 0.5, abs=2.0)] * 3

    # A transparent layer never paints the backdrop, whatever the mode.
    top.transform["opacity"] = 0
    assert pixel(engine) == [BASE] * 3


def test_preview_and_export_agree_on_blends():
    engine, _bottom, top = solid_engine(size=(64, 36))
    top.blend = "color_dodge"
    full = engine.render_frame(3, 1.0)[1]
    preview = engine.render_frame(3, 0.5)[1]
    import cv2
    reduced = cv2.resize(full, (preview.shape[1], preview.shape[0]),
                         interpolation=cv2.INTER_AREA)
    assert frame_diff(reduced, preview) < 1.0
    assert pixel(engine, 3, 1.0) == pixel(engine, 3, 0.5)


def test_blend_mode_changes_are_undoable():
    engine, _bottom, top = solid_engine()
    assert engine.session.set_blend_mode(top.id, "multiply") is True
    assert engine.timeline.find_clip(top.id).blend == "multiply"
    engine.session.undo()
    assert engine.timeline.find_clip(top.id).blend == "normal"
    engine.session.redo()
    assert engine.timeline.find_clip(top.id).blend == "multiply"


def _brightness(clip, amount=50.0):
    clip.effects = [{"effect_id": "color_correct", "enabled": True,
                     "params": {"brightness": amount}}]


def adjustment_engine(with_top=True):
    engine, bottom, top = solid_engine()
    track = engine.timeline.add_track("video")          # above the bottom clip
    adjust = engine.timeline.new_clip("Adjustment", "", track, 0, 10, adjustment=True)
    if with_top:
        # Put the top clip back above the adjustment layer so we can prove isolation.
        engine.timeline.tracks.remove(top.track)
        engine.timeline.tracks.insert(0, top.track)
    else:
        engine.timeline.tracks.remove(top.track)
    return engine, bottom, adjust, top


def test_adjustment_layer_grades_what_is_below_it():
    engine, bottom, adjust, top = adjustment_engine()
    _brightness(adjust)
    graded_bottom = pixel(engine)
    assert graded_bottom[0] == pytest.approx(191, abs=3)
    assert not any(entry["effect_id"] == "color_correct" for entry in bottom.effects)


def test_adjustment_layer_never_touches_the_layer_above_it():
    engine, bottom, adjust, top = adjustment_engine()
    _brightness(adjust, 100.0)      # would clip any picture it reached
    assert top.blend == "normal"
    # The top clip is opaque and above the adjustment layer: its pixels are untouched.
    above = pixel(engine)
    assert above == [TOP] * 3
    # And removing it changes nothing about the top layer.
    engine.timeline.tracks[1].clips.remove(adjust)
    assert pixel(engine) == [TOP] * 3


def test_adjustment_layer_only_applies_inside_its_range():
    engine, _bottom, adjust, _top = adjustment_engine(with_top=False)
    _brightness(adjust)
    graded = pixel(engine, 5)[0]
    plain = pixel(engine, 20)[0]
    assert graded == pytest.approx(191, abs=3)
    assert plain == BASE, "the grade leaked past the adjustment layer's end"


def test_adjustment_layer_opacity_mixes_the_grade():
    engine, _bottom, adjust, _top = adjustment_engine(with_top=False)
    _brightness(adjust, 100.0)
    full = pixel(engine)[0]
    adjust.transform["opacity"] = 50
    half = pixel(engine)[0]
    assert full == pytest.approx(255, abs=2)
    assert half == pytest.approx((BASE + full) / 2, abs=3)


def test_adjustment_layer_fades_like_a_clip():
    engine, _bottom, adjust, _top = adjustment_engine(with_top=False)
    _brightness(adjust, 100.0)
    adjust.fade_in = 10
    # A fade is a multiplier on the mix, so frame 0 is only a sliver of the grade.
    assert pixel(engine, 0)[0] < BASE + 25, "a fade-in must start almost at the original"
    assert pixel(engine, 9)[0] > BASE + 100


def test_adjustment_layer_is_one_undo_step():
    engine, _bottom, _adjust, _top = adjustment_engine()
    count = sum(len(track.clips) for track in engine.timeline.tracks)
    adjust = next(clip for track in engine.timeline.tracks for clip in track.clips
                  if clip.adjustment)
    engine.session.stack.run("Remove adjustment", lambda: adjust.track.clips.remove(adjust))
    engine.session.undo()
    assert sum(len(track.clips) for track in engine.timeline.tracks) == count


def test_effect_mix_blends_the_original_with_the_result():
    engine, _bottom, top = solid_engine()
    engine.timeline.tracks.remove(top.track)        # one clip: the fast path must not hide it
    engine.timeline.tracks[0].clips[0].effects = [{
        "effect_id": "color_correct", "enabled": True,
        "params": {"brightness": 100.0, "mix": 50}}]
    half = pixel(engine)[0]
    engine.timeline.tracks[0].clips[0].effects[0]["params"]["mix"] = 100
    full = pixel(engine)[0]
    engine.timeline.tracks[0].clips[0].effects[0]["params"]["mix"] = 0
    original = pixel(engine)[0]
    assert original == BASE
    assert full == pytest.approx(255, abs=2)
    assert half == pytest.approx((original + full) / 2, abs=3)


def test_mix_is_animatable_and_follows_the_playhead():
    from meenifx.model.keyframes import AnimatedValue

    engine, _bottom, top = solid_engine()
    engine.timeline.tracks.remove(top.track)
    curve = AnimatedValue()
    curve.set_keyframe(0, 0)
    curve.set_keyframe(20, 100)
    engine.timeline.tracks[0].clips[0].effects = [{
        "effect_id": "color_correct", "enabled": True,
        "params": {"brightness": 100.0, "mix": curve}}]
    start = pixel(engine, 0)[0]
    middle = pixel(engine, 10)[0]
    end = pixel(engine, 20)[0]
    assert start == pytest.approx(BASE, abs=2)
    assert middle > start + 40
    assert end == pytest.approx(255, abs=2)


def test_blend_and_adjustment_survive_a_save_and_load():
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, _bottom, top = solid_engine()
    top.blend = "soft_light"
    track = engine.timeline.add_track("video")
    engine.timeline.new_clip("Adjustment", "", track, 0, 12, adjustment=True)
    sections = timeline_to_sections(engine.timeline, lambda path: path)
    restored = sections_to_timeline(sections, 30, lambda media: media)
    clips = {clip.name: clip for clip in restored.all_clips()}
    assert clips["top.png"].blend == "soft_light"
    assert clips["Adjustment"].adjustment is True
    assert clips["bottom.png"].blend == "normal"


def test_the_cache_digest_notices_blend_and_adjustment_changes():
    from meenifx.preview.invalidation import clip_digest

    engine, _bottom, top = solid_engine()
    before = clip_digest(top)
    top.blend = "screen"
    assert clip_digest(top) != before, "a blend change must not reuse cached frames"
    track = engine.timeline.add_track("video")
    adjust = engine.timeline.new_clip("Adjustment", "", track, 0, 10)
    plain = clip_digest(adjust)
    adjust.adjustment = True
    assert clip_digest(adjust) != plain


def test_the_clip_card_has_a_blend_mode_dropdown(qapp):
    from PyQt6.QtWidgets import QComboBox

    from meenifx.ui.panels import EffectsPanel

    engine, _bottom, top = solid_engine()
    engine.session.set_selection({top.id})
    panel = EffectsPanel(engine)
    panel.refresh_properties()
    combos = [combo for combo in panel.properties.findChildren(QComboBox) if combo.count() == 12]
    assert combos, "the clip card must offer the twelve blend modes"
    combo = combos[0]
    combo.setCurrentIndex(2)
    combo.activated.emit(2)
    assert engine.timeline.find_clip(top.id).blend == combo.itemData(2) != "normal"


def test_every_effect_card_gets_a_mix_slider(qapp):
    from meenifx.ui.panels import EffectsPanel

    engine, _bottom, clip = solid_engine()
    clip.effects = [{"effect_id": "invert", "enabled": True, "params": {}},
                    {"effect_id": "vignette", "enabled": True, "params": {}}]
    engine.session.set_selection({clip.id})
    panel = EffectsPanel(engine)
    panel.refresh_properties()
    mix_rows = [row for row in panel._rows
                if row.get("kind") == "param" and row.get("name") == "mix"]
    assert len(mix_rows) == 2, "one Mix control per effect card"
    assert mix_rows[0]["spec"].default == 100


def test_the_add_menu_creates_an_adjustment_layer(editor):
    from PyQt6.QtGui import QAction

    action = next(item for item in editor.findChildren(QAction)
                  if item.text() == "Adjustment Layer")
    action.trigger()
    clips = [clip for clip in editor.engine.timeline.all_clips() if clip.adjustment]
    assert clips, "Add > Adjustment Layer must create a clip"
    assert clips[0].track.type == "video"
    assert editor.engine.session.selected == {clips[0].id}


def test_older_projects_without_the_columns_still_load():
    from meenifx.project import container as C

    sections = {"clips": {"id": [1], "track": [0], "media": [""], "name": ["x"], "start": [0],
                          "dur": [10], "src_in": [0], "src_out": [10], "src_min": [0],
                          "src_max": [10], "src_fps": [30], "tl_fps": [30], "speed": [1.0],
                          "reverse": [False], "loop": [False], "freeze": [False], "offset": [0],
                          "volume": [1.0], "fade_in": [0], "fade_out": [0],
                          "fade_in_curve": [0.0], "fade_out_curve": [0.0],
                          "stream": ["video"], "link": [0], "transform": [{}], "meta": [{}],
                          "text": [None], "gain": [0.0], "presets": [None]},
                "tracks": {"tracks": [{"id": 1, "type": "video", "name": "V1", "muted": False,
                                       "solo": False, "locked": False, "visible": True,
                                       "volume": 1.0, "height": None}],
                           "counters": {"video": 1, "audio": 0}, "max_clip": 1},
                "effects": {"clip": [], "effect_id": [], "enabled": [], "params": [], "pins": [],
                            "data": []},
                "keyframes": {"owners": [], "kinds": [], "owner": [], "time": [], "value": [],
                              "interp": [], "in_influence": [], "out_influence": [],
                              "in_value_handle": [], "out_value_handle": [], "interp_names": []},
                "markers": {"markers": [], "loop_in": None, "loop_out": None,
                            "loop_enabled": False, "loop_at_end": True}}
    migrated = C.migrate(sections, 6)
    assert migrated["clips"]["blend"] == ["normal"]
    assert migrated["clips"]["adjust"] == [False]
