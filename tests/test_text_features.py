"""Advanced text: installed fonts, real icons, formatting and Split Text options."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import build_engine  # noqa: E402

from meenifx.icons import icon  # noqa: E402
from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import text as TXT  # noqa: E402
from meenifx.model import transform as T  # noqa: E402
from meenifx.processing import fonts, text as TEXT, text_units  # noqa: E402


def add_text(engine, content, align="center", **values):
    clip_id = engine.session.add_text_clip(content, 0, 90)
    clip = engine.timeline.find_clip(clip_id)
    clip.text["align"] = align
    clip.text.update(values)
    return clip


def render(clip, width=640, height=360):
    return TEXT.render(TXT.resolve(clip, 0), width, height)


def ink(frame):
    return frame[:, :, 3] > 24


def centroid(mask):
    ys, xs = np.nonzero(mask)
    return float(xs.mean()), float(ys.mean())


# ------------------------------------------------------------------------ fonts
def test_installed_fonts_are_discovered_without_a_hardcoded_list(qapp):
    families = fonts.system_families()
    assert len(families) > 10
    for name in ("Arial", "Consolas", "Comic Sans MS"):
        if fonts.installed(name):
            assert name.casefold() in {item.casefold() for item in families}
    assert fonts.installed("Arial")
    # A face resolution points at a real file and the bold/italic choice differs.
    regular = fonts.resolve("Arial")
    bold_italic = fonts.resolve("Arial", True, True)
    assert regular is not None and os.path.exists(regular[0])
    assert bold_italic is not None and bold_italic != regular
    assert fonts.resolve("Definitely Not Installed 123") is None
    assert not fonts.installed("Definitely Not Installed 123")


def test_a_custom_installed_font_resolves_and_renders(qapp):
    """Angelic War is the font the report used; skip when it is not installed."""
    if not fonts.installed("Angelic War"):
        pytest.skip("Angelic War is not installed on this machine")
    face = fonts.resolve("Angelic War")
    assert face is not None and os.path.exists(face[0])
    font = TEXT.font_for("Angelic War", False, False, 48)
    assert font.getname()[0].casefold() == "angelic war"
    class Clip:
        pass
    clip = Clip()
    clip.text = TXT.blank()
    clip.text["text"] = "HELLO"
    clip.text["font"] = "Angelic War"
    clip.text["size"] = 72
    custom = render(clip)
    clip.text["font"] = "Arial"
    arial = render(clip)
    assert ink(custom).sum() > 50
    assert not np.array_equal(ink(custom), ink(arial))


# ------------------------------------------------------------------------ icons
def test_every_text_formatting_icon_paints_something(qapp):
    names = ("bold", "italic", "underline", "strikethrough", "align_left", "align_center",
             "align_right", "align_justify", "text_color", "text_outline", "text_shadow",
             "letter_spacing", "line_spacing")
    for name in names:
        pixmap = icon(name).pixmap(16, 16)
        assert not pixmap.isNull(), name
        image = pixmap.toImage()
        opaque = sum(1 for x in range(image.width()) for y in range(image.height())
                     if image.pixelColor(x, y).alpha() > 0)
        assert opaque > 5, f"{name} is blank"


# -------------------------------------------------------------------- formatting
def test_formatting_changes_the_rendered_picture(qapp):
    class Clip:
        pass
    clip = Clip()
    clip.text = TXT.blank()
    clip.text["text"] = "HELLO WORLD"
    clip.text["size"] = 72
    plain = render(clip)
    clip.text["gradient"] = True
    clip.text["gradient_color"] = "#FF0000"
    gradient = render(clip)
    assert not np.array_equal(plain, gradient)
    clip.text["strikethrough"] = True
    struck = render(clip)
    assert not np.array_equal(gradient, struck)
    clip.text["word_spacing"] = 40
    spaced = render(clip)
    assert TEXT.bounds(TXT.resolve(clip, 0), 640, 360)[2] > \
        TEXT.bounds({**TXT.resolve(clip, 0), "word_spacing": 0.0}, 640, 360)[2]


def test_case_conversion_happens_before_layout(qapp):
    class Clip:
        pass
    lower = Clip()
    lower.text = TXT.blank()
    lower.text["text"] = "hello"
    lower.text["case"] = "upper"
    upper = Clip()
    upper.text = TXT.blank()
    upper.text["text"] = "HELLO"
    assert np.array_equal(render(lower), render(upper))


def test_anchor_moves_the_block_vertically(qapp):
    class Clip:
        pass
    clip = Clip()
    clip.text = TXT.blank()
    clip.text["text"] = "HELLO"
    clip.text["anchor"] = "top"
    top = TEXT.bounds(TXT.resolve(clip, 0), 640, 360)[1]
    clip.text["anchor"] = "bottom"
    bottom = TEXT.bounds(TXT.resolve(clip, 0), 640, 360)[1]
    assert bottom > top


def test_justify_fills_short_lines(qapp):
    class Clip:
        pass
    clip = Clip()
    clip.text = TXT.blank()
    clip.text["text"] = "A MUCH LONGER LINE\nSHORT"
    clip.text["size"] = 48
    clip.text["align"] = "left"
    left = render(clip)
    clip.text["align"] = "justify"
    justified = render(clip)
    assert ink(justified).sum() >= ink(left).sum()
    assert not np.array_equal(left, justified)


# --------------------------------------------------------------------- graphemes
def test_graphemes_keep_marks_emojis_and_flags_together(qapp):
    assert TEXT.graphemes("e\u0301") == ["e\u0301"]
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
    assert TEXT.graphemes("A" + family) == ["A", family]
    assert TEXT.graphemes("\U0001F1FA\U0001F1F8") == ["\U0001F1FA\U0001F1F8"]
    assert TEXT.needs_shaping("\u0645\u0631\u062d\u0628\u0627")
    assert not TEXT.needs_shaping("HELLO")


# ------------------------------------------------------------------ preview box
def test_transform_corners_for_an_explicit_content_rect(qapp):
    """Regression for the Alpha 4 preview crash: the helper must exist and work."""
    assert hasattr(T, "content_corners_rect")
    state = T.TransformState(position_x=0.0, position_y=0.0, scale_x=100.0,
                             scale_y=100.0, rotation=0.0, anchor_x=0.0, anchor_y=0.0)
    corners = T.content_corners_rect(state, (640, 360), (640, 360),
                                     (100.0, 50.0, 200.0, 80.0))
    assert len(corners) == 4
    xs = sorted({round(point[0], 3) for point in corners})
    ys = sorted({round(point[1], 3) for point in corners})
    assert len(xs) == 2 and len(ys) == 2


# ------------------------------------------------------------------ split text
def test_split_words_keeps_left_aligned_text_in_place(qapp):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = build_engine(duration=90)
    clip = add_text(engine, "HELLO WORLD", align="left", size=64)
    width, height = engine.project_size()
    units = text_units.layout_units(TXT.resolve(clip, 0), width, height, 1.0)["word"]
    original = ink(render(clip, width, height))
    canvas = TimelinePanel(engine, None).canvas
    pieces = [engine.timeline.find_clip(cid) for cid in canvas.split_text(clip, "word")]
    assert len(pieces) == len(units) == 2
    joined = np.zeros_like(original)
    for piece, (centre_x, _centre_y, word) in zip(pieces, units):
        assert piece.text["text"] == word
        assert piece.text["align"] == "center"
        assert piece.text["wrap"] == 0.0
        assert width / 2.0 + piece.text["offset_x"] == pytest.approx(centre_x, abs=0.6)
        joined |= ink(render(piece, width, height))
    before_x, before_y = centroid(original)
    after_x, after_y = centroid(joined)
    assert abs(before_x - after_x) < 1.5 and abs(before_y - after_y) < 1.5


def test_split_lines_keeps_every_line_in_place(qapp):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = build_engine(duration=90)
    clip = add_text(engine, "ONE\nTWO\nTHREE", align="left", size=56,
                    line_spacing=1.4)
    width, height = engine.project_size()
    units = text_units.layout_units(TXT.resolve(clip, 0), width, height, 1.0)["line"]
    original = ink(render(clip, width, height))
    canvas = TimelinePanel(engine, None).canvas
    pieces = [engine.timeline.find_clip(cid) for cid in canvas.split_text(clip, "line")]
    assert len(pieces) == len(units) == 3
    joined = np.zeros_like(original)
    for piece, (centre_x, centre_y, _line) in zip(pieces, units):
        assert width / 2.0 + piece.text["offset_x"] == pytest.approx(centre_x, abs=0.6)
        assert height / 2.0 + piece.text["offset_y"] == pytest.approx(centre_y, abs=0.6)
        joined |= ink(render(piece, width, height))
    before_x, before_y = centroid(original)
    after_x, after_y = centroid(joined)
    assert abs(before_x - after_x) < 1.5 and abs(before_y - after_y) < 1.5


def test_split_can_keep_a_hidden_original_and_undo_in_one_step(qapp):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = build_engine(duration=90)
    clip = add_text(engine, "AB CD", size=64)
    original_id = clip.id
    canvas = TimelinePanel(engine, None).canvas
    pieces = canvas.split_text(clip, "word", {"action": "hide", "new_track": True})
    assert len(pieces) == 2
    hidden_tracks = [track for track in engine.timeline.tracks if not track.visible
                     and any(member.id == original_id for member in track.clips)]
    assert hidden_tracks, "the original should be on a hidden track"
    assert engine.timeline.find_clip(original_id) is not None
    engine.session.stack.undo()
    restored = engine.timeline.find_clip(original_id)
    assert restored is not None and restored.track.visible
    assert all(engine.timeline.find_clip(cid) is None for cid in pieces)


def test_duplicate_and_split_keeps_the_original_visible(qapp):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = build_engine(duration=90)
    clip = add_text(engine, "AB CD", size=64)
    original_id = clip.id
    canvas = TimelinePanel(engine, None).canvas
    pieces = canvas.split_text(clip, "word", {"action": "duplicate"})
    assert engine.timeline.find_clip(original_id) is not None
    assert all(engine.timeline.find_clip(cid) is not None for cid in pieces)
    engine.session.stack.undo()
    assert engine.timeline.find_clip(original_id) is not None
    assert all(engine.timeline.find_clip(cid) is None for cid in pieces)


def test_stagger_is_only_applied_when_an_animation_was_chosen(qapp):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = build_engine(duration=90)
    clip = add_text(engine, "ABCD", size=64)
    canvas = TimelinePanel(engine, None).canvas
    plain = [engine.timeline.find_clip(cid) for cid in canvas.split_text(clip, "letter")]
    assert all(not CH.key_times(piece, "transform.opacity") for piece in plain)
    assert all("animation" not in (piece.text or {}) for piece in plain)

    clip = add_text(engine, "WXYZ", size=64)
    options = {"action": "replace", "new_track": False,
               "stagger": {"unit": "letter", "delay": 5, "direction": "forward",
                           "preset": "Fade In", "duration": 8, "easing": "Smooth"}}
    staggered = [engine.timeline.find_clip(cid)
                 for cid in canvas.split_text(clip, "letter", options)]
    firsts = [min(CH.key_times(piece, "transform.opacity")) for piece in staggered]
    assert firsts == sorted(firsts) and firsts[0] < firsts[-1]
    assert all(CH.key_times(piece, "transform.opacity") for piece in staggered)


def test_split_is_one_undo_step_with_options(qapp):
    from meenifx.ui.timeline_widget import TimelinePanel

    engine, _clip = build_engine(duration=90)
    clip = add_text(engine, "ONE TWO", size=64)
    canvas = TimelinePanel(engine, None).canvas
    created = canvas.split_text(clip, "word", {"action": "hide", "new_track": True})
    engine.session.stack.undo()
    assert len([track for track in engine.timeline.tracks
                if any(member.id in created for member in track.clips)]) == 0
    assert len([clip for track in engine.timeline.tracks for clip in track.clips
                if clip.is_text]) == 1
