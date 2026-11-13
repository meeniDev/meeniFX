"""Real-editor acceptance test for the creative workflow overhaul.

This test drives the actual MainWindow with QTest mouse/keyboard input and Qt drop
events.  It deliberately keeps model setup to the minimum needed to create the project
and import footage; every workflow below is exercised through the user-facing window.
"""

import os
import time

import cv2
import numpy as np
from PyQt6.QtCore import QMimeData, QPointF, Qt
from PyQt6.QtGui import QDropEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLineEdit, QToolButton,
                             QWidget)

from meenifx.constants import GENERATOR_MIME
from meenifx.model import alpha as ALPHA
from meenifx.model import generators as GENERATORS
from meenifx.model import mask as MASK
from meenifx.model import matte as MATTE
from meenifx.model import path as PATHS
from meenifx.model import shape as SHAPE
from meenifx.model import transform as T
from meenifx.project.settings import ProjectSettings

WIDTH, HEIGHT = 320, 180
VIDEO_DARK = np.array((35, 45, 55), np.uint8)          # BGR
BACKGROUND_BGR = np.array((192, 48, 224), np.uint8)    # #E030C0 in BGR
SUBJECT_BGR = np.array((220, 150, 60), np.uint8)       # vivid BGR circle


def pump(seconds=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.004)


def wait_for(predicate, timeout=30.0, message="condition did not become true"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        pump(0.04)
    assert predicate(), message
    return True


def make_video(path):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0,
                             (WIDTH, HEIGHT))
    assert writer.isOpened()
    for frame_no in range(26):
        frame = np.full((HEIGHT, WIDTH, 3), VIDEO_DARK, np.uint8)
        cv2.circle(frame, (40 + frame_no * 5, 90), 24, tuple(int(v) for v in SUBJECT_BGR), -1)
        writer.write(frame)
    writer.release()
    assert os.path.isfile(path)


def create_window(qapp):
    from meenifx.ui.main_window import MainWindow

    MainWindow._load_demo_project = lambda self: None
    window = MainWindow(qapp)
    window.resize(1280, 800)
    window.show()
    pump(0.35)
    return window


def first_video_clip(window):
    return next((clip for clip in window.engine.timeline.all_clips()
                 if clip.track is not None and clip.track.type == "video"
                 and getattr(clip, "shape", None) is None), None)


def pen_click(window, project_point, modifiers=Qt.KeyboardModifier.NoModifier):
    view = window.preview_panel.view
    widget_point = window.overlay.to_widget(*project_point)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, modifiers, widget_point.toPoint())
    pump(0.05)


def pen_drag(window, project_start, project_end, modifiers=Qt.KeyboardModifier.NoModifier,
             steps=2):
    view = window.preview_panel.view
    start = window.overlay.to_widget(*project_start).toPoint()
    end = window.overlay.to_widget(*project_end).toPoint()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, modifiers, start)
    for index in range(1, steps + 1):
        point = start + (end - start) * index / steps
        QTest.mouseMove(view, point)
        pump(0.01)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, modifiers, end)
    pump(0.1)


def drawn_paths(clip):
    return SHAPE.ensure_shape(clip).get("path") or {}


def evaluated_shape_points(window, clip_id):
    clip = window.engine.timeline.find_clip(clip_id)
    return PATHS.points(PATHS.at(drawn_paths(clip), 0))


def evaluated_mask_points(window, clip_id, mask_id):
    clip = window.engine.timeline.find_clip(clip_id)
    mask = MASK.find(clip, mask_id)
    return PATHS.points(PATHS.at(mask.get("path") or {}, 0))


def visible_checkbox(window, text):
    return next(box for box in window.findChildren(QCheckBox)
                if box.text() == text and box.isVisible())


def generator_card(panel, key):
    """Find the fixed-source-type generator inspector card."""
    return panel.properties.findChild(QWidget, f"generator-card-{key}")


def set_scrub_field(card, label, value):
    from meenifx.ui.widgets import ScrubField

    field = next(field for field in card.findChildren(ScrubField)
                 if getattr(field, "_label_full", field.label.text()) == label)
    field.value_widget.begin_edit()
    field.value_widget.edit.setText(str(value))
    field.value_widget.edit.confirmed.emit()
    pump(0.2)


def export_once(window, fmt, path, *, alpha=False, start=0, end=1):
    from meenifx.ui.dialogs import ExportDialog

    dialog = ExportDialog(window.engine, window, export_range=(start, end))
    dialog.format.setCurrentText(fmt)
    dialog.alpha.setChecked(bool(alpha))
    dialog.use_source_size.setChecked(True)
    dialog.path.setText(str(path))
    settings = dialog.settings()
    dialog.close()

    results = []
    window.bridge.export_done.connect(lambda result: results.append(("done", result)))
    window.bridge.export_failed.connect(lambda error: results.append(("failed", error)))
    window._start_export(settings)
    wait_for(lambda: bool(results), timeout=120,
             message=f"{fmt} export did not finish")
    kind, payload = results[0]
    assert kind == "done", f"{fmt} export failed: {payload}"
    assert os.path.isfile(path), f"{fmt} export did not create {path}"
    return payload


