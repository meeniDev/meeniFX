"""Recorder preferences persisted in QSettings."""

from dataclasses import dataclass, fields

from PyQt6.QtCore import QSettings

GROUP = "recorder"

# One setting drives bitrate headroom, x264 speed and the equivalent NVENC preset. The
# factors are relative to width*height*fps; "balanced" keeps the previous 0.20 screen
# default, while performance halves the encoder work and high quality spends more bits.
QUALITY_PRESETS = {
    "performance": {"label": "Performance", "screen": 0.10, "camera": 0.08,
                    "x264": "ultrafast", "nvenc": "p1"},
    "balanced": {"label": "Balanced", "screen": 0.20, "camera": 0.12,
                 "x264": "veryfast", "nvenc": "p4"},
    "high": {"label": "High Quality", "screen": 0.30, "camera": 0.18,
             "x264": "medium", "nvenc": "p6"},
}
QUALITY_KEYS = tuple(QUALITY_PRESETS)


def _store():
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "meeniFX", "Studios")


@dataclass
class RecorderSettings:
    webcam_on: bool = True
    screen_on: bool = False
    mic_on: bool = True
    camera_id: str = ""
    camera_format: str = ""          # "WxH@fps"
    screen_mode: str = "screen"      # screen | window
    screen_name: str = ""
    # Qt exposes a capturable window only by its description, so the title is the
    # handle the recorder can save and the only thing it can match on reload.
    window_title: str = ""
    show_cursor: bool = True
    mic_id: str = ""
    mic_volume: int = 100            # percent
    system_on: bool = True           # record what the speakers play (WASAPI loopback)
    system_id: str = ""
    system_volume: int = 100         # percent
    audio_rate: int = 48000          # sample rate of the recorded WAVs
    audio_channels: int = 2          # 1 = mono, 2 = stereo
    # Off by default: the microphone and the system sound are inserted as two separate,
    # independently editable clips. Turning this on mixes them into the video instead, which
    # is convenient for handing a clip to someone else but loses the separate sources.
    mux_audio: bool = False
    countdown: bool = True
    voice_over: bool = False
    folder: str = ""                 # empty: the project's Recordings folder
    video_bitrate_mbps: int = 0      # 0 = automatic
    quality_preset: str = "balanced"  # performance | balanced | high
    hotkey_stop: str = "Ctrl+Shift+R"
    hotkey_pause: str = "Ctrl+Shift+P"
    minimize_while_recording: bool = True
    # Off by default: the window itself shows the recording time and the stop control, so
    # no always-on-top window sits over other applications unless the user asks for one.
    floating_bar: bool = False

    def __post_init__(self):
        if self.quality_preset not in QUALITY_PRESETS:
            self.quality_preset = "balanced"

    @classmethod
    def load(cls, store=None):
        store = store or _store()
        out = cls()
        store.beginGroup(GROUP)
        try:
            for f in fields(cls):
                if not store.contains(f.name):
                    continue
                raw = store.value(f.name)
                default = getattr(out, f.name)
                try:
                    if isinstance(default, bool):
                        value = raw if isinstance(raw, bool) else str(raw).lower() in ("true", "1")
                    elif isinstance(default, int):
                        value = int(raw)
                    else:
                        value = str(raw)
                except (TypeError, ValueError):
                    continue
                setattr(out, f.name, value)
        finally:
            store.endGroup()
        if out.quality_preset not in QUALITY_PRESETS:
            out.quality_preset = "balanced"
        return out

    def save(self, store=None):
        store = store or _store()
        store.beginGroup(GROUP)
        for f in fields(self):
            store.setValue(f.name, getattr(self, f.name))
        store.endGroup()
        store.sync()

    def sources(self):
        """Which sources are switched on, in track order (voice-over is the mic alone)."""
        out = []
        if self.webcam_on and not self.voice_over:
            out.append("webcam")
        if self.screen_on and not self.voice_over:
            out.append("screen")
        if self.mic_on or self.voice_over:
            out.append("mic")
        if self.system_on and not self.voice_over:
            out.append("system")
        return out
