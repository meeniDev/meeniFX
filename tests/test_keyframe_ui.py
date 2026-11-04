"""Keyframes in the properties panel and on the timeline."""

import time

import pytest

from conftest import build_engine, synthetic_frame


def pump(qapp, seconds=0.35):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


@pytest.fixture()
def editor(qapp, tmp_path, monkeypatch):
    """A MainWindow with one synthetic clip selected and a Mosaic effect on it."""
    from meenifx.media.cache import MediaEntry
    from meenifx.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    window = MainWindow(qapp)
    window.resize(1500, 950)
    window.show()
    pump(qapp, 0.2)

    picture = synthetic_frame(640, 360)
    path = str(tmp_path / "synthetic.png")
    entry = MediaEntry("video", 90, 30)
    entry.frames = [picture.copy() for _ in range(90)]
    window.engine.media._entries[path] = entry
    window.engine.source_width, window.engine.source_height = 640, 360
    timeline = window.engine.timeline
    timeline.tracks.clear()
    track = timeline.add_track("video")
    clip = timeline.new_clip("test", path, track, 0, 60, source_out=90, source_fps=30)
    window.engine.session.set_selection({clip.id})
    panel = window.effects_panel
    panel.add_effect("mosaic")
    pump(qapp, 0.3)
    window._test_clip_id = clip.id
    yield window
    window.close()


def _clip(window):
    return window.engine.timeline.find_clip(window._test_clip_id)


def _param_row(window):
    return next(row for row in window.effects_panel._rows
                if row["kind"] == "param" and row["name"] == "scale")


def test_stopwatch_animates_and_keyframe_cluster_appears(editor, qapp):
    panel = editor.effects_panel
    row = _param_row(editor)
    assert row["cluster"].isHidden(), "static parameters must not show the keyframe cluster"
    assert not row["stopwatch"].isChecked()

    row["stopwatch"].setChecked(True)
    pump(qapp, 0.2)
    row = _param_row(editor)
    assert row["stopwatch"].isChecked(), "the stopwatch stays on for an animated parameter"
    assert not row["cluster"].isHidden(), "animated parameters show previous/diamond/next"
    stored = _clip(editor).effects[0]["params"]["scale"]
    from meenifx.model.keyframes import AnimatedValue
    assert isinstance(stored, AnimatedValue)
    assert stored.key_at(0) is not None, "turning the stopwatch on keys the current value"
    assert row["cluster"]._diamond.property("on") is True

    editor.engine.session.set_playhead(10)
    pump(qapp, 0.15)
    assert _param_row(editor)["cluster"]._diamond.property("on") is False, "hollow between keyframes"
    editor.engine.session.set_playhead(0)
    pump(qapp, 0.15)
    assert _param_row(editor)["cluster"]._diamond.property("on") is True, "filled exactly on a keyframe"


def test_slider_follows_playhead_and_writes_keyframes(editor, qapp):
    panel = editor.effects_panel
    _param_row(editor)["stopwatch"].setChecked(True)
    pump(qapp, 0.2)

    editor.engine.session.set_playhead(20)
    pump(qapp, 0.15)
    row = _param_row(editor)
    row["field"].box.setText("80")
    row["field"].box.confirmed.emit()
    pump(qapp, 0.3)
    from meenifx.model.keyframes import AnimatedValue
    stored = _clip(editor).effects[0]["params"]["scale"]
    assert isinstance(stored, AnimatedValue)
    assert stored.key_at(20) is not None and stored.key_at(20).value == 80

    editor.engine.session.set_playhead(10)
    pump(qapp, 0.15)
    assert _param_row(editor)["field"].value() == pytest.approx(60, abs=1), "slider follows the playhead"
    editor.engine.session.set_playhead(20)
    pump(qapp, 0.15)
    assert _param_row(editor)["field"].value() == pytest.approx(80, abs=1)


