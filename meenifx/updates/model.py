"""Releases as plain data: parsing, asset naming, trust rules and channel selection.

Everything here is pure, so the network layer and the tests can share one definition of
what a usable Windows release looks like. An asset is accepted only by its exact expected
name and only from this repository's own release download path.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from ..version import Version, parse_version

GITHUB_REPO = "meeniDev/meeniFX"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
CHECKSUMS_NAME = "SHA256SUMS.txt"
INSTALLER_SUFFIX = "-Windows-Setup.exe"
PORTABLE_SUFFIX = "-Windows-Portable.zip"
ASSET_PREFIX = "meeniFX-Studios-"
_ASSET_PATH = f"/{GITHUB_REPO}/releases/download/"


def installer_name(version_text):
    return f"{ASSET_PREFIX}{version_text}{INSTALLER_SUFFIX}"


def portable_name(version_text):
    return f"{ASSET_PREFIX}{version_text}{PORTABLE_SUFFIX}"


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class Release:
    tag: str
    version: Version
    prerelease: bool
    notes: str
    published_at: str
    html_url: str
    assets: tuple = ()

    @property
    def version_text(self):
        return str(self.version)

    def asset_named(self, name):
        wanted = str(name).casefold()
        for asset in self.assets:
            if asset.name.casefold() == wanted:
                return asset
        return None


@dataclass(frozen=True)
class UpdateInfo:
    """One installable release: the release plus the exact assets a user may download."""

    release: Release
    installer: Asset
    portable: Asset = None
    checksums: Asset = None

    @property
    def version(self):
        return self.release.version

    @property
    def version_text(self):
        return self.release.version_text

    @property
    def notes(self):
        return self.release.notes

    @property
    def published_at(self):
        return self.release.published_at

    @property
    def html_url(self):
        return self.release.html_url

    @property
    def installer_size(self):
        return int(self.installer.size or 0)


def is_trusted_asset_url(url):
    """True for an https release asset URL of *this* repository, and nothing else."""
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.netloc.lower() == "github.com"
            and parsed.path.startswith(_ASSET_PATH))


def is_trusted_redirect_url(url):
    """A followed redirect may only land on GitHub's asset hosts."""
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc.lower() in (
        "github.com", "objects.githubusercontent.com")


def is_trusted_release_url(url):
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.netloc.lower() == "github.com"
            and parsed.path.startswith(f"/{GITHUB_REPO}/releases/"))


def _asset_from(raw):
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    url = str(raw.get("browser_download_url") or "").strip()
    if not name or not is_trusted_asset_url(url):
        return None
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return Asset(name, url, max(0, size))


def parse_release(payload):
    """A Release from one GitHub API release object, or None when it is not usable.

    Drafts, malformed tags and non-Windows releases are rejected here so no later layer
    has to guess whether something is offerable.
    """
    if not isinstance(payload, dict) or payload.get("draft"):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    version = parse_version(tag)
    if version is None:
        return None
    assets = tuple(asset for asset in (_asset_from(raw) for raw in (payload.get("assets") or ()))
                   if asset is not None)
    html_url = str(payload.get("html_url") or "")
    if not is_trusted_release_url(html_url):
        html_url = f"{RELEASES_PAGE}/tag/{tag}"
    return Release(
        tag=tag,
        version=version,
        prerelease=bool(payload.get("prerelease", version.prerelease)),
        notes=str(payload.get("body") or ""),
        published_at=str(payload.get("published_at") or payload.get("created_at") or ""),
        html_url=html_url,
        assets=assets,
    )


def release_update(release):
    """The UpdateInfo for a release, or None when it has no trusted Windows installer."""
    installer = release.asset_named(installer_name(release.version_text))
    if installer is None or not is_trusted_asset_url(installer.url):
        return None
    portable = release.asset_named(portable_name(release.version_text))
    return UpdateInfo(release=release, installer=installer, portable=portable,
                      checksums=release.asset_named(CHECKSUMS_NAME))


def select_update(releases, current, channel="alpha"):
    """The newest installable release newer than ``current`` on ``channel``.

    ``alpha`` includes prereleases; ``stable`` never does. Comparing Version objects
    means 0.1.0-alpha.2 is newer than 0.1.0-alpha.1 but never newer than 0.1.0.
    """
    current_version = current if isinstance(current, Version) else parse_version(current)
    if current_version is None:
        return None
    best = None
    for release in releases:
        if channel == "stable" and release.prerelease:
            continue
        if release.version <= current_version:
            continue
        info = release_update(release)
        if info is None:
            continue
        if best is None or info.version > best.version:
            best = info
    return best