def test_the_editor_session_end_to_end(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("MEENIFX_DISABLE_ROTO_MODEL", "1")
    monkeypatch.setenv("MEENIFX_NO_RECENT", "1")
    monkeypatch.setenv("MEENIFX_ROTO_CACHE", str(tmp_path / "roto-cache"))
    qapp.setQuitOnLastWindowClosed(False)

    video_path = tmp_path / "moving subject.avi"
    make_video(video_path)

    window = create_window(qapp)
    try:
        settings = ProjectSettings(name="Creative session", width=WIDTH, height=HEIGHT,
                                   fps=30.0)
        assert window.projects.create_project(settings, str(tmp_path))
        pump(0.15)
        window.import_paths([str(video_path)], 0)
        wait_for(lambda: first_video_clip(window) is not None, timeout=20,
                 message="the footage was not imported")
        video = first_video_clip(window)
        window.engine.session.set_selection({video.id})
        window.project_panel.rebuild(force=True)
        pump(0.15)

        # A contrasting lower layer makes mask/alpha inversion visible.
        lower_track = window.engine.timeline.add_track(
            "video", index=len(window.engine.timeline.tracks))
        background_id = window.engine.session.add_generator_clip(
            "solid", start=0, duration=26, track=lower_track)
        background = window.engine.timeline.find_clip(background_id)
        SHAPE.ensure_shape(background).update(gen_color="#E030C0", gen_opacity=100.0)
        window.engine.session.set_selection({video.id})
        pump(0.2)

        # ---------------------------------------------------------- effects discovery
        browser = window.effects_panel.browser
        browser.tabs.setCurrentIndex(0)
        for query in ("Roto Brush", "Smart Key"):
            browser.search.setText(query)
            pump(0.25)
            ids = [card.item.get("id") for card in browser.findChildren(
                __import__("meenifx.ui.browser", fromlist=["BrowserCard"]).BrowserCard)]
            assert "roto_brush" not in ids
            assert "smart_key" not in ids
        browser.search.setText("Chroma Key")
        pump(0.25)
        ids = [card.item.get("id") for card in browser.findChildren(
            __import__("meenifx.ui.browser", fromlist=["BrowserCard"]).BrowserCard)]
        assert "chroma_key" in ids
        browser.search.clear()
        pump(0.2)

        # ------------------------------------------------------------- pen tool: shape
        view = window.preview_panel.view
        view.set_zoom(1.2)
        window.pen_menu_button.menu().actions()[0].trigger()
        pump(0.1)
        assert window.overlay.mode == "pen"
        assert view.cursor().shape() == Qt.CursorShape.CrossCursor
        pen_drag(window, (40, 40), (52, 38))
        pen_click(window, (100, 40))
        pen_click(window, (70, 90))
        pen_click(window, (40, 40))
        pump(0.25)
        shape_clip = next((clip for clip in window.engine.timeline.all_clips()
                           if clip.shape and clip.shape.get("kind") == "path"), None)
        assert shape_clip is not None
        shape_id = shape_clip.id
        shape_points = evaluated_shape_points(window, shape_id)
        assert len(shape_points) == 3
        assert any(point.get("out") for point in shape_points), "the first point is Bezier"

        # A committed path is one session undo step; redo puts it back.
        window.undo()
        pump(0.15)
        assert window.engine.timeline.find_clip(shape_id) is None
        window.redo()
        pump(0.15)
        assert window.engine.timeline.find_clip(shape_id) is not None

        # Edit the shape after a clip transform, at 120% zoom.
        shape_clip = window.engine.timeline.find_clip(shape_id)
        T.set_channel(shape_clip, "rotation", 18.0)
        T.set_channel(shape_clip, "scale_x", 90.0)
        T.set_channel(shape_clip, "scale_y", 90.0)
        window.engine.session.set_selection({shape_id})
        window._request_preview()
        pump(0.2)
        before = evaluated_shape_points(window, shape_id)
        anchor = before[1]
        matrix = np.asarray(window.overlay.matrix(
            window.engine.timeline.find_clip(shape_id)), dtype=np.float64)
        anchor_project = matrix @ np.array([anchor["x"], anchor["y"], 1.0])
        pen_click(window, tuple(anchor_project[:2]))
        pump(0.15)
        assert window.overlay.mode == "pen"
        assert window.overlay.pen._editing_shape == shape_id
        moved_project = (float(anchor_project[0]) + 12.0, float(anchor_project[1]) + 7.0)
        pen_drag(window, tuple(anchor_project[:2]), moved_project)
        pump(0.2)
        after = evaluated_shape_points(window, shape_id)
        assert after != before, "dragging a transformed shape anchor did not edit its path"
        QTest.keyClick(view, Qt.Key.Key_Escape)
        pump(0.15)
        assert window.overlay.mode == "select"

        # -------------------------------------------------------------- pen tool: mask
        window.engine.session.set_selection({video.id})
        window.pen_menu_button.menu().actions()[1].trigger()
        pump(0.1)
        assert window._pen_mode == "mask"
        pen_drag(window, (150, 60), (165, 58))
        pen_click(window, (280, 60))
        pen_click(window, (280, 150))
        pen_click(window, (150, 60))
        pump(0.3)
        video = window.engine.timeline.find_clip(video.id)
        assert MASK.masks(video), "the mask gesture did not create a mask"
        mask_id = int(MASK.masks(video)[0]["id"])

        # Edit the existing mask, insert on a segment, delete, and undo the deletion.
        window.engine.session.set_selection({video.id})
        mask = MASK.find(video, mask_id)
        project_mask = MASK.project_path(video, mask, 0, (WIDTH, HEIGHT))
        mask_points = PATHS.points(project_mask)
        pen_click(window, (mask_points[1]["x"], mask_points[1]["y"]))
        pump(0.15)
        assert window.overlay.mode == "pen"
        assert window.overlay.pen._editing == (video.id, mask_id)
        count_before_insert = len(evaluated_mask_points(window, video.id, mask_id))
        p2 = mask_points[1]
        p3 = mask_points[2]
        midpoint = ((p2["x"] + p3["x"]) / 2.0, (p2["y"] + p3["y"]) / 2.0)
        assert window.overlay.pen._segment_at(
            window.overlay.to_widget(*midpoint)) is not None, (
                "the clicked mask segment was not detected")
        pen_click(window, midpoint)
        pump(0.2)
        assert len(evaluated_mask_points(window, video.id, mask_id)) == count_before_insert + 1
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Delete)
        pump(0.2)
        assert len(evaluated_mask_points(window, video.id, mask_id)) == count_before_insert
        window.undo()
        pump(0.2)
        assert len(evaluated_mask_points(window, video.id, mask_id)) == count_before_insert + 1
        window.redo()
        pump(0.15)
        assert len(evaluated_mask_points(window, video.id, mask_id)) == count_before_insert
        QTest.keyClick(view, Qt.Key.Key_Escape)
        pump(0.1)
        assert window.overlay.mode == "select"

        # Draft undo and Escape do not leave a stray mask behind.
        window.engine.session.set_selection({video.id})
        window.pen_menu_button.menu().actions()[1].trigger()
        pump(0.1)
        pen_click(window, (30, 120))
        pen_click(window, (90, 120))
        pump(0.15)
        assert len(window.overlay.pen.path["points"]) == 2
        view.setFocus()
        window.undo_button.click()
        pump(0.15)
        assert len(window.overlay.pen.path["points"]) == 1, (
            "Undo during path creation must undo the draft")
        window.redo_button.click()
        pump(0.1)
        assert len(window.overlay.pen.path["points"]) == 2
        window.undo_button.click()
        pump(0.1)
        assert len(window.overlay.pen.path["points"]) == 1
        # A focused text field keeps destructive shortcuts away from the canvas.
        window.activateWindow()
        pump(0.1)
        editor = QLineEdit(window)
        editor.show()
        editor.raise_()
        editor.setFocus()
        pump(0.3)
        assert QApplication.focusWidget() is editor
        QTest.keyClick(editor, Qt.Key.Key_Delete)
        pump(0.1)
        assert window.undo() is False
        assert window.engine.timeline.find_clip(video.id) is not None
        editor.close()
        editor.deleteLater()
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Escape)
        pump(0.15)
        assert window.overlay.mode == "select"
        assert len(MASK.masks(window.engine.timeline.find_clip(video.id))) == 1

        # --------------------------------------------------- explicit mask/alpha invert
        panel = window.effects_panel
        window.engine.session.set_selection({video.id})
        window._refresh_panels()
        pump(0.3)
        plain = window.engine.compose_frame(0, 1.0)
        inside = (110, 220)
        outside = (25, 300)
        assert np.linalg.norm(plain[inside].astype(float) - BACKGROUND_BGR.astype(float)) > 60
        visible_checkbox(window, "Invert Mask").click()
        pump(0.25)
        inverted = window.engine.compose_frame(0, 1.0)
        assert np.linalg.norm(inverted[inside].astype(float) - BACKGROUND_BGR.astype(float)) < 45
        assert np.linalg.norm(inverted[outside].astype(float) - BACKGROUND_BGR.astype(float)) > 60
        assert np.linalg.norm(plain[outside].astype(float) - BACKGROUND_BGR.astype(float)) < 45
        assert MASK.find(window.engine.timeline.find_clip(video.id),
                         mask_id).get("invert") is True

        # Clip alpha inversion is separate, explicit, and undoable through its checkbox.
        shape_track = window.engine.timeline.find_clip(shape_id).track
        lower_track.visible = False
        shape_track.visible = False
        window._request_preview()
        pump(0.15)
        alpha_before = window.engine.compose_frame(0, 1.0, with_alpha=True)[..., 3].copy()
        visible_checkbox(window, "Invert Clip Alpha").click()
        pump(0.25)
        assert ALPHA.inverted(window.engine.timeline.find_clip(video.id))
        alpha_after = window.engine.compose_frame(0, 1.0, with_alpha=True)[..., 3]
        assert np.array_equal(alpha_after, 255 - alpha_before.astype(np.uint8)), (
            "Invert Clip Alpha did not invert the finished mask alpha")
        visible_checkbox(window, "Invert Clip Alpha").click()
        pump(0.2)
        assert not ALPHA.inverted(window.engine.timeline.find_clip(video.id))
        assert np.array_equal(
            window.engine.compose_frame(0, 1.0, with_alpha=True)[..., 3], alpha_before)
        lower_track.visible = True
        shape_track.visible = True

        # The track-matte inversion switch is a separate, visible control.
        window.engine.session.set_selection({video.id})
        window._refresh_panels()
        pump(0.25)
        source_combo = next(
            box for box in window.findChildren(QComboBox)
            if box.isVisible() and box.findData(background_id) >= 0)
        source_index = source_combo.findData(background_id)
        source_combo.setCurrentIndex(source_index)
        source_combo.activated.emit(source_index)
        pump(0.3)
        assert MATTE.ensure(window.engine.timeline.find_clip(video.id))["source_id"] == background_id
        white = np.full((HEIGHT, WIDTH, 4), 255, np.uint8)
        normal_matte = window.engine._apply_matte(
            window.engine.timeline.find_clip(video.id), white.copy(), 0, 1.0)
        assert int(normal_matte[..., 3].min()) == 255
        invert_matte = visible_checkbox(window, "Invert Matte")
        invert_matte.click()
        pump(0.25)
        inverted_matte = window.engine._apply_matte(
            window.engine.timeline.find_clip(video.id), white.copy(), 0, 1.0)
        assert int(inverted_matte[..., 3].max()) == 0, (
            "Invert Matte did not invert the selected matte")
        invert_matte = visible_checkbox(window, "Invert Matte")
        invert_matte.click()
        pump(0.25)
        window._refresh_panels()
        pump(0.2)
        source_combo = next(
            box for box in window.findChildren(QComboBox)
            if box.isVisible() and box.findData(background_id) >= 0)
        source_combo.setCurrentIndex(0)
        source_combo.activated.emit(0)
        pump(0.25)

        # Hide the mask for a clean subject-isolation pass.
        window.engine.session.update_clip_mask(video.id, mask_id, {"visible": False})
        window._request_preview()
        pump(0.2)

        # -------------------------------------------------------- Roto Brush workflow
        window.engine.session.set_selection({video.id})
        window.roto_tool_button.click()
        pump(0.3)
        assert window.overlay.mode == "roto"
        bar = window.preview_panel.roto_bar
        assert not bar.isHidden()
        assert "not installed" in bar.model_button.toolTip().lower()
        bar.size_spin.setValue(7)
        subject_start = window.overlay.to_widget(
            *window.overlay.roto_to_project(window.engine.timeline.find_clip(video.id),
                                            (0.16, 0.50)))
        subject_end = window.overlay.to_widget(
            *window.overlay.roto_to_project(window.engine.timeline.find_clip(video.id),
                                            (0.23, 0.50)))
        QTest.mousePress(view, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, subject_start.toPoint())
        QTest.mouseMove(view, subject_end.toPoint())
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier, subject_end.toPoint())
        pump(0.5)
        roto_entry = next((entry for entry in window.engine.timeline.find_clip(
            video.id).effects if entry.get("effect_id") == "roto_brush"), None)
        assert roto_entry is not None and roto_entry.get("strokes")

        # Exercise the real Stop button before the full propagation. The toolbar's
        # running state is synchronous, so this is deterministic even on a fast machine.
        bar.forward_button.click()
        pump(0.02)
        assert not bar.stop_button.isHidden(), "Stop is not offered while analysis runs"
        bar.stop_button.click()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and window._roto_worker is not None:
            pump(0.04)
        assert window._roto_worker is None, "Stop did not end the Roto worker"

        bar.forward_button.click()
        progressed = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            pump(0.06)
            label = bar.status.text()
            if label and label not in progressed:
                progressed.append(label)
            if window._roto_worker is None:
                break
        assert window._roto_worker is None
        assert progressed, "the toolbar never showed progress"
        from meenifx.roto.cache import MaskCache, mask_key
        cache = MaskCache()
        roto_key = mask_key(roto_entry)
        assert len(cache.frames(window.engine.timeline.find_clip(video.id), roto_key)) >= 10

        # Foreground/background correction and re-propagation.
        bar.bg_button.click()
        pump(0.1)
        correction = window.overlay.to_widget(
            *window.overlay.roto_to_project(window.engine.timeline.find_clip(video.id),
                                            (0.03, 0.08)))
        QTest.mouseClick(view, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, correction.toPoint())
        pump(0.3)
        roto_entry = next(entry for entry in window.engine.timeline.find_clip(
            video.id).effects if entry.get("effect_id") == "roto_brush")
        assert any(stroke.get("mode") == "background" for stroke in roto_entry["strokes"])
        bar.forward_button.click()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and window._roto_worker is not None:
            pump(0.06)
        assert window._roto_worker is None

        # Freeze/unfreeze are real cache states, reached through the toolbar.
        bar.freeze_button.click()
        pump(0.2)
        assert cache.frozen(window.engine.timeline.find_clip(video.id))
        bar.freeze_button.click()
        pump(0.2)
        assert not cache.frozen(window.engine.timeline.find_clip(video.id))

        # Isolated output really removes the background and keeps the moving subject.
        _raw, isolated = window.engine.render_frame(10, 1.0)
        subject_x = 40 + 5 * 10
        assert int(isolated[90, subject_x].max()) > 150
        assert np.linalg.norm(isolated[6, 6].astype(float) -
                              BACKGROUND_BGR.astype(float)) < 45
        bar.done_button.click()
        pump(0.15)
        assert window.overlay.mode == "select"
        assert bar.isHidden()

        # --------------------------------------------------------------- media picker
        asset_one = tmp_path / "sprite ü.png"
        asset_two = tmp_path / "sprite two.png"
        ok, png_one = cv2.imencode(".png", np.full((24, 24, 3), (20, 140, 220), np.uint8))
        assert ok
        asset_one.write_bytes(png_one.tobytes())
        ok, png_two = cv2.imencode(".png", np.full((24, 24, 3), (220, 40, 80), np.uint8))
        assert ok
        asset_two.write_bytes(png_two.tobytes())
        assert asset_one.is_file() and asset_two.is_file()
        library = window.projects.project.library
        item_one = library.add(str(asset_one), 2.0, 24, 24, 30.0, media_type="image")
        item_two = library.add(str(asset_two), 2.0, 24, 24, 30.0, media_type="image")
        window.project_panel.rebuild(force=True)
        window.engine.session.set_selection({video.id})
        panel.add_effect("face_tracker")
        pump(0.3)
        row = next(entry for entry in panel._rows if entry.get("name") == "sprite_path")
        picker = row["picker"]
        media_menu = next(action.menu() for action in picker.choose.menu().actions()
                          if action.menu())
        next(action for action in media_menu.actions()
             if action.text() == item_one.name).trigger()
        pump(0.25)
        face_entry = next(entry for entry in window.engine.timeline.find_clip(
            video.id).effects if entry.get("effect_id") == "face_tracker")
        assert face_entry["params"]["sprite_path"] == str(asset_one)
        assert asset_one.name in picker.name.text()

        # Replace through the same project-media menu.
        media_menu = next(action.menu() for action in picker.choose.menu().actions()
                          if action.menu())
        next(action for action in media_menu.actions()
             if action.text() == item_two.name).trigger()
        pump(0.25)
        picker = next(entry for entry in panel._rows
                      if entry.get("name") == "sprite_path")["picker"]
        assert str(asset_two) == picker.path()

        # A cancelled file dialog leaves the current asset alone.
        browse = next(action for action in picker.choose.menu().actions()
                      if "Browse" in action.text())
        monkeypatch.setattr("meenifx.ui.media_picker.open_file",
                            lambda *args, **kwargs: "")
        browse.trigger()
        pump(0.2)
        assert picker.path() == str(asset_two)
        assert picker.last_error == ""

        # A browse result the decoder cannot use is rejected with an explanation.
        unsupported = tmp_path / "not an image.txt"
        unsupported.write_text("not an image", encoding="utf-8")
        monkeypatch.setattr("meenifx.ui.media_picker.open_file",
                            lambda *args, **kwargs: str(unsupported))
        browse = next(action for action in picker.choose.menu().actions()
                      if "Browse" in action.text())
        browse.trigger()
        pump(0.2)
        assert picker.path() == str(asset_two), (
            "an unsupported file replaced the working media selection")
        assert "unsupported" in picker.last_error.lower()

        # Missing files are explained and can be relinked; Clear empties the field.
        os.rename(asset_two, asset_two.with_suffix(".moved"))
        # Reopen the effect card the way selecting the clip again does.
        window.engine.session.set_selection(set())
        pump(0.1)
        window.engine.session.set_selection({video.id})
        window._refresh_panels()
        pump(0.3)
        picker = next(entry for entry in panel._rows
                      if entry.get("name") == "sprite_path")["picker"]
        assert "missing" in picker.name.text().lower()
        os.replace(asset_two.with_suffix(".moved"), asset_two)
        media_menu = next(action.menu() for action in picker.choose.menu().actions()
                          if action.menu())
        next(action for action in media_menu.actions()
             if action.text() == item_one.name).trigger()
        pump(0.25)
        picker = next(entry for entry in panel._rows
                      if entry.get("name") == "sprite_path")["picker"]
        picker.clear.click()
        pump(0.2)
        assert picker.path() == ""
        media_menu = next(action.menu() for action in picker.choose.menu().actions()
                          if action.menu())
        next(action for action in media_menu.actions()
             if action.text() == item_one.name).trigger()
        pump(0.2)

        # -------------------------------------------------------------------- ASCII
        panel.add_effect("ascii")
        pump(0.35)
        preset_row = next(entry for entry in panel._rows
                          if entry.get("name") == "preset" and entry.get("kind") == "choice")
        combo = preset_row["combo"]
        combo.setCurrentIndex(combo.findData("custom"))
        combo.activated.emit(combo.findData("custom"))
        pump(0.3)
        charset_row = next(entry for entry in panel._rows
                           if entry.get("name") == "charset"
                           and entry.get("kind") == "text_param")
        charset = charset_row["field"]
        charset.setText(" .·•●█")
        charset.editingFinished.emit()
        pump(0.3)
        font_row = next(entry for entry in panel._rows
                        if entry.get("name") == "font" and entry.get("kind") == "font_param")
        font_picker = font_row["picker"]
        from meenifx.processing import fonts as FONTS
        installed = FONTS.system_families()
        family = next((name for name in installed if "Courier New" in name), None)
        if family is None:
            family = next((name for name in installed if "Consolas" in name), installed[0])
        font_picker.click()
        pump(0.2)
        popup = font_picker._popup
        assert popup is not None
        popup.search.setText(family)
        pump(0.2)
        font_item = next(item for item in (popup.list.item(i)
                                           for i in range(popup.list.count()))
                         if item.text().strip() and family.casefold() in item.text().casefold())
        popup.list.itemClicked.emit(font_item)
        pump(0.35)
        ascii_entry = next(entry for entry in window.engine.timeline.find_clip(
            video.id).effects if entry.get("effect_id") == "ascii")
        assert ascii_entry["params"]["preset"] == "custom"
        assert ascii_entry["params"]["charset"] == " .·•●█"
        assert ascii_entry["params"]["font"] == family
        ascii_frame = window.engine.compose_frame(0, 1.0)
        assert ascii_frame is not None

        # --------------------------------------------------------- Dither palette UI
        panel.add_effect("dither")
        pump(0.4)
        dither_entry = next(entry for entry in window.engine.timeline.find_clip(
            video.id).effects if entry.get("effect_id") == "dither")
        original_palette = dither_entry["params"]["palette_text"]
        palette_buttons = [button for button in QApplication.allWidgets()
                           if isinstance(button, QToolButton)
                           and button.objectName() == "paletteAdd"
                           and button.property("effectId") == "dither"]
        assert palette_buttons
        add_button = palette_buttons[-1]
        add_button.click()
        pump(0.35)
        dither_entry = next(entry for entry in window.engine.timeline.find_clip(
            video.id).effects if entry.get("effect_id") == "dither")
        added = dither_entry["params"]["palette_text"]
        assert len(added.split(",")) == len(original_palette.split(",")) + 1
        algorithm_row = next(entry for entry in panel._rows
                            if entry.get("name") == "algorithm" and entry.get("kind") == "choice")
        algorithm_combo = algorithm_row["combo"]
        before_dither = window.engine.compose_frame(0, 1.0).copy()
        algorithm_combo.setCurrentIndex(algorithm_combo.findData("bayer4"))
        algorithm_combo.activated.emit(algorithm_combo.findData("bayer4"))
        pump(0.35)
        after_dither = window.engine.compose_frame(0, 1.0)
        assert np.abs(after_dither.astype(np.int16) -
                      before_dither.astype(np.int16)).mean() > 0.2

        # --------------------------------------------------------------- generators
        canvas = window.timeline_panel.canvas
        canvas.set_track_height(canvas.MIN_TRACK_H)
        for _index in range(5):
            window.engine.timeline.add_track("video")
        pump(0.2)
        lanes = [track for track in window.engine.timeline.tracks_of_type("video")
                 if track is not window.engine.timeline.find_clip(video.id).track
                 and track is not lower_track]
        assert len(lanes) >= 5
        keys = list(GENERATORS.ORDER)
        for key, lane in zip(keys, lanes[-5:]):
            mime = QMimeData()
            mime.setData(GENERATOR_MIME, key.encode("utf-8"))
            point = QPointF(canvas.x_at(30),
                            canvas.header_y(lane) + canvas.track_height(lane) / 2.0)
            event = QDropEvent(point, Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            canvas.dropEvent(event)
            pump(0.2)
        pump(0.3)
        generator_clips = {}
        for key in keys:
            matches = [clip for clip in window.engine.timeline.all_clips()
                       if clip.shape and clip.shape.get("kind") == "generator"
                       and clip.shape.get("generator") == key]
            generator_clips[key] = matches[-1] if matches else None
            assert generator_clips[key] is not None, f"{key} was not created by the drop"

        # Edit each generated clip through its inspector fields.
        panel = window.effects_panel
        window.engine.session.set_playhead(30)
        for key in keys:
            clip = generator_clips[key]
            window.engine.session.set_selection({clip.id})
            window._refresh_panels()
            pump(0.25)
            card = generator_card(panel, key)
            assert card is not None, f"{key} has no inspector settings"
            if key == "solid":
                set_scrub_field(card, "Opacity", 55)
            elif key == "gradient":
                combo = next(box for box in card.findChildren(QComboBox)
                             if box.findData("radial") >= 0)
                combo.setCurrentIndex(combo.findData("radial"))
                combo.activated.emit(combo.findData("radial"))
                pump(0.25)
            elif key == "checkerboard":
                set_scrub_field(card, "Cell size", 20)
            elif key == "fractal_noise":
                set_scrub_field(card, "Seed", 42)
            elif key == "grid":
                set_scrub_field(card, "Spacing", 20)
        stored = {key: SHAPE.ensure_shape(window.engine.timeline.find_clip(
            generator_clips[key].id)) for key in keys}
        assert float(stored["solid"]["gen_opacity"]) == 55.0
        assert stored["gradient"]["gen_type"] == "radial"
        assert float(stored["checkerboard"]["gen_size"]) == 20.0
        assert float(stored["fractal_noise"]["gen_seed"]) == 42.0
        assert float(stored["grid"]["gen_spacing"]) == 20.0
        # The clips stay independent.
        assert stored["gradient"]["gen_type"] == "radial"
        assert stored["fractal_noise"]["gen_seed"] == 42.0
        assert stored["solid"]["gen_opacity"] == 55.0
        window.engine.session.set_playhead(0)
        pump(0.2)

        # ------------------------------------------------------- save, close, reopen
        project_path = window.projects.project.path
        missing_assets = [(item.name, item.path) for item in
                          window.projects.project.library.items.values()
                          if item.path and not os.path.isfile(item.path)]
        assert not missing_assets, missing_assets
        assert window.projects.save()
        window.projects._set_clean()
        window.close()
        pump(0.3)

        reopened = create_window(qapp)
        try:
            assert reopened.projects.open_path(project_path)
            pump(0.5)
            assert reopened.engine.timeline.find_clip(video.id) is not None
            reopened_video = reopened.engine.timeline.find_clip(video.id)
            assert MASK.find(reopened_video, mask_id).get("invert") is True
            shape_clip = reopened.engine.timeline.find_clip(shape_id)
            assert shape_clip.shape.get("kind") == "path"
            assert evaluated_shape_points(reopened, shape_id)
            expected_generator = {
                "solid": ("gen_opacity", 55.0),
                "gradient": ("gen_type", "radial"),
                "checkerboard": ("gen_size", 20.0),
                "fractal_noise": ("gen_seed", 42.0),
                "grid": ("gen_spacing", 20.0),
            }
            for key in keys:
                clip = reopened.engine.timeline.find_clip(generator_clips[key].id)
                assert clip.shape.get("generator") == key
                field, value = expected_generator[key]
                assert clip.shape.get(field) == value, f"{key} lost its edited {field}"
            reopened_ascii = next(entry for entry in reopened_video.effects
                                 if entry.get("effect_id") == "ascii")
            assert reopened_ascii["params"]["preset"] == "custom"
            assert reopened_ascii["params"]["font"] == family
            reopened_face = next(entry for entry in reopened_video.effects
                                 if entry.get("effect_id") == "face_tracker")
            assert reopened_face["params"]["sprite_path"] == str(asset_one)
            assert reopened.engine.timeline.find_clip(background_id) is not None

            # --------------------------------------------------- preview/export parity
            reopened.engine.session.set_playhead(0)
            reopened._request_preview()
            pump(0.4)
            preview_bgr = reopened.engine.compose_frame(0, 1.0)
            export_png = tmp_path / "export preview.png"
            export_once(reopened, "png", export_png, start=0, end=1)
            exported_bgr = cv2.imread(str(export_png), cv2.IMREAD_COLOR)
            assert exported_bgr is not None
            mismatch = np.abs(exported_bgr.astype(np.float32)
                              - preview_bgr.astype(np.float32)).mean()
            assert mismatch < 2.0, f"PNG export differs from preview by {mismatch}"

            export_avi = tmp_path / "export sequence.avi"
            export_once(reopened, "avi", export_avi, start=0, end=26)
            capture = cv2.VideoCapture(str(export_avi))
            try:
                ok, avi_frame = capture.read()
                assert ok and avi_frame is not None
                count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            finally:
                capture.release()
            assert count == 26
            lossy_mismatch = np.abs(avi_frame.astype(np.float32)
                                    - preview_bgr.astype(np.float32)).mean()
            assert lossy_mismatch < 20.0, (
                f"AVI (lossy) differs from preview by {lossy_mismatch}")

            # Explicit PNG alpha: show only the target layer, invert the mask, and
            # verify transparent, opaque and partially transparent pixels.
            mask = MASK.find(reopened_video, mask_id)
            reopened.engine.session.update_clip_mask(
                reopened_video.id, mask_id, {"visible": True, "invert": True})
            for track in reopened.engine.timeline.tracks_of_type("video"):
                if track is not reopened_video.track:
                    track.visible = False
            for entry in reopened_video.effects:
                if entry.get("effect_id") == "ascii":
                    entry["enabled"] = False
            reopened._request_preview()
            pump(0.3)
            alpha_preview = reopened.engine.compose_frame(0, 1.0, with_alpha=True)
            export_alpha = tmp_path / "export alpha.png"
            export_once(reopened, "png", export_alpha, alpha=True, start=0, end=1)
            alpha_exported = cv2.imread(str(export_alpha), cv2.IMREAD_UNCHANGED)
            assert alpha_exported is not None and alpha_exported.shape[2] == 4
            alpha = alpha_exported[..., 3]
            assert int(alpha.min()) == 0, "the inverted mask did not make a hole"
            assert int(alpha.max()) >= 240, "the retained subject has no opaque pixels"
            assert np.any((alpha > 0) & (alpha < 255)), "there are no partial alpha edges"
            alpha_diff = np.abs(alpha_exported[..., :3].astype(np.float32)
                                - alpha_preview[..., :3].astype(np.float32)).mean()
            assert alpha_diff < 2.0, (
                f"alpha PNG preview/export mismatch: {alpha_diff}")

            # A legacy Smart Key clip can be handed to Roto Brush without changing the
            # cutout it already renders. Build the same disk cache Smart Key analysis
            # writes, open the legacy card's explicit route, and compare the frame
            # before/after migration. This runs with the model deliberately disabled.
            from meenifx.roto.cache import MaskCache, mask_key, mask_size

            legacy_track = reopened.engine.timeline.add_track("video")
            legacy = reopened.engine.timeline.new_clip(
                "Legacy Smart Key", reopened_video.source_path, legacy_track,
                0, 26, source_out=26, source_fps=30.0)
            legacy_cache = MaskCache(scope="smart_key")
            legacy_params = dict(reopened.engine.runtime_effects["smart_key"].capture_params())
            legacy_params.update({"feather": 0.0, "smooth": 0.0, "choke": 0.0,
                                  "edge_contrast": 0.0, "invert": False,
                                  "matte_view": "composite", "subject": "auto",
                                  "quality": "balanced"})
            legacy_entry = {"effect_id": "smart_key", "enabled": True,
                            "params": legacy_params}
            legacy_key = mask_key(legacy_entry)
            legacy_size = mask_size(WIDTH, HEIGHT)
            yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
            legacy_cache.configure(legacy, legacy_key, legacy_size)
            for source_frame in range(26):
                # Move the synthetic subject so every cached frame is distinct.
                centre_x = 150 + source_frame
                matte = ((((xx - centre_x) / 95.0) ** 2
                          + ((yy - 90) / 55.0) ** 2) <= 1.0).astype(np.float32)
                legacy_cache.store(legacy, source_frame, matte,
                                   legacy_key, legacy_size)
            legacy_entry["cache"] = {"key": legacy_key, "frames": 26,
                                     "scope": "smart_key"}
            legacy_entry["analysis"] = {"seed": 0, "range": [0, 25],
                                        "analysed": list(range(26))}
            legacy.effects = [legacy_entry]
            visibility = {track: track.visible
                          for track in reopened.engine.timeline.tracks_of_type("video")}
            try:
                for track in visibility:
                    track.visible = track is legacy_track
                reopened.engine.session.set_selection({legacy.id})
                reopened._refresh_panels()
                pump(0.3)
                probe_frames = (0, 12, 25)
                before_migration = {frame: reopened.engine.compose_frame(
                    frame, 1.0, with_alpha=True) for frame in probe_frames}
                assert reopened.effects_panel._smart_key is not None
                refine = reopened.effects_panel._smart_key["refine"]
                refine.click()
                pump(0.35)

                migrated = reopened.engine.timeline.find_clip(legacy.id)
                old_entry = next(item for item in migrated.effects
                                 if item.get("effect_id") == "smart_key")
                roto_entry = next(item for item in migrated.effects
                                  if item.get("effect_id") == "roto_brush")
                assert old_entry["enabled"] is False
                assert roto_entry["enabled"] is True
                assert roto_entry["cache"]["frames"] == 26
                assert MaskCache().frames(migrated, mask_key(roto_entry)) == list(range(26))
                reopened._invalidate_preview_cache()
                for frame in probe_frames:
                    after_migration = reopened.engine.compose_frame(
                        frame, 1.0, with_alpha=True)
                    assert np.array_equal(before_migration[frame], after_migration), (
                        f"opening legacy Smart Key in Roto Brush changed its cutout "
                        f"at frame {frame}")
                assert reopened.overlay.mode == "roto"
                bar = reopened.preview_panel.roto_bar
                assert not bar.isHidden()

                # The compatibility entry is a real Roto workspace: a foreground stroke
                # can now refine it, and the legacy node remains in the project.
                start_point = reopened.overlay.to_widget(
                    *reopened.overlay.roto_to_project(migrated, (0.50, 0.50)))
                end_point = reopened.overlay.to_widget(
                    *reopened.overlay.roto_to_project(migrated, (0.62, 0.50)))
                QTest.mousePress(reopened.preview_panel.view, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, start_point.toPoint())
                QTest.mouseMove(reopened.preview_panel.view, end_point.toPoint())
                QTest.mouseRelease(reopened.preview_panel.view, Qt.MouseButton.LeftButton,
                                   Qt.KeyboardModifier.NoModifier, end_point.toPoint())
                pump(0.35)
                refined = next(item for item in reopened.engine.timeline.find_clip(
                    legacy.id).effects if item.get("effect_id") == "roto_brush")
                assert refined.get("strokes"), "the legacy clip was not editable in Roto"
                bar.done_button.click()
                pump(0.15)
            finally:
                for track, visible in visibility.items():
                    track.visible = visible
        finally:
            reopened.projects._set_clean()
            reopened.close()
            pump(0.2)
    finally:
        if window.isVisible():
            window.projects._set_clean()
            window.close()
            pump(0.2)
