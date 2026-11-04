"""The timeline keyframe workflow: envelopes, gain, anchors and text presets.

These tests exercise the shared animation model the timeline, the Inspector and the
Graph Editor all use, plus the two places where the workflow meets Qt (the clip lanes
and the Graph Editor dock).
"""

import time

import numpy as np
import pytest

from conftest import build_engine, synthetic_frame

from meenifx.audio import mix, plan
from meenifx.model import animation as ANIM
from meenifx.model import channels as CH
from meenifx.model import text as TXT
from meenifx.model import transform as T
from meenifx.model.keyframes import AnimatedValue


def pump(qapp, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


def _key(engine, clip, channel, frame, value):
    """Key one channel at a clip-local frame, like a stopwatch edit does."""
    def action():
        target = engine.timeline.find_clip(clip.id)
        CH.set_value(target, channel, value, frame, True)
    engine.session.stack.run("Key", action)
    engine.session.changed.emit()


def _times(clip, channel):
    stored = CH.stored(clip, channel)
    return [key.time for key in stored.keyframes] if isinstance(stored, AnimatedValue) else []


def _find(engine, clip_id):
    return engine.timeline.find_clip(clip_id)


def _undo_count(engine):
    return len(engine.session.stack._undo)


def _audio_clip(engine, duration=60, start=0):
    track = engine.timeline.add_track("audio")
    return engine.timeline.new_clip("sound", "synthetic.wav", track, start, duration)


def _text_clip(engine, content="Hello", duration=60):
    track = engine.timeline.add_track("video", name="Text")
    clip = engine.timeline.new_clip("Title", None, track, 0, duration)
    TXT.ensure_text(clip)["text"] = content
    return clip


def _samples(seconds=2.0, rate=8000):
    frames = int(seconds * rate)
    wave = np.sin(np.linspace(0, 40 * np.pi, frames)).astype(np.float32) * 0.5
    return np.stack([wave, wave], axis=1)


def _audio_only(path):
    """Only the audio clip's path decodes, so the video lane contributes no segment."""
    return _samples() if str(path).endswith(".wav") else None


# --------------------------------------------------------------- keyframe editing
def test_multi_selected_keyframes_move_together():
    engine, clip = build_engine()
    _key(engine, clip, "transform.opacity", 0, 100)
    _key(engine, clip, "transform.opacity", 20, 40)
    clip = _find(engine, clip.id)
    entries = [(clip.id, "transform.opacity", 0), (clip.id, "transform.opacity", 20)]

    bases = engine.session.key_bases(entries)
    low, high = engine.session.key_shift_limits(bases)
    assert (low, high) == (0, 39), "a group move stops at the clip's ends"
    engine.session.shift_keyframes(bases, 5)
    clip = _find(engine, clip.id)
    assert _times(clip, "transform.opacity") == [5, 25], "both keys move by the same delta"
    assert CH.value_at(clip, "transform.opacity", 25) == pytest.approx(40), "values stay put"


def test_keyframe_group_move_is_undoable_and_clamped():
    engine, clip = build_engine()
    _key(engine, clip, "transform.opacity", 0, 100)
    _key(engine, clip, "transform.opacity", 55, 0)
    clip = _find(engine, clip.id)
    entries = [(clip.id, "transform.opacity", 0), (clip.id, "transform.opacity", 55)]
    bases = engine.session.key_bases(entries)
    assert engine.session.key_shift_limits(bases)[1] == 4

    before = _undo_count(engine)
    snapshot = engine.timeline.snapshot()
    engine.session.shift_keyframes(bases, 4)
    engine.session.stack.commit("Move keyframes", snapshot)
    assert _undo_count(engine) == before + 1
    assert _times(_find(engine, clip.id), "transform.opacity") == [4, 59]

    engine.session.undo()
    assert _times(_find(engine, clip.id), "transform.opacity") == [0, 55]


def test_easing_presets_apply_to_a_multi_selection():
    engine, clip = build_engine()
    for frame, value in ((0, 100), (20, 20), (40, 100)):
        _key(engine, clip, "transform.opacity", frame, value)
    clip = _find(engine, clip.id)
    entries = [(clip.id, "transform.opacity", frame) for frame in (0, 20)]

    assert engine.session.set_key_interpolation(entries, "hold", clear_handles=True)
    clip = _find(engine, clip.id)
    stored = CH.stored(clip, "transform.opacity")
    assert [key.interpolation for key in stored.keyframes] == ["hold", "hold", "linear"]

    assert engine.session.apply_easing(entries, "Ease In")
    clip = _find(engine, clip.id)
    stored = CH.stored(clip, "transform.opacity")
    assert stored.keyframes[0].interpolation != "linear"
    assert stored.keyframes[1].interpolation != "linear", "both selected keys ease"
    assert stored.keyframes[2].interpolation == "linear", "the unselected key is untouched"


def test_copy_paste_and_duplicate_keyframes():
    engine, clip = build_engine()
    _key(engine, clip, "transform.opacity", 5, 90)
    _key(engine, clip, "transform.opacity", 15, 10)
    clip = _find(engine, clip.id)
    assert engine.session.copy_keyframes([(clip.id, "transform.opacity", 5),
                                          (clip.id, "transform.opacity", 15)]) == 2
    engine.session.set_selection({clip.id})
    engine.session.set_playhead(30)
    assert engine.session.paste_keyframes_at_playhead() == 2
    clip = _find(engine, clip.id)
    times = _times(clip, "transform.opacity")
    assert (5, 15, 30, 40) == tuple(time for time in times if time in (5, 15, 30, 40))
    assert CH.value_at(clip, "transform.opacity", 30) == pytest.approx(90), "pasted at the playhead"

    assert engine.session.duplicate_keyframes([(clip.id, "transform.opacity", 30),
                                               (clip.id, "transform.opacity", 40)]) == 2
    clip = _find(engine, clip.id)
    assert 41 in _times(clip, "transform.opacity")
    assert 51 in _times(clip, "transform.opacity")


def test_add_keyframe_at_playhead_button():
    engine, clip = build_engine()
    engine.session.set_selection({clip.id})
    engine.session.set_playhead(12)
    assert engine.session.add_keyframes_at_playhead() == 1, "a static clip keys its envelope"
    clip = _find(engine, clip.id)
    assert _times(clip, "transform.opacity") == [12]
    assert engine.session.selected_key_entries() == [(clip.id, "transform.opacity", 12)]


# -------------------------------------------------------------------- clip opacity
def test_clip_opacity_dims_preview_and_export_frames():
    engine, clip = build_engine()
    baseline, _ = engine.render_frame(0, 1.0)
    clip.transform["opacity"] = 40.0
    dimmed, _ = engine.render_frame(0, 1.0)
    preview, _ = engine.render_frame(0, 0.5)

    assert dimmed.mean() < baseline.mean() * 0.9, "the composited clip is really transparent"
    brighter = float((dimmed.astype(float) > baseline.astype(float) + 8).mean())
    assert brighter < 0.01, "nothing got brighter"
    # The exporter renders at scale 1.0, the preview at 0.5; both call render_frame, so
    # the transparency is the same value, only sampled at a different resolution.
    dim_ratio = dimmed.astype(float).mean() / max(1.0, baseline.astype(float).mean())
    preview_ratio = preview.astype(float).mean() / max(1.0, baseline.astype(float).mean())
    assert dim_ratio == pytest.approx(preview_ratio, abs=0.12)
    assert 0.2 < dim_ratio < 0.75


def test_clip_opacity_is_its_own_labelled_channel():
    engine, clip = build_engine()
    entry = next(row for row in CH.list_channels(clip) if row["id"] == "transform.opacity")
    assert entry["label"] == "Clip Opacity"
    assert entry["group"] == "Compositing"

    _key(engine, clip, "transform.opacity", 0, 100)
    _key(engine, clip, "transform.opacity", 30, 0)
    clip = _find(engine, clip.id)
    assert CH.value_at(clip, "transform.opacity", 15) == pytest.approx(50, abs=2)
    assert clip.text is None, "clip opacity never creates text settings"
    assert TXT.ensure_text(clip)["fill_opacity"] == 100, "text fill opacity is separate"


# ---------------------------------------------------------------------- envelopes
def test_opacity_envelope_sets_animated_opacity_and_undoes():
    engine, clip = build_engine()
    _key(engine, clip, "transform.opacity", 0, 100)
    snapshot = engine.timeline.snapshot()
    engine.session.set_envelope_value(clip.id, "transform.opacity", 30.0, 30)
    engine.session.stack.commit("Opacity envelope", snapshot)
    assert CH.value_at(_find(engine, clip.id), "transform.opacity", 30) == pytest.approx(30.0)

    engine.session.undo()
    clip = _find(engine, clip.id)
    assert CH.value_at(clip, "transform.opacity", 30) == pytest.approx(100.0)
    assert _times(clip, "transform.opacity") == [0]


def test_envelope_key_add_and_reset():
    engine, clip = build_engine()
    _key(engine, clip, "transform.opacity", 0, 100)
    assert engine.session.add_envelope_key(clip.id, "transform.opacity", 40)
    assert 40 in _times(_find(engine, clip.id), "transform.opacity")
    assert engine.session.reset_envelope(clip.id)
    clip = _find(engine, clip.id)
    assert not isinstance(clip.transform["opacity"], AnimatedValue)
    assert CH.value_at(clip, "transform.opacity", 40) == pytest.approx(100.0)


def test_gain_envelope_reaches_the_mixer():
    engine, _video = build_engine()
    clip = _audio_clip(engine)
    _key(engine, clip, CH.gain_channel(), 0, 0.0)
    _key(engine, clip, CH.gain_channel(), 30, -60.0)
    clip = _find(engine, clip.id)
    assert clip.gain_db_at(15) == pytest.approx(-30.0, abs=1.0)

    segments = plan.build_segments(engine.timeline, _audio_only, 8000)
    assert len(segments) == 1 and segments[0].gain_points
    segment = segments[0]
    assert segment.gain == 1.0, "the automation carries the gain, not the constant"

    loud = mix.render([segment], 0.0, 400, 8000, 2)
    silent = mix.render([segment], 0.95, 200, 8000, 2)
    assert mix.peak_of(loud) > 0.05
    assert mix.peak_of(silent) < mix.peak_of(loud) * 0.2, "the envelope fades to silence"


def test_static_gain_is_folded_into_the_segment():
    engine, _video = build_engine()
    clip = _audio_clip(engine)
    clip.gain = -6.0
    segments = plan.build_segments(engine.timeline, _audio_only, 8000)
    assert len(segments) == 1
    segment = segments[0]
    assert segment.gain_points == ()
    assert segment.gain == pytest.approx(10 ** (-6.0 / 20.0), rel=1e-3)


def test_gain_inspector_value_silences_and_boosts():
    engine, _video = build_engine()
    clip = _audio_clip(engine)
    assert CH.value_at(clip, CH.gain_channel(), 0) == 0.0, "0 dB by default"
    assert engine.session.set_clip_gain(clip.id, -6.0)
    assert _find(engine, clip.id).gain == pytest.approx(-6.0)
    engine.session.set_clip_gain(clip.id, 99.0)
    assert _find(engine, clip.id).gain == pytest.approx(ANIM.GAIN_CEILING_DB)
    engine.session.set_clip_gain(clip.id, -999.0)
    assert _find(engine, clip.id).gain_linear_at(0) == 0.0, "-inf is real silence"


def test_gain_ceiling_preference_changes_the_safe_range(monkeypatch):
    engine, _video = build_engine()
    clip = _audio_clip(engine)
    before = ANIM.GAIN_CEILING_DB
    try:
        assert ANIM.set_gain_ceiling(6) == 6.0
        engine.session.set_clip_gain(clip.id, 12.0)
        assert _find(engine, clip.id).gain == pytest.approx(6.0)
    finally:
        ANIM.set_gain_ceiling(before)


def test_gain_and_opacity_are_independent_on_linked_clips():
    engine, video = build_engine()
    audio = _audio_clip(engine)
    video.link_id = audio.id
    audio.link_id = video.id
    engine.session.set_selection({video.id, audio.id})
    engine.session.set_clip_gain(audio.id, -12.0)
    _key(engine, video, "transform.opacity", 0, 25)
    assert _find(engine, audio.id).gain == pytest.approx(-12.0)
    assert _find(engine, video.id).gain == 0.0
    assert CH.value_at(_find(engine, video.id), "transform.opacity", 0) == 25.0


# ------------------------------------------------------------------------- anchors
def test_anchor_presets_keep_the_visible_position():
    engine, clip = build_engine()
    engine.session.project_size = engine.project_size()
    clip.transform["position_x"] = 110.0
    clip.transform["position_y"] = -40.0
    before = T.evaluate(clip, 0)
    assert engine.session.set_anchor_preset(clip.id, "bottom_right")
    clip = _find(engine, clip.id)
    after = T.evaluate(clip, 0)
    expected_x, expected_y = T.anchor_for("bottom_right", engine.project_size(), (320, 180))
    assert after.anchor_x == pytest.approx(expected_x)
    assert after.anchor_y == pytest.approx(expected_y)
    # With no rotation or scale, the picture stays put when position - anchor is kept.
    assert after.position_x - after.anchor_x == pytest.approx(
        before.position_x - before.anchor_x, abs=0.5)
    assert after.position_y - after.anchor_y == pytest.approx(
        before.position_y - before.anchor_y, abs=0.5)


@pytest.mark.parametrize("preset", [row[0] for row in T.ANCHOR_PRESETS])
def test_every_anchor_preset_maps_to_its_grid_cell(preset):
    engine, clip = build_engine()
    engine.session.project_size = engine.project_size()
    assert engine.session.set_anchor_preset(clip.id, preset)
    clip = _find(engine, clip.id)
    values = T.evaluate(clip, 0)
    expected_x, expected_y = T.anchor_for(preset, engine.project_size(), (320, 180))
    assert (values.anchor_x, values.anchor_y) == pytest.approx((expected_x, expected_y))


def test_center_anchor_button_and_custom_anchor():
    engine, clip = build_engine()
    engine.session.project_size = engine.project_size()
    assert engine.session.center_anchor(clip.id)
    assert T.evaluate(_find(engine, clip.id), 0).anchor_x == pytest.approx(0.0)

    before = T.evaluate(_find(engine, clip.id), 0)
    assert engine.session.set_anchor(clip.id, 12.0, -8.0, keep_position=False)
    after = T.evaluate(_find(engine, clip.id), 0)
    assert (after.anchor_x, after.anchor_y) == pytest.approx((12.0, -8.0))
    assert after.position_x == pytest.approx(before.position_x), "opt-out keeps the position"


def test_anchor_point_can_be_keyframed():
    engine, clip = build_engine()
    engine.session.auto_keyframe.set(True)
    engine.session.set_playhead(20)
    engine.session.set_anchor(clip.id, 50.0, 50.0, keep_position=False)
    clip = _find(engine, clip.id)
    assert isinstance(clip.transform["anchor_x"], AnimatedValue)
    assert clip.transform["anchor_x"].key_at(20).value == pytest.approx(50.0)


def test_anchor_preset_still_works_when_the_anchor_is_animated():
    """The preset must read the animated anchor at the playhead, not the AnimatedValue."""
    engine, clip = build_engine()
    engine.session.project_size = engine.project_size()
    engine.session.auto_keyframe.set(True)
    engine.session.set_playhead(20)
    engine.session.set_anchor(clip.id, 40.0, 30.0, keep_position=False)
    assert engine.session.set_anchor_preset(clip.id, "bottom_right"), \
        "a keyframed anchor must not break the anchor presets"
    clip = _find(engine, clip.id)
    values = T.evaluate(clip, 20)
    expected_x, expected_y = T.anchor_for("bottom_right", engine.project_size(), (320, 180))
    assert (values.anchor_x, values.anchor_y) == pytest.approx((expected_x, expected_y))


# -------------------------------------------------------------------- text presets
@pytest.mark.parametrize("preset", list(ANIM.TEXT_IN_PRESETS) + list(ANIM.TEXT_OUT_PRESETS))
def test_every_text_preset_generates_editable_keyframes(preset):
    engine, _video = build_engine()
    text_clip = _text_clip(engine, "Hello meeniFX")
    slot = "in" if preset in ANIM.TEXT_IN_PRESETS else "out"
    assert engine.session.apply_text_animation(text_clip.id, slot, preset,
                                              duration=20, easing="Smooth")
    clip = _find(engine, text_clip.id)
    meta = clip.text["animation"][slot]
    assert meta["preset"] == preset
    generated = {channel: times for channel, times in meta["keys"].items() if times}
    assert generated, f"{preset} writes real keyframes"
    for channel, times in generated.items():
        assert 0 <= min(times) and max(times) <= clip.duration - 1, "keys stay inside the clip"
        stored = CH.stored(clip, channel)
        assert isinstance(stored, AnimatedValue)
        assert sorted(key.time for key in stored.keyframes) == sorted(times)

    # Re-applying "None" removes the generated keys and restores the resting values.
    assert engine.session.apply_text_animation(clip.id, slot, "None")
    clip = _find(engine, text_clip.id)
    assert slot not in (clip.text.get("animation") or {})
    for channel in generated:
        assert _times(clip, channel) == [], f"{channel} went back to static"


def test_typewriter_reveals_letters_over_time():
    engine, _video = build_engine()
    text_clip = _text_clip(engine, "Typewriter")
    engine.session.apply_text_animation(text_clip.id, "in", "Typewriter", duration=10)
    clip = _find(engine, text_clip.id)
    values = [CH.value_at(clip, "text.reveal", frame) for frame in (0, 5, 10)]
    assert values[0] == 0
    assert 0 < values[1] < values[2] <= len("Typewriter")


def test_text_animation_is_one_undo_step():
    engine, _video = build_engine()
    text_clip = _text_clip(engine, "Fade")
    before = _undo_count(engine)
    engine.session.apply_text_animation(text_clip.id, "in", "Fade In", duration=12)
    assert _undo_count(engine) == before + 1
    engine.session.undo()
    clip = _find(engine, text_clip.id)
    assert clip.text is None or not clip.text.get("animation")


# --------------------------------------------------------------- project round-trip
def _roundtrip(timeline, path):
    from meenifx.project import MediaLibrary, ProjectSettings
    from meenifx.project import container as C
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    library = MediaLibrary()
    settings = ProjectSettings.from_preset("HD 1080p 30", "T")
    data = C.ProjectData()
    data["settings"] = settings.to_dict()
    for key, value in timeline_to_sections(timeline, lambda p: library.add(p).id).items():
        data[key] = value
    data["media"] = library.to_dict()
    data["meta"] = C.make_meta("T", settings, timeline.duration, 0)
    data["ui"] = {}
    data.window_state = b""
    C.write_container(str(path), data)

    read = C.read_container(str(path))
    reopened_library = MediaLibrary.from_dict(read["media"])
    reopened = sections_to_timeline(read.sections, timeline.fps,
                                    lambda mid: reopened_library.items[mid].path)
    return reopened, read


def test_gain_and_text_animation_survive_save_and_reopen(tmp_path):
    engine, _video = build_engine()
    audio = _audio_clip(engine)
    _key(engine, audio, CH.gain_channel(), 0, 0.0)
    _key(engine, audio, CH.gain_channel(), 40, -60.0)
    text_clip = _text_clip(engine, "Saved")
    engine.session.apply_text_animation(text_clip.id, "in", "Slide In Left", duration=15,
                                        easing="Ease Out", delay=2)

    reopened, sections = _roundtrip(engine.timeline, tmp_path / "roundtrip.mfxpf")
    clips = {clip.name: clip for clip in reopened.all_clips()}
    sound = clips["sound"]
    assert sound.gain_db_at(20) == pytest.approx(-30.0, abs=1.5), "animated gain round-trips"
    title = clips["Title"]
    meta = title.text["animation"]["in"]
    assert meta["preset"] == "Slide In Left"
    assert (meta["duration"], meta["easing"], meta["delay"]) == (15, "Ease Out", 2)
    assert meta["keys"], "the generated keyframes are stored too"
    for channel, times in meta["keys"].items():
        assert _times(title, channel) == times
    assert sections["clips"]["gain"], "the format carries a gain column"


def test_migrated_v4_projects_get_a_zero_gain_column():
    from meenifx.project import container as C

    sections = {"clips": {"id": [1, 2], "name": ["a", "b"]}}
    migrated = C.migrate(sections, 4)
    assert migrated["clips"]["gain"] == [0.0, 0.0], "old projects open at unity gain"


def test_hidden_track_digest_hides_text_clips():
    from meenifx.preview.invalidation import timeline_digests

    engine, _video = build_engine()
    text_clip = _text_clip(engine, "Hidden?")
    visible = timeline_digests(engine.timeline)[text_clip.id]
    text_clip.track.visible = False
    hidden = timeline_digests(engine.timeline)[text_clip.id]
    assert hidden[0] != visible[0], "hiding the track invalidates the cached text frame"
    assert hidden[0].endswith(":hidden")
    text_clip.track.visible = True
    assert timeline_digests(engine.timeline)[text_clip.id][0] == visible[0]


# ------------------------------------------------------------------------- Qt wiring
@pytest.fixture()
def editor(qapp, tmp_path, monkeypatch):
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
    pump(qapp, 0.2)
    window._test_clip_id = clip.id
    yield window
    window.close()


def _clip(window):
    return window.engine.timeline.find_clip(window._test_clip_id)


def test_timeline_double_click_lane_adds_a_keyframe(editor, qapp):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    canvas = editor.timeline_panel.canvas
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    clip = _clip(editor)
    _key(editor.engine, clip, "transform.opacity", 0, 100)
    pump(qapp, 0.2)
    clip = _clip(editor)
    canvas._toggle_keyframe_rows(clip)
    _full, entries = canvas._keyframe_row_geometry(clip)
    row, mid = entries[0]
    # Click the middle of frame 24's cell: the left edge is a boundary between two frames,
    # and rounding there made this test land on 23 depending on the zoom that ran before.
    x = canvas.x_at(clip.timeline_start + 24) + canvas.ppf / 2.0
    event = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(x, mid),
                        QPointF(x, mid), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas.mouseDoubleClickEvent(event)
    assert 24 in _times(_clip(editor), "transform.opacity")


def test_timeline_and_graph_editor_share_the_selection(editor, qapp):
    canvas = editor.timeline_panel.canvas
    clip = _clip(editor)
    _key(editor.engine, clip, "transform.opacity", 0, 100)
    _key(editor.engine, clip, "transform.opacity", 20, 50)
    pump(qapp, 0.2)
    clip = _clip(editor)

    editor.engine.session.set_key_selection({(clip.id, "transform.opacity", 20)})
    pump(qapp, 0.15)
    assert editor.graph_editor.selection == {("transform.opacity", 20)}, \
        "a timeline selection shows up in the Graph Editor"

    editor.graph_editor.select_keys({("transform.opacity", 0)})
    pump(qapp, 0.15)
    assert editor.engine.session.key_selection == {(clip.id, "transform.opacity", 0)}, \
        "a Graph Editor selection shows up on the timeline"

    canvas.open_keyframes_in_graph([(clip.id, "transform.opacity", 20)])
    pump(qapp, 0.25)
    assert editor.docks["graph"].isVisible()
    assert editor.graph_editor.selection == {("transform.opacity", 20)}


def test_envelope_drag_is_one_undo_step(editor, qapp):
    from PyQt6.QtCore import Qt

    canvas = editor.timeline_panel.canvas
    clip = _clip(editor)
    _key(editor.engine, clip, "transform.opacity", 0, 100)
    pump(qapp, 0.2)
    clip = _clip(editor)
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    before = editor.qt_undo_stack.undo_stack.count()

    canvas._begin_envelope_drag(clip, "transform.opacity", "line", 0,
                                Qt.KeyboardModifier.NoModifier)
    canvas._envelope_move(canvas.x_at(clip.timeline_start + 10),
                          canvas._envelope_y(clip, 35))
    canvas._envelope_release()
    pump(qapp, 0.2)
    assert CH.value_at(_clip(editor), "transform.opacity", 10) == pytest.approx(35, abs=3)
    assert editor.qt_undo_stack.undo_stack.count() == before + 1

    editor.engine.session.undo()
    pump(qapp, 0.2)
    assert CH.value_at(_clip(editor), "transform.opacity", 10) == pytest.approx(100, abs=3)


def test_lane_keyframe_drag_snaps_to_the_playhead(editor, qapp):
    from PyQt6.QtCore import Qt

    canvas = editor.timeline_panel.canvas
    canvas.ppf = 6.0
    canvas.scroll_x = 0.0
    clip = _clip(editor)
    _key(editor.engine, clip, "transform.opacity", 0, 100)
    pump(qapp, 0.2)
    clip = _clip(editor)
    canvas._toggle_keyframe_rows(clip)
    editor.engine.session.set_playhead(40)
    pump(qapp, 0.15)

    row = canvas.animation_rows(clip)[0]
    canvas._begin_keyframe_drag(clip, row, 0, Qt.KeyboardModifier.NoModifier)
    canvas._keyframe_move(canvas.x_at(clip.timeline_start + 40) + 3)
    canvas._keyframe_release()
    pump(qapp, 0.2)
    assert 40 in _times(_clip(editor), "transform.opacity"), "snapped onto the playhead"


def test_keyframe_context_menu_lists_only_curve_operations(editor, qapp):
    canvas = editor.timeline_panel.canvas
    clip = _clip(editor)
    _key(editor.engine, clip, "transform.opacity", 0, 100)
    pump(qapp, 0.2)
    clip = _clip(editor)
    entries = [(clip.id, "transform.opacity", 0)]
    assert editor.engine.session.easing_entries(entries) == entries
    editor.engine.session.set_key_selection(entries)

    menu = canvas._build_keyframe_menu(clip, "transform.opacity", 0)
    labels = [action.text() for action in menu.actions() if action.text()]
    # Friendly families first: a beginner picks "Ease In Out" or "Bounce Out", not
    # "interpolation".
    for expected in ("Linear", "Ease In", "Ease Out", "Ease In Out", "Sine In Out",
                     "Quadratic Out", "Exponential In Out", "Bounce Out", "Elastic In Out",
                     "Smooth", "Advanced Interpolation", "Open in Graph Editor",
                     "Copy", "Paste", "Delete"):
        assert expected in labels
    advanced = next(action.menu() for action in menu.actions()
                    if action.text() == "Advanced Interpolation")
    specs = [action.text() for action in advanced.actions() if action.text()]
    assert specs == ["Hold", "Custom B\u00e9zier", "Auto B\u00e9zier", "Continuous B\u00e9zier",
                     "Broken B\u00e9zier Handles"]
    assert labels.index("Linear") < labels.index("Advanced Interpolation") \
        < labels.index("Open in Graph Editor") < labels.index("Copy")
    assert labels.index("Copy") < labels.index("Paste") < labels.index("Delete")
    menu.deleteLater()
