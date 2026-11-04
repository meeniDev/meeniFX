"""Graph Editor repaint cost: one geometry pass per paint, and only what is on screen.

The freeze this pins down was quadratic. ``_curve_path`` mapped every sampled point through
``y_of``, ``y_of`` recomputed the value range, and the range re-sampled *every* visible
curve: a 2400-point curve of a five-curve project cost ~2400 full passes, and one repaint of
a real project took over 50 seconds. The fix is structural, so the tests assert structure
(samples per paint, sampling window) rather than wall-clock alone - plus one generous timing
guard that still fails by two orders of magnitude if the quadratic path returns.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model.keyframes import AnimatedValue, Keyframe  # noqa: E402
from meenifx.project.media import media_type_for  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")


def pump(seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.002)


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
def animated_clip(window, tmp_path):
    """A still clip long enough to matter, with all five transform channels animated."""
    window.projects._set_clean()
    settings = ProjectSettings(name=f"Perf {uuid.uuid4().hex[:6]}", width=1080, height=1080,
                               fps=60.0)
    assert window.projects.create_project(settings, str(tmp_path))
    library = window.projects.project.library
    item = library.add(STILL, 4.0, 640, 360, 30.0, media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    ids = window.insert_media_items([item], frame=0)
    clip = window.engine.timeline.find_clip(ids[0])
    window.engine.timeline.set_clip_range(clip.id, 0, 600) if hasattr(
        window.engine.timeline, "set_clip_range") else None
    from meenifx.model import transform as T
    transform = T.ensure_transform(clip)
    for index, name in enumerate(("position_x", "position_y", "scale_y", "anchor_x",
                                  "anchor_y")):
        value = AnimatedValue()
        for frame in (0, 60, 140, 260, 380, 520):
            value.keyframes.append(Keyframe(frame, 20.0 + index * 8 + frame * 0.35))
        transform[name] = value          # the stored form an animated channel really has
    window.engine.session.set_selection({clip.id})
    pump(0.05)
    return clip


def test_a_repaint_samples_each_curve_once(window, animated_clip, monkeypatch):
    """One ``sample_channel`` per visible curve, no matter how many points it draws."""
    window._toggle_graph_dock()
    pump(0.1)
    graph = window.graph_editor
    graph.refresh()
    canvas = graph.canvas
    canvas.resize(900, 320)
    counters = {"samples": 0}
    original = type(canvas).sample_channel

    def counting(self, channel, max_points=2400):
        counters["samples"] += 1
        return original(self, channel, max_points)

    monkeypatch.setattr(type(canvas), "sample_channel", counting)
    visible = graph.visible_channels()
    assert visible, "the fixture must animate at least one channel"
    canvas.grab()
    assert counters["samples"] == len(visible), (
        "each visible curve is sampled exactly once per repaint "
        f"(got {counters['samples']} for {len(visible)} curves)")


def test_the_value_range_is_not_recomputed_per_point(window, animated_clip, monkeypatch):
    """``y_of`` must not run during a repaint: that was the quadratic step."""
    window._toggle_graph_dock()
    pump(0.1)
    graph = window.graph_editor
    canvas = graph.canvas
    canvas.resize(900, 320)
    counts = {"y_of": 0, "ranges": 0}
    canvas_type = type(canvas)
    original_y = canvas_type.y_of
    original_range = canvas_type.value_range

    def y_of(self, value):
        counts["y_of"] += 1
        return original_y(self, value)

    def value_range(self):
        counts["ranges"] += 1
        return original_range(self)

    monkeypatch.setattr(canvas_type, "y_of", y_of)
    monkeypatch.setattr(canvas_type, "value_range", value_range)
    canvas.grab()
    assert counts["y_of"] == 0
    assert counts["ranges"] == 0


def test_sampling_is_bounded_by_the_visible_area(window, animated_clip):
    window._toggle_graph_dock()
    pump(0.1)
    graph = window.graph_editor
    canvas = graph.canvas
    canvas.resize(640, 300)
    frames, values = canvas.sample_channel("transform.position_x")
    assert len(frames) == len(values) > 1
    assert len(frames) <= 640, "never more than one sample per pixel"
    first, last = frames[0], frames[-1]
    window_start = canvas.frame_at(0)
    window_end = canvas.frame_at(canvas.width())
    assert first >= min(0.0, window_start) - 1
    assert last <= max(window_end, first) + 1
    assert last - first <= (window_end - window_start) + 2, (
        "only frames that can be on screen are sampled")


def test_zooming_in_does_not_explode_the_sample_count(window, animated_clip):
    window._toggle_graph_dock()
    pump(0.1)
    graph = window.graph_editor
    canvas = graph.canvas
    canvas.resize(900, 320)
    canvas.zoom_x = 40.0
    frames, _values = canvas.sample_channel("transform.position_x")
    assert len(frames) <= 900


def test_one_repaint_has_a_frame_budget(window, animated_clip):
    """A generous ceiling: the quadratic version took tens of seconds for this project."""
    window._toggle_graph_dock()
    pump(0.1)
    graph = window.graph_editor
    canvas = graph.canvas
    canvas.resize(900, 320)
    canvas.grab()                       # warm the fonts and the paint engine
    started = time.perf_counter()
    canvas.grab()
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"one Graph Editor repaint took {elapsed * 1000:.0f} ms"


def test_opening_the_dock_is_immediate(window, animated_clip):
    """Opening the dock must not wait for the curves to be computed 2400 times over."""
    dock = window.docks.get("graph")
    assert dock is not None
    if dock.isVisible():
        window._toggle_graph_dock()
        pump(0.05)
    started = time.perf_counter()
    window._toggle_graph_dock()
    pump(0.2)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"opening the Graph Editor took {elapsed * 1000:.0f} ms"
