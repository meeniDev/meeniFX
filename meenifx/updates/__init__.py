"""Application updates: GitHub Releases, settings, verified downloads and handoff."""

from .model import (GITHUB_REPO, RELEASES_PAGE, Asset, Release, UpdateInfo, parse_release,
                    select_update, installer_name, portable_name)
from .service import CheckResult, ReleaseService, UpdateError
from .settings import UpdateSettings
from .download import (DownloadError, checksum_for, download as download_installer, parse_checksums,
                       verify_file)
from .installer import InstallerError, can_auto_install, installation_kind, validate_installer

__all__ = [
    "GITHUB_REPO", "RELEASES_PAGE", "Asset", "Release", "UpdateInfo", "parse_release",
    "select_update", "installer_name", "portable_name", "CheckResult", "ReleaseService",
    "UpdateError", "UpdateSettings", "DownloadError", "checksum_for", "download_installer",
    "parse_checksums", "verify_file", "InstallerError", "can_auto_install",
    "installation_kind", "validate_installer",
]
