"""The application's single source of truth for its version.

``VERSION`` is the only place the release number is written. The About box, Preferences,
the update service, the crash log, the PyInstaller resource and the Inno Setup include are
all generated from (or read) this value, so the app can never announce one version while
being another.

Tags use ``MAJOR.MINOR.PATCH[-alpha.N|-beta.N|-rc.N]``. A prerelease always sorts below
the stable release with the same base version, which is what GitHub's Releases API needs
when an alpha channel is enabled.
"""

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path

VERSION = "0.1.0-alpha.4"
CHANNEL = "alpha"

STABLE = 4
_STAGE_NAMES = {1: "alpha", 2: "beta", 3: "rc"}
_STAGE_NUMBERS = {name: number for number, name in _STAGE_NAMES.items()}
_PATTERN = re.compile(
    r"^(?:v)?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<stage>alpha|beta|rc)\.(?P<number>0|[1-9]\d*))?$",
    re.IGNORECASE,
)


@total_ordering
@dataclass(frozen=True)
class Version:
    """A comparable version: prereleases sort below the matching stable release."""

    major: int
    minor: int
    patch: int
    stage: int = STABLE
    number: int = 0

    @property
    def prerelease(self):
        return self.stage < STABLE

    @property
    def key(self):
        # The tuple is the whole comparison rule. ``stage`` makes 0.1.0-alpha.9 older
        # than 0.1.0-rc.1, and both older than 0.1.0 - never the other way round.
        return (self.major, self.minor, self.patch, self.stage, self.number)

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self.key < other.key

    def __str__(self):
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease and self.stage in _STAGE_NAMES:
            text += f"-{_STAGE_NAMES[self.stage]}.{self.number}"
        return text


def parse_version(text):
    """A Version for a release tag, or None when the tag is not one of ours.

    Malformed tags are rejected rather than guessed at: treating ``build-2026`` as
    version 0 would offer users a download that is not a release at all.
    """
    if not isinstance(text, str):
        return None
    match = _PATTERN.match(text.strip())
    if match is None:
        return None
    stage_name = (match.group("stage") or "").lower()
    stage = _STAGE_NUMBERS.get(stage_name, STABLE)
    number = int(match.group("number")) if stage_name else 0
    return Version(int(match.group("major")), int(match.group("minor")),
                   int(match.group("patch")), stage, number)


def current_version():
    """The running build's Version (always parseable; the constant above is tested)."""
    parsed = parse_version(VERSION)
    if parsed is None:                     # a typo in VERSION must not crash the app
        return Version(0, 0, 0, STABLE, 0)
    return parsed


def numeric_version(version_text=VERSION):
    """Four integers for Windows file metadata (prereleases never look newer)."""
    version = parse_version(version_text) or current_version()
    footer = 0 if not version.prerelease else version.stage * 1000 + version.number
    return (version.major, version.minor, version.patch, footer)


def channel_label(channel=CHANNEL):
    return "Alpha" if str(channel).lower() == "alpha" else "Stable"


def display_version(version_text=VERSION):
    """``0.1.0-alpha.1 (Alpha)`` for the About box, Updates page and logs."""
    return f"{version_text} ({channel_label()})"


def release_notes_folder():
    """Where user-facing notes for each release live, in source and in a frozen build."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        bundled = Path(frozen_root) / "docs" / "releases"
        if bundled.is_dir():
            return bundled
    return Path(__file__).resolve().parents[1] / "docs" / "releases"


def release_notes_path(version_text=VERSION):
    return release_notes_folder() / f"{version_text}.md"


def read_release_notes(version_text=VERSION):
    """The bundled user-facing notes for a version, or an empty string."""
    try:
        return release_notes_path(version_text).read_text(encoding="utf-8")
    except OSError:
        return ""


def diagnostics():
    """One line included in crash reports and diagnostic logs."""
    frozen = "installed build" if getattr(sys, "frozen", False) else "source checkout"
    return (f"meeniFX Studios {VERSION} ({channel_label()} channel, {frozen}) | "
            f"Python {platform.python_version()} | {platform.platform()}")
