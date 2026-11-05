"""Smart Paste: Ctrl+V turns whatever is on the clipboard into project media.

The browser's "Copy Image", a screenshot tool, Explorer files and a plain picture URL all
arrive here. Nothing is written before the decision is made: a text field keeps its own
paste, the Media panel imports without touching the timeline, and everywhere else the
timeline inserts at the playhead (or after the selection).

Clipboard pictures are saved as real PNG files in the project's ``Media`` folder, so they
survive restarts, saves and relinking like any other imported file. Nothing depends on the
clipboard after the paste.
"""

import os
import re
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit

from ..constants import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
from ..project.storage import DATA_DIR
from . import layout

SUPPORTED = tuple(IMAGE_EXT + VIDEO_EXT + AUDIO_EXT)
# Animated GIFs carry their own frame data: as a file they are a video, not a still.
STILLS = tuple(ext for ext in IMAGE_EXT if ext != ".gif")
DOWNLOAD_LIMIT = 32 * 1024 * 1024

_PLACEMENTS = ("playhead", "after")
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_PICTURE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def _flag(value):
    """QSettings returns bools as text in an INI file; accept both shapes."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class PasteSettings:
    """Smart Paste preferences. They live in the app's INI file, never in a project."""

    def __init__(self, seconds=5.0, placement="playhead", select=True, fit=False):
        try:
            self.seconds = max(0.1, min(600.0, float(seconds)))
        except (TypeError, ValueError):
            self.seconds = 5.0
        self.placement = placement if placement in _PLACEMENTS else "playhead"
        self.select = bool(select)
        self.fit = bool(fit)

    @classmethod
    def load(cls, store=None):
        settings = store if store is not None else layout.app_settings()
        try:
            seconds = float(settings.value("paste/seconds", 5.0))
        except (TypeError, ValueError):
            seconds = 5.0
        return cls(seconds,
                   str(settings.value("paste/placement", "playhead")),
                   _flag(settings.value("paste/select", True)),
                   _flag(settings.value("paste/fit", False)))

    def save(self, store=None):
        settings = store if store is not None else layout.app_settings()
        settings.setValue("paste/seconds", float(self.seconds))
        settings.setValue("paste/placement", self.placement)
        settings.setValue("paste/select", bool(self.select))
        settings.setValue("paste/fit", bool(self.fit))
        settings.sync()
        return self


# --------------------------------------------------------------------- storage
def managed_folder(project):
    """Where pasted media is stored: the project's ``Media`` folder, or the app data dir.

    A saved project keeps its clipboard pictures next to itself, so the relative paths in
    ``.mfxpf`` keep working when the whole folder is moved. A project that has never been
    saved falls back to the per-user data folder; those files are referenced absolutely.
    """
    folder = project.folder if project is not None and getattr(project, "path", None) else None
    target = os.path.join(folder, "Media") if folder else os.path.join(DATA_DIR, "Media")
    os.makedirs(target, exist_ok=True)
    return target


def unique_path(folder, stem, suffix):
    """A never-used path such as ``Media/Pasted 20261105-142530.png``."""
    base = time.strftime("Pasted %Y%m%d-%H%M%S")
    candidate = os.path.join(folder, base + suffix)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base} {counter}{suffix}")
        counter += 1
    return candidate


def save_clipboard_image(image, folder):
    """Write clipboard pixels to a PNG, alpha included, and return the file path."""
    if image is None or image.isNull():
        raise ValueError("the clipboard picture is empty")
    picture = image.convertToFormat(QImage.Format.Format_ARGB32)
    path = unique_path(folder, "Pasted", ".png")
    if not picture.save(path, "PNG"):
        raise ValueError("the clipboard picture could not be written to disk")
    return path


def fetch_image(url, folder, limit=DOWNLOAD_LIMIT):
    """Download a remote picture and save it as PNG, or raise ValueError.

    The URL is never trusted: the response must be an image content type, must decode as a
    picture and must stay under the size limit. Since the bytes are re-encoded, the file on
    disk is a validated picture rather than whatever the server returned.
    """
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ValueError("only http(s) links can be downloaded")
    request = Request(url, headers={"User-Agent": "meeniFX Smart Paste"})
    with urlopen(request, timeout=20) as response:
        content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            raise ValueError(f"that link is {content_type or 'not an image'}, not a picture")
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("the picture is larger than 32 MB")
    import cv2
    import numpy as np

    frame = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_UNCHANGED)
    if frame is None:
        raise ValueError("the downloaded file is not a picture this build can read")
    path = unique_path(folder, "Pasted", ".png")
    ok, buffer = cv2.imencode(".png", frame)
    if not ok:
        raise ValueError("the downloaded picture could not be converted")
    buffer.tofile(path)
    return path


