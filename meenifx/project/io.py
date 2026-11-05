"""Glue between the engine and the project container: build a ProjectData from a live editor and apply one back."""

import os

import cv2

from ..media.cache import entry_size

from . import container as C
from .convert import convert_timeline_fps
from .media import MediaLibrary, media_type_for
from .serialize import sections_to_timeline, timeline_to_sections
from .settings import ProjectSettings

THUMB_WIDTH = 320


class Project:
    """What the editor knows about the open project besides the timeline itself."""

    def __init__(self, settings=None, path=None):
        self.settings = settings or ProjectSettings()
        self.path = path
        self.library = MediaLibrary()
        self.ui = {}
        self.window_state = None
        self.thumbnails = {}   # entry name -> bytes, carried over between saves

    @property
    def name(self):
        return self.settings.name

    @property
    def folder(self):
        return os.path.dirname(self.path) if self.path else None


def encode_thumb(frame_bgr, width=THUMB_WIDTH):
    """JPEG bytes of a frame scaled to `width`, or None."""
    if frame_bgr is None:
        return None
    h, w = frame_bgr.shape[:2]
    small = cv2.resize(frame_bgr, (width, max(1, int(round(h * width / w)))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


def register_media(engine, library, path, audio_duration=None):
    """Add `path` to the library with whatever is known about it without decoding anything new.

    An existing item whose details are still blank is filled in once the engine has decoded the file.
    """
    existing = library.by_path(path)
    if existing is not None and (existing.width or existing.duration or existing.type == "audio"):
        return existing
    kind = media_type_for(path)
    duration = width = height = fps = 0
    entry = engine.media._entries.get(path)
    if entry is not None and kind != "audio":
        fps = float(entry.fps or 0)
        duration = 0.0 if entry.kind == "image" else entry.frame_count / max(1e-6, entry.fps)
        size = entry_size(entry)
        if size:
            width, height = size
    if kind == "audio" and audio_duration:
        duration = float(audio_duration(path) or 0)
    if existing is not None:
        if width:
            existing.width, existing.height, existing.fps, existing.duration = width, height, fps, duration
        elif duration:
            existing.duration = duration
        return existing
    return library.add(path, duration, width, height, fps, media_type=kind)


def collect(engine, project, ui=None, window_state=None, thumb=None, audio_duration=None):
    """Snapshot the live editor into a ProjectData (no I/O)."""
    timeline, library = engine.timeline, project.library
    folder = project.folder
    sections = timeline_to_sections(
        timeline, lambda p: register_media(engine, library, p, audio_duration).id)
    data = C.ProjectData()
    for key, value in sections.items():
        data[key] = value
    data["settings"] = project.settings.to_dict()
    data["media"] = library.to_dict(folder)
    clip_count = sum(len(t.clips) for t in timeline.tracks)
    data["meta"] = C.make_meta(project.settings.name, project.settings, timeline.duration, clip_count)
    data["ui"] = ui if ui is not None else project.ui
    data.window_state = window_state if window_state is not None else project.window_state
    data.thumbnails = dict(project.thumbnails)
    if thumb:
        data.thumbnails[C.PROJECT_THUMB] = thumb
    return data


def apply(engine, data, path):
    """Install a loaded ProjectData into the engine. Returns (Project, missing MediaItems)."""
    settings = ProjectSettings.from_dict(data["settings"])
    folder = os.path.dirname(os.path.abspath(path)) if path else None
    library = MediaLibrary.from_dict(data["media"], folder)

    def path_for(media_id):
        item = library.items.get(media_id)
        return item.path if item else ""

    timeline = sections_to_timeline(data.sections, settings.nominal_fps, path_for)
    engine.reset_media()
    engine.timeline.adopt(timeline)
    engine.apply_project_settings(settings, folder)
    # Older projects may carry a picture effect on an audio clip (or the reverse). Keep
    # the entry and its settings but switch it off, so opening never crashes or renders it.
    engine.sanitize_effects()
    # And an animation that is not a curve (an older build could keyframe a colour
    # preset) is flattened to its first value, so the curve editor never sees one.
    engine.sanitize_animation()
    project = Project(settings, path)
    project.library = library
    project.ui = data["ui"] or {}
    project.window_state = data.window_state
    project.thumbnails = dict(data.thumbnails)
    missing = library.missing()
    engine.missing_paths = {i.path for i in missing}
    return project, missing


def change_settings(engine, new_settings):
    """Switch resolution/frame rate; clip times are converted so real durations stay the same."""
    old_fps = engine.timeline.fps
    new_settings.validate()
    if new_settings.nominal_fps != old_fps:
        convert_timeline_fps(engine.timeline, new_settings.nominal_fps)
    engine.apply_project_settings(new_settings)
