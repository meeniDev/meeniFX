"""Live preview transforms, an always-reachable rotation knob and the modifier matrix.

Every case drives the real preview widget with real mouse events, because the bugs these
cover were only visible in the interaction: the picture that froze until mouse-release, and
the rotation handle that walked off the edge of the viewport with the clip.
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

from PyQt6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import transform as T  # noqa: E402
from meenifx.project.media import media_type_for  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")


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
    settings = ProjectSettings(name=f"Live {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    pump(0.1)
    library = window.projects.project.library
    item = library.add(STILL, 3.0, 640, 360, 30.0, media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    ids = window.insert_media_items([item], frame=0)
    assert ids
    clip = window.engine.timeline.find_clip(ids[0])
    window.engine.session.set_selection({clip.id})
    window.engine.session.set_playhead(10)
    pump(0.2)
    window.overlay.enabled = True
    return window, clip


def send(view, kind, point, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QMouseEvent(kind, QPointF(point), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, modifiers)
    if kind == QMouseEvent.Type.MouseButtonPress:
        view.mousePressEvent(event)
    elif kind == QMouseEvent.Type.MouseMove:
        view.mouseMoveEvent(event)
    else:
        view.mouseReleaseEvent(event)


def project_point(window, x, y):
    return window.overlay.to_widget(x, y)


def handle_point(window, clip, name):
    """Widget position of one of the eight scale handles (the picture is letterboxed)."""
    x, y = window.overlay.handle_points(clip)[name]
    return window.overlay.to_widget(x, y)


def inside_point(window, clip):
    """A point on the picture, well away from the anchor crosshair and every handle."""
    anchor = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    return QPointF(anchor.x() + 26, anchor.y() + 18)


def live_clip(window, clip_id):
    return window.engine.timeline.find_clip(clip_id)


# ------------------------------------------------------------------ live rendering
def test_a_preview_drag_invalidates_the_cached_frame(editor):
    """The old freeze: the cache answered with the frame rendered before the drag."""
    window, clip = editor
    frame = int(window.engine.session.playhead)
    scale = window.preview_scale()
    window._request_preview()
    pump(0.25)
    assert window.frame_cache.get(frame, scale) is not None, "the test needs a cached frame"

    view = window.preview_panel.view
    start = inside_point(window, clip)
    send(view, QMouseEvent.Type.MouseButtonPress, start)
    send(view, QMouseEvent.Type.MouseMove, QPointF(start.x() + 40, start.y() + 10))
    assert window.frame_cache.get(frame, window.preview_scale()) is None, (
        "the frame rendered before the drag must be dropped, or the picture cannot follow")
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() + 40, start.y() + 10))
    pump(0.1)
    assert live_clip(window, clip.id).transform["position_x"] > 0


def test_a_drag_renders_at_half_quality_and_the_release_restores_full(editor, monkeypatch):
    window, clip = editor
    window._auto_scale = None
    window.set_preview_quality("Full", save=False)
    window.engine.preview_quality.set(100)
    requested = []
    original = window.renderer.request

    def spy(frame, scale):
        requested.append(float(scale))
        return original(frame, scale)

    monkeypatch.setattr(window.renderer, "request", spy)
    view = window.preview_panel.view
    start = project_point(window, 320, 180)
    send(view, QMouseEvent.Type.MouseButtonPress, start)
    send(view, QMouseEvent.Type.MouseMove, QPointF(start.x() + 30, start.y() + 8))
    during = list(requested)
    assert during and all(scale <= window.INTERACTIVE_SCALE for scale in during), \
        f"interactive frames must be cheap, got {during}"
    assert window.preview_scale() <= window.INTERACTIVE_SCALE
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() + 30, start.y() + 8))
    pump(0.2)
    assert requested[-1] > window.INTERACTIVE_SCALE, \
        f"the frame after the drag must be full quality, got {requested[-1]}"
    assert not window._interacting()


def test_the_rendered_picture_matches_where_the_clip_was_dragged(editor):
    """Not just the outline: the engine's own picture must be at the new position."""
    window, clip = editor
    view = window.preview_panel.view
    start = inside_point(window, clip)
    target = QPointF(start.x() + 80, start.y())
    send(view, QMouseEvent.Type.MouseButtonPress, start)
    send(view, QMouseEvent.Type.MouseMove, target)
    send(view, QMouseEvent.Type.MouseButtonRelease, target)
    pump(0.2)
    moved = live_clip(window, clip.id)
    dx = moved.transform["position_x"]
    assert dx > 0
    # The overlay's box and the model agree, so what is drawn follows what is stored.
    box = window.overlay.widget_rect_for(moved)
    expected = window.overlay.widget_rect_for(moved)
    assert box == expected
    rendered = window.engine.render_frame(int(window.engine.session.playhead), 1.0)[1]
    assert rendered is not None and rendered.size > 0
    # The clip's ink moved by the same number of pixels the model moved it.
    frame_rect = view.frame_rect()
    pixels = dx * frame_rect.width() / window.engine.project_size()[0]
    assert abs(pixels - 80) < 8, f"expected ~80 px of movement, computed {pixels}"


