"""Recorder preferences persisted in QSettings."""

from dataclasses import dataclass, fields

from PyQt6.QtCore import QSettings

GROUP = "recorder"


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
    window_id: int = 0
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
    hotkey_stop: str = "Ctrl+Shift+R"
    hotkey_pause: str = "Ctrl+Shift+P"
    minimize_while_recording: bool = True

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
