"""YouTube imports, kept behind one service boundary.

Smart Paste recognises a YouTube link in :func:`parse_url`; the rest of this module talks to
yt-dlp and to the bundled ffmpeg and never to a widget, so the dialog, the editor and the
tests all drive the same code. Nothing here promises more than it can do:

* a link has to name one video, and only the hosts below count as YouTube - a domain that
  merely contains the word does not;
* metadata is read without downloading anything, and a video that is private, members-only,
  age-gated, geo-blocked, live or DRM-protected is refused with a reason. No sign-in, region
  or DRM workaround is attempted;
* a download is written into a temporary folder inside the project's ``Media`` folder,
  trimmed and muxed by ffmpeg, validated, and only then moved into place, so a cancelled or
  failed import leaves nothing behind;
* audio-only imports are ordinary audio files (``.m4a``), which the timeline, waveforms,
  effects and Auto Captions already understand.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, urlopen

from . import link_import
from .hardware import ffmpeg_exe

log = logging.getLogger("meenifx.youtube")

# Only these hosts are YouTube. ``notyoutube.com`` or ``youtube.example.com`` are not.
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be", "www.youtu.be",
})
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_FORMS = re.compile(r"^/(?:shorts|live|embed|v)/([^/]+)", re.IGNORECASE)
_CLOCK_HINT = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$", re.IGNORECASE)

THUMBNAIL_BYTES = 5 * 1024 * 1024
THUMBNAIL_TIMEOUT = 8.0
METADATA_TIMEOUT = 20.0
SOCKET_TIMEOUT = 15.0
MIN_FREE_SLACK = 64 * 1024 * 1024
MODES = ("video_audio", "video_only", "audio_only")
MODE_LABELS = {"video_audio": "Video + Audio", "video_only": "Video Only",
               "audio_only": "Audio Only"}
USER_AGENT = "meeniFX Studios"


# ---------------------------------------------------------------------- problems
class YouTubeError(Exception):
    """A YouTube import problem the dialog can explain to the person importing it."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class YouTubeCancelled(YouTubeError):
    """The import was cancelled; distinct so the caller does not report it as a failure."""

    def __init__(self):
        super().__init__("Import cancelled.")


# ------------------------------------------------------------------------ links
@dataclass(frozen=True)
class YouTubeLink:
    """One video, and the timestamp the link itself asked for (``?t=`` / ``#t=``)."""

    video_id: str
    url: str
    start: float = 0.0
    host: str = "youtube.com"

    @property
    def watch_url(self):
        return f"https://www.youtube.com/watch?v={self.video_id}"


