"""Generated text/shape sharpness and animated-text colour correctness."""

import numpy as np
import pytest

from conftest import build_engine

from meenifx.model import presets as P
from meenifx.preview.invalidation import clip_digest


def blank_engine(width=640, height=360):
    engine, _clip = build_engine()
    engine.timeline.tracks.clear()
    engine.source_width, engine.source_height = width, height
    engine.session.project_size = (width, height)
    return engine


def add_shape(engine, scale=10.0, kind="rectangle", **values):
    track = engine.timeline.add_track("video")
    clip_id = engine.session.add_shape_clip(kind, start=0, duration=60, track=track)
    clip = engine.timeline.find_clip(clip_id)
    clip.shape.update({"width": 32.0, "height": 20.0, "fill_color": "#FFFFFF",
                       "stroke_enabled": False})
    clip.shape.update(values)
    clip.transform.update({"scale_x": scale * 100.0, "scale_y": scale * 100.0})
    return engine, clip


def add_text(engine, text="I", size=24.0, scale=10.0, color="#FFFFFF", **values):
    track = engine.timeline.add_track("video")
    clip_id = engine.session.add_text_clip(text, start=0, duration=60, track=track)
    clip = engine.timeline.find_clip(clip_id)
    clip.text.update({"size": size, "font": "DejaVu Sans", "color": color,
                      "stroke_width": 0.0, "shadow_opacity": 0.0,
                      "fill_opacity": 100.0})
    clip.text.update(values)
    clip.transform.update({"scale_x": scale * 100.0, "scale_y": scale * 100.0})
    return engine, clip


