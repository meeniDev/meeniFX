"""Timeline and preview preferences persisted in the app's INI file.

These are application preferences, not project settings: they describe how the editor
behaves on this machine, so opening another project never changes the snap feel.
"""

from __future__ import annotations

from .layout import app_settings

GROUP = "timeline"

DEFAULTS = {
    # VEGAS-style automatic transition when one clip is dropped over another.
    "auto_overlap_transitions": True,
    # Timeline snapping.
    "snap_timeline": True,
    "timeline_snap_px": 8,
    # Preview transform snapping.
    "snap_preview": True,
    "preview_snap_px": 7,
    "show_snap_guides": True,
    "snap_edges": True,
    "snap_center": True,
    "snap_objects": True,
}

BOOL_KEYS = ("auto_overlap_transitions", "snap_timeline", "snap_preview",
             "show_snap_guides", "snap_edges", "snap_center", "snap_objects")
INT_KEYS = {"timeline_snap_px": (1, 40), "preview_snap_px": (1, 40)}


def _as_bool(value, fallback=True):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return fallback
    return bool(value)


def _as_int(value, fallback, bounds=(1, 40)):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = int(fallback)
    return max(bounds[0], min(bounds[1], number))


class TimelineSettings:
    """Small validated record; ``save()`` writes it to the app's INI file."""

    def __init__(self, values=None):
        data = dict(DEFAULTS)
        data.update(values or {})
        for name in BOOL_KEYS:
            setattr(self, name, _as_bool(data.get(name), DEFAULTS[name]))
        for name, bounds in INT_KEYS.items():
            setattr(self, name, _as_int(data.get(name), DEFAULTS[name], bounds))
        # Be forgiving with callers that used the old attribute spelling.
        self.snap_timeline_threshold = self.timeline_snap_px
        self.snap_preview_threshold = self.preview_snap_px

    @classmethod
    def load(cls):
        settings = app_settings()
        values = {}
        settings.beginGroup(GROUP)
        for name, default in DEFAULTS.items():
            values[name] = settings.value(name, default)
        settings.endGroup()
        return cls(values)

    def save(self):
        settings = app_settings()
        settings.beginGroup(GROUP)
        for name in DEFAULTS:
            settings.setValue(name, getattr(self, name))
        settings.endGroup()
        settings.sync()

    def as_dict(self):
        return {key: getattr(self, key) for key in DEFAULTS}

    def copy(self):
        return TimelineSettings(self.as_dict())