def _clock_seconds(text):
    """``90``, ``1m30s``, ``01:30`` and ``1:02:03`` all become seconds."""
    text = (text or "").strip().lower()
    if not text:
        return 0.0
    if ":" in text:
        try:
            parts = [float(piece) for piece in text.split(":")]
        except ValueError:
            return 0.0
        if len(parts) > 3 or any(part < 0 for part in parts):
            return 0.0
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    match = _CLOCK_HINT.match(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(value or 0) for value in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    try:
        return max(0.0, float(text))
    except ValueError:
        return 0.0


def _start_hint(query, fragment):
    for key in ("t", "start", "time_continue"):
        if key in query:
            return min(24 * 3600.0, _clock_seconds(query[key]))
    fragment = (fragment or "").strip()
    if fragment.startswith("t="):
        return min(24 * 3600.0, _clock_seconds(fragment[2:]))
    return 0.0


def parse_url(text):
    """The YouTube video a clipboard holds, or None when the text is not a YouTube link.

    Raises :class:`YouTubeError` for a YouTube address that does not name exactly one usable
    video (a playlist, a channel, a broken or invented ID) so the editor can say why.
    """
    text = (text or "").strip()
    if not text or len(text) > link_import.MAX_URL or any(ch.isspace() for ch in text):
        return None
    if not text.lower().startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if host not in YOUTUBE_HOSTS:
        return None
    if parts.username or parts.password:
        raise YouTubeError("That link carries a username and password. Paste the plain video "
                           "link instead.")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    path = parts.path.rstrip("/")
    video_id = ""
    if host in ("youtu.be", "www.youtu.be"):
        video_id = path.lstrip("/").split("/")[0]
    elif path == "/watch":
        video_id = str(query.get("v") or "").strip()
    else:
        match = _PATH_FORMS.match(path)
        if match:
            video_id = match.group(1)
    if not video_id:
        if query.get("list") or path.startswith(("/playlist", "/channel", "/c/", "/user/",
                                                  "/results", "/feed", "/@")):
            raise YouTubeError("That is a YouTube playlist or channel link. Paste the link to "
                               "one video instead.")
        raise YouTubeError("That YouTube link does not name a video.")
    if not VIDEO_ID.match(video_id):
        raise YouTubeError("That YouTube link has an invalid video ID.")
    return YouTubeLink(video_id, f"https://www.youtube.com/watch?v={video_id}",
                       _start_hint(query, parts.fragment), host)


# ------------------------------------------------------------------ timecodes
def format_timecode(seconds, *, milliseconds=True):
    """``00:01:30.500`` - fixed width, so the fields line up and are easy to compare."""
    seconds = max(0.0, float(seconds or 0.0))
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    if milliseconds:
        text += f".{int(round((seconds - whole) * 1000)):03d}"
    return text


def parse_timecode(text):
    """Seconds from a timecode field; ``-1`` when the text is not a time at all."""
    text = (text or "").strip()
    if not text or not re.fullmatch(r"-?\d{1,2}(?::\d{1,2}){0,2}(?:[.,]\d{1,3})?", text):
        return -1.0
    text = text.replace(",", ".")
    parts = text.split(":")
    try:
        numbers = [float(piece) for piece in parts]
    except ValueError:
        return -1.0
    seconds, factor = 0.0, 1.0
    for piece in reversed(numbers):
        seconds += piece * factor
        factor *= 60.0
    return seconds


def check_range(start, end, duration, *, minimum=0.05):
    """Explain why a start/end pair cannot be imported, or return "" when it is fine."""
    if start < 0:
        return "The start time cannot be negative."
    if end <= start:
        return "The end time must be after the start time."
    if end - start < minimum:
        return "The selection is too short to import."
    if duration and end > duration + 0.01:
        return f"The end time is past the end of the video ({format_timecode(duration)})."
    return ""


# --------------------------------------------------------------------- formats
@dataclass(frozen=True)
class VideoFormat:
    """One stream yt-dlp reported; only fields the extractor actually sent are filled."""

    format_id: str
    ext: str = "mp4"
    vcodec: str = "none"
    acodec: str = "none"
    height: int = 0
    width: int = 0
    fps: float = 0.0
    abr: float = 0.0
    tbr: float = 0.0
    filesize: int = 0

    @property
    def has_video(self):
        return self.vcodec not in ("", "none")

    @property
    def has_audio(self):
        return self.acodec not in ("", "none")

    @property
    def is_audio_only(self):
        return self.has_audio and not self.has_video

    @property
    def label(self):
        if self.is_audio_only:
            rate = f" · {self.abr:.0f} kbps" if self.abr else ""
            return f"{self.format_id} · {self.ext} audio{rate}"
        height = f"{self.height}p" if self.height else "video"
        sound = " + audio" if self.has_audio else ""
        size = f" · {link_import.human_size(self.filesize)}" if self.filesize else ""
        return f"{self.format_id} · {self.ext} · {height}{sound}{size}"


@dataclass(frozen=True)
class Choice:
    """One option in the quality dropdown, built from formats the video really offers."""

    key: str
    label: str
    height: int = 0
    video: str = ""      # video-only format id
    audio: str = ""      # audio-only format id
    muxed: str = ""      # a format that already carries sound
    abr: float = 0.0


@dataclass(frozen=True)
class VideoMeta:
    """What the import dialog shows before anything is downloaded."""

    link: YouTubeLink
    title: str
    channel: str
    duration: float
    formats: tuple[VideoFormat, ...]
    thumbnail: bytes = b""
    thumbnail_type: str = ""
    live: bool = False
    partial_hint: bool = False       # the source advertises starting a download mid-video

    @property
    def video_formats(self):
        return tuple(f for f in self.formats if f.has_video)

    @property
    def audio_formats(self):
        return tuple(f for f in self.formats if f.is_audio_only)

    @property
    def heights(self):
        return sorted({f.height for f in self.formats if f.has_video and f.height}, reverse=True)

    @property
    def video_choices(self):
        return video_choices(self)

    @property
    def audio_choices(self):
        return audio_choices(self)


def _best(fmts, key):
    return max(fmts, key=key) if fmts else None


def _size_of(fmt):
    return fmt.filesize if fmt and fmt.filesize else 0


def _pick_video(formats, height=None):
    """The best picture at (or below) a height: high quality, small file breaks the tie."""
    candidates = [f for f in formats if f.has_video]
    if height:
        exact = [f for f in candidates if f.height == height]
        candidates = exact or [f for f in candidates if f.height <= height]
    if not candidates:
        return None
    separate = [f for f in candidates if not f.has_audio]
    pool = separate or candidates
    return max(pool, key=lambda f: (f.height, f.fps, f.tbr, _size_of(f)))


def _pick_audio(formats):
    return _best([f for f in formats if f.is_audio_only], lambda f: (f.abr, f.tbr, _size_of(f)))


def _best_muxed(formats):
    return _best([f for f in formats if f.has_video and f.has_audio],
                 lambda f: (f.height, f.fps, f.tbr, _size_of(f)))


def video_choices(meta):
    """Quality options that exist for this video - never a resolution the source lacks."""
    formats = meta.formats
    audio = _pick_audio(formats)
    best = _pick_video(formats)
    choices = []
    if best is not None:
        muxed = "" if audio else (_best_muxed(formats).format_id if best.has_audio else "")
        choices.append(Choice("best", f"Best available ({best.height}p)" if best.height
                              else "Best available", best.height, best.format_id,
                              audio.format_id if audio else "",
                              best.format_id if best.has_audio else muxed))
    for height in meta.heights:
        pick = _pick_video(formats, height)
        if pick is None:
            continue
        choices.append(Choice(str(height), f"{height}p", height, pick.format_id,
                              audio.format_id if audio else "",
                              pick.format_id if pick.has_audio else ""))
    return tuple(choices)


def audio_choices(meta):
    """Audio quality options, in the order the source offers them."""
    formats = list(meta.audio_formats)
    if not formats:
        muxed = _best_muxed(meta.formats)
        return (Choice("best", "Best available", audio=muxed.format_id if muxed else ""),)
    formats.sort(key=lambda f: (f.abr, f.tbr), reverse=True)
    choices = [Choice("best", "Best available"
                      + (f" ({formats[0].abr:.0f} kbps)" if formats[0].abr else ""),
                      audio=formats[0].format_id, abr=formats[0].abr)]
    seen = {round(formats[0].abr)}
    for fmt in formats[1:]:
        bucket = round(fmt.abr)
        if not bucket or bucket in seen:
            continue
        seen.add(bucket)
        choices.append(Choice(f"audio-{bucket}", f"{bucket} kbps", audio=fmt.format_id,
                              abr=fmt.abr))
    return tuple(choices)


def choices_for(meta, mode):
    return audio_choices(meta) if mode == "audio_only" else video_choices(meta)


def find_choice(meta, mode, key):
    choices = choices_for(meta, mode)
    for choice in choices:
        if choice.key == key:
            return choice
    return choices[0] if choices else None


def _clean_formats(info):
    formats = []
    for raw in info.get("formats") or ():
        if not isinstance(raw, dict):
            continue
        format_id = str(raw.get("format_id") or "")
        ext = str(raw.get("ext") or "")
        if not format_id or ext in ("mhtml", "json"):
            continue
        # A direct media URL (the generic extractor) reports no codecs at all; the file itself
        # is a muxed container, so it counts as picture and sound until probed otherwise.
        raw_v, raw_a = raw.get("vcodec"), raw.get("acodec")
        if raw_v is None and raw_a is None:
            raw_v, raw_a = "unknown", "unknown"
        vcodec = str(raw_v or "none")
        acodec = str(raw_a or "none")
        if vcodec == "none" and acodec == "none":
            continue
        try:
            size = int(raw.get("filesize") or raw.get("filesize_approx") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            height = int(raw.get("height") or 0)
            width = int(raw.get("width") or 0)
        except (TypeError, ValueError):
            height = width = 0
        formats.append(VideoFormat(
            format_id, ext or "mp4", vcodec, acodec, height, width,
            float(raw.get("fps") or 0.0), float(raw.get("abr") or 0.0),
            float(raw.get("tbr") or 0.0), max(0, size)))
    return tuple(formats)


def _friendly(message):
    """Turn yt-dlp's English diagnostics into something a person can act on."""
    text = str(message or "").replace("ERROR: ", "").strip()
    low = text.lower()
    if "drm" in low:
        return ("This video is protected by DRM, and meeniFX will not bypass that. You can "
                "import a file you already have.")
    if "private video" in low or "is private" in low:
        return "This video is private. Ask the owner for access, or import a local file."
    if "members-only" in low or "join this channel" in low or "member" in low and "only" in low:
        return ("This video is for channel members only, so it cannot be imported here. "
                "Import a file you are allowed to use instead.")
    if "not a bot" in low or "429" in low or "too many requests" in low:
        return ("YouTube is asking for a sign-in to prove this is not automated traffic. "
                "meeniFX does not sign in, so try again later or import a local file.")
    if "age" in low and ("confirm" in low or "restricted" in low) or "sign in to confirm" in low:
        return ("YouTube wants a signed-in account to confirm this video's age. meeniFX does "
                "not sign in for you, so import a file you already have.")
    if "your country" in low or "blocked it in your country" in low \
            or "geo" in low and "restrict" in low:
        return ("This video is not available in your region, and meeniFX does not work around "
                "regional restrictions.")
    if "live event will begin" in low or "is live" in low or "premieres in" in low:
        return ("This is a live or upcoming stream. Import it after it has finished and "
                "YouTube offers it as a normal video.")
    if "unavailable" in low or "does not exist" in low or "removed" in low:
        return ("This video is unavailable. It may have been deleted, made private, or the "
                "link may be wrong.")
    if "unsupported url" in low:
        return "yt-dlp does not recognise that address as a video it can read."
    if "requested format" in low:
        return "That quality is no longer offered for this video. Pick another one."
    if "timed out" in low or "timeout" in low:
        return "The request to YouTube timed out. Check your connection and try again."
    return text[:300] or "The video details could not be read."


def meta_from_info(link, info, *, thumbnail=b"", thumbnail_type=""):
    """Build a :class:`VideoMeta` from a yt-dlp ``info_dict``; pure, so tests can craft one."""
    if not isinstance(info, dict):
        raise YouTubeError("The video details came back in an unexpected shape.")
    if info.get("_type") == "playlist" or info.get("entries"):
        raise YouTubeError("That link is a playlist. Paste the link to one video instead.")
    status = str(info.get("live_status") or "")
    if info.get("is_live") or status in ("is_live", "is_upcoming"):
        raise YouTubeError("This is a live or upcoming stream. Import it after it has "
                           "finished and YouTube offers it as a normal video.")
    if info.get("has_drm"):
        raise YouTubeError("This video is protected by DRM, and meeniFX will not bypass that. "
                           "You can import a file you already have.")
    formats = _clean_formats(info)
    if not formats:
        raise YouTubeError("This video offers no picture or sound this build can import.")
    duration = 0.0
    try:
        duration = max(0.0, float(info.get("duration") or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    return VideoMeta(
        link=link,
        title=str(info.get("title") or "YouTube video").strip() or "YouTube video",
        channel=str(info.get("channel") or info.get("uploader") or "").strip(),
        duration=duration,
        formats=formats,
        thumbnail=bytes(thumbnail or b""),
        thumbnail_type=str(thumbnail_type or ""),
        partial_hint=bool(duration and any(f.ext in ("m3u8", "mp4", "webm")
                                           for f in formats)),
    )


def _ytdlp_options(**extra):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "no_color": True,
        "cachedir": False,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "ignoreerrors": False,
        # Access restrictions are respected, never worked around.
        "geo_bypass": False,
        "geo_bypass_country": None,
        "age_limit": 0,
        "cookiefile": None,
        "username": None,
        "password": None,
    }
    options.update(extra)
    return options


def _extract_info(url, timeout=METADATA_TIMEOUT):
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:                     # pragma: no cover - depends on the build
        raise YouTubeError("YouTube import needs the yt-dlp package, which is not installed in "
                           "this build. You can still import a local file.") from exc
    try:
        with YoutubeDL(_ytdlp_options(socket_timeout=min(SOCKET_TIMEOUT, timeout))) as ydl:
            return ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise YouTubeError(_friendly(exc)) from exc
    except Exception as exc:                       # a broken extractor must not take the app down
        log.exception("reading YouTube details failed for %s", url)
        raise YouTubeError(_friendly(exc)) from exc


def _thumbnail(info, *, timeout=THUMBNAIL_TIMEOUT):
    """The best thumbnail the extractor offered, downloaded small and checked like a link."""
    candidates = []
    for thumb in reversed(info.get("thumbnails") or ()):
        url = str(thumb.get("url") or "")
        if url.lower().startswith("https://"):
            candidates.append(url)
    if str(info.get("thumbnail") or "").startswith("https://"):
        candidates.append(str(info["thumbnail"]))
    for url in candidates:
        try:
            link_import.check_url(url)
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                kind = str(response.headers.get("Content-Type") or "").split(";")[0].strip()
                if not kind.startswith("image/"):
                    continue
                data = response.read(THUMBNAIL_BYTES + 1)
            if 0 < len(data) <= THUMBNAIL_BYTES:
                return data, kind
        except (link_import.LinkError, HTTPError, URLError, OSError, ValueError) as exc:
            log.debug("thumbnail unavailable: %s", exc)
    return b"", ""


def fetch_metadata(link, *, timeout=METADATA_TIMEOUT, thumbnail=True):
    """Read the video's details without downloading it. Runs off the GUI thread."""
    log.info("reading YouTube details for %s", link.video_id)
    info = _extract_info(link.url, timeout)
    picture, kind = _thumbnail(info) if thumbnail else (b"", "")
    return meta_from_info(link, info, thumbnail=picture, thumbnail_type=kind)


# ------------------------------------------------------------------- planning
@dataclass(frozen=True)
class ImportRequest:
    """The dialog's decisions, in one place so the download never re-reads widgets."""

    mode: str = "video_audio"
    quality: str = "best"
    format_id: str = ""            # advanced: an exact video/muxed format id
    audio_format_id: str = ""      # advanced: an exact audio-only format id
    start: float = 0.0
    end: float = 0.0               # 0 or <= start means "the whole video"
    partial: bool = True
    add: bool = True
    insert: bool = True

    def bounds(self, duration):
        """The requested range. An end past the video is refused, not silently clamped."""
        start = max(0.0, float(self.start or 0.0))
        end = float(self.end or 0.0)
        if end <= start:
            end = float(duration or 0.0)
        return start, end


@dataclass(frozen=True)
class Plan:
    mode: str
    video: str = ""        # format id carrying the picture
    audio: str = ""        # separate audio-only format id
    muxed: bool = False    # the video format already carries sound
    ext: str = "mp4"       # container to download into


def resolve(meta, request):
    """Exactly which streams to fetch, or a :class:`YouTubeError` explaining why not."""
    mode = request.mode if request.mode in MODES else "video_audio"
    formats = {f.format_id: f for f in meta.formats}
    if request.format_id:
        chosen = formats.get(request.format_id)
        if chosen is None:
            raise YouTubeError("That exact format is no longer offered for this video.")
        audio_id = request.audio_format_id if request.audio_format_id in formats else ""
        if mode == "audio_only":
            if chosen.is_audio_only:
                return Plan(mode, audio=chosen.format_id, ext=chosen.ext)
            if chosen.has_audio:
                return Plan(mode, video=chosen.format_id, muxed=True, ext=chosen.ext)
            raise YouTubeError("That format carries no sound to import.")
        if mode == "video_only":
            if not chosen.has_video:
                raise YouTubeError("That format carries no picture.")
            return Plan(mode, video=chosen.format_id, muxed=chosen.has_audio,
                        ext=chosen.ext)
        if not chosen.has_video:
            raise YouTubeError("That format carries no picture.")
        if chosen.has_audio and not audio_id:
            return Plan(mode, video=chosen.format_id, muxed=True, ext=chosen.ext)
        audio = audio_id or (audio_choices(meta)[0].audio if audio_choices(meta) else "")
        if chosen.has_audio:
            audio = ""
        if not audio and not chosen.has_audio:
            raise YouTubeError("This video has no separate sound stream to add.")
        return Plan(mode, video=chosen.format_id, audio=audio, muxed=chosen.has_audio,
                    ext=chosen.ext)

    choice = find_choice(meta, mode, request.quality)
    if choice is None:
        raise YouTubeError("This video offers nothing to import in that mode.")
    if mode == "audio_only":
        if choice.audio and formats.get(choice.audio) is not None:
            audio = formats[choice.audio]
            if audio.is_audio_only:
                return Plan(mode, audio=audio.format_id, ext=audio.ext)
            return Plan(mode, video=audio.format_id, muxed=audio.has_audio, ext=audio.ext)
        muxed = _best_muxed(meta.formats)
        if muxed is None:
            raise YouTubeError("This video has no sound to import.")
        return Plan(mode, video=muxed.format_id, muxed=True, ext=muxed.ext)
    if mode == "video_only":
        video = formats.get(choice.video) or formats.get(choice.muxed)
        if video is None:
            raise YouTubeError("This video has no picture to import.")
        return Plan(mode, video=video.format_id, muxed=video.has_audio, ext=video.ext)
    if choice.muxed and formats.get(choice.muxed) is not None:
        return Plan(mode, video=choice.muxed, muxed=True, ext=formats[choice.muxed].ext)
    video = formats.get(choice.video)
    if video is None:
        raise YouTubeError("This video has no picture to import.")
    audio = choice.audio if choice.audio in formats else (
        _pick_audio(meta.formats).format_id if _pick_audio(meta.formats) else "")
    if video.has_audio:
        return Plan(mode, video=video.format_id, muxed=True, ext=video.ext)
    if not audio:
        raise YouTubeError("This video has no sound stream to combine with the picture.")
    return Plan(mode, video=video.format_id, audio=audio, ext=video.ext)


def estimate_bytes(meta, plan):
    """A rough size for the disk-space check; 0 when the extractor gave no sizes."""
    wanted = {plan.video, plan.audio} - {""}
    total = sum(f.filesize for f in meta.formats if f.format_id in wanted)
    return total


# ------------------------------------------------------------------ downloading
@dataclass(frozen=True)
class Imported:
    """A finished YouTube import: a normal local file the timeline already understands."""

    path: str
    title: str
    seconds: float = 0.0
    width: int = 0
    height: int = 0
    size: int = 0
    partial: bool = False

    def summary(self):
        parts = []
        if self.width and self.height:
            parts.append(f"{self.width}×{self.height}")
        if self.seconds > 0:
            parts.append(format_timecode(self.seconds, milliseconds=False))
        if self.partial:
            parts.append("selected range")
        return " · ".join(parts)


def _check_space(folder, needed):
    if not needed:
        return
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if free < needed + MIN_FREE_SLACK:
        raise YouTubeError(f"Not enough free disk space: this import needs about "
                           f"{link_import.human_size(needed)} and "
                           f"{link_import.human_size(free)} is free.")


def _download_streams(link, plan, folder, *, timeout, progress, cancelled, request,
                      start, end):
    """Fetch the chosen streams with yt-dlp into `folder`; returns {format_id: path}."""
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:                     # pragma: no cover - depends on the build
        raise YouTubeError("YouTube import needs the yt-dlp package, which is not installed "
                           "in this build. You can still import a local file.") from exc
    wanted = [fid for fid in (plan.video, plan.audio) if fid]
    if not wanted:
        raise YouTubeError("Nothing was selected to download.")
    files = {}
    for index, format_id in enumerate(wanted):
        if cancelled is not None and cancelled():
            raise YouTubeCancelled()
        before = set(os.listdir(folder))
        span = 1.0 / len(wanted)
        base = index * span

        def hook(status, _base=base, _span=span):
            if cancelled is not None and cancelled():
                raise YouTubeCancelled()
            if progress is None:
                return
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            done = float(status.get("downloaded_bytes") or 0)
            if status.get("status") == "finished":
                progress("Downloading", _base + _span, _base + _span)
            else:
                progress("Downloading", _base + _span * (done / total if total else 0.0),
                         _base + _span)

        options = _ytdlp_options(
            format=format_id,
            outtmpl=os.path.join(folder, f"{index}-%(format_id)s.%(ext)s"),
            progress_hooks=[hook],
            socket_timeout=min(SOCKET_TIMEOUT, timeout),
        )
        range_ok = (request.partial and plan.mode != "audio_only" and start > 0.001
                    and end > start and not cancelled)
        if range_ok:
            # Ask only for the selected seconds; unsupported sources fall back to the whole
            # file and the ffmpeg pass trims it instead.
            options["download_ranges"] = lambda info_dict, ydl: [
                {"start_time": float(start), "end_time": float(end)}]
            options["force_keyframes_at_cuts"] = False
        try:
            with YoutubeDL(options) as ydl:
                ydl.download([link.url])
        except YouTubeCancelled:
            raise
        except DownloadError as exc:
            if cancelled is not None and cancelled():
                raise YouTubeCancelled() from exc
            if options.get("download_ranges") is not None:
                # A source that cannot start mid-file still deserves the full-download path.
                log.info("range download refused for %s; retrying whole file", format_id)
                return _download_streams(
                    link, plan, folder, timeout=timeout, progress=progress,
                    cancelled=cancelled,
                    request=replace(request, partial=False), start=start, end=end)
            raise YouTubeError(_friendly(exc)) from exc
        except Exception as exc:
            if cancelled is not None and cancelled():
                raise YouTubeCancelled() from exc
            raise YouTubeError(_friendly(exc)) from exc
        created = [name for name in set(os.listdir(folder)) - before
                   if not name.endswith((".part", ".ytdl"))]
        if not created:
            raise YouTubeError("yt-dlp reported success but wrote no file.")
        newest = max((os.path.join(folder, name) for name in created), key=os.path.getsize)
        if os.path.getsize(newest) <= 0:
            raise YouTubeError("The downloaded stream was empty.")
        files[format_id] = newest
    return files


def _ffmpeg():
    exe = ffmpeg_exe()
    if not exe:
        raise YouTubeError("Importing video needs ffmpeg, which this build could not find.")
    return exe


def _compose(files, plan, meta, request, folder, *, progress, cancelled):
    """Trim, mux and encode the downloaded streams into one local file."""
    start, end = request.bounds(meta.duration)
    wanted = end - start if end > start else 0.0
    video_path = files.get(plan.video) if plan.video else None
    audio_path = files.get(plan.audio) if plan.audio else None
    if plan.mode == "audio_only":
        video_path, audio_path = None, video_path or audio_path
    elif plan.mode == "video_only":
        audio_path = None
    if video_path is None and audio_path is None:
        raise YouTubeError("The download produced no usable stream.")

    # A ranged download may already start at `start`; trimming it again would lose seconds.
    precut = False
    probe_path = video_path or audio_path
    if probe_path and request.partial and start > 0.001 and wanted > 0:
        _, _, seconds, _ = link_import.describe_file(probe_path)
        if seconds and abs(seconds - wanted) <= 1.5:
            precut = True

    trim_start = 0.0 if precut else start
    trim_length = wanted if wanted > 0 else 0.0
    if precut:
        trim_length = min(wanted, max(0.0, end - start)) or wanted
    whole = (trim_start <= 0.001 and (not trim_length or
                                      (meta.duration and trim_start + trim_length
                                       >= meta.duration - 0.02)))
    ext = ".m4a" if plan.mode == "audio_only" else ".mp4"
    tag = "" if whole else f" [{format_timecode(start, milliseconds=False).replace(':', '.')}-" \
                           f"{format_timecode(min(end, meta.duration or end), milliseconds=False).replace(':', '.')}]"
    stem = link_import.safe_filename(meta.title or "YouTube video", ext)[:-len(ext)]
    target = link_import.destination(folder, f"{stem}{tag}{ext}", ext)

    simple = (whole and plan.mode == "video_audio" and not plan.audio
              and plan.ext == "mp4")
    if simple:
        # The file already is exactly what was asked for: no reason to re-encode it.
        os.replace(video_path, target)
        if progress:
            progress("Finishing", 1.0, 1.0)
        return _finish(target, plan, start, end, whole)

    command = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    inputs = []
    if video_path:
        inputs.append(video_path)
    if audio_path:
        inputs.append(audio_path)
    if not precut and not whole:
        for path in inputs:
            command += ["-ss", f"{trim_start:.3f}", "-i", path]
    else:
        for path in inputs:
            command += ["-i", path]
    if trim_length and not (precut and whole):
        command += ["-t", f"{trim_length:.3f}"]
    video_index = 0
    audio_index = 1 if video_path else 0
    if plan.mode == "audio_only":
        source = 0
        command += ["-vn", "-map", f"{source}:a:0"]
        command += ["-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-map", f"{video_index}:v:0"]
        if plan.mode == "video_audio":
            if plan.audio:
                command += ["-map", f"{audio_index}:a:0"]
            else:
                command += ["-map", f"{video_index}:a:0?"]
        if whole and not trim_length:
            command += ["-c:v", "copy"]
            if plan.mode == "video_audio":
                command += ["-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-pix_fmt", "yuv420p"]
            if plan.mode == "video_audio":
                command += ["-c:a", "aac", "-b:a", "192k"]
        if plan.mode == "video_only":
            command += ["-an"]
        command += ["-movflags", "+faststart"]
    command += ["-progress", "pipe:1", "-nostats", target]
    _run_ffmpeg(command, wanted or trim_length, progress, cancelled)
    if not os.path.isfile(target) or os.path.getsize(target) == 0:
        raise YouTubeError("ffmpeg did not write the trimmed file.")
    return _finish(target, plan, start, end, whole)


def _ffmpeg_seconds(path):
    """Duration from ffmpeg's own header line; the audio probe cannot read an .m4a."""
    try:
        result = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=25)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _audio_ok(path):
    """True when ffmpeg really decodes a slice of the sound (a sniff alone proves nothing)."""
    try:
        result = subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", path,
                                 "-map", "0:a:0", "-t", "0.2", "-vn", "-f", "null", "-"],
                                capture_output=True, timeout=40)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _finish(target, plan, start, end, whole):
    if link_import.sniff(_head(target)) is None:
        raise YouTubeError("The import finished but the file is not media this build can read.")
    media_type = "audio" if plan.mode == "audio_only" else "video"
    width, height, seconds, _ = link_import.describe_file(target, media_type=media_type)
    if plan.mode == "audio_only":
        if not _audio_ok(target):
            raise YouTubeError("The imported sound file carries no audio this build can read.")
        seconds = seconds or _ffmpeg_seconds(target)
    elif seconds <= 0:
        seconds = _ffmpeg_seconds(target)
    size = os.path.getsize(target)
    return Imported(target, "", seconds, width, height, size, partial=not whole)


