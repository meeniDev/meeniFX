"""Text generator: rendering, text keyframes, transform, effects and persistence."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import build_engine, synthetic_frame  # noqa: E402

from meenifx.model import channels as CH  # noqa: E402
from meenifx.model import text as TXT  # noqa: E402
from meenifx.model import transform as T  # noqa: E402
from meenifx.model.keyframes import AnimatedValue, Keyframe  # noqa: E402


def text_engine(text="HELLO", start=200, duration=90):
    """An engine whose frame 200.. holds one text clip and nothing else."""
    engine, clip = build_engine()
    engine.timeline.find_clip(clip.id).duration = 150
    track = engine.timeline.add_track("video")
    engine.session.set_playhead(start)
    text_id = engine.session.add_text_clip(text, start=start, duration=duration, track=track)
    assert text_id is not None
    return engine, engine.timeline.find_clip(text_id)


def render(engine, frame, scale=1.0):
    return engine.render_frame(frame, scale)[1]


def bright_pixels(frame, threshold=200):
    grey = frame[:, :, :3].astype(np.int16).sum(axis=2)
    return np.argwhere(grey > threshold * 3)


def test_text_clip_renders_into_the_frame():
    engine, clip = text_engine()
    frame = render(engine, 210)
    assert frame is not None
    pixels = bright_pixels(frame)
    assert len(pixels) > 200, "the text should paint a lot of near-white pixels"
    centre = pixels.mean(axis=0)
    height, width = frame.shape[:2]
    assert abs(centre[1] - width / 2) < width * 0.2
    assert abs(centre[0] - height / 2) < height * 0.2


def test_empty_text_draws_nothing():
    engine, clip = text_engine(text="   ")
    frame = render(engine, 210)
    # Only the media clip underneath is visible: no extra white glyph pixels.
    assert len(bright_pixels(frame)) == 0


def test_text_keyframes_change_the_words_over_time():
    engine, clip = text_engine(text="ONE")
    TXT.set_content(clip, "ONE", 0, True)
    TXT.set_content(clip, "A MUCH LONGER SECOND TITLE", 45, True)
    assert TXT.content_keys(clip) == [0, 45]
    assert TXT.content_at(clip, 0) == "ONE"
    assert TXT.content_at(clip, 44) == "ONE"
    assert TXT.content_at(clip, 45).startswith("A MUCH")
    early = render(engine, 200 + 10)
    late = render(engine, 200 + 60)
    diff = float(np.abs(early.astype(np.int16) - late.astype(np.int16)).mean())
    assert diff > 2.0, "the keyed words should change the picture"
    assert len(bright_pixels(late)) > len(bright_pixels(early))
    # Moving a text key and deleting one keeps the model consistent.
    assert TXT.move_content_key(clip, 45, 60) == 60
    assert TXT.content_keys(clip) == [0, 60]
    assert TXT.content_at(clip, 50) == "ONE"
    assert TXT.toggle_content_key(clip, 60) is False
    assert TXT.content_keys(clip) == [0]


def test_text_channels_are_keyframeable_through_the_channel_layer():
    engine, clip = text_engine()
    size_channel = CH.text_channel("size")
    assert CH.is_animated(clip, size_channel) is False
    CH.set_value(clip, size_channel, 40.0, 0, True)
    CH.set_value(clip, size_channel, 160.0, 60, True)
    assert CH.key_times(clip, size_channel) == [0, 60]
    small = render(engine, 200 + 5)
    big = render(engine, 200 + 55)
    assert len(bright_pixels(big)) > len(bright_pixels(small)) * 1.5
    rows = [row for row in CH.list_channels(clip) if row["id"] == size_channel]
    assert rows and rows[0]["animated"] is True


def test_text_follows_the_transform_channels():
    engine, clip = text_engine()
    T.set_channel(clip, "position_x", 0.0)
    baseline = render(engine, 210)
    base_centre = bright_pixels(baseline).mean(axis=0)[1]
    T.set_channel(clip, "position_x", -80.0)
    moved = render(engine, 210)
    moved_centre = bright_pixels(moved).mean(axis=0)[1]
    assert moved_centre < base_centre - 20
    T.set_channel(clip, "scale_x", 50.0)
    T.set_channel(clip, "scale_y", 50.0)
    scaled = render(engine, 210)
    assert len(bright_pixels(scaled)) < len(bright_pixels(moved))


def test_effects_run_on_text_clips():
    engine, clip = text_engine()
    plain = render(engine, 210)
    clip.effects.append({"effect_id": "mosaic", "enabled": True, "params": {"tile": 12}})
    blocky = render(engine, 210)
    assert plain.shape == blocky.shape
    assert float(np.abs(plain.astype(np.int16) - blocky.astype(np.int16)).mean()) > 0.2


def test_text_clip_split_keeps_words_and_keys():
    engine, clip = text_engine()
    TXT.set_content(clip, "FIRST", 0, True)
    TXT.set_content(clip, "SECOND", 60, True)
    right_id = engine.timeline.split_clip(clip.id, 200 + 60)
    assert right_id is not None
    left = engine.timeline.find_clip(clip.id)
    right = engine.timeline.find_clip(right_id)
    assert left.duration == 60 and right.duration == 30
    assert TXT.content_at(left, 0) == "FIRST"
    assert TXT.content_at(left, 59) == "FIRST"
    assert TXT.content_at(right, 0) == "SECOND"
    assert right.is_text and left.is_text


def test_text_clip_round_trips_through_the_project_file(tmp_path):
    from meenifx.project import ProjectSettings
    from meenifx.project import container as C
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = text_engine(text="SAVED")
    TXT.set_content(clip, "SAVED", 0, True)
    TXT.set_content(clip, "AGAIN", 30, True)
    clip.text["size"] = AnimatedValue([Keyframe(0, 30.0), Keyframe(20, 120.0)], 30.0)
    clip.text["font"] = "Georgia"
    clip.text["color"] = "#22CC88"
    T.set_channel(clip, "rotation", 12.0)

    path = str(tmp_path / "text.mfxpf")
    settings = ProjectSettings.from_preset("HD 720p 30", "T")
    data = C.ProjectData()
    data["settings"] = settings.to_dict()
    for key, value in timeline_to_sections(engine.timeline, lambda p: "").items():
        data[key] = value
    data["media"] = {"items": {}, "next_id": 1}
    data["meta"] = C.make_meta("T", settings, engine.timeline.duration, 0)
    data["ui"] = {}
    C.write_container(path, data)

    restored = C.read_container(path)
    timeline = sections_to_timeline(restored.sections, 30, lambda media_id: "")
    loaded = next(c for c in timeline.all_clips() if c.is_text)
    assert TXT.content_keys(loaded) == [0, 30]
    assert TXT.content_at(loaded, 0) == "SAVED"
    assert TXT.content_at(loaded, 45) == "AGAIN"
    assert loaded.text["font"] == "Georgia"
    assert loaded.text["color"] == "#22CC88"
    assert isinstance(loaded.text["size"], AnimatedValue)
    assert loaded.text["size"].value_at(20) == pytest.approx(120.0)
    assert T.evaluate(loaded, 0).rotation == pytest.approx(12.0)


def test_old_projects_without_text_still_load(tmp_path):
    """A format-3 project has no text column and must open unchanged."""
    from meenifx.project import container as C
    from meenifx.project.serialize import sections_to_timeline, timeline_to_sections

    engine, clip = build_engine()
    sections = timeline_to_sections(engine.timeline, lambda p: "")
    sections["clips"].pop("text", None)
    sections["keyframes"].pop("kinds", None)
    migrated = C.migrate(sections, 3)
    assert migrated["clips"]["text"] == [None]
    timeline = sections_to_timeline(migrated, 30, lambda media_id: "")
    assert len(timeline.all_clips()) == 1
    assert not any(c.is_text for c in timeline.all_clips())
