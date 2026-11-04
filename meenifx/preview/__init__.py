"""Preview rendering: frame cache, render thread, GPU backend and playback metrics.

Nothing in this package owns project data; it only makes the engine's single
`render_frame()` cheap to call repeatedly and safe to call off the GUI thread.
"""

from .backend import gpu
from .cache import PreviewCache
from .metrics import PlaybackMetrics
from .renderer import PreviewRenderer
from .settings import PlaybackSettings

__all__ = ["gpu", "PreviewCache", "PlaybackMetrics", "PreviewRenderer", "PlaybackSettings"]