def image_url(text):
    """The URL a plain-text clipboard holds, when it points at a picture."""
    text = (text or "").strip()
    if not text or len(text) > 2048 or not _URL.match(text):
        return ""
    return text if urlsplit(text).path.lower().endswith(_PICTURE_SUFFIXES) else ""


def gif_bytes(mime):
    """The real bytes of an animated GIF on the clipboard, when the source provided them."""
    if mime is None or not mime.hasFormat("image/gif"):
        return None
    data = bytes(mime.data("image/gif"))
    return data if data[:3] == b"GIF" else None


# ---------------------------------------------------------------------- workers
class _DecodeSignals(QObject):
    ready = pyqtSignal(str, object, object, float)
    failed = pyqtSignal(str, str)


class _DecodeJob(QRunnable):
    """Decode one pasted file off the GUI thread (a large PNG must not freeze the app)."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.signals = _DecodeSignals()
        self.setAutoDelete(False)

    def run(self):
        from ..media.reader import read_media

        try:
            kind, frames, fps = read_media(self.path)
        except Exception as exc:
            self.signals.failed.emit(self.path, str(exc))
            return
        self.signals.ready.emit(self.path, kind, frames, float(fps))


class _FetchSignals(QObject):
    ready = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


class _FetchJob(QRunnable):
    def __init__(self, url, folder):
        super().__init__()
        self.url = url
        self.folder = folder
        self.signals = _FetchSignals()
        self.setAutoDelete(False)

    def run(self):
        try:
            path = fetch_image(self.url, self.folder)
        except Exception as exc:
            self.signals.failed.emit(self.url, str(exc))
            return
        self.signals.ready.emit(self.url, path)


# ------------------------------------------------------------------- controller
class SmartPaste(QObject):
    """The Ctrl+V dispatcher: text stays text, pictures and files become media."""

    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self._serial = 0
        self._internal_copy_serial = -1
        self._decoding = {}
        self._fetching = set()
        self._still_queue = []
        self._queue_frame = None
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.dataChanged.connect(self._clipboard_changed)

    # ---- state ---------------------------------------------------------
    def _clipboard_changed(self):
        self._serial += 1

    def note_internal_copy(self):
        """An in-app clip copy does not touch the OS clipboard; remember that it is newer."""
        self._internal_copy_serial = self._serial

    def _text_focus(self):
        widget = QApplication.focusWidget()
        return widget if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)) else None

    def _media_panel_active(self):
        widget = QApplication.focusWidget()
        panel = getattr(self.w, "project_panel", None)
        return bool(panel is not None and widget is not None
                    and (widget is panel or panel.isAncestorOf(widget)))

    # ---- clipboard inspection -----------------------------------------
    def payload(self):
        """What the clipboard holds for us: {"kind": image|gif|files|url, ...} or None."""
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None:
            return None
        if mime.hasUrls():
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            supported = [p for p in paths
                         if p and os.path.isfile(p) and p.lower().endswith(SUPPORTED)]
            if supported:
                return {"kind": "files", "paths": supported}
        data = gif_bytes(mime)
        if data:
            return {"kind": "gif", "data": data}
        if mime.hasImage():
            image = clipboard.image()
            if not image.isNull():
                return {"kind": "image", "image": image}
        if mime.hasText():
            url = image_url(mime.text())
            if url:
                return {"kind": "url", "url": url}
        return None

    def has_media(self):
        return self.payload() is not None

    # ---- paste ---------------------------------------------------------
    def paste(self):
        """Ctrl+V. Returns True when the paste was handled here."""
        widget = self._text_focus()
        if widget is not None:
            # A text or numeric field owns its clipboard: type, don't import.
            widget.paste()
            return False
        payload = self.payload()
        from ..model.session import TimelineSession

        internal = bool(getattr(TimelineSession, "_clipboard", None))
        fresh_media = payload is not None and (not internal or self._internal_copy_serial != self._serial)
        if fresh_media:
            self._import_payload(payload)
            return True
        if internal:
            self.w.engine.session.paste()
            return True
        if payload is not None:
            self._import_payload(payload)
            return True
        self.w.toast_message("Nothing on the clipboard to paste", kind="info")
        return False

    def _import_payload(self, payload):
        project = self.w.projects.project
        if project is None:
            self.w.toast_message("Open or create a project before pasting media", kind="error")
            return
        folder = managed_folder(project)
        kind = payload.get("kind")
        if kind == "url":
            if not ask_import_url(self.w, payload["url"]):
                return
            job = _FetchJob(payload["url"], folder)
            job.signals.ready.connect(self._fetched)
            job.signals.failed.connect(self._fetch_failed)
            self._fetching.add(payload["url"])
            self.w.pool.start(job)
            return
        try:
            if kind == "image":
                paths = [save_clipboard_image(payload["image"], folder)]
            elif kind == "gif":
                path = unique_path(folder, "Pasted", ".gif")
                with open(path, "wb") as handle:
                    handle.write(payload["data"])
                paths = [path]
            else:
                paths = list(payload.get("paths") or ())
        except (OSError, ValueError) as exc:
            self.w.toast_message(f"Paste failed: {exc}", kind="error", duration_ms=4000)
            return
        if not paths:
            return
        if self._media_panel_active():
            self._add_to_library(paths)
            return
        self._insert(paths)

    # ---- insertion -----------------------------------------------------
    def _insert_frame(self):
        if PasteSettings.load().placement == "after":
            timeline = self.w.engine.timeline
            selected = [timeline.find_clip(i) for i in self.w.engine.session.selected]
            selected = [clip for clip in selected if clip is not None]
            if selected:
                return max(clip.end for clip in selected)
        return self.w.engine.session.playhead

    def _insert(self, paths):
        stills = [p for p in paths if p.lower().endswith(STILLS)]
        moving = [p for p in paths if not p.lower().endswith(STILLS)]
        frame = self._insert_frame()
        if stills:
            self._still_queue = stills[1:]
            self._queue_frame = frame
            self._decode_still(stills[0])
        if moving:
            self.w.import_paths(moving, frame, None, True)

    def _decode_still(self, path):
        job = _DecodeJob(path)
        job.signals.ready.connect(self._still_ready)
        job.signals.failed.connect(self._still_failed)
        self._decoding[path] = job
        self.w.pool.start(job)

    def _next_still(self):
        if not self._still_queue:
            self._queue_frame = None
            return
        path = self._still_queue.pop(0)
        self._decode_still(path)

    def _still_ready(self, path, kind, frames, fps):
        self._decoding.pop(path, None)
        project = self.w.projects.project
        if project is None:
            self._still_queue = []
            return
        engine = self.w.engine
        settings = PasteSettings.load()
        duration = max(1, int(round(settings.seconds * engine.timeline.fps)))
        try:
            engine.register_decoded(path, kind, frames, fps)
            self._register_library(path)
            ids = engine.add_decoded_image(path, kind, frames, fps, self._insert_frame(), None, True, True,
                                           duration=duration, select=settings.select, fit=settings.fit,
                                           label="Paste image")
        except Exception as exc:
            self.w.toast_message(f"Paste failed: {exc}", kind="error", duration_ms=4000)
            self._next_still()
            return
        if not ids:
            self.w.toast_message("Could not insert: the target track is locked", kind="error")
        self._after_insert()
        self._next_still()

    def _still_failed(self, path, message):
        self._decoding.pop(path, None)
        self.w.toast_message(f"Paste failed: {os.path.basename(path)}: {message}", kind="error",
                             duration_ms=4000)
        self._next_still()

    def _fetched(self, url, path):
        self._fetching.discard(url)
        if self._media_panel_active():
            self._add_to_library([path])
        else:
            self._insert([path])
        self.w.toast_message(f"Downloaded {os.path.basename(path)}")

    def _fetch_failed(self, url, message):
        self._fetching.discard(url)
        self.w.toast_message(f"Could not download that picture: {message}", kind="error", duration_ms=4000)

    # ---- library / refresh ---------------------------------------------
    def _register_library(self, path):
        from ..project import io as project_io

        project = self.w.projects.project
        if project is None:
            return None
        return project_io.register_media(self.w.engine, project.library, path,
                                         self.w.waveforms.library.duration_seconds)

    def _add_to_library(self, paths):
        added = self.w.project_panel.add_files(paths)
        if added:
            self.w.toast_message(f"Added {added} pasted file{'s' if added != 1 else ''} to the media panel")

    def _after_insert(self):
        self.w._refresh_panels()
        self.w._request_preview()
        self.w.timeline_panel.canvas.update()


# --------------------------------------------------------------------- dialogs
def ask_import_url(parent, url):
    """Ask before downloading: a pasted link is not a promise of a picture."""
    from PyQt6.QtWidgets import QMessageBox

    host = urlsplit(url).netloc or url
    answer = QMessageBox.question(
        parent, "Paste image from the web",
        f"Import this picture from {host}?\n\n{url[:300]}\n\n"
        "Nothing is downloaded until you agree, and the file is saved into the project's "
        "Media folder.")
    return answer == QMessageBox.StandardButton.Yes
