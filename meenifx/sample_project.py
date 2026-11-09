"""Locate and open the bundled sample project without writing inside the installation.

The sample is a real ``.mfxpf`` file under ``examples/Getting Started``. It uses only
generated text and shape clips, so there is no media to relink. A copy is made in the
user's Documents folder the first time it is opened, which keeps edits and autosaves out
of Program Files (or the per-user install folder).
"""

from __future__ import annotations

import os
import shutil

from .config import ASSETS_ROOT
from .project import storage

SAMPLE_NAME = "Getting Started"


def bundled_path():
    return os.path.join(str(ASSETS_ROOT), "examples", SAMPLE_NAME,
                        f"{SAMPLE_NAME}.mfxpf")


def ensure_sample_copy():
    """The writable copy of the sample, or None when this build did not ship it."""
    source = bundled_path()
    if not os.path.isfile(source):
        return None
    folder = os.path.join(storage.default_projects_dir(), SAMPLE_NAME)
    target = os.path.join(folder, f"{SAMPLE_NAME}.mfxpf")
    if os.path.isfile(target):
        return target
    try:
        os.makedirs(folder, exist_ok=True)
        shutil.copy2(source, target)
        for sub in ("Recordings", "Cache", "Media"):
            os.makedirs(os.path.join(folder, sub), exist_ok=True)
    except OSError:
        return None
    return target