def _head(path, count=4096):
    with open(path, "rb") as handle:
        return handle.read(count)


def _run_ffmpeg(command, length, progress, cancelled):
    """Run ffmpeg, report its progress, and stop it promptly on cancel."""
    log.debug("ffmpeg %s", " ".join(command[:12]))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    tail = []
    try:
        for line in process.stdout:
            if cancelled is not None and cancelled():
                process.terminate()
                raise YouTubeCancelled()
            line = line.strip()
            if not line:
                continue
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                raw = line.split("=", 1)[1]
                try:
                    micros = float(raw)
                except ValueError:
                    continue
                seconds = micros / 1_000_000.0
                if length and progress:
                    progress("Trimming", min(1.0, seconds / length), 1.0)
            elif line == "progress=end" and progress:
                progress("Trimming", 1.0, 1.0)
            else:
                tail.append(line)
                del tail[:-12]
        process.wait(timeout=30)
    except YouTubeCancelled:
        try:
            process.kill()
        except OSError:
            pass
        raise
    except Exception:
        try:
            process.kill()
        except OSError:
            pass
        raise
    if process.returncode:
        reason = " ".join(tail[-3:]) or f"ffmpeg exited with code {process.returncode}"
        raise YouTubeError(f"The selected range could not be encoded: {reason[:300]}")


