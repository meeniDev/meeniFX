"""App icon and wordmark loading; the only place that knows where brand files live.

Everything is read from ``config.ASSETS_DIR`` so a frozen build finds the copied
``assets`` folder next to the bundled code. Missing files degrade quietly to ``None``
instead of raising, so a stripped-down build still starts with a default icon.

Both helpers are device-pixel-ratio aware: the artwork is resampled straight from the
high-resolution source at the size the *screen* needs and then tagged with its scale, so
the wordmark and the app tile are never drawn from a bitmap that was enlarged afterwards.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QPixmap

from ..config import APP_ICON, APP_ICON_PNGS, APP_LOGO, APP_LOGO_DARK

# The icon file happens to be the first pick; the PNGs add ready-made large sizes so
# Qt never has to upscale for high-DPI taskbars and alt-tab previews.
_ICON_SOURCES = tuple(path for path in (APP_ICON, *APP_ICON_PNGS) if path.exists())
_LOGO_CACHE = {}


def app_icon():
    """Multi-resolution icon for the window, the taskbar and message boxes."""
    if not _ICON_SOURCES:
        return QIcon()
    icon = QIcon()
    for path in _ICON_SOURCES:
        icon.addFile(str(path))
    return icon


def _smooth(pixmap, width, height, dpr):
    return pixmap.scaled(max(1, int(round(width * dpr))), max(1, int(round(height * dpr))),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)


def wordmark(width=440, dpr=1.0):
    """The horizontal logo at ``width`` logical pixels, resampled with a smooth filter.

    ``dpr`` is the target screen scale: the source is 2200 px wide, so even a 3x screen is
    still a downscale, which is what makes the wordmark look like vector art rather than
    like an enlarged bitmap.
    """
    from .. import tokens
    logo = APP_LOGO_DARK if tokens.is_dark() else APP_LOGO
    if not logo.exists():
        return None
    key = (int(round(width)), round(float(dpr or 1.0), 2), tokens.is_dark())
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    source = QPixmap(str(logo))
    if source.isNull():
        return None
    scaled = _smooth(source, width, source.height() * width / max(1, source.width()), dpr)
    scaled.setDevicePixelRatio(max(0.1, float(dpr or 1.0)))
    _LOGO_CACHE[key] = scaled
    return scaled


