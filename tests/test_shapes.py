"""Shape clips: geometry, alpha, every kind, keyframes, effects and the project file.

The renderer is exercised directly (so a bad path, a wrong stroke alignment or a preview
scale that disagrees with the export fails without a window), and the clip side runs in
the real session so undo, split, paste and save/reopen prove it is an ordinary clip.
"""

import os
import sys
import time
import uuid

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import shape as SHAPE  # noqa: E402
from meenifx.processing import shapes as RENDER  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")
PROJECT = (640, 360)


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
    settings = ProjectSettings(name=f"Shape {uuid.uuid4().hex[:6]}", width=PROJECT[0],
                               height=PROJECT[1], fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    window.engine.session.set_playhead(0)
    pump(0.08)
    add_still(window)
    return window


def add_still(window):
    from meenifx.project.media import media_type_for
    project = window.projects.project
    item = project.library.add(STILL, 3.0, 320, 180, 30.0, media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    clips = window.insert_media_items([item], frame=0)
    assert clips, "the still was not inserted"
    return window.engine.timeline.find_clip(clips[0])


class _ShapeClip:
    """The only thing the model needs: a settings dict."""

    def __init__(self, kind="rectangle", **overrides):
        self.shape = SHAPE.blank(kind)
        self.shape.update(overrides)


def spec(kind="rectangle", **overrides):
    return SHAPE.resolve(_ShapeClip(kind, **overrides), 0)


def alpha(kind="rectangle", canvas_w=640, canvas_h=360, scale=1.0, **overrides):
    return RENDER.render(spec(kind, **overrides), canvas_w, canvas_h, scale)[:, :, 3]


def bbox(mask):
    rows = np.where(mask.max(axis=1) > 8)[0]
    cols = np.where(mask.max(axis=0) > 8)[0]
    if not len(rows) or not len(cols):
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])


# ------------------------------------------------------------------- the renderer
@pytest.mark.parametrize("kind", SHAPE.KINDS)
def test_every_shape_kind_draws_pixels(kind):
    mask = alpha(kind)
    assert mask.max() == 255, f"{kind} drew nothing"
    painted = float((mask > 8).mean())
    if kind == "line":
        assert 0.001 < painted < 0.10, painted
    else:
        assert 0.03 < painted < 0.60, painted
    # Transparent frame corners: a shape never comes back as a filled rectangle.
    assert mask[0, 0] == 0 and mask[-1, -1] == 0


