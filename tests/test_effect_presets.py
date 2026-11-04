"""Effect presets: whole-clip, undoable, no accidental keyframes, readable labels.

The old behaviour wrote the preset string straight into the model: no repaint, no undo
step, no dirty flag, and the dropdown showed raw keys. These tests drive the real widgets
and the real undo stack, so a regression shows up as a user would see it.
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QComboBox  # noqa: E402

from meenifx.engine import EditorEngine  # noqa: E402
from meenifx.model import channels as CH  # noqa: E402
from meenifx.model.keyframes import AnimatedValue  # noqa: E402
from meenifx.project.media import media_type_for  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STILL = os.path.join(ROOT, "mosaic-output.png")


def pump(seconds=0.08):
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
    settings = ProjectSettings(name=f"Presets {uuid.uuid4().hex[:6]}", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, str(tmp_path))
    window.engine.session.set_selection(set())
    pump(0.05)
    library = window.projects.project.library
    item = library.add(STILL, 3.0, 640, 360, 30.0, media_type=media_type_for(STILL))
    window.project_panel.rebuild(force=True)
    ids = window.insert_media_items([item], frame=0)
    assert ids
    clip = window.engine.timeline.find_clip(ids[0])
    window.engine.session.set_selection({clip.id})
    pump(0.05)
    window.effects_panel.add_effect("color_correct")
    pump(0.05)
    return window, clip


def preset_combo(window):
    """The live preset combo: the row list the panel actually updates."""
    for row in getattr(window.effects_panel, "_rows", ()):
        if row.get("kind") == "choice" and row.get("name") == "preset":
            return row["combo"]
    for combo in window.effects_panel.findChildren(QComboBox):
        if combo.count() and combo.itemData(0) == "none":
            return combo
    return None


def entry_of(clip):
    return next(item for item in clip.effects if item["effect_id"] == "color_correct")


def reread(window, clip_id, effect_id):
    """The live clip after an undo: restoring a snapshot replaces the Clip objects."""
    clip = window.engine.timeline.find_clip(clip_id)
    return clip, next(item for item in clip.effects if item["effect_id"] == effect_id)


def choose(combo, key):
    """Choose a preset the way a user does: pick the row, then let Qt report it."""
    for index in range(combo.count()):
        if combo.itemData(index) == key:
            combo.setCurrentIndex(index)
            combo.activated.emit(index)
            return index
    raise AssertionError(f"{key} is not in the preset list")


def undo_count(window):
    return window.qt_undo_stack.undo_stack.count()


def test_the_dropdown_shows_labels_and_keeps_keys(editor):
    window, clip = editor
    combo = preset_combo(window)
    assert combo is not None
    texts = [combo.itemText(index) for index in range(combo.count())]
    assert "Black and White" in texts and "Cinematic Contrast" in texts
    assert not any("_" in text for text in texts), f"raw keys leaked into the UI: {texts}"
    assert combo.currentData() == "none", "a new effect starts on the neutral preset"


def test_choosing_a_preset_is_one_undo_step_and_repaints(editor):
    window, clip = editor
    combo = preset_combo(window)
    before = undo_count(window)
    changes = []
    window.engine.session.changed.connect(lambda: changes.append(1))
    choose(combo, "vintage")
    pump(0.05)
    assert entry_of(clip)["params"]["preset"] == "vintage"
    assert undo_count(window) == before + 1, "a preset change must be undoable in one step"
    assert changes, "the change must notify the model so the preview and title update"
    assert window.projects.is_dirty()


def test_a_preset_never_creates_keyframes(editor):
    window, clip = editor
    window.engine.session.auto_keyframe.set(True)      # even with auto-key on
    try:
        choose(preset_combo(window), "cinematic")
        pump(0.05)
    finally:
        window.engine.session.auto_keyframe.set(False)
    assert not isinstance(entry_of(clip)["params"]["preset"], AnimatedValue)
    animated = [row["id"] for row in CH.list_channels(clip, window.engine.runtime_effects)
                if row["animated"]]
    assert "fx.color_correct.preset" not in animated
    assert animated == [], "choosing a preset must not key any parameter"


def test_undo_restores_the_previous_preset_and_the_dropdown_follows(editor):
    window, clip = editor
    combo = preset_combo(window)
    choose(combo, "warm")
    pump(0.05)
    choose(combo, "cool")
    pump(0.05)
    assert entry_of(clip)["params"]["preset"] == "cool"
    window.engine.session.undo()
    pump(0.05)
    clip, entry = reread(window, clip.id, "color_correct")
    assert entry["params"]["preset"] == "warm"
    window.engine.session.set_selection({clip.id})
    window.effects_panel.refresh_properties()
    pump(0.05)
    assert preset_combo(window).currentData() == "warm", "the combo must show the real state"


def test_an_existing_curve_survives_a_preset_change(editor):
    window, clip = editor
    CH.set_value(clip, CH.effect_channel("color_correct", "saturation"), 10.0, 0, auto_key=True)
    CH.set_value(clip, CH.effect_channel("color_correct", "saturation"), 80.0, 24, auto_key=True)
    window.engine.session.changed.emit()
    pump(0.05)
    choose(preset_combo(window), "high_contrast")
    pump(0.05)
    stored = entry_of(clip)["params"]["saturation"]
    assert isinstance(stored, AnimatedValue), "the animation must not be flattened"
    assert len(stored.keyframes) == 2
    assert entry_of(clip)["params"]["preset"] == "high_contrast"


def test_choosing_the_same_preset_twice_adds_no_undo_step(editor):
    window, clip = editor
    combo = preset_combo(window)
    choose(combo, "cool")
    pump(0.05)
    before = undo_count(window)
    choose(combo, "cool")
    pump(0.05)
    assert undo_count(window) == before


def test_the_active_preset_is_visible_when_the_card_is_collapsed(editor):
    window, clip = editor
    choose(preset_combo(window), "vintage")
    pump(0.05)
    badges = [label for label in window.effects_panel.findChildren(type(window.effects_panel.findChild(object))) ]
    del badges
    from PyQt6.QtWidgets import QLabel
    texts = [label.text() for label in window.effects_panel.findChildren(QLabel)
             if label.objectName() == "presetBadge" and label.isVisible()
             and not label.isHidden()]
    assert "Vintage" in texts, f"the header should name the active preset, got {texts}"


def test_a_legacy_preset_value_falls_back_to_a_valid_choice(editor):
    window, clip = editor
    entry_of(clip)["params"]["preset"] = "some_old_grade"
    window.effects_panel.refresh_properties()
    pump(0.05)
    combo = preset_combo(window)
    assert combo.currentData() == "none", "an unknown key must not be shown as a choice"


def test_every_choice_parameter_has_readable_labels():
    labels = {}
    for effect_class in EditorEngine.EFFECT_TYPES:
        for spec in effect_class.PARAMS:
            if spec.kind != "choice":
                continue
            items = spec.choice_items()
            assert items, f"{effect_class.key}.{spec.name} has no choices"
            for key, label in items:
                assert label and label != key or "_" not in str(key), (
                    f"{effect_class.key}.{spec.name}: {key} has no readable label")
                assert "_" not in label, f"{effect_class.key}.{spec.name}: {label} looks like a key"
            labels[(effect_class.key, spec.name)] = items
    assert ("color_correct", "preset") in labels


def test_bool_and_text_parameters_are_undoable_too(editor):
    window, clip = editor
    window.effects_panel.add_effect("gaussian_blur")
    pump(0.05)
    blur = next(item for item in clip.effects if item["effect_id"] == "gaussian_blur")
    before = undo_count(window)
    window.effects_panel._commit_choice(clip, blur, "keep_edges", True,
                                        next(spec for spec in window.engine.runtime_effects[
                                            "gaussian_blur"].param_specs()
                                             if spec.name == "keep_edges"))
    pump(0.05)
    assert blur["params"]["keep_edges"] is True
    assert undo_count(window) == before + 1
    window.engine.session.undo()
    pump(0.05)
    _clip, blur = reread(window, clip.id, "gaussian_blur")
    assert blur["params"]["keep_edges"] is False