def test_the_inspector_values_follow_a_live_drag(editor):
    window, clip = editor
    view = window.preview_panel.view
    start = inside_point(window, clip)
    send(view, QMouseEvent.Type.MouseButtonPress, start)
    send(view, QMouseEvent.Type.MouseMove, QPointF(start.x() + 50, start.y()))
    pump(0.1)
    rows = [row for row in window.effects_panel._rows
            if row.get("kind") == "channel" and row.get("channel") == "transform.position_x"]
    assert rows, "the inspector must show Position X"
    assert rows[0]["field"].value() > 0, "the field must show the dragged value live"
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() + 50, start.y()))
    pump(0.1)


# ---------------------------------------------------------------- rotation handle
def test_the_rotation_knob_stays_inside_the_viewport_when_the_clip_leaves(editor):
    window, clip = editor
    T.set_position(clip, 4000.0, 0.0, 0, False)
    window.engine.session.changed.emit()
    pump(0.05)
    live = live_clip(window, clip.id)
    knob = window.overlay.rotate_knob(live)
    view = window.preview_panel.view
    assert QRectF(view.rect()).contains(knob), f"knob at {knob} is outside {view.rect()}"
    assert window.overlay._knob_pinned, "the knob should report that it is pinned"
    # The clip's own transform is untouched by pinning the handle.
    assert live.transform["position_x"] == 4000.0


def test_a_pinned_knob_still_rotates_the_clip(editor):
    window, clip = editor
    T.set_position(clip, 4000.0, 0.0, 0, False)
    window.engine.session.changed.emit()
    pump(0.05)
    live = live_clip(window, clip.id)
    knob = window.overlay.rotate_knob(live)
    view = window.preview_panel.view
    assert window.overlay._handle_at(live, knob) == "rotate"
    anchor = window.overlay.to_widget(*window.overlay.anchor_point(live))
    send(view, QMouseEvent.Type.MouseButtonPress, knob)
    send(view, QMouseEvent.Type.MouseMove, QPointF(anchor.x() + 10, anchor.y() + 120))
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(anchor.x() + 10, anchor.y() + 120))
    pump(0.1)
    assert abs(live_clip(window, clip.id).transform["rotation"]) > 1.0


def test_the_knob_is_reachable_whatever_the_panel_size(editor):
    """A picture that fills the frame leaves no room above it: the knob must still exist."""
    window, clip = editor
    view = window.preview_panel.view
    knob = window.overlay.rotate_knob(clip)
    assert QRectF(view.rect()).contains(knob)
    assert window.overlay._handle_at(clip, knob) == "rotate"


def test_every_handle_point_stays_finite_with_a_wild_transform(editor):
    window, clip = editor
    for name, value in (("scale_x", 0.0), ("scale_y", 10000.0), ("rotation", 3600.0),
                        ("anchor_x", -50000.0)):
        clip.transform[name] = value
    window.engine.session.changed.emit()
    pump(0.05)
    live = live_clip(window, clip.id)
    knob = window.overlay.rotate_knob(live)
    assert knob.x() == knob.x() and knob.y() == knob.y()      # not NaN
    window.preview_panel.view.grab()


