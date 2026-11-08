"""Verified downloading of a release installer.

The downloader refuses any URL that is not this repository's own release asset, streams to
a ``.part`` file, checks the free space first, enforces a size limit, hashes the bytes while
writing, and only renames the file after the SHA-256 in the release's checksum asset
matches. Cancellation and failures remove the temporary file, so a broken transfer can
never be executed later.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .model import is_trusted_asset_url, is_trusted_redirect_url

CHUNK = 128 * 1024
MAX_INSTALLER_BYTES = 400 * 1024 * 1024
MIN_FREE_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")


class DownloadError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def parse_checksums(text):
    """{file name: lowercase sha256} from a ``sha256sum`` style file."""
    found = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].lower()
        name = parts[-1].lstrip("*")
        if _SHA.match(digest):
            found[name] = digest
    return found


def checksum_for(text, asset_name):
    checksums = parse_checksums(text)
    wanted = str(asset_name)
    return checksums.get(wanted) or checksums.get(os.path.basename(wanted))


def verify_file(path, expected_sha256):
    """(ok, actual digest) for a file against a hex digest."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return False, ""
    actual = digest.hexdigest()
    return actual == str(expected_sha256 or "").lower(), actual


def _free_space(path):
    probe = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def download(url, destination, expected_sha256, *, expected_size=0, max_bytes=MAX_INSTALLER_BYTES,
             progress=None, cancel=None, opener=urlopen, timeout=20):
    """Download and verify one release asset; returns the final path.

    ``cancel`` is a ``threading.Event``; ``progress(done, total)`` is called while
    streaming. Nothing is left behind on failure.
    """
    url = str(url or "")
    if not is_trusted_asset_url(url):
        raise DownloadError("untrusted", "This download is not from the official meeniFX release.")
    digest_expected = str(expected_sha256 or "").lower()
    if not _SHA.match(digest_expected):
        raise DownloadError("checksum", "This release does not provide a usable checksum.")
    try:
        expected_size = int(expected_size or 0)
    except (TypeError, ValueError):
        expected_size = 0
    if expected_size > max_bytes:
        raise DownloadError("too_large", "This installer is larger than the download limit.")
    folder = os.path.dirname(os.path.abspath(destination))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise DownloadError("write", f"Could not create the download folder: {exc}") from exc
    free = _free_space(folder)
    needed = (expected_size if expected_size > 0 else MIN_FREE_BYTES) + MIN_FREE_BYTES
    if free is not None and free < needed:
        raise DownloadError("disk", "Not enough free disk space for this update.")
    temporary = str(destination) + ".part"
    hasher = hashlib.sha256()
    total = expected_size
    done = 0
    try:
        with opener(url, timeout=timeout) as response:
            final_url = None
            geturl = getattr(response, "geturl", None)
            if callable(geturl):
                final_url = geturl()
            if final_url and not is_trusted_redirect_url(final_url):
                raise DownloadError("untrusted", "The download was redirected away from GitHub.")
            headers = getattr(response, "headers", None) or {}
            try:
                length = int(headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length > 0:
                total = length
                if total > max_bytes:
                    raise DownloadError("too_large", "This installer is larger than the download limit.")
                if expected_size and total != expected_size:
                    raise DownloadError("size", "The download size does not match the release.")
            with open(temporary, "wb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadError("cancelled", "The download was cancelled.")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > max_bytes:
                        raise DownloadError("too_large", "This installer is larger than the download limit.")
                    hasher.update(chunk)
                    handle.write(chunk)
                    if progress is not None:
                        progress(done, total)
                handle.flush()
                os.fsync(handle.fileno())
    except DownloadError:
        _discard(temporary)
        raise
    except HTTPError as exc:
        _discard(temporary)
        raise DownloadError("network", f"GitHub returned HTTP {exc.code} while downloading.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        _discard(temporary)
        raise DownloadError("network", f"The download could not be completed: {exc}") from exc
    if cancel is not None and cancel.is_set():
        _discard(temporary)
        raise DownloadError("cancelled", "The download was cancelled.")
    actual = hasher.hexdigest()
    if actual != digest_expected:
        _discard(temporary)
        raise DownloadError("checksum", "The downloaded installer failed its checksum check.")
    if expected_size and done != expected_size:
        _discard(temporary)
        raise DownloadError("size", "The downloaded installer is incomplete.")
    try:
        os.replace(temporary, destination)
    except OSError as exc:
        _discard(temporary)
        raise DownloadError("write", f"Could not save the installer: {exc}") from exc
    return str(destination)


def _discard(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
