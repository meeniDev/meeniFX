import sys
import os
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller: bundled data lives next to the extracted code (_MEIPASS);
    # write outputs next to the exe instead.
    ASSETS_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    ASSETS_ROOT = Path(__file__).resolve().parents[1]
    PROJECT_ROOT = ASSETS_ROOT

APP_TITLE = "meeniFX Studios"
# One authoritative version, shared by the About box, Preferences, the update service,
# the crash log and the generated packaging resources.
from .version import CHANNEL as APP_CHANNEL, VERSION as APP_VERSION  # noqa: E402

# Brand artwork lives in assets/ and is resolved through ASSETS_ROOT, so the same paths
# work when the app runs from source and from a frozen PyInstaller bundle.
ASSETS_DIR = ASSETS_ROOT / "assets"
APP_ICON = ASSETS_DIR / "icon.ico"
APP_ICON_PNGS = tuple(ASSETS_DIR / name for name in ("icon_1024.png", "icon_512.png", "icon_256.png"))
APP_LOGO = ASSETS_DIR / "logo_horizontal.png"
APP_LOGO_DARK = ASSETS_DIR / "logo_horizontal_on_dark.png"
DEFAULT_INPUT = str(PROJECT_ROOT / "flight-reacts-flight.gif")
DEFAULT_OUTPUT = str(PROJECT_ROOT / "mosaic-output.mp4")

TILE_FILES = ["black.png", "whiteandblack.png", "white.png"]
DEFAULT_TILE_PATHS = [ASSETS_DIR / "mosaic" / name for name in TILE_FILES]


def user_config_dir():
    """Per-user writable folder for presets and caches (never inside the bundle)."""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = str(Path.home() / ".config")
    folder = Path(base) / "meeniFX" / "Studios"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return str(Path.home())
    return str(folder)
