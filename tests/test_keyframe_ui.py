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
    from PyQt6.QtCore import QSettings

    from meenifx.media.cache import MediaEntry
    from meenifx.ui import layout as dock_layout
    from meenifx.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_load_demo_project", lambda self: None)
    # A private settings file: the window must not restore (or overwrite) the user's own
    # dock layout, and every test starts from the default arrangement.
    monkeypatch.setattr(dock_layout, "app_settings",
                        lambda: QSettings(str(tmp_path / "layout.ini"),
                                          QSettings.Format.IniFormat))
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


def test_field_follows_playhead_and_writes_keyframes(editor, qapp):
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


def test_timeline_shows_a_compact_badge_and_opens_the_keyframe_editor(editor, qapp):
    from PyQt6.QtCore import Qt

    canvas = editor.timeline_panel.canvas
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    _param_row(editor)["stopwatch"].setChecked(True)
    pump(qapp, 0.2)

    clip = _clip(editor)
    rows = canvas.animation_rows(clip)
    assert len(rows) == 1 and rows[0]["value"].keyframes
    summary = canvas._keyframe_summary_rect(clip)
    assert summary.width() > 20, "an animated clip has one compact summary badge"
    assert canvas._keyframe_toggle_hit(summary.center().x(), summary.center().y()) is clip
    assert canvas._keyframe_hit(summary.center().x(), summary.center().y()) == (None, None, None)

    canvas._toggle_keyframe_rows(clip)
    pump(qapp, 0.25)
    assert editor.docks["keyframe"].isVisible()
    assert not canvas._expanded_clips, "the timeline must not grow dense keyframe rows"
    assert canvas._visible_keyframe_rows(clip, summary) == []

    panel = editor.keyframe_editor
    channel = rows[0]["channel"]
    row = next(item for item in panel.rows if item["id"] == channel)
    panel.fit()
    panel._set_active_channel(channel)
    panel.session.set_key_selection({(clip.id, channel, 0)})
    before = editor.qt_undo_stack.undo_stack.count()
    panel.begin_key_drag(row, 0, panel.x_of(0), Qt.KeyboardModifier.NoModifier)
    panel.drag_to(panel.x_of(12))
    panel.end_key_drag()
    pump(qapp, 0.2)
    stored = _clip(editor).effects[0]["params"]["scale"]
    assert stored.key_at(12) is not None
    assert stored.key_at(0) is None, "dragging moves a keyframe instead of copying it"
    assert editor.qt_undo_stack.undo_stack.count() == before + 1, "a keyframe drag is one undo step"

    assert panel.delete_selected()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"].key_at(12) is None
    editor.engine.session.undo()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"].key_at(12) is not None


def test_dragging_a_keyframe_snaps_to_the_playhead(editor, qapp):
    from PyQt6.QtCore import Qt

    _param_row(editor)["stopwatch"].setChecked(True)
    pump(qapp, 0.25)
    editor.engine.session.set_playhead(40)
    pump(qapp, 0.15)

    from meenifx.model import channels as CH

    clip = _clip(editor)
    editor.timeline_panel.canvas._toggle_keyframe_rows(clip)
    panel = editor.keyframe_editor
    channel = CH.effect_channel(clip.effects[0]["effect_id"], "scale")
    row = next(item for item in panel.rows if item["id"] == channel)
    panel.fit()
    panel.begin_key_drag(row, 0, panel.x_of(0), Qt.KeyboardModifier.NoModifier)
    # Release 3 px away from the playhead: inside the snap distance, so it lands on it.
    panel.drag_to(panel.x_of(40) + 3)
    panel.end_key_drag()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"].key_at(40) is not None


def scrub_value(field, dx):
    """Drag the inspector value sideways with real mouse events."""
    from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication

    widget = field.value_widget
    start = widget.width() // 2

    def send(kind, x, buttons=Qt.MouseButton.LeftButton):
        QApplication.sendEvent(widget, QMouseEvent(
            kind, QPointF(x, widget.height() // 2),
            QPointF(widget.mapToGlobal(QPoint(int(x), widget.height() // 2))),
            Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier))

    send(QEvent.Type.MouseButtonPress, start)
    for step in range(1, 5):
        send(QEvent.Type.MouseMove, start + round(dx * step / 4))
    send(QEvent.Type.MouseButtonRelease, start + dx, Qt.MouseButton.NoButton)


def test_a_scrub_drag_is_one_undo_step(editor, qapp):
    row = _param_row(editor)
    before = editor.qt_undo_stack.undo_stack.count()
    field = row["field"]
    scrub_value(field, 25)
    pump(qapp, 0.3)
    assert editor.qt_undo_stack.undo_stack.count() == before + 1, "a whole drag is one undo step"
    assert _clip(editor).effects[0]["params"]["scale"] == row["field"].value()
    editor.engine.session.undo()
    pump(qapp, 0.2)
    assert _clip(editor).effects[0]["params"]["scale"] != row["field"].value(), "and undo takes it back"


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


def test_disabling_an_effect_rerenders_the_preview_immediately(editor, qapp):
    import numpy as np
    pump(qapp, 0.6)
    base = editor.last_frame.copy()
    clip = _clip(editor)
    entry = clip.effects[0]
    editor.effects_panel._set_enabled(clip, entry, False)
    pump(qapp, 0.8)
    changed = editor.last_frame
    assert not entry.get("enabled", True)
    assert float(np.abs(base.astype(int) - changed.astype(int)).mean()) > 1.0, \
        "toggling an effect must reach the preview without another edit"


def test_timeline_keyframes_menu_only_opens_the_editor_when_triggered(qapp, monkeypatch):
    """Building the menu must not run its items.

    The item was created with ``self.keyframe_editor_requested.emit([])``, so the emission
    happened while the menu was being assembled: Qt rejected the None slot with a
    TypeError and every right-click on an empty lane tried to open the editor.
    """
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QMenu

    from meenifx.ui.timeline_widget import TimelineCanvas

    canvas = TimelineCanvas(build_engine()[0], None)
    opened = []
    canvas.keyframe_editor_requested.connect(opened.append)
    shown = []
    monkeypatch.setattr(QMenu, "exec", lambda menu, *args, **kwargs: shown.append(menu))

    canvas._timeline_menu(QPoint(40, 40))
    assert shown, "the empty-lane menu was not built"
    assert opened == [], "assembling the menu must not open the editor"

    keyframes = next(action.menu() for action in shown[0].actions()
                     if action.menu() is not None and action.text() == "Keyframes")
    item = next(action for action in keyframes.actions()
                if action.text() == "Open Keyframe Editor")
    item.trigger()
    assert opened == [[]], "choosing the item opens the full editor"

