"""Talk to GitHub's Releases API without blocking the UI and without hammering it.

The service returns a result object instead of raising: "no internet" and "rate limited"
are ordinary answers to an update check, not failures of the editor. Responses are cached
in the per-user settings folder and revalidated with an ETag, so repeated checks cost one
conditional request at most.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..version import VERSION
from .model import GITHUB_REPO, select_update, parse_release

API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30"
CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
USER_AGENT = f"meeniFX-Studios/{VERSION}"


class UpdateError(RuntimeError):
    """A check that produced no usable answer, with a kind for the page/notification."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class CheckResult:
    update: object = None
    checked_at: datetime = None
    from_cache: bool = False
    error: UpdateError = None

    @property
    def ok(self):
        return self.error is None


def _now():
    return datetime.now(timezone.utc)


def _cache_age(cache, now):
    try:
        return max(0.0, float(now) - float(cache.get("fetched_at")))
    except (TypeError, ValueError):
        return None


class ReleaseService:
    """Fetches and caches releases, then selects the best one for a channel."""

    def __init__(self, cache_path=None, opener=urlopen, clock=time.time, timeout=10,
                 cache_ttl=CACHE_TTL_SECONDS):
        self.cache_path = cache_path
        self.opener = opener
        self.clock = clock
        self.timeout = float(timeout)
        self.cache_ttl = float(cache_ttl)

    # ------------------------------------------------------------------ cache
    def _read_cache(self):
        if not self.cache_path:
            return None
        try:
            with open(self.cache_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("payload"), list):
            return None
        return data

    def _write_cache(self, payload, etag=None):
        if not self.cache_path:
            return
        folder = os.path.dirname(os.path.abspath(self.cache_path))
        try:
            os.makedirs(folder, exist_ok=True)
            temporary = str(self.cache_path) + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"fetched_at": self.clock(), "etag": etag, "payload": payload},
                          handle)
            os.replace(temporary, self.cache_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ network
    def _payload(self, force=False):
        cache = self._read_cache()
        now = self.clock()
        age = _cache_age(cache, now) if cache else None
        if not force and cache is not None and age is not None and age <= self.cache_ttl:
            return cache["payload"], True
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        request = Request(API_URL, headers=headers)
        etag = (cache or {}).get("etag")
        if etag:
            request.add_header("If-None-Match", str(etag))
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                headers_out = getattr(response, "headers", None) or {}
        except HTTPError as exc:
            if exc.code == 304 and cache is not None:
                self._write_cache(cache["payload"], etag)
                return cache["payload"], True
            if exc.code in (403, 429):
                raise UpdateError("rate_limited", self._rate_limit_message(exc)) from None
            if exc.code == 404:
                raise UpdateError("no_release",
                                  "This repository has no published releases yet.") from None
            raise UpdateError("http", f"GitHub returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise UpdateError("offline",
                              "Could not reach GitHub. Check your internet connection.") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise UpdateError("invalid", "The release list was unexpectedly large.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise UpdateError("invalid", "GitHub returned release data that could not be read.") from None
        if not isinstance(payload, list):
            raise UpdateError("invalid", "GitHub returned an unexpected release list.")
        try:
            new_etag = headers_out.get("ETag")
        except AttributeError:
            new_etag = None
        self._write_cache(payload, new_etag)
        return payload, False

    @staticmethod
    def _rate_limit_message(exc):
        reset = None
        try:
            reset = (exc.headers or {}).get("X-RateLimit-Reset")
        except AttributeError:
            reset = None
        when = ""
        try:
            when = datetime.fromtimestamp(int(reset), timezone.utc).astimezone().strftime(
                " at %H:%M")
        except (TypeError, ValueError):
            pass
        return f"GitHub's request limit was reached. Try again{when or ' later'}."

    def check(self, current=VERSION, channel="alpha", force=False):
        """Check for an update; never raises for network or metadata problems."""
        try:
            payload, from_cache = self._payload(force=force)
        except UpdateError as exc:
            return CheckResult(None, _now(), False, exc)
        releases = [release for release in (parse_release(item) for item in payload
                                            if isinstance(item, dict))
                    if release is not None]
        return CheckResult(select_update(releases, current, channel), _now(), from_cache)
