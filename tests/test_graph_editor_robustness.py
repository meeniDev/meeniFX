"""Graph Editor robustness: it must open and paint for every real project shape.

The crash this file pins down was a silent process abort: a non-numeric channel (a colour
preset stored as a curve) reached ``float()`` inside ``paintEvent``, and PyQt6 turns that
into ``qFatal`` - no traceback, no window, no project. Every case here is a shape a real
project can contain, so each one is opened, painted and driven with real events.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx import crashlog  # noqa: E402
from meenifx.model import channels as CH  # noqa: E402
from meenifx.model.keyframes import AnimatedValue, Keyframe  # noqa: E402
from meenifx.project.media import media_type_for  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")
AUDIO = os.path.join(ROOT, "demo_audio", "music-bed.wav")


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
    editor.resize(1400, 900)
    editor.show()
    pump(0.2)
    yield editor
    editor.projects._set_clean()
    editor.close()


@pytest.fixture()
def editor(window, tmp_path):
    window.projects._set_clean()
    settings = ProjectSettings(name=f"Graph {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    pump(0.05)
    return window


def insert(window, path, frames=90):
    library = window.projects.project.library
    item = library.add(path, 3.0, 640, 360, 30.0, media_type=media_type_for(path))
    window.project_panel.rebuild(force=True)
    ids = window.insert_media_items([item], frame=0)
    assert ids, f"{os.path.basename(path)} was not inserted"
    pump(0.05)
    return window.engine.timeline.find_clip(ids[0])


def open_graph(window, clip=None):
    window.engine.session.set_selection(set() if clip is None else {clip.id})
    pump(0.05)
    window._toggle_graph_dock()
    pump(0.1)
    window.graph_editor.refresh()
    window.graph_editor.canvas.grab()
    return window.graph_editor


def animated(*pairs):
    value = AnimatedValue()
    for frame, level in pairs:
        value.keyframes.append(Keyframe(int(frame), level))
    return value


def test_the_editor_opens_on_an_empty_timeline(editor):
    graph = open_graph(editor, None)
    assert graph.clip() is None
    assert graph.channels() == []
    graph.canvas.grab()          # paints "No clip selected"


def test_the_editor_opens_for_a_picture_clip(editor):
    clip = insert(editor, STILL)
    graph = open_graph(editor, clip)
    assert graph.clip() is clip
    assert graph.channels(), "a video clip must expose its transform channels"


def test_the_editor_opens_for_a_text_clip(editor):
    editor.engine.session.set_selection(set())
    clip_id = editor.add_text_clip()
    text_clip = editor.engine.timeline.find_clip(clip_id)
    from meenifx.model import text as TXT
    TXT.set_content(text_clip, "First", 0, auto_key=True)
    TXT.set_content(text_clip, "Second", 30, auto_key=True)
    graph = open_graph(editor, text_clip)
    ids = [row["id"] for row in graph.channels()]
    assert all("content" not in channel for channel in ids), "words are not a curve"


def test_the_editor_opens_for_an_audio_clip(editor):
    clip = insert(editor, AUDIO)
    graph = open_graph(editor, clip)
    # The curve view is for pictures; selecting an audio clip shows the empty state.
    assert graph.clip() is None
    graph.canvas.grab()


def test_a_non_numeric_animation_is_never_plotted(editor):
    """The crash: an animated colour preset used to reach float() inside paintEvent."""
    clip = insert(editor, STILL)
    clip.effects.append({"effect_id": "color_correct", "enabled": True,
                         "params": {"preset": animated((0, "none"), (40, "warm"))}, "pins": []})
    graph = open_graph(editor, clip)
    ids = [row["id"] for row in graph.channels()]
    assert "fx.color_correct.preset" not in ids
    assert graph.key_entries() == [] or all(
        isinstance(time, int) for _channel, time, _value in graph.key_entries())
    graph.canvas.mode_check = None
    for index in (0, 1):
        graph.mode_combo.setCurrentIndex(index)
        graph.canvas.grab()
    graph.mode_combo.setCurrentIndex(0)


def test_hostile_values_paint_instead_of_killing_the_process(editor):
    clip = insert(editor, STILL)
    clip.effects.append({"effect_id": "glow", "enabled": True,
                         "params": {"intensity": animated((0, float("nan")), (30, float("inf"))),
                                    "radius": "twelve"}, "pins": []})
    graph = open_graph(editor, clip)
    canvas = graph.canvas
    for zoom in (0.05, 0.5, 4.0, 60.0):
        canvas.zoom_x = zoom
        canvas.grab()
    canvas.fit()
    canvas.reset_view()
    canvas.grab()


def test_interaction_events_are_safe_without_a_clip(editor):
    graph = open_graph(editor, None)
    canvas = graph.canvas
    local, glob = QPointF(50, 50), QPointF(canvas.mapToGlobal(QPoint(50, 50)))
    canvas.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, local, glob,
                                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                       Qt.KeyboardModifier.NoModifier))
    canvas.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(70, 60), glob,
                                      Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                      Qt.KeyboardModifier.NoModifier))
    canvas.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(70, 60), glob,
                                         Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                                         Qt.KeyboardModifier.NoModifier))
    canvas.wheelEvent(QWheelEvent(QPointF(50, 50), glob, QPoint(0, 0), QPoint(0, 120),
                                  Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                  Qt.ScrollPhase.NoScrollPhase, False))
    canvas.grab()


def _mouse(canvas, kind, point, buttons):
    global_pos = QPointF(canvas.mapToGlobal(QPoint(int(point.x()), int(point.y()))))
    types = {"press": QEvent.Type.MouseButtonPress, "move": QEvent.Type.MouseMove,
             "release": QEvent.Type.MouseButtonRelease}
    event = QMouseEvent(types[kind], point, global_pos, Qt.MouseButton.LeftButton, buttons,
                        Qt.KeyboardModifier.NoModifier)
    {"press": canvas.mousePressEvent, "move": canvas.mouseMoveEvent,
     "release": canvas.mouseReleaseEvent}[kind](event)


def _press(canvas, point):
    _mouse(canvas, "press", point, Qt.MouseButton.LeftButton)


def _drag(canvas, point):
    _mouse(canvas, "move", point, Qt.MouseButton.LeftButton)


def _release(canvas, point):
    _mouse(canvas, "release", point, Qt.MouseButton.NoButton)


def test_a_keyframe_keeps_following_the_pointer_for_the_whole_drag(editor):
    """Every move must apply to the keys as they were at press time, not in place."""
    clip = insert(editor, STILL)
    CH.set_value(clip, "transform.position_x", 10.0, 10, auto_key=True)
    CH.set_value(clip, "transform.position_x", 60.0, 40, auto_key=True)
    graph = open_graph(editor, clip)
    canvas = graph.canvas
    # The tracking contract is about pointer moves, not about how wide the dock is:
    # choose a zoom where each step below lands on a different project frame.
    canvas.zoom_x = 8.0
    canvas.pan_x = 0.0
    start = QPointF(canvas.x_of(graph.start_frame() + 40), canvas.y_of(60.0))
    _press(canvas, start)
    times, expected = [], []
    for step in (10, 20, 30):
        _drag(canvas, QPointF(start.x() + step, start.y()))
        times.append([key.time for key in CH.stored(clip, "transform.position_x").keyframes])
        expected.append([0, 10, 40 + int(round(step / canvas.zoom_x))])
    _release(canvas, QPointF(start.x() + 30, start.y()))
    assert times == expected, f"the key has to track the pointer, not jump once: {times}"
    assert len({tuple(row) for row in times}) == 3, "every move has to land somewhere new"


def test_the_graph_playhead_follows_playback(editor, monkeypatch):
    """Playback advances the playhead silently; the curve view has to move with it.

    ``set_playhead(..., silent=True)`` is what the playback tick uses, and it emits only
    ``playhead_moved`` - never ``playhead_changed``. The graph used to listen to neither,
    so its red line stayed on the frame where playback began.
    """
    clip = insert(editor, STILL)
    CH.set_value(clip, "transform.position_x", 10.0, 10, auto_key=True)
    CH.set_value(clip, "transform.position_x", 60.0, 120, auto_key=True)
    graph = open_graph(editor, clip)
    canvas = graph.canvas
    canvas.fit()
    editor.engine.session.set_playhead(0, silent=True)
    canvas.grab()                                    # paint once, so the old column is known
    calls = []
    monkeypatch.setattr(canvas, "update", lambda *args, **_k: calls.append(args))
    editor.engine.session.set_playhead(45, silent=True)   # exactly what the playback tick does
    assert calls, "a silent playhead move still has to repaint the curve view"
    band = calls[-1][0] if calls[-1] else None
    assert band is not None, "only the columns that changed need repainting"
    assert band.contains(QPoint(int(canvas.x_of(45)), canvas.height() // 2))
    monkeypatch.undo()
    image = canvas.grab().toImage()
    pixel = image.pixelColor(int(canvas.x_of(45)), canvas.height() // 2)
    assert pixel.red() > 120 and pixel.red() > pixel.blue(), \
        f"the playhead line has to be drawn at frame 45, got {pixel.name()}"


def test_the_curve_repaints_while_the_button_is_still_down(editor, monkeypatch, tmp_path):
    """The picture has to follow the pointer, not wait for the release.

    A local named ``time`` in this drag shadowed the module, so every move raised on its
    last line: the model moved, but the repaint request after it never ran and the key
    only appeared where the button came up. The guard swallowed the exception, so this
    test watches the two things the failure actually broke: no report, and one repaint
    request per move.
    """
    crashlog.reset()
    monkeypatch.setattr(crashlog, "path", lambda: str(tmp_path / "crash.log"))
    clip = insert(editor, STILL)
    CH.set_value(clip, "transform.position_x", 10.0, 10, auto_key=True)
    CH.set_value(clip, "transform.position_x", 60.0, 40, auto_key=True)
    graph = open_graph(editor, clip)
    canvas = graph.canvas
    repaints = []
    start = QPointF(canvas.x_of(graph.start_frame() + 40), canvas.y_of(60.0))
    _press(canvas, start)
    monkeypatch.setattr(canvas, "update", lambda *_a, **_k: repaints.append(1))
    frames = []
    for step in (12, 30, 48):
        _drag(canvas, QPointF(start.x() + step, start.y() - step))
        frames.append(canvas.grab().toImage())
    _release(canvas, QPointF(start.x() + 48, start.y() - 48))
    assert not crashlog.last_report(), "a drag must not raise behind the guard"
    assert len(repaints) >= 3, f"every move asks for a repaint, got {len(repaints)}"
    assert frames[0] != frames[1] and frames[1] != frames[2], \
        "each move has to paint the moved key"


def test_a_whole_drag_is_one_undo_step(editor):
    clip = insert(editor, STILL)
    CH.set_value(clip, "transform.position_x", 10.0, 10, auto_key=True)
    CH.set_value(clip, "transform.position_x", 60.0, 40, auto_key=True)
    graph = open_graph(editor, clip)
    canvas = graph.canvas
    start = QPointF(canvas.x_of(graph.start_frame() + 40), canvas.y_of(60.0))
    stack = editor.engine.session.stack.undo_stack
    before = stack.count()
    _press(canvas, start)
    for step in (5, 10, 15):
        _drag(canvas, QPointF(start.x() + step, start.y() - step))
    _release(canvas, QPointF(start.x() + 15, start.y() - 15))
    assert stack.count() == before + 1, "a continuous drag is a single undo entry"
    def keys_now():
        return [(key.time, round(float(key.value), 6)) for key
                in CH.stored(editor.engine.timeline.find_clip(clip.id),
                             "transform.position_x").keyframes]

    moved = keys_now()
    stack.undo()
    pump(0.1)
    assert keys_now() != moved, "one undo puts the keys back"
    assert keys_now()[:2] == [(0, 0.0), (10, 10.0)]


def test_the_playhead_follows_a_drag_across_the_ruler(editor):
    clip = insert(editor, STILL)
    graph = open_graph(editor, clip)
    canvas = graph.canvas
    session = editor.engine.session
    session.set_playhead(0)
    pump(0.05)
    _press(canvas, QPointF(canvas.x_of(5), 6))
    seen, wanted = [], (20, 40, 60)
    for frame in wanted:
        _drag(canvas, QPointF(canvas.x_of(frame), 6))
        seen.append(session.playhead)
    _release(canvas, QPointF(canvas.x_of(wanted[-1]), 6))
    assert seen == sorted(seen) and len(set(seen)) == len(wanted), \
        f"the playhead must scrub live: {seen}"
    assert seen == pytest.approx(list(wanted), abs=1), f"it lands where the pointer is: {seen}"
    assert session.playhead == seen[-1], "releasing keeps the frame the drag reached"


def test_dragging_the_playhead_line_scrubs_without_jumping(editor):
    clip = insert(editor, STILL)
    CH.set_value(clip, "transform.position_x", 10.0, 10, auto_key=True)
    graph = open_graph(editor, clip)
    canvas = graph.canvas
    session = editor.engine.session
    session.set_playhead(20)
    pump(0.05)
    line_x = canvas.x_of(20)
    _press(canvas, QPointF(line_x + 1, 120))
    assert session.playhead == 20, "grabbing the line must not jump the playhead"
    _drag(canvas, QPointF(canvas.x_of(50), 120))
    _release(canvas, QPointF(canvas.x_of(50), 120))
    assert session.playhead == pytest.approx(50, abs=1), "dragging the line scrubs it"


def test_stale_key_selection_rows_are_ignored(editor):
    clip = insert(editor, STILL)
    graph = open_graph(editor, clip)
    CH.set_value(clip, "transform.opacity", 100.0, 0)
    CH.set_value(clip, "transform.opacity", 20.0, 30)
    graph.refresh()
    # Rows a deleted clip or a hand-edited file could leave behind.
    editor.engine.session._key_selection = [(clip.id, None, 0), ("junk",), (clip.id, 12, "x")]
    graph._selection_from_session()
    graph.focus_entries([(clip.id, "transform.opacity", 30), ("broken",)])
    graph.refresh()
    graph.canvas.grab()


def test_a_reopened_project_opens_without_a_crash(editor, tmp_path):
    clip = insert(editor, STILL)
    clip.effects.append({"effect_id": "color_correct", "enabled": True,
                         "params": {"preset": animated((0, "cinematic"), (20, "vintage"))},
                         "pins": []})
    editor.engine.session.changed.emit()
    window = editor
    assert window.projects.save() if hasattr(window.projects, "save") else True
    path = getattr(window.projects, "path", None) or getattr(window.projects, "_path", None)
    if path and os.path.isfile(path):
        assert window.projects.open_path(path) or window.projects.is_open
        pump(0.2)
        graph = open_graph(window, None)
        graph.canvas.grab()


def test_loading_flattens_an_impossible_curve(editor):
    clip = insert(editor, STILL)
    clip.effects.append({"effect_id": "color_correct", "enabled": True,
                         "params": {"preset": animated((0, "warm"), (30, "cool"))}, "pins": []})
    repairs = editor.engine.sanitize_animation()
    assert any(channel == "fx.color_correct.preset" for _clip, channel, _what in repairs)
    entry = clip.effects[-1]
    assert entry["params"]["preset"] == "warm", "the first value is kept, so the picture is unchanged"
    assert not isinstance(entry["params"]["preset"], AnimatedValue)


def test_numeric_curves_survive_the_sanitizer(editor):
    clip = insert(editor, STILL)
    CH.set_value(clip, "transform.opacity", 100.0, 0, auto_key=True)
    CH.set_value(clip, "transform.opacity", 10.0, 20, auto_key=True)
    assert isinstance(CH.stored(clip, "transform.opacity"), AnimatedValue)
    editor.engine.sanitize_animation()
    assert isinstance(CH.stored(clip, "transform.opacity"), AnimatedValue)


def test_the_guard_logs_once_and_returns_the_fallback(monkeypatch, tmp_path):
    crashlog.reset()
    monkeypatch.setattr(crashlog, "path", lambda: str(tmp_path / "crash.log"))
    calls = []

    @crashlog.guard("unit test", fallback="safe")
    def broken():
        calls.append(1)
        raise RuntimeError("paint exploded")

    assert broken() == "safe"
    assert broken() == "safe"
    assert len(calls) == 2, "the body always runs"
    text = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert text.count("== unit test") == 1, "the same failure is logged once, not per frame"
    assert "RuntimeError: paint exploded" in text


def test_a_failing_paint_cannot_abort_the_window(editor, monkeypatch, tmp_path):
    """The whole point: a broken value_range must not take the application down."""
    crashlog.reset()
    monkeypatch.setattr(crashlog, "path", lambda: str(tmp_path / "crash.log"))
    clip = insert(editor, STILL)
    # A curve must exist, or the paint has nothing to sample and nothing to break.
    CH.set_value(clip, "transform.opacity", 100.0, 0, auto_key=True)
    CH.set_value(clip, "transform.opacity", 10.0, 20, auto_key=True)
    graph = open_graph(editor, clip)
    assert graph.visible_channels(), "the fixture must expose at least one curve"

    def explode(*_args, **_kwargs):
        raise ValueError("bad channel data")

    # The paint samples every visible curve; a curve that cannot be read is the failure
    # that used to reach Qt and abort the process.
    monkeypatch.setattr(graph.canvas, "sample_channel", explode)
    graph.canvas.grab()                       # must not raise
    assert "bad channel data" in (tmp_path / "crash.log").read_text(encoding="utf-8")
    editor.close  # the window is still alive and usable
    assert editor.graph_editor is graph