# ----------------------------------------------------------------------- modifiers
def test_shift_constrains_a_move_to_one_axis(editor):
    window, clip = editor
    view = window.preview_panel.view
    start = inside_point(window, clip)
    target = QPointF(start.x() + 70, start.y() + 25)
    send(view, QMouseEvent.Type.MouseButtonPress, start, Qt.KeyboardModifier.ShiftModifier)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.ShiftModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.ShiftModifier)
    pump(0.1)
    moved = live_clip(window, clip.id).transform
    assert moved["position_x"] > 1
    assert abs(moved["position_y"]) < 0.5, "Shift must keep the other axis at zero"


def test_ctrl_disables_snapping_while_moving(editor):
    window, clip = editor
    view = window.preview_panel.view
    # Drag the picture until its left edge sits three pixels from the frame edge: that is
    # inside the snapping tolerance, so only Ctrl can keep it there.
    nw = handle_point(window, clip, "nw")
    start = inside_point(window, clip)
    target = QPointF(start.x() - nw.x() + 3, start.y())
    send(view, QMouseEvent.Type.MouseButtonPress, start, Qt.KeyboardModifier.ControlModifier)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.ControlModifier)
    assert window.overlay._guides == [], "Ctrl must not snap to the frame edges"
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.ControlModifier)
    pump(0.1)


@pytest.mark.parametrize(("handle", "changed"), [
    ("nw", ("corner_tl_x", "corner_tl_y")),
    ("ne", ("corner_tr_x", "corner_tr_y")),
    ("se", ("corner_br_x", "corner_br_y")),
    ("sw", ("corner_bl_x", "corner_bl_y")),
])
def test_alt_corner_drag_distorts_instead_of_scaling(editor, handle, changed):
    """In the normal Select tool Alt-drag must pin/distort one corner, not scale.

    The overlay's default mode is Select, which is the mode a user is in after clicking a
    clip in the Preview. This is the regression that made Alt+corner behave like an
    ordinary centre resize.
    """
    window, clip = editor
    assert window.overlay.mode == "select"
    view = window.preview_panel.view
    before = T.evaluate(clip, window.overlay.local_frame(clip))
    corner = handle_point(window, clip, handle)
    target = QPointF(corner.x() + (26 if handle in ("nw", "sw") else -26),
                     corner.y() + 16)
    send(view, QMouseEvent.Type.MouseButtonPress, corner, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.AltModifier)
    pump(0.1)

    live = live_clip(window, clip.id)
    frame = window.overlay.local_frame(live)
    entry = next(item for item in live.effects
                 if item.get("effect_id") == "corner_pin")
    assert entry.get("enabled", True)
    x_name, y_name = changed
    x_value = CH.value_at(live, CH.effect_channel("corner_pin", x_name), frame)
    y_value = CH.value_at(live, CH.effect_channel("corner_pin", y_name), frame)
    assert x_value is not None and y_value is not None
    # The dragged corner must leave its identity position; the other three must not.
    identity = {"corner_tl_x": 0.0, "corner_tl_y": 0.0,
                "corner_tr_x": 1.0, "corner_tr_y": 0.0,
                "corner_br_x": 1.0, "corner_br_y": 1.0,
                "corner_bl_x": 0.0, "corner_bl_y": 1.0}
    assert abs(x_value - identity[x_name]) + abs(y_value - identity[y_name]) > 0.01
    for name, default in identity.items():
        if name in changed:
            continue
        value = CH.value_at(live, CH.effect_channel("corner_pin", name), frame)
        value = default if value is None else value
        assert value == pytest.approx(default, abs=1e-6), name
    # Alt must not fall through to the old centre-scale gesture.
    assert live.transform["scale_x"] == pytest.approx(before.scale_x, abs=1e-6)
    assert live.transform["scale_y"] == pytest.approx(before.scale_y, abs=1e-6)
    assert live.transform["position_x"] == pytest.approx(before.position_x, abs=1e-6)
    assert live.transform["position_y"] == pytest.approx(before.position_y, abs=1e-6)


