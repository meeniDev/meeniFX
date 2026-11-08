"""Composition navigation, Project-panel assets and the Frame Rate control, in a window.

These run the real MainWindow: the breadcrumb bar, the asset list and the inspector are
built by the application's own code, so a regression in the wiring (not just in the
model) fails here.
"""

import time

import numpy as np
import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication

from meenifx.constants import COMPOSITION_MIME
from meenifx.media.cache import MediaEntry
from meenifx.model.keyframes import AnimatedValue
from meenifx.project.settings import ProjectSettings
from meenifx.ui import layout as dock_layout
from meenifx.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(app, seconds=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    monkeypatch.setattr(dock_layout, "app_settings",
                        lambda: QSettings(str(tmp_path / "layout.ini"),
                                          QSettings.Format.IniFormat))
    editor = MainWindow(qapp)
    editor.resize(1500, 950)
    editor.show()
    pump(qapp, 0.25)
    editor.projects.autosave_timer.stop()
    # The Project panel and the disk cache need a real project folder.
    assert editor.projects.create_project(
        ProjectSettings(name="CompUI", width=640, height=360, fps=30.0),
        str(tmp_path / "project"))
    pump(qapp, 0.2)
    # The session, the canvas and the engine must agree on the timeline on screen.
    assert editor.engine.session.timeline is editor.engine.timeline
    assert editor.timeline_panel.canvas.timeline is editor.engine.timeline
    editor.closeEvent = lambda event: event.accept()
    yield editor
    editor.close()
    pump(qapp, 0.05)


def add_clip(window, name="Shot", path="synthetic-effect-test.png"):
    picture = np.zeros((90, 160, 3), np.uint8)
    picture[:, :] = (40, 90, 160)
    entry = MediaEntry("image", 1, 30)
    entry.frames = [picture]
    window.engine.media._entries[path] = entry
    timeline = window.engine.timeline
    lanes = timeline.tracks_of_type("video")
    # A window with the demo disabled starts empty: create the lane we need.
    track = lanes[0] if lanes else timeline.add_track("video")
    return timeline.new_clip(name, path, track, 0, 60, source_out=60, source_fps=30)


def precompose(window, name="Breadcrumb Comp"):
    clip = add_clip(window)
    window.engine.session.set_selection({clip.id})
    problem = window.engine.session.precompose_problem([clip.id])
    assert not problem, problem
    nested_id = window.engine.session.precompose(name, [clip.id])
    assert nested_id is not None, window.engine.session.precompose_problem([clip.id])
    # Assert against the editing context, not `engine.timeline`: a nested preview
    # render swaps that engine field to the composition for the duration of the render.
    session_timeline = window.engine.session.timeline
    nested = session_timeline.find_clip(nested_id)
    assert nested is not None, "the nested clip is not on the editing timeline"
    return window.engine.compositions.get(nested.composition_id), nested_id


def test_breadcrumb_opens_a_composition_and_returns_to_the_parent(window):
    comp, nested_id = precompose(window)
    bar = window.composition_bar
    assert bar is not None
    assert "Main Timeline" in [action.text() for action in bar.actions()]
    assert "Editing: Main Timeline" in window.composition_context_label.text()

    assert window.open_composition(comp.id)
    assert window.editing_composition is comp
    assert window.engine.session.timeline is comp.timeline
    assert window.timeline_panel.canvas.timeline is comp.timeline, \
        "the timeline must draw the composition that is open"
    assert "Editing: " in window.composition_context_label.text()
    assert comp.name in window.composition_context_label.text()
    assert comp.name in window.windowTitle()
    labels = [action.text() for action in bar.actions()]
    assert "Back to Parent" in labels and "Settings..." in labels
    assert comp.name in labels

    # Double-clicking the nested clip resolves to the same composition.
    assert window.back_to_parent()
    assert window.engine.session.timeline is window.engine.root_timeline
    assert window.timeline_panel.canvas.timeline is window.engine.root_timeline
    assert window.open_composition_clip(nested_id)
    assert window.editing_composition is comp
    assert window.open_composition(comp.id) is False, "a composition cannot contain itself"


def test_project_panel_shows_compositions_as_their_own_asset(window):
    comp, _nested_id = precompose(window)
    panel = window.project_panel
    panel.rebuild(force=True)
    rows = [panel.list.item(index) for index in range(panel.list.count())]
    compositions = [item for item in rows
                    if item.data(Qt.ItemDataRole.UserRole)[0] == "comp"]
    assert len(compositions) == 1
    item = compositions[0]
    assert item.data(Qt.ItemDataRole.UserRole)[1] == comp.id
    assert not item.icon().isNull()
    assert "Composition" in item.toolTip()

    mime = panel.list.mimeData([item])
    assert mime.hasFormat(COMPOSITION_MIME)
    assert bytes(mime.data(COMPOSITION_MIME)).decode() == comp.id

    panel._double_clicked(item)
    assert window.editing_composition is comp
    assert window.back_to_parent()

    timeline = window.engine.session.timeline
    before = len(timeline.all_clips())
    assert window._composition_dropped([comp.id], 30, None) == 1
    assert len(timeline.all_clips()) == before + 1
    placed = [clip for clip in timeline.all_clips()
              if clip.is_composition and clip.timeline_start == 30]
    assert placed


def test_frame_rate_control_is_labeled_precise_and_animatable(window):
    clip = add_clip(window, name="Rate shot")
    clip.effects = [{"effect_id": "posterize_time", "enabled": True,
                     "params": {"fps": 23.976}}]
    window.engine.session.set_selection({clip.id})
    window.engine.session.set_playhead(0)
    window.effects_panel.refresh_properties()
    pump(window.qt_app, 0.2)

    def rate_row():
        return next(row for row in window.effects_panel._rows
                    if row.get("name") == "fps" and row.get("clip_id") == clip.id)

    row = rate_row()
    field = row["field"]
    assert field._label_full == "Frame Rate"
    assert field.label.text() == "Frame Rate", "the label must not be elided"
    assert field.unit.strip() == "fps"
    assert field.decimals == 3, "23.976 must not be shown as 23.98"
    assert field.value() == pytest.approx(23.976)
    # The displayed value survives the control's own step snapping.
    assert field.value_widget._quantize(23.976) == pytest.approx(23.976)
    assert field.value_widget._quantize(29.97) == pytest.approx(29.97)

    field.set_value(59.94)
    assert field.value() == pytest.approx(59.94)
    field.reset_to_default()
    assert field.value() == pytest.approx(12.0)

    # The stopwatch makes Frame Rate keyframable through the real inspector path.
    rate_row()["stopwatch"].setChecked(True)
    pump(window.qt_app, 0.2)
    stored = rate_row()["entry"]["params"]["fps"]
    assert isinstance(stored, AnimatedValue)
