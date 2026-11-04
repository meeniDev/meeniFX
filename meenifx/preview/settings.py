"""Persisted playback/performance preferences (Playback & Performance section)."""

from __future__ import annotations

from ..ui.layout import app_settings

GROUP = "playback"

DEFAULTS = {
    "gpu_mode": "auto",            # auto | on | off
    "quality_mode": "Auto",        # Auto | Full | 1/2 | 1/4 | 1/8
    "ram_cache_mb": 1024,
    "disk_cache_mb": 4096,
    "cache_folder": "",            # empty = <project>/Cache/preview
    "background_render": True,
    "auto_proxy": True,
    "proxy_width": 1280,
    "hardware_decode": "auto",     # auto | on | off
    "hardware_encode": "auto",
    "show_overlay": False,
    "prefetch_seconds": 4,          # idle prerender window ahead of the playhead
    "gain_headroom_db": 12,         # safe boost ceiling for the audio gain control
    # Which waveform the timeline draws: the file as decoded, or the clip after its gain,
    # fades and audio effect chain - that is, the sound that actually plays.
    "waveform_mode": "Processed",
    # Background caching is routine; announcing every finished batch interrupts editing, so
    # the cache state is shown by the timeline indicator instead. Failures still surface.
    "cache_notifications": False,
}

QUALITY_MODES = ("Auto", "Full", "1/2", "1/4", "1/8")
TRINARY = ("auto", "on", "off")
WAVEFORM_MODES = ("Processed", "Source")


def _clamp_int(value, low, high, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


class PlaybackSettings:
    """Small validated record; ``save()`` writes it to the app's INI file."""

    def __init__(self, values=None):
        data = dict(DEFAULTS)
        data.update(values or {})
        self.gpu_mode = data["gpu_mode"] if data["gpu_mode"] in TRINARY else "auto"
        self.quality_mode = (data["quality_mode"] if data["quality_mode"] in QUALITY_MODES
                             else "Auto")
        self.ram_cache_mb = _clamp_int(data["ram_cache_mb"], 64, 16384, 1024)
        self.disk_cache_mb = _clamp_int(data["disk_cache_mb"], 0, 102400, 4096)
        self.cache_folder = str(data["cache_folder"] or "").strip()
        self.background_render = bool(data["background_render"])
        self.auto_proxy = bool(data["auto_proxy"])
        self.proxy_width = _clamp_int(data["proxy_width"], 320, 3840, 1280)
        self.hardware_decode = (data["hardware_decode"]
                                if data["hardware_decode"] in TRINARY else "auto")
        self.hardware_encode = (data["hardware_encode"]
                                if data["hardware_encode"] in TRINARY else "auto")
        self.show_overlay = bool(data["show_overlay"])
        self.prefetch_seconds = _clamp_int(data["prefetch_seconds"], 0, 120, 4)
        # The gain ceiling is a real limit used by the mixer and the envelopes, not a
        # cosmetic slider stop; 0 dB means "no boost", 24 dB is the safe maximum.
        self.gain_headroom_db = _clamp_int(data["gain_headroom_db"], 0, 24, 12)
        mode = str(data.get("waveform_mode") or "Processed")
        self.waveform_mode = mode if mode in WAVEFORM_MODES else "Processed"
        self.cache_notifications = bool(data["cache_notifications"])

    # ------------------------------------------------------------------ storage
    @classmethod
    def load(cls):
        settings = app_settings()
        settings.beginGroup(GROUP)
        values = {}
        for key, default in DEFAULTS.items():
            raw = settings.value(key, default)
            if isinstance(default, bool):
                if isinstance(raw, str):
                    raw = raw.strip().lower() in ("1", "true", "yes", "on")
                values[key] = bool(raw)
            elif isinstance(default, int):
                values[key] = _clamp_int(raw, 0, 1 << 20, default)
            else:
                values[key] = raw
        settings.endGroup()
        return cls(values)

    def save(self):
        settings = app_settings()
        settings.beginGroup(GROUP)
        for key in DEFAULTS:
            settings.setValue(key, getattr(self, key))
        settings.endGroup()
        settings.sync()

    # -------------------------------------------------------------------- utils
    def as_dict(self):
        return {key: getattr(self, key) for key in DEFAULTS}

    def copy(self):
        return PlaybackSettings(self.as_dict())

    def __repr__(self):
        return "PlaybackSettings(" + ", ".join(f"{k}={v!r}" for k, v in self.as_dict().items()) + ")"

    # The quality combo's label maps to a render scale; Auto is decided at runtime.
    @staticmethod
    def quality_scale(label):
        from .cache import scale_key  # noqa: F401  (kept for symmetry with the cache)
        return {"Full": 1.0, "1/2": 0.5, "1/4": 0.25, "1/8": 0.125}.get(label)
