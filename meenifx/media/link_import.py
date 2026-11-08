"""Media links: decide whether a pasted URL may be imported, then fetch it safely.

Smart Paste accepts a direct link to a picture, an animated GIF, a video or a sound file.
Everything that decides *whether* a link may be fetched lives here, away from widgets, so the
editor and the tests share exactly the same rules:

* only ``http`` and ``https`` links are opened;
* only public hosts are opened - loopback, private, link-local and reserved addresses are
  refused, including when a redirect or a DNS answer points at one;
* signed query strings (Discord's ``ex``, ``is`` and ``hm``) are sent untouched, kept out of
  the local file name and kept out of every log line;
* the response is size- and time-limited, streamed into a ``.part`` file in the destination
  folder, and only renamed once its bytes really decode as media this build can read.

Nothing here starts a download on its own: :func:`probe` asks a server for headers only, and
:func:`download` is called after the person importing the link has agreed to it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass, replace
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

log = logging.getLogger("meenifx.link_import")

MAX_BYTES = 32 * 1024 * 1024      # the ceiling Smart Paste applies to one import
CHUNK = 64 * 1024
TIMEOUT = 20.0                    # one socket read may not stall longer than this
PROBE_TIMEOUT = 8.0
MAX_SECONDS = 300.0               # a whole download gives up after this
MAX_REDIRECTS = 5
MAX_URL = 2048
USER_AGENT = "meeniFX Smart Paste"

# Tests serve links from 127.0.0.1. Nothing else in the app ever sets this, and a remote
# page cannot set it, so the loopback rule still holds for real imports.
ALLOW_LOCAL_ENV = "MEENIFX_ALLOW_LOCAL_LINKS"

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi")
AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a")
MEDIA_SUFFIXES = IMAGE_SUFFIXES + VIDEO_SUFFIXES + AUDIO_SUFFIXES

# Sites that serve a player page rather than the file itself. A pasted YouTube link is not a
# video file, and pretending otherwise is what turns an import into a broken clip.
PLATFORM_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "tiktok.com", "twitch.tv",
    "instagram.com", "facebook.com", "fb.watch", "x.com", "twitter.com", "soundcloud.com",
    "streamable.com", "bilibili.com", "rumble.com", "pinterest.com", "reddit.com",
)
PLATFORM_NAMES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion", "tiktok.com": "TikTok", "twitch.tv": "Twitch",
    "instagram.com": "Instagram", "facebook.com": "Facebook", "fb.watch": "Facebook",
    "x.com": "X", "twitter.com": "X", "soundcloud.com": "SoundCloud",
    "streamable.com": "Streamable", "bilibili.com": "Bilibili", "rumble.com": "Rumble",
    "pinterest.com": "Pinterest", "reddit.com": "Reddit",
}
WEB_SUFFIXES = (".html", ".htm", ".xhtml", ".php", ".asp", ".aspx", ".jsp")

_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')
_RESERVED_NAMES = {"con", "prn", "aux", "nul",
                   *(f"com{index}" for index in range(1, 10)),
                   *(f"lpt{index}" for index in range(1, 10))}


# ----------------------------------------------------------------------- problems
class LinkError(Exception):
    """A problem with a pasted link that the user can act on."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class UnsupportedLink(LinkError):
    """The link is a page, a platform or a file type this build cannot import."""


class BlockedLink(LinkError):
    """The link points at this machine or at a private network."""


class Cancelled(LinkError):
    """The person importing the link pressed Cancel."""

    def __init__(self):
        super().__init__("Import cancelled.")


# ------------------------------------------------------------------------ shapes
@dataclass(frozen=True)
class LinkInfo:
    """What is known about a link before anything is downloaded."""

    url: str
    host: str
    filename: str
    suffix: str
    media_type: str                # image | video | audio
    size: int = 0
    content_type: str = ""
    signed: bool = False
    expires: float = 0.0           # unix seconds from a signed "ex" parameter, 0 = unknown

    @property
    def kind(self):
        """image, animation, video or audio - what the import dialog calls the media."""
        return "animation" if self.suffix == ".gif" else self.media_type

    @property
    def type_label(self):
        return {"image": "Image", "animation": "Animated GIF",
                "video": "Video", "audio": "Audio"}[self.kind]