def import_media(link, meta, request, folder, *, progress=None, cancelled=None,
                 timeout=METADATA_TIMEOUT):
    """Download and prepare one import. Returns :class:`Imported`; leaves nothing on failure."""
    if not isinstance(meta, VideoMeta):
        raise TypeError("import_media() needs the VideoMeta that fetch_metadata() returned")
    plan = resolve(meta, request)
    start, end = request.bounds(meta.duration)
    problem = check_range(start, end, meta.duration) if end > start else ""
    if problem:
        raise YouTubeError(problem)
    if end <= start:
        end = meta.duration or start
    os.makedirs(folder, exist_ok=True)
    _check_space(folder, estimate_bytes(meta, plan))
    holder = tempfile.mkdtemp(prefix=".meenifx-yt-", dir=folder)
    log.info("importing YouTube %s (%s, %.1f-%.1f s)", link.video_id, plan.mode, start, end)
    try:
        if progress:
            progress("Downloading", 0.0, 1.0)
        files = _download_streams(link, plan, holder, timeout=timeout, progress=progress,
                                  cancelled=cancelled, request=request, start=start, end=end)
        if cancelled is not None and cancelled():
            raise YouTubeCancelled()
        if progress:
            progress("Preparing", 0.0, 1.0)
        imported = _compose(files, plan, meta, replace(request, start=start, end=end),
                            folder, progress=progress, cancelled=cancelled)
        return replace(imported, title=meta.title)
    except YouTubeCancelled:
        raise
    except YouTubeError:
        raise
    except OSError as exc:
        log.warning("YouTube import could not be saved: %s", exc)
        raise YouTubeError(f"The import could not be saved: "
                           f"{getattr(exc, 'strerror', None) or exc}") from exc
    except Exception as exc:                       # never let a worker exception vanish
        log.exception("YouTube import failed for %s", link.video_id)
        raise YouTubeError(f"Unexpected error: {exc}") from exc
    finally:
        shutil.rmtree(holder, ignore_errors=True)
