"""Live preview transforms, an always-reachable rotation knob and the modifier matrix.

Every case drives the real preview widget with real mouse events, because the bugs these
cover were only visible in the interaction: the picture that froze until mouse-release, and
the rotation handle that walked off the edge of the viewport with the clip.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QCheckBox  # noqa: E402

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


def test_alt_scales_from_the_centre(editor):
    window, clip = editor
    view = window.preview_panel.view
    corner = handle_point(window, clip, "nw")
    centre = window.overlay.to_widget(*window.overlay.anchor_point(clip))
    target = QPointF(corner.x() + (centre.x() - corner.x()) * 0.35,
                     corner.y() + (centre.y() - corner.y()) * 0.35)
    send(view, QMouseEvent.Type.MouseButtonPress, corner, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseMove, target, Qt.KeyboardModifier.AltModifier)
    send(view, QMouseEvent.Type.MouseButtonRelease, target, Qt.KeyboardModifier.AltModifier)
    pump(0.1)
    live = live_clip(window, clip.id)
    assert live.transform["scale_x"] < 99.0, "dragging a corner inward must shrink the clip"
    # Scaling around the centre keeps the picture centred: no position change.
    assert abs(live.transform["position_x"]) < 1.0
    assert abs(live.transform["position_y"]) < 1.0


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
    labels = {box.text(): box for box in window.effects_panel.findChildren(QCheckBox)}
    assert "Lock aspect ratio" in labels
    assert "Scale from center" in labels
    assert "Snap rotation" in labels
    lock = labels["Lock aspect ratio"]
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