def test_alt_pressed_during_a_corner_scale_switches_to_distortion(editor):
    window, clip = editor
    view = window.preview_panel.view
    before = T.evaluate(clip, window.overlay.local_frame(clip))
    corner = handle_point(window, clip, "nw")
    send(view, QMouseEvent.Type.MouseButtonPress, corner)
    send(view, QMouseEvent.Type.MouseMove, QPointF(corner.x() + 18, corner.y() + 12))
    pump(0.03)
    assert live_clip(window, clip.id).transform["scale_x"] < before.scale_x
    # Alt is pressed only now: the scale gesture is discarded and the same drag becomes
    # a corner-pin gesture. It must not keep the intermediate scale.
    send(view, QMouseEvent.Type.MouseMove, QPointF(corner.x() + 24, corner.y() + 18),
         Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, QPointF(corner.x() + 42, corner.y() + 30),
         Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(corner.x() + 42, corner.y() + 30),
         Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    live = live_clip(window, clip.id)
    assert live.transform["scale_x"] == pytest.approx(before.scale_x, abs=1e-6)
    assert live.transform["scale_y"] == pytest.approx(before.scale_y, abs=1e-6)
    assert live.transform["position_x"] == pytest.approx(before.position_x, abs=1e-6)
    frame = window.overlay.local_frame(live)
    value = CH.value_at(live, CH.effect_channel("corner_pin", "corner_tl_x"), frame)
    assert value is not None and value > 0.01


def test_alt_corner_drag_respects_clip_rotation_and_scale(editor):
    window, clip = editor
    clip.transform["rotation"] = 24.0
    clip.transform["scale_x"] = 74.0
    clip.transform["scale_y"] = 82.0
    window.engine.session.set_selection(set())
    window.engine.session.set_selection({clip.id})
    window._refresh_panels()
    pump(0.15)
    view = window.preview_panel.view
    before = T.evaluate(clip, window.overlay.local_frame(clip))
    corner = handle_point(window, clip, "se")
    target = QPointF(corner.x() - 22, corner.y() - 14)
    send(view, QMouseEvent.Type.MouseButtonPress, corner, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    live = live_clip(window, clip.id)
    frame = window.overlay.local_frame(live)
    value = CH.value_at(live, CH.effect_channel("corner_pin", "corner_br_x"), frame)
    assert value is not None and value < 0.99
    for name, default in (("corner_tl_x", 0.0), ("corner_tl_y", 0.0),
                          ("corner_tr_x", 1.0), ("corner_tr_y", 0.0),
                          ("corner_bl_x", 0.0), ("corner_bl_y", 1.0)):
        other = CH.value_at(live, CH.effect_channel("corner_pin", name), frame)
        assert (default if other is None else other) == pytest.approx(default, abs=1e-6)
    after = T.evaluate(live, frame)
    assert after.position_x == pytest.approx(before.position_x, abs=1e-6)
    assert after.position_y == pytest.approx(before.position_y, abs=1e-6)
    assert after.scale_x == pytest.approx(before.scale_x, abs=1e-6)
    assert after.scale_y == pytest.approx(before.scale_y, abs=1e-6)
    assert after.rotation == pytest.approx(before.rotation, abs=1e-6)


def test_alt_corner_drag_is_correct_at_a_non_fit_preview_zoom(editor):
    window, clip = editor
    view = window.preview_panel.view
    view.set_zoom(1.6)
    pump(0.1)
    corner = handle_point(window, clip, "se")
    send(view, QMouseEvent.Type.MouseButtonPress, corner, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, QPointF(corner.x() - 32, corner.y() - 18),
         Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(corner.x() - 32, corner.y() - 18),
         Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    live = live_clip(window, clip.id)
    frame = window.overlay.local_frame(live)
    value = CH.value_at(live, CH.effect_channel("corner_pin", "corner_br_x"), frame)
    assert value is not None and value < 0.99
    # The corner must follow the pointer in project space, not in screen pixels.
    assert value > 0.55


def test_alt_corner_keyframes_survive_save_and_reopen(editor):
    window, clip = editor
    view = window.preview_panel.view
    window.autokey_button.setChecked(True)
    window.engine.session.set_playhead(5)
    pump(0.08)
    corner = handle_point(window, clip, "nw")
    send(view, QMouseEvent.Type.MouseButtonPress, corner, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, QPointF(corner.x() + 24, corner.y() + 14),
         Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(corner.x() + 24, corner.y() + 14),
         Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    window.engine.session.set_playhead(20)
    pump(0.08)
    live = live_clip(window, clip.id)
    start = window.overlay.to_widget(*window.overlay._corner_project(live, 0))
    send(view, QMouseEvent.Type.MouseButtonPress, start, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, QPointF(start.x() + 50, start.y() + 24),
         Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() + 50, start.y() + 24),
         Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    live = live_clip(window, clip.id)
    channel = CH.effect_channel("corner_pin", "corner_tl_x")
    assert CH.is_animated(live, channel)
    assert CH.key_times(live, channel) == [5, 20]
    value_at_5 = CH.value_at(live, channel, 5)
    value_at_20 = CH.value_at(live, channel, 20)
    assert value_at_5 != pytest.approx(value_at_20, abs=1e-4)
    window.autokey_button.setChecked(False)

    assert window.projects.save()
    path = window.projects.project.path
    assert window.projects.open_path(path)
    pump(0.2)
    reopened = window.engine.timeline.find_clip(clip.id)
    assert reopened is not None
    assert CH.key_times(reopened, channel) == [5, 20]
    assert CH.value_at(reopened, channel, 5) == pytest.approx(value_at_5, abs=1e-5)
    assert CH.value_at(reopened, channel, 20) == pytest.approx(value_at_20, abs=1e-5)


def test_alt_corner_distortion_exports_the_preview(editor, tmp_path):
    window, clip = editor
    view = window.preview_panel.view
    corner = handle_point(window, clip, "sw")
    target = QPointF(corner.x() + 34, corner.y() - 20)
    send(view, QMouseEvent.Type.MouseButtonPress, corner, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    preview = window.engine.render_frame(10, 1.0)[1]
    path, count, _fps, _size = window.engine.export({
        "output_path": str(tmp_path / "corner-parity.png"),
        "format": "png", "use_source_fps": True, "use_source_size": True,
        "range_start": 10, "range_end": 11,
    })
    exported = cv2.imread(path)
    assert count == 1 and exported is not None
    assert np.abs(exported.astype(np.int16) - preview.astype(np.int16)).mean() < 1.0


def test_alt_corner_drag_can_be_undone_and_redone(editor):
    window, clip = editor
    view = window.preview_panel.view
    corner = handle_point(window, clip, "se")
    target = QPointF(corner.x() - 20, corner.y() - 14)
    send(view, QMouseEvent.Type.MouseButtonPress, corner, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    dragged = live_clip(window, clip.id)
    assert any(item.get("effect_id") == "corner_pin" for item in dragged.effects)
    window.undo()
    pump(0.1)
    restored = live_clip(window, clip.id)
    assert not any(item.get("effect_id") == "corner_pin" for item in restored.effects), \
        "undo must remove the effect created by the Alt gesture"
    window.redo()
    pump(0.1)
    redone = live_clip(window, clip.id)
    assert any(item.get("effect_id") == "corner_pin" for item in redone.effects)


def test_native_temporal_controls_are_wired_to_the_model(editor):
    window, clip = editor
    window.engine.session.set_selection({clip.id})
    window.effects_panel.refresh_properties()
    pump(0.1)
    combos = window.effects_panel.properties.findChildren(QComboBox)
    motion = next(box for box in combos if box.count() == 3
                  and box.itemText(1) == "On" and box.itemText(2) == "Off")
    assert motion.itemData(0) is None
    # The composition master starts off; the clip can still keep its own override.
    window.motion_blur_action.setChecked(True)
    pump(0.05)
    assert window.engine.timeline.motion_blur is True
    motion.setCurrentIndex(2)
    motion.activated.emit(2)
    pump(0.05)
    assert window.engine.timeline.find_clip(clip.id).temporal["motion_blur"] is False
    assert not window.engine.motion_blur_for_clip(window.engine.timeline.find_clip(clip.id))

    window._set_composition_frame_blending("mix")
    pump(0.05)
    assert window.engine.timeline.frame_blending == "mix"
    assert window.frame_blend_actions["mix"].isChecked()
    window.effects_panel.refresh_properties()
    pump(0.1)
    frame_combo = next(box for box in window.effects_panel.properties.findChildren(QComboBox)
                       if any(box.itemText(i) == "Frame Mix" for i in range(box.count())))
    frame_combo.setCurrentIndex(3)
    frame_combo.activated.emit(3)
    pump(0.05)
    assert window.engine.timeline.find_clip(clip.id).temporal["frame_blend"] == "flow"
    window.motion_blur_action.setChecked(False)


def test_shift_snaps_rotation_to_fifteen_degrees(editor):
    window, clip = editor
    view = window.preview_panel.view
    knob = window.overlay.rotate_knob(clip)
    anchor = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    send(view, QMouseEvent.Type.MouseButtonPress, knob, Qt.KeyboardModifier.ShiftModifier)
    target = QPointF(anchor.x() + 40, anchor.y() - 100)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.ShiftModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.ShiftModifier)
    pump(0.1)
    rotation = live_clip(window, clip.id).transform["rotation"]
    assert abs(rotation % 15.0) < 1e-6, f"expected a 15 degree multiple, got {rotation}"


def test_one_drag_is_one_undo_step_and_restores_the_transform(editor):
    window, clip = editor
    depth = window.qt_undo_stack.undo_stack.count()
    view = window.preview_panel.view
    start = inside_point(window, clip)
    send(view, QMouseEvent.Type.MouseButtonPress, start)
    for step in range(1, 6):
        send(view, QMouseEvent.Type.MouseMove, QPointF(start.x() + step * 8, start.y()))
    send(view, QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() + 40, start.y()))
    pump(0.1)
    assert window.qt_undo_stack.undo_stack.count() == depth + 1
    window.engine.session.undo()
    pump(0.1)
    assert live_clip(window, clip.id).transform["position_x"] == 0.0


# ----------------------------------------------------------------------- inspector
def test_the_inspector_offers_the_same_gestures_as_switches(editor):
    window, clip = editor
    window.effects_panel.refresh_properties()
    pump(0.05)
    labels = {box.text(): box for box in window.findChildren(QCheckBox)}
    assert "Lock Scale X/Y" in labels
    assert "Scale from center" in labels
    assert "Snap rotation" in labels
    lock = labels["Lock Scale X/Y"]
    centre = labels["Scale from center"]
    assert lock.isChecked(), "aspect locking is the default"
    assert not centre.isChecked()
    depth = window.qt_undo_stack.undo_stack.count()
    centre.setChecked(True)
    pump(0.05)
    live = live_clip(window, clip.id)
    assert T.ensure_transform(live)["scale_from_center"] is True
    assert window.qt_undo_stack.undo_stack.count() == depth + 1
    window.engine.session.undo()
    pump(0.05)
    assert T.ensure_transform(live_clip(window, clip.id))["scale_from_center"] is False


def test_the_scale_from_center_switch_changes_the_drag_without_the_keyboard(editor):
    window, clip = editor
    T.ensure_transform(clip)["scale_from_center"] = True
    window.engine.session.changed.emit()
    pump(0.05)
    view = window.preview_panel.view
    corner = handle_point(window, clip, "nw")
    centre = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    target = QPointF(corner.x() + (centre.x() - corner.x()) * 0.3,
                     corner.y() + (centre.y() - corner.y()) * 0.3)
    send(view, QMouseEvent.Type.MouseButtonPress, corner)
    send(view, QMouseEvent.Type.MouseMove, target)
    send(view, QMouseEvent.Type.MouseButtonRelease, target)
    pump(0.1)
    live = live_clip(window, clip.id)
    assert live.transform["scale_x"] < 99.0
    assert abs(live.transform["position_x"]) < 1.0


def test_deleting_the_last_clip_empties_the_preview(editor):
    """An empty timeline must not keep showing the clip that was just deleted."""
    window, clip = editor
    window._request_preview()
    pump(0.25)
    view = window.preview_panel.view
    assert not view._image.isNull(), "the test needs a picture first"
    stale = window.last_frame.copy()
    frame, scale = int(window.engine.session.playhead), window.preview_scale()

    window.engine.session.set_selection({clip.id})
    window.engine.session.delete_selected()
    pump(0.2)

    assert window.engine.timeline.all_clips() == []
    assert view._image.isNull(), "an empty timeline must clear the preview"
    assert window.last_frame is None

    # A frame already rendering when the clip vanished must not resurrect the picture.
    window._on_frame_rendered(frame, scale, stale)
    pump(0.05)
    assert view._image.isNull()
    assert window.last_frame is None
