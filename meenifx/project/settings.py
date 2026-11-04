"""Project settings: resolution, frame rate, background, audio rate and the preset list."""

from dataclasses import dataclass, asdict, fields

PROJECT_EXTENSION = ".mfxpf"
PROJECT_FILTER = "meeniFX Project Files (*.mfxpf)"

FRAME_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0)
SAMPLE_RATES = (44100, 48000, 96000)

# name -> (width, height, fps)
PRESETS = {
    "YouTube 1080p 30": (1920, 1080, 30.0),
    "HD 1080p 60": (1920, 1080, 60.0),
    "4K 30": (3840, 2160, 30.0),
    "Vertical 9:16 1080x1920": (1080, 1920, 30.0),
    "Square 1:1 1080x1080": (1080, 1080, 30.0),
    "Cinema 24 fps": (1920, 1080, 24.0),
    "720p 30": (1280, 720, 30.0),
}
CUSTOM_PRESET = "Custom"
DEFAULT_PRESET = "YouTube 1080p 30"

MIN_SIZE, MAX_SIZE = 16, 16384


def nominal_fps(fps):
    """Whole-frame rate used by the timeline (29.97 -> 30, 23.976 -> 24, 59.94 -> 60)."""
    return max(1, int(round(float(fps))))


def fps_text(fps):
    return f"{float(fps):g}"


def preset_for(width, height, fps):
    for name, (w, h, f) in PRESETS.items():
        if (w, h) == (int(width), int(height)) and abs(f - float(fps)) < 1e-6:
            return name
    return CUSTOM_PRESET


@dataclass
class ProjectSettings:
    name: str = "Untitled"
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    background: str = "#000000"
    sample_rate: int = 48000
    import_mode: str = "link"  # "link" or "copy"
    preset: str = DEFAULT_PRESET

    @property
    def nominal_fps(self):
        return nominal_fps(self.fps)

    def validate(self):
        """Clamp to sane values and return self."""
        self.name = str(self.name or "Untitled").strip() or "Untitled"
        self.width = max(MIN_SIZE, min(MAX_SIZE, int(self.width)))
        self.height = max(MIN_SIZE, min(MAX_SIZE, int(self.height)))
        self.width -= self.width % 2
        self.height -= self.height % 2
        self.fps = max(1.0, min(240.0, float(self.fps)))
        bg = str(self.background or "#000000")
        self.background = bg if len(bg) == 7 and bg.startswith("#") else "#000000"
        self.sample_rate = int(self.sample_rate) if int(self.sample_rate) in SAMPLE_RATES else 48000
        self.import_mode = "copy" if self.import_mode == "copy" else "link"
        self.preset = self.preset if self.preset in PRESETS or self.preset == CUSTOM_PRESET else CUSTOM_PRESET
        return self

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(data or {}).items() if k in known}).validate()

    @classmethod
    def from_preset(cls, preset, name="Untitled", **extra):
        w, h, f = PRESETS.get(preset, PRESETS[DEFAULT_PRESET])
        return cls(name=name, width=w, height=h, fps=f, preset=preset if preset in PRESETS else CUSTOM_PRESET,
                   **extra).validate()

    def background_rgb(self):
        c = self.background.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