def edge_stats(image):
    """Peak horizontal gradient and transition pixels along the shape's centre row."""
    gray = image.max(axis=2).astype(np.int16)
    gradient = np.abs(np.diff(gray, axis=1))
    row = np.abs(np.diff(gray[gray.shape[0] // 2]))
    return int(gradient.max()), int((row > 8).sum())


@pytest.mark.parametrize("scale", [5.0, 10.0])
def test_small_shapes_are_rasterised_at_their_transformed_density(scale):
    engine, _clip = add_shape(blank_engine(), scale=scale)
    image = engine.render_frame(0, 1.0)[1]
    peak, soft = edge_stats(image)
    assert peak > 100, f"a {scale * 100:.0f}% shape edge is still being upscaled (gradient {peak})"
    assert soft <= 8, f"the edge covers {soft} pixels on a centre row; it was enlarged"


@pytest.mark.parametrize("scale", [5.0, 10.0])
def test_small_text_is_rasterised_at_its_transformed_density(scale):
    engine, _clip = add_text(blank_engine(), scale=scale)
    image = engine.render_frame(0, 1.0)[1]
    peak, _soft = edge_stats(image)
    assert peak > 120, f"{scale * 100:.0f}% text lost its outline (gradient {peak})"
    assert int((image.max(axis=2) > 128).sum()) > 300, "the text disappeared"


@pytest.mark.parametrize("kind", ["rectangle", "ellipse", "rounded_rectangle", "polygon", "star", "line"])
def test_every_shape_kind_still_draws_through_the_detail_path(kind):
    engine, _clip = add_shape(blank_engine(), scale=5.0, kind=kind)
    image = engine.render_frame(0, 1.0)[1]
    assert int((image.max(axis=2) > 16).sum()) > 200, f"{kind} drew nothing"


def test_rotation_and_scale_keep_the_generated_picture_filled():
    engine, clip = add_shape(blank_engine(), scale=5.0)
    clip.transform.update({"rotation": 33.0})
    image = engine.render_frame(0, 1.0)[1]
    assert int((image.max(axis=2) > 16).sum()) > 1000


def test_rotation_keeps_the_corners_outside_the_untransformed_box():
    engine, clip = add_shape(blank_engine(), scale=5.0)
    clip.transform.update({"rotation": 45.0})
    image = engine.render_frame(0, 1.0)[1]
    mask = image.max(axis=2) > 16
    ys, xs = np.nonzero(mask)
    assert xs.size and (ys.max() - ys.min()) > 150, \
        "the rotated shape was cropped to its unrotated bounds"


def test_a_generated_effect_still_renders_through_the_detail_path():
    engine, clip = add_shape(blank_engine(), scale=10.0)
    clip.effects = [{"effect_id": "color_correct", "enabled": True, "params": {}}]
    image = engine.render_frame(0, 1.0)[1]
    peak, soft = edge_stats(image)
    assert int((image.max(axis=2) > 16).sum()) > 1000
    assert peak > 100 and soft <= 8, "an identity effect forced the layer back to a low-resolution bitmap"


@pytest.mark.parametrize("color", ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"])
def test_animated_text_keeps_its_stored_colour(color):
    engine, clip = add_text(blank_engine(), text="TEST", size=100.0, scale=1.0,
                            color=color, stroke_width=0.0)
    P.add(clip, "fade", "in", program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.85, "easing": "Linear"})
    image = engine.render_frame(6, 1.0)[1]
    mask = image.max(axis=2) > 20
    assert mask.any(), "the animated text was not drawn"
    mean = image[mask].mean(axis=0)
    # The animation is a fade, so the channel value is scaled by opacity; what must not
    # change is the hue. BGR order: red = channel 2, green = 1, blue = 0.
    if color == "#FF0000":
        assert mean[2] > 60 and mean[2] > 4.0 * max(mean[0], mean[1])
    elif color == "#00FF00":
        assert mean[1] > 60 and mean[1] > 4.0 * max(mean[0], mean[2])
    elif color == "#0000FF":
        assert mean[0] > 60 and mean[0] > 4.0 * max(mean[1], mean[2])
    else:
        assert mean.min() > 60 and float(mean.max() - mean.min()) < 6.0


@pytest.mark.parametrize("category,frame", [("in", 8), ("out", 45), ("loop", 5)])
def test_in_out_and_loop_animations_do_not_change_hue(category, frame):
    engine, clip = add_text(blank_engine(), text="RED", size=100.0, scale=1.0,
                            color="#FF0000")
    P.add(clip, "fade", category, program="fade", duration=20,
          knobs={"unit": "letter", "stagger": 0.4, "easing": "Linear"})
    image = engine.render_frame(frame, 1.0)[1]
    mask = image.max(axis=2) > 20
    assert mask.any(), f"the {category} animation was not visible at frame {frame}"
    mean = image[mask].mean(axis=0)
    assert mean[2] > 60 and mean[2] > 4.0 * max(mean[0], mean[1]), \
        f"{category} turned red into BGR {mean}"


def test_stroke_shadow_and_multiple_fonts_render_without_losing_text():
    for font in ("Segoe UI", "Arial", "DejaVu Sans", "Times New Roman"):
        engine, clip = add_text(blank_engine(), text="Ag", size=60.0, scale=5.0,
                                stroke_width=4.0, shadow_opacity=0.7,
                                shadow_blur=4.0, font=font)
        image = engine.render_frame(0, 1.0)[1]
        assert int((image.max(axis=2) > 32).sum()) > 200, font


def test_lowered_preview_quality_keeps_generated_framing():
    engine, _clip = add_shape(blank_engine(), scale=5.0)
    full = engine.render_frame(0, 1.0)[1]
    preview = engine.render_frame(0, 0.5)[1]
    for image, factor in ((full, 1.0), (preview, 0.5)):
        mask = image.max(axis=2) > 16
        ys, xs = np.nonzero(mask)
        assert xs.size > 100
        cx, cy = (xs.min() + xs.max()) / 2.0, (ys.min() + ys.max()) / 2.0
        assert abs(cx - image.shape[1] / 2.0) < 3.0
        assert abs(cy - image.shape[0] / 2.0) < 3.0
        if factor < 1.0:
            assert abs((xs.max() - xs.min()) - 80.0) < 4.0


def test_shape_digest_notices_geometry_and_colour_changes():
    engine, clip = add_shape(blank_engine())
    before = clip_digest(clip)
    clip.shape["width"] = 40.0
    assert clip_digest(clip) != before
    before = clip_digest(clip)
    clip.shape["fill_color"] = "#00FF00"
    assert clip_digest(clip) != before
    before = clip_digest(clip)
    clip.shape["stroke_enabled"] = True
    clip.shape["stroke_width"] = 3.0
    assert clip_digest(clip) != before
    before = clip_digest(clip)
    clip.transform["rotation"] = 20.0
    assert clip_digest(clip) != before


def test_stop_playback_restores_auto_quality_but_not_a_manual_choice():
    from types import SimpleNamespace as NS
    from meenifx.ui.main_window import MainWindow

    class Timer:
        def start(self, *_args): pass
        def stop(self): pass

    def window(label):
        calls = []
        fake = NS(
            _playing=True, play_timer=Timer(), camera_timer=Timer(),
            _snippet_timer=Timer(), _prefetch_timer=Timer(),
            audio=NS(pause=lambda: None),
            renderer=NS(finish_playback=lambda: None, cancel_interactive=lambda: None),
            bridge=NS(status=NS(emit=lambda *_args: None)),
            _quality_label=label, _auto_scale=0.125, _camera=None,
            _sync_transport=lambda: None,
            _request_preview=lambda: calls.append("preview"),
        )
        return fake, calls

    auto, calls = window("Auto")
    MainWindow.stop_playback(auto)
    assert auto._auto_scale is None and calls == ["preview"]
    manual, calls = window("75")
    MainWindow.stop_playback(manual)
    assert manual._auto_scale == 0.125 and calls == ["preview"]


def test_disk_fingerprint_includes_the_output_resolution():
    engine, _clip = add_shape(blank_engine())
    from meenifx.preview.invalidation import fingerprint
    assert fingerprint(engine.timeline, (640, 360)) != fingerprint(engine.timeline, (1280, 720))


def test_export_and_preview_keep_generated_layers_sharp(tmp_path):
    engine, _clip = add_shape(blank_engine(320, 180), scale=10.0)
    settings = {"output_path": str(tmp_path / "shape.png"), "format": "png",
                "range_start": 0, "range_end": 1, "use_source_size": True,
                "use_source_fps": True}
    written, count, _fps, size = engine.export(settings)
    assert count == 1 and size == (320, 180)
    exported = __import__("cv2").imread(written)
    assert exported is not None
    preview = engine.render_frame(0, 0.5)[1]
    assert preview.shape[:2] == (90, 160)
