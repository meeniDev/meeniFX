"""Where we are installed and how the installer is handed the update.

The application never overwrites its own executable: the verified Inno Setup installer
does that. These helpers only decide whether automatic installation is appropriate, check
the file the updater is about to run, and keep the installer's arguments in one place.
"""

from __future__ import annotations

import os
import sys

from .model import INSTALLER_SUFFIX


class InstallerError(RuntimeError):
    pass


def installation_kind(frozen=None, exe_dir=None):
    """'installed', 'portable' or 'source'.

    An Inno Setup install has an uninstaller next to the executable; a portable build has
    the same bundled files without one, and a source checkout is neither.
    """
    frozen = getattr(sys, "frozen", False) if frozen is None else bool(frozen)
    if not frozen:
        return "source"
    folder = exe_dir or os.path.dirname(os.path.abspath(sys.executable))
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    if any(name.lower().startswith("unins") and name.lower().endswith(".exe") for name in names):
        return "installed"
    return "portable"


def can_auto_install():
    return installation_kind() == "installed"


def validate_installer(path):
    """Check the file is exactly the installer asset we expect to execute."""
    text = str(path or "")
    if not text or not os.path.isfile(text):
        raise InstallerError("The downloaded update is missing or incomplete.")
    if not os.path.basename(text).lower().endswith(INSTALLER_SUFFIX.lower()):
        raise InstallerError("The downloaded file is not a meeniFX installer.")
    try:
        if os.path.getsize(text) <= 0:
            raise InstallerError("The downloaded installer is empty.")
    except OSError as exc:
        raise InstallerError(f"The downloaded installer could not be read: {exc}") from exc
    return text


def installer_arguments():
    """Inno Setup switches for an unattended, per-user update; no elevation is requested."""
    return ["/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS"]