@dataclass(frozen=True)
class Downloaded:
    """A finished import: the validated file, what it is, and what was learned from it."""

    path: str
    info: LinkInfo
    size: int
    width: int = 0
    height: int = 0
    seconds: float = 0.0
    frames: int = 0

    def summary(self):
        parts = []
        if self.width and self.height:
            parts.append(f"{self.width}×{self.height}")
        if self.seconds > 0:
            parts.append(f"{self.seconds:.1f} s")
        if self.frames > 1:
            parts.append(f"{self.frames} frames")
        return " · ".join(parts)


# ----------------------------------------------------------------------- helpers
def allows_local():
    """True only in the test environment, where links point at 127.0.0.1."""
    return os.environ.get(ALLOW_LOCAL_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def redact(url):
    """The URL without its query string: signed tokens must never reach a log or a name."""
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def human_size(value):
    value = float(value or 0)
    if value < 1024:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


def _query(query):
    try:
        return dict(parse_qsl(query or "", keep_blank_values=True))
    except ValueError:
        return {}


def _signed(query):
    """Discord signs its attachment links with ex / is / hm. Other links are merely long."""
    keys = _query(query)
    return "ex" in keys and "hm" in keys


def _expiry(query):
    raw = _query(query).get("ex", "")
    try:
        value = int(str(raw), 16)
    except (TypeError, ValueError):
        return 0.0
    return float(value) if 1_000_000_000 < value < 4_000_000_000 else 0.0


def _is_platform(host):
    return any(host == name or host.endswith("." + name) for name in PLATFORM_HOSTS)


def _platform_name(host):
    for name in PLATFORM_HOSTS:
        if host == name or host.endswith("." + name):
            return PLATFORM_NAMES.get(name, name)
    return host


def _suffix(path):
    ext = os.path.splitext(unquote(path or ""))[1].lower()
    return ext if ext in MEDIA_SUFFIXES else ""


def safe_filename(name, suffix=""):
    """A local file name built from an untrusted one: no path, no query, no control bytes.

    The caller's `suffix` wins, because the extension is decided by the bytes that were
    actually downloaded rather than by what the server claimed in its URL.
    """
    name = unquote(name or "").strip().split("?", 1)[0].split("#", 1)[0]
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME.sub("_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    if ext.lower() not in MEDIA_SUFFIXES:
        stem, ext = name, suffix or ""
    stem = _UNSAFE_NAME.sub("_", stem).strip(" .")[:80].strip(" .")
    if not stem:
        stem = "Imported media"
    if stem.lower() in _RESERVED_NAMES:
        stem = "_" + stem
    return stem + ((suffix or ext).lower())


def destination(folder, filename, suffix):
    """A path in `folder` that no other import uses, e.g. ``cat.gif`` then ``cat (2).gif``."""
    stem = os.path.splitext(safe_filename(filename, suffix))[0] or "Imported media"
    candidate = os.path.join(folder, stem + suffix)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem} ({counter}){suffix}")
        counter += 1
    return candidate


# ----------------------------------------------------------------- classifcation
def classify(text):
    """What a plain-text clipboard holds.

    Returns a :class:`LinkInfo` for a direct media link, ``None`` when the text is not a link
    at all, and raises :class:`UnsupportedLink` for a link that must not be downloaded (a
    web page, a video platform page, an unsupported file type).
    """
    text = (text or "").strip()
    if not text or len(text) > MAX_URL or any(ch.isspace() for ch in text):
        return None
    if not _SCHEME.match(text):
        return None
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    if parts.username or parts.password:
        raise UnsupportedLink("That link carries a username and password. Paste the plain link "
                              "instead.")
    suffix = _suffix(parts.path)
    if suffix:
        if suffix in AUDIO_SUFFIXES:
            media_type = "audio"
        elif suffix in VIDEO_SUFFIXES:
            media_type = "video"
        else:
            media_type = "image"
        return LinkInfo(text, host, safe_filename(os.path.basename(unquote(parts.path)), suffix),
                        suffix, media_type, signed=_signed(parts.query),
                        expires=_expiry(parts.query))
    if _is_platform(host):
        raise UnsupportedLink(f"That is a {_platform_name(host)} page, not a media file. Copy a "
                              "direct link to the file itself, or download it first.")
    if parts.path.lower().endswith(WEB_SUFFIXES) or not os.path.splitext(parts.path)[1]:
        raise UnsupportedLink("That link opens a web page, not a media file. Copy a direct link "
                              "to the image, video or sound file.")
    raise UnsupportedLink("That file type cannot be imported. Smart Paste reads images, "
                          "animated GIFs, videos and audio.")


# ---------------------------------------------------------------------- security
def _check_host(host, port, allow_local):
    if allow_local:
        return
    if (host in ("localhost", "localhost.localdomain") or host.endswith(".localhost")
            or host.endswith(".local") or host.endswith(".internal")):
        raise BlockedLink("That link points at a local address, so it was not opened.")
    try:
        answers = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LinkError(f"The address of {host} could not be found "
                        f"({getattr(exc, 'strerror', None) or exc}).") from exc
    for answer in answers:
        address = str(answer[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global:
            raise BlockedLink("That link points at a private or local address, so it was not "
                              "opened.")


def check_url(url, allow_local=None):
    """Refuse anything that is not a public http(s) address, before a socket is opened."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsupportedLink("That link is not a valid address.") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsupportedLink("Only http and https links can be imported.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsupportedLink("That link has no address.")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsupportedLink("That link has an invalid port.") from exc
    _check_host(host, port, allows_local() if allow_local is None else allow_local)


class _SafeRedirects(HTTPRedirectHandler):
    """Follow redirects only to addresses that would have been allowed in the first place."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_local):
        super().__init__()
        self._allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self._allow_local)
        log.debug("following redirect %s -> %s", redact(req.full_url), redact(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ------------------------------------------------------------------------ errors
def _http_error(exc, url):
    signed = _signed(urlsplit(url).query)
    code = int(getattr(exc, "code", 0) or 0)
    if signed and code in (400, 401, 403, 410):
        return LinkError("That signed link has expired. Copy a fresh link from the source and "
                         "paste it again.")
    if code in (401, 403):
        return LinkError(f"The server refused the download (HTTP {code}). The file may be "
                         "private, or the link may have expired.")
    if code in (404, 410):
        return LinkError(f"That file is no longer at that address (HTTP {code}). It may have "
                         "been deleted or the link may be wrong.")
    if code == 429:
        return LinkError("The server is limiting downloads right now (HTTP 429). Try again in "
                         "a minute.")
    if 500 <= code < 600:
        return LinkError(f"The server had a problem (HTTP {code}). Try again later.")
    return LinkError(f"The download failed (HTTP {code}).")


def _transport_error(exc, url):
    reason = getattr(exc, "reason", exc)
    host = urlsplit(url).netloc
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
        return LinkError(f"{host} took too long to answer. Check the link or try again.")
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)) or "reset" in str(reason).lower():
        return LinkError(f"The connection to {host} was closed before the file arrived.")
    return LinkError(f"Could not reach {host}: {reason}")


def _reject_page(content_type):
    if content_type.startswith(("text/html", "application/xhtml", "application/json")):
        raise UnsupportedLink("That link returned a web page instead of a media file. Copy a "
                              "direct link to the file itself.")


def _not_media_message(content_type):
    if content_type.startswith(("text/", "application/json")):
        return ("That link returned a web page instead of a media file. Copy a direct link to "
                "the file itself.")
    described = content_type or "an unknown file type"
    return f"That link returned {described}, which is not media this build can read."


def _length(value):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------- byte sniffing
def sniff(head):
    """(suffix, media_type) read from the first bytes of a download, or None.

    A URL says what someone hoped the file would be; these bytes say what it is. The suffix
    that ends up on disk always comes from here, never from the query string or header.
    """
    head = bytes(head or b"")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg", "image"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav", "audio"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi", "video"
    if head[:4] == b"fLaC":
        return ".flac", "audio"
    if head[:4] == b"OggS":
        return ".ogg", "audio"
    if head[:3] == b"ID3":
        return ".mp3", "audio"
    if len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return ".mp3", "audio"
    if head[4:8] == b"ftyp":
        return (".mov", "video") if head[8:12] in (b"qt  ", b"qt\t") else (".mp4", "video")
    if head[:4] == b"\x1aE\xdf\xa3":
        return ".webm", "video"
    if head[:2] == b"BM":
        return ".bmp", "image"
    return None


def _validate(path, suffix, media_type):
    """True when the saved bytes really decode as that kind of media."""
    try:
        if media_type == "audio":
            if suffix == ".wav":
                import wave

                with wave.open(path, "rb") as handle:
                    return handle.getnframes() > 0
            return True                       # the magic bytes already named the format
        if media_type == "image":
            if suffix == ".gif":
                from PIL import Image

                with Image.open(path) as image:
                    return int(getattr(image, "n_frames", 1)) >= 1
            import cv2

            picture = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            return picture is not None and picture.size > 0
        import cv2

        capture = cv2.VideoCapture(path)
        try:
            if not capture.isOpened():
                return False
            ok, frame = capture.read()
            return bool(ok and frame is not None)
        finally:
            capture.release()
    except Exception as exc:                   # a decoder is allowed to reject anything
        log.debug("validation failed for %s: %s", os.path.basename(path), exc)
        return False


def describe_file(path, suffix="", media_type=""):
    """(width, height, seconds, frames) read cheaply from a saved file; zeros when unknown."""
    suffix = suffix or os.path.splitext(path or "")[1].lower()
    if not media_type:
        media_type = ("audio" if suffix in AUDIO_SUFFIXES
                      else "video" if suffix in VIDEO_SUFFIXES else "image")
    if media_type == "audio":
        if suffix == ".wav":
            try:
                import wave

                with wave.open(path, "rb") as handle:
                    return 0, 0, handle.getnframes() / max(1, handle.getframerate()), 0
            except Exception:
                return 0, 0, 0.0, 0
        return 0, 0, 0.0, 0
    try:
        if media_type == "image":
            from PIL import Image, ImageSequence

            with Image.open(path) as image:
                frames = int(getattr(image, "n_frames", 1))
                seconds = 0.0
                if frames > 1:
                    seconds = sum(float(frame.info.get("duration", 0) or 0)
                                  for frame in ImageSequence.Iterator(image)) / 1000.0
                return int(image.size[0]), int(image.size[1]), seconds, frames
        import cv2

        capture = cv2.VideoCapture(path)
        try:
            if not capture.isOpened():
                return 0, 0, 0.0, 0
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            return width, height, (count / fps if fps > 0 and count > 0 else 0.0), count
        finally:
            capture.release()
    except Exception:
        return 0, 0, 0.0, 0


# ------------------------------------------------------------------------- probe
def probe(url, *, timeout=PROBE_TIMEOUT, allow_local=None):
    """Ask the server for headers only, so the dialog can show size and type.

    Nothing is downloaded here, and a HEAD request that a server refuses is not an error: the
    link is still offered, just without a size.
    """
    info = classify(url)
    if info is None:
        raise UnsupportedLink("That is not a link.")
    allow_local = allows_local() if allow_local is None else allow_local
    check_url(url, allow_local)
    request = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    log.info("checking link %s", redact(url))
    try:
        with build_opener(_SafeRedirects(allow_local)).open(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            _reject_page(content_type)
            size = _length(response.headers.get("Content-Length"))
            log.info("link %s -> %s, %s, %s", redact(url), content_type or "unknown type",
                     human_size(size) if size else "unknown size", info.kind)
            return replace(info, size=size, content_type=content_type)
    except LinkError:
        raise
    except HTTPError as exc:
        if exc.code in (405, 501):
            log.info("server refuses HEAD (%s); importing without a size", exc.code)
            return info
        log.warning("link %s refused: HTTP %s", redact(url), exc.code)
        raise _http_error(exc, url) from exc
    except (URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        log.warning("link %s unreachable: %s", redact(url), exc)
        raise _transport_error(exc, url) from exc


def _reencode_png(path):
    """Rewrite a downloaded still as PNG in place and return the new suffix."""
    import cv2

    picture = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if picture is None:
        raise LinkError("The downloaded picture could not be converted to PNG.")
    ok, buffer = cv2.imencode(".png", picture)
    if not ok:
        raise LinkError("The downloaded picture could not be converted to PNG.")
    target = path + ".png"
    try:
        buffer.tofile(target)
        os.unlink(path)
        os.replace(target, path)
    finally:
        if os.path.exists(target):
            try:
                os.unlink(target)
            except OSError:
                pass
    return ".png"


# ---------------------------------------------------------------------- download
def download(info, folder, *, limit=MAX_BYTES, timeout=TIMEOUT, progress=None, cancelled=None,
             allow_local=None, max_seconds=MAX_SECONDS, preserve=True):
    """Fetch `info` into `folder`, or raise LinkError having left nothing behind.

    The bytes are written to a ``.part`` file next to their destination and only renamed once
    they are a complete, decodable media file, so a cancelled or broken import can never be
    registered as a project asset.
    """
    if not isinstance(info, LinkInfo):
        raise TypeError("download() needs the LinkInfo that classify() or probe() returned")
    allow_local = allows_local() if allow_local is None else allow_local
    check_url(info.url, allow_local)
    os.makedirs(folder, exist_ok=True)
    log.info("downloading %s (%s)", redact(info.url), info.kind)
    started = time.monotonic()
    partial = ""
    try:
        opener = build_opener(_SafeRedirects(allow_local))
        request = Request(info.url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        with opener.open(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            _reject_page(content_type)
            # Name the file after where the redirects landed, not after the shortcut.
            landed = urlsplit(response.geturl() or info.url)
            landed_suffix = _suffix(landed.path)
            if landed_suffix:
                landed_name = os.path.basename(unquote(landed.path))
                if landed_name:
                    info = replace(info, filename=safe_filename(landed_name, landed_suffix))
            declared = _length(response.headers.get("Content-Length"))
            if declared and declared > limit:
                raise LinkError(f"That file is {human_size(declared)}, larger than the "
                                f"{human_size(limit)} limit.")
            handle = tempfile.NamedTemporaryFile(dir=folder, prefix=".meenifx-", suffix=".part",
                                                 delete=False)
            partial = handle.name
            total, head, done = declared, b"", 0
            with handle:
                while True:
                    if cancelled is not None and cancelled():
                        raise Cancelled()
                    if time.monotonic() - started > max_seconds:
                        raise LinkError("That download took too long and was stopped.")
                    try:
                        chunk = response.read(CHUNK)
                    except IncompleteRead as exc:
                        raise LinkError("The connection closed before the whole file "
                                        "arrived.") from exc
                    if not chunk:
                        break
                    if len(head) < 4096:
                        head += chunk[:4096 - len(head)]
                    done += len(chunk)
                    if done > limit:
                        raise LinkError(f"That file is larger than the {human_size(limit)} "
                                        "limit.")
                    handle.write(chunk)
                    if progress is not None:
                        progress(done, total)
                handle.flush()
                os.fsync(handle.fileno())
        if done == 0:
            raise LinkError("The server sent an empty file.")
        found = sniff(head)
        if found is None:
            raise LinkError(_not_media_message(content_type))
        suffix, media_type = found
        if not _validate(partial, suffix, media_type):
            raise LinkError(f"The downloaded file could not be read as {media_type}. It may be "
                            "damaged or use a format this build cannot decode.")
        if not preserve and media_type == "image" and suffix != ".gif":
            # "Preserve original" off: a still becomes a PNG, as Smart Paste used to do.
            suffix = _reencode_png(partial)
        final = destination(folder, info.filename, suffix)
        os.replace(partial, final)
        partial = ""
        width, height, seconds, frames = describe_file(final, suffix, media_type)
        imported = replace(info, suffix=suffix, filename=os.path.basename(final),
                           media_type=media_type, size=done, content_type=content_type)
        log.info("imported %s as %s (%s%s)", redact(info.url), os.path.basename(final),
                 human_size(done), f", {frames} frames" if frames > 1 else "")
        return Downloaded(final, imported, done, width, height, seconds, frames)
    except LinkError:
        raise
    except HTTPError as exc:
        log.warning("download %s refused: HTTP %s", redact(info.url), exc.code)
        raise _http_error(exc, info.url) from exc
    except (URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        log.warning("download %s failed: %s", redact(info.url), exc)
        raise _transport_error(exc, info.url) from exc
    except HTTPException as exc:
        log.warning("download %s broke mid-transfer: %s", redact(info.url), exc)
        raise LinkError("The connection closed before the whole file arrived.") from exc
    except OSError as exc:
        log.warning("download %s could not be saved: %s", redact(info.url), exc)
        raise LinkError(f"The file could not be saved: {getattr(exc, 'strerror', None) or exc}") from exc
    finally:
        if partial:
            try:
                os.unlink(partial)
            except OSError:
                pass