def test_circle_and_square_are_square_and_round():
    box = bbox(alpha("ellipse", 1000, 1000, 1.0, width=1000.0, height=1000.0))
    assert box is not None
    left, top, right, bottom = box
    assert abs((right - left) - (bottom - top)) <= 2
    # The corners of the bounding box stay empty: this is an ellipse, not a rectangle.
    mask = alpha("ellipse", 1000, 1000, 1.0, width=1000.0, height=1000.0)
    assert mask[top + 2, left + 2] == 0
    assert mask[(top + bottom) // 2, (left + right) // 2] == 255


def test_curved_edges_are_antialiased():
    mask = alpha("ellipse", 400, 200, 1.0, width=320.0, height=180.0)
    soft = int(((mask > 0) & (mask < 255)).sum())
    assert soft > 40, f"only {soft} soft pixels: the edge is not antialiased"


def test_preview_scale_is_the_same_picture_as_the_export():
    big = alpha("star", 640, 360, 1.0)
    small = alpha("star", 320, 180, 0.5)
    big_box = np.array(bbox(big)) * 1.0
    small_box = np.array(bbox(small)) * 2.0            # back into project pixels
    assert np.abs(big_box - small_box).max() <= 3, (big_box, small_box)
    coverage_big = float((big > 8).mean())
    coverage_small = float((small > 8).mean())
    assert abs(coverage_big - coverage_small) < 0.01


def test_fill_and_stroke_are_independent():
    stat = spec("rectangle", fill_opacity=0.0, stroke_width=16.0)
    layer = RENDER.render(stat, 640, 360, 1.0)
    mask = layer[:, :, 3]
    assert mask[180, 320] == 0, "the fill should be invisible"
    assert mask.max() == 255, "the stroke should still be drawn"
    # The stroke colour is white and the (invisible) fill is blue: the ring is white.
    ring = layer[mask > 200]
    assert ring[:, :3].min() > 240


def test_stroke_alignment_puts_the_ring_where_it_says():
    def ring(align):
        return RENDER.render(spec("rectangle", width=320.0, height=180.0,
                                  stroke_width=20.0, stroke_align=align), 400, 200, 1.0)

    inside, centre, outside = ring("inside"), ring("center"), ring("outside")
    # The path is x 40..360 and the stroke is 20 px wide, so its ring runs either side
    # of the outline. Alpha cannot show the ring inside the shape (the fill is opaque),
    # so look at the pixel colour: the stroke is white, the fill is blue.
    assert outside[100, 35][3] == 255 and inside[100, 35][3] == 0
    assert outside[100, 35][1] > 240 and centre[100, 35][1] > 240
    # 15 px inside the outline: an inside stroke paints white there, an outside one
    # leaves the blue fill untouched.
    assert inside[100, 55][1] > 240 and outside[100, 55][1] < 200
    # Inside never draws past the outline; centre and outside both reach past it.
    assert bbox(inside[:, :, 3])[0] > bbox(outside[:, :, 3])[0]
    assert bbox(inside[:, :, 3])[2] < bbox(outside[:, :, 3])[2]


def test_fill_colour_reaches_the_pixels():
    layer = RENDER.render(spec("rectangle", fill_color="#FF0000", stroke_enabled=False),
                          640, 360, 1.0)
    b, g, r, a = layer[180, 320]
    assert a == 255 and r > 245 and g < 12 and b < 12


def test_line_thickness_and_arrowheads_are_drawable():
    plain = alpha("line", stroke_width=6.0)
    thick = alpha("line", stroke_width=18.0)
    assert (thick > 8).sum() > (plain > 8).sum() * 2
    arrowed = alpha("line", stroke_width=6.0, arrow_end=True)
    assert (arrowed > 8).sum() > (plain > 8).sum()


def test_inner_radius_and_sides_change_the_geometry():
    thin_star = (alpha("star", inner_ratio=20.0) > 8).sum()
    fat_star = (alpha("star", inner_ratio=90.0) > 8).sum()
    assert fat_star > thin_star
    triangle = bbox(alpha("polygon", 640, 360, 1.0, sides=3.0,
                          width=400.0, height=340.0))
    hexagon = bbox(alpha("polygon", 640, 360, 1.0, sides=6.0,
                         width=400.0, height=340.0))
    assert triangle != hexagon


# ------------------------------------------------------------------- the clip
def test_add_shape_clip_is_a_real_clip_at_the_playhead(editor):
    window = editor
    window.engine.session.set_playhead(45)
    clip_id = window.engine.session.add_shape_clip("ellipse", box=(100, 50, 200, 120))
    assert clip_id is not None
    clip = window.engine.timeline.find_clip(clip_id)
    assert clip.is_shape and not clip.is_text
    assert clip.track.type == "video" and clip.timeline_start == 45
    assert clip.duration == round(window.engine.timeline.fps * 5)
    assert clip.shape["kind"] == "ellipse"
    assert abs(clip.shape["width"] - 200) < 0.01 and abs(clip.shape["height"] - 120) < 0.01
    # The clip is centred on the dragged box, not on the frame.
    assert abs(clip.transform["position_x"] - (200 - (PROJECT[0] - 1) / 2.0)) < 0.01
    assert clip_id in window.engine.session.selected
    window.engine.session.undo()
    assert window.engine.timeline.find_clip(clip_id) is None


def test_a_shape_composes_over_the_picture(editor):
    window = editor
    window.engine.session.set_playhead(0)
    clip_id = window.engine.session.add_shape_clip(
        "rectangle", box=(220, 130, 200, 100))
    assert clip_id is not None
    pump(0.1)
    frame, _processed = window.engine.render_frame(0, 1.0)
    assert frame.shape[:2] == (PROJECT[1], PROJECT[0])
    b, g, r = frame[180, 320]
    # The default fill is #4C9AFF: blue-dominant, and clearly not the dark still.
    assert b > 180 and r < 140 and g > 100, (b, g, r)


def test_numeric_shape_channels_are_keyframeable(editor):
    window = editor
    clip_id = window.engine.session.add_shape_clip("rectangle", box=(220, 130, 100, 60))
    clip = window.engine.timeline.find_clip(clip_id)
    CH.animate(clip, "shape.width", 0)
    CH.set_value(clip, "shape.width", 100.0, 0, True)
    CH.set_value(clip, "shape.width", 400.0, 30, True)
    assert CH.key_times(clip, "shape.width") == [0, 30]
    assert SHAPE.resolve(clip, 0)["width"] < SHAPE.resolve(clip, 30)["width"]
    rows = {row["id"]: row for row in CH.list_channels(clip)}
    assert rows["shape.width"]["animated"] is True
    assert rows["shape.width"]["group"] == "Shape"
    CH.toggle_key(clip, "shape.width", 30)
    assert CH.key_times(clip, "shape.width") == [0]
    CH.toggle_key(clip, "shape.fill_opacity", 12)
    assert CH.key_times(clip, "shape.fill_opacity") == [12]
    assert CH.numeric_channel(clip, "shape.fill_opacity") is True


def test_fill_colour_keys_hold_until_the_next_key(editor):
    window = editor
    clip_id = window.engine.session.add_shape_clip("star", box=(220, 100, 200, 200))
    clip = window.engine.timeline.find_clip(clip_id)
    CH.set_value(clip, "shape.fill_color", "#FF0000", 0, True)
    CH.set_value(clip, "shape.fill_color", "#00FF00", 20, True)
    assert CH.key_times(clip, "shape.fill_color") == [0, 20]
    assert SHAPE.color_at(clip, "fill_color", 10) == "#FF0000"
    assert SHAPE.color_at(clip, "fill_color", 20) == "#00FF00"
    assert CH.value_at(clip, "shape.fill_color", 5) == "#FF0000"
    keys = CH.key_records(clip, "shape.fill_color")
    assert [row[0] for row in keys] == [0, 20] and keys[0][2] == "hold"
    CH.remove_key(clip, "shape.fill_color", 20)
    assert CH.key_times(clip, "shape.fill_color") == [0]


def test_split_keeps_the_shape_on_both_pieces(editor):
    window = editor
    clip_id = window.engine.session.add_shape_clip("polygon", box=(220, 100, 200, 200))
    clip = window.engine.timeline.find_clip(clip_id)
    before = set(clip.shape)
    window.engine.session.split_clip_at(clip, clip.timeline_start + 60)
    pieces = window.engine.timeline.all_clips()
    shapes = [piece for piece in pieces if piece.is_shape]
    assert len(shapes) == 2, "the clip did not split"
    for piece in shapes:
        assert piece.shape["kind"] == "polygon"
        assert set(piece.shape) == before
        assert abs(piece.shape["width"] - 200) < 0.01


def test_copy_and_paste_keeps_the_shape(editor):
    window = editor
    clip_id = window.engine.session.add_shape_clip(
        "rounded_rectangle", box=(100, 100, 180, 90))
    clip = window.engine.timeline.find_clip(clip_id)
    CH.set_value(clip, "shape.radius", 33.0)
    clip.shape["fill_color"] = "#123456"
    before = {piece.id for piece in window.engine.timeline.all_clips()}
    record = window.engine.timeline.clip_record(clip)
    assert record.get("shape")
    window.engine.timeline.paste_records([record], 400)
    pasted = [piece for piece in window.engine.timeline.all_clips()
              if piece.id not in before]
    assert pasted, "nothing was pasted"
    copy = pasted[0]
    assert copy.is_shape and copy.shape["fill_color"] == "#123456"
    assert abs(copy.shape["radius"] - 33.0) < 0.01
    assert copy.shape["kind"] == "rounded_rectangle"


def test_effects_apply_to_a_shape(editor):
    window = editor
    clip_id = window.engine.session.add_shape_clip("ellipse", box=(220, 130, 200, 100))
    clip = window.engine.timeline.find_clip(clip_id)
    window.engine.session.apply_to_clips([clip.id], "Add puppet", lambda c: c.effects.append(
        {"effect_id": "puppet_warp", "enabled": True, "params": {}}))
    pump(0.1)
    frame, _processed = window.engine.render_frame(0, 1.0)
    assert frame is not None and frame[180, 320].max() > 0


def test_shape_survives_save_and_reopen(editor):
    window = editor
    clip_id = window.engine.session.add_shape_clip("star", box=(220, 100, 200, 200))
    clip = window.engine.timeline.find_clip(clip_id)
    clip.shape["kind"] = "star"
    clip.shape["points"] = 7.0
    clip.shape["stroke_align"] = "outside"
    CH.animate(clip, "shape.width", 0)
    CH.set_value(clip, "shape.width", 200.0, 0, True)
    CH.set_value(clip, "shape.width", 320.0, 20, True)
    CH.set_value(clip, "shape.fill_color", "#FF00AA", 0, True)
    CH.set_value(clip, "shape.fill_color", "#00FFAA", 15, True)
    path = window.projects.project.path
    assert window.projects.save()
    pump(0.1)
    assert window.projects.close_project()
    pump(0.1)
    assert window.projects.open_path(path)
    pump(0.25)
    reloaded = window.engine.timeline.find_clip(clip_id)
    assert reloaded is not None and reloaded.is_shape
    assert reloaded.shape["kind"] == "star"
    assert abs(reloaded.shape["points"] - 7.0) < 0.01
    assert reloaded.shape["stroke_align"] == "outside"
    width = reloaded.shape["width"]
    assert [key.time for key in width.keyframes] == [0, 20]
    assert CH.key_times(reloaded, "shape.fill_color") == [0, 15]
    assert SHAPE.color_at(reloaded, "fill_color", 10) == "#FF00AA"
    assert int(SHAPE.resolve(reloaded, 20)["width"]) == 320


def test_export_and_preview_agree_on_a_shape(editor, tmp_path):
    window = editor
    window.engine.session.set_playhead(0)
    window.engine.session.add_shape_clip("polygon", box=(180, 90, 280, 180))
    pump(0.15)
    frame, _processed = window.engine.render_frame(0, 1.0)
    out = tmp_path / "shape.png"
    settings = {"output_path": str(out), "format": "png", "range_start": 0, "range_end": 1,
                "use_source_size": True, "use_source_fps": True}
    written, count, _fps, _size = window.engine.export(settings)
    assert count == 1
    exported = __import__("cv2").imread(written)
    assert exported.shape[:2] == (PROJECT[1], PROJECT[0])
    diff = float(np.abs(exported.astype(np.int16) - frame.astype(np.int16)).mean())
    assert diff < 2.0, "the export must be the frame the preview showed"