def test_previous_and_next_jump_between_keyframes(editor, qapp):
    panel = editor.effects_panel
    _param_row(editor)["stopwatch"].setChecked(True)
    pump(qapp, 0.2)
    editor.engine.session.set_playhead(30)
    pump(qapp, 0.15)
    row = _param_row(editor)
    row["field"].box.setText("70")
    row["field"].box.confirmed.emit()
    pump(qapp, 0.25)

    cluster = _param_row(editor)["cluster"]
    cluster.findChildren(type(cluster._diamond))[0].click()   # "â€¹"
    pump(qapp, 0.1)
    assert editor.engine.session.playhead == 0
    cluster = _param_row(editor)["cluster"]
    cluster.findChildren(type(cluster._diamond))[2].click()   # "â€º"
    pump(qapp, 0.1)
    assert editor.engine.session.playhead == 30


def test_timeline_diamonds_expand_drag_and_delete(editor, qapp):
    from PyQt6.QtCore import Qt
    canvas = editor.timeline_panel.canvas
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    _param_row(editor)["stopwatch"].setChecked(True)
    pump(qapp, 0.2)

    clip = _clip(editor)
    rows = canvas.animation_rows(clip)
    assert len(rows) == 1 and rows[0]["value"].keyframes

    canvas._toggle_keyframe_rows(clip)
    assert clip.id in canvas._expanded_clips
    _full, entries = canvas._keyframe_row_geometry(clip)
    assert entries, "the expanded clip draws an animation row"
    row, mid = entries[0]
    x = canvas.x_at(clip.timeline_start + row["value"].keyframes[0].time)
    hit_clip, hit_row, hit_time = canvas._keyframe_hit(x, mid)
    assert hit_clip is clip and hit_time == 0

    before = editor.qt_undo_stack.undo_stack.count()
    canvas._begin_keyframe_drag(clip, hit_row, hit_time, Qt.KeyboardModifier.NoModifier)
    canvas._keyframe_move(canvas.x_at(clip.timeline_start + 12))
    canvas._keyframe_release()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"].key_at(12) is not None
    assert editor.qt_undo_stack.undo_stack.count() == before + 1, "a keyframe drag is one undo step"

    assert canvas._delete_selected_keyframes()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"].key_at(12) is None
    editor.engine.session.undo()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"].key_at(12) is not None


def test_dragging_a_keyframe_snaps_to_the_playhead(editor, qapp):
    from PyQt6.QtCore import Qt
    canvas = editor.timeline_panel.canvas
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    _param_row(editor)["stopwatch"].setChecked(True)
    pump(qapp, 0.25)
    editor.engine.session.set_playhead(40)
    pump(qapp, 0.15)

    clip = _clip(editor)
    row = canvas.animation_rows(clip)[0]
    key_time = row["value"].keyframes[0].time
    canvas._begin_keyframe_drag(clip, row, key_time, Qt.KeyboardModifier.NoModifier)
    # Release 3 px away from the playhead: inside the snap distance, so it lands on it.
    canvas._keyframe_move(canvas.x_at(clip.timeline_start + 40) + 3)
    canvas._keyframe_release()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"].key_at(40) is not None


def test_a_slider_drag_is_one_undo_step(editor, qapp):
    row = _param_row(editor)
    before = editor.qt_undo_stack.undo_stack.count()
    field = row["field"]
    field.slider.sliderPressed.emit()
    for ticks in (120, 250, 400, 520):
        field.slider.setValue(ticks)
        pump(qapp, 0.05)
    field.slider.sliderReleased.emit()
    pump(qapp, 0.3)
    assert editor.qt_undo_stack.undo_stack.count() == before + 1
    assert _clip(editor).effects[0]["params"]["scale"] == row["field"].value()


def test_paused_parameter_edit_rerenders_the_preview(editor, qapp):
    import numpy as np
    pump(qapp, 0.6)
    base = editor.last_frame.copy()
    field = _param_row(editor)["field"]
    field.box.setText("90")
    field.box.confirmed.emit()
    pump(qapp, 0.8)
    changed = editor.last_frame
    assert float(np.abs(base.astype(int) - changed.astype(int)).mean()) > 1.0

