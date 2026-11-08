"""Timeline preferences persisted in the app's INI file (Preferences > Timeline)."""

from __future__ import annotations

from .layout import app_settings

GROUP = "timeline"

DEFAULTS = {
    # VEGAS-style automatic transition when one clip is dropped over another.
    "auto_overlap_transitions": True,
}


def _as_bool(value, fallback=True):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return fallback
    return bool(value)


class TimelineSettings:
    """Small validated record; ``save()`` writes it to the app's INI file."""

    def __init__(self, values=None):
        data = dict(DEFAULTS)
        data.update(values or {})
        self.auto_overlap_transitions = _as_bool(data["auto_overlap_transitions"])

    @classmethod
    def load(cls):
        settings = app_settings()
        settings.beginGroup(GROUP)
        value = settings.value("auto_overlap_transitions",
                               DEFAULTS["auto_overlap_transitions"])
        settings.endGroup()
        return cls({"auto_overlap_transitions": _as_bool(value)})

    def save(self):
        settings = app_settings()
        settings.beginGroup(GROUP)
        settings.setValue("auto_overlap_transitions", self.auto_overlap_transitions)
        settings.endGroup()
        settings.sync()

    def as_dict(self):
        return {key: getattr(self, key) for key in DEFAULTS}

    def copy(self):
        return TimelineSettings(self.as_dict())
