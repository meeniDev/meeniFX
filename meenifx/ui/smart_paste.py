"""Smart Paste: Ctrl+V turns whatever is on the clipboard into project media.

The browser's "Copy Image", a screenshot tool, Explorer files, a plain picture URL and a
Discord attachment link all arrive here. Nothing is written before the decision is made: a
text field keeps its own paste, the Media panel imports without touching the timeline, and
everywhere else the timeline inserts at the playhead (or after the selection).

Clipboard pictures are saved as real PNG files in the project's ``Media`` folder, so they
survive restarts, saves and relinking like any other imported file. Nothing depends on the
clipboard after the paste.

A link is only ever fetched after the "Import Media from Link" dialog is accepted, and the
download itself runs on the thread pool (see :mod:`meenifx.media.link_import`): the editor
never blocks, and a cancel leaves no file behind.
"""

import logging
import os
import time
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, QRunnable, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                             QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton,
                             QTextEdit, QVBoxLayout)

from ..constants import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
from ..media import link_import, youtube
from ..project.storage import DATA_DIR
from . import layout, youtube_import

log = logging.getLogger("meenifx.smart_paste")

SUPPORTED = tuple(IMAGE_EXT + VIDEO_EXT + AUDIO_EXT)
# Animated GIFs carry their own frame data: as a file they are a video, not a still.
STILLS = tuple(ext for ext in IMAGE_EXT if ext != ".gif")
DOWNLOAD_LIMIT = link_import.MAX_BYTES

_PLACEMENTS = ("playhead", "after")


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


def image_url(text):
    """The URL a plain-text clipboard holds, when it points at a picture.

    Kept apart from :func:`classify_clipboard` because a still is the only shape the older
    Smart Paste knew about; a web page or a platform link answers "".
    """
    try:
        info = link_import.classify(text)
    except link_import.UnsupportedLink:
        return ""
    if info is None or info.kind not in ("image", "animation"):
        return ""
    return info.url


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


class _ProbeSignals(QObject):
    ready = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class _ProbeJob(QRunnable):
    """Ask the server for the link's headers only. No media bytes are fetched here."""

    def __init__(self, info):
        super().__init__()
        self.info = info
        self.signals = _ProbeSignals()
        self.setAutoDelete(False)

    def run(self):
        try:
            probed = link_import.probe(self.info.url)
        except link_import.LinkError as exc:
            self.signals.failed.emit(self.info.url, exc.message)
            return
        except Exception:
            # Never let a worker exception vanish: it would look like a crash to the user.
            log.exception("link probe failed for %s", link_import.redact(self.info.url))
            self.signals.failed.emit(self.info.url, "That link could not be checked.")
            return
        self.signals.ready.emit(self.info.url, probed)


class _DownloadSignals(QObject):
    progress = pyqtSignal(int, int)
    ready = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)
    cancelled = pyqtSignal(str)


class _DownloadJob(QRunnable):
    """One link download. SmartPaste keeps a reference until it reports back.

    ``setAutoDelete(False)`` means the Python object owns the runnable, so dropping the only
    reference while ``run()`` is executing destroys the object under the worker thread and
    takes the whole process down. That was the Discord GIF crash: the job used to be a local
    variable, so it was collected the moment the dialog returned.
    """

    def __init__(self, info, folder, limit=DOWNLOAD_LIMIT, preserve=True):
        super().__init__()
        self.info = info
        self.folder = folder
        self.limit = limit
        self.preserve = preserve
        self.signals = _DownloadSignals()
        self._cancel = False
        self.setAutoDelete(False)

    def cancel(self):
        self._cancel = True

    def _cancelled(self):
        return self._cancel

    def run(self):
        try:
            result = link_import.download(self.info, self.folder, limit=self.limit,
                                          preserve=self.preserve,
                                          progress=self.signals.progress.emit,
                                          cancelled=self._cancelled)
        except link_import.Cancelled:
            self.signals.cancelled.emit(self.info.url)
            return
        except link_import.LinkError as exc:
            self.signals.failed.emit(self.info.url, exc.message)
            return
        except Exception as exc:
            log.exception("link download crashed for %s", link_import.redact(self.info.url))
            self.signals.failed.emit(self.info.url, f"Unexpected error: {exc}")
            return
        self.signals.ready.emit(self.info.url, result)


# ---------------------------------------------------------------------- dialogs
class ImportLinkDialog(QDialog):
    """The consent step: what the link is, and what to do with it once it is here."""

    def __init__(self, parent, info, insert_default=True):
        super().__init__(parent)
        self.info = info
        self.setWindowTitle("Import Media from Link")
        self.setModal(True)
        self.setMinimumWidth(470)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        heading = QLabel("Import Media from Link", self)
        heading.setObjectName("dialogHeading")
        outer.addWidget(heading)
        note = QLabel("Nothing is downloaded until you choose Import. The file is saved into "
                      "the project's Media folder.", self)
        note.setObjectName("dialogNote")
        note.setWordWrap(True)
        outer.addWidget(note)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(6)
        form.addRow("Source", self._text(info.host))
        form.addRow("Media", self._text(info.type_label))
        form.addRow("File", self._text(info.filename or "Unknown"))
        form.addRow("Size", self._text(link_import.human_size(info.size) if info.size
                                       else "Checked while downloading"))
        if info.expires:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.expires))
            form.addRow("Link expires", self._text(stamp))
        details = QLabel("Dimensions, duration and frame count are read from the file once "
                         "it has downloaded.", self)
        details.setObjectName("dialogNote")
        details.setWordWrap(True)
        outer.addLayout(form)
        outer.addWidget(details)

        self.add_box = QCheckBox("Add to Project Media", self)
        self.add_box.setChecked(True)
        self.insert_box = QCheckBox("Insert at Playhead", self)
        self.insert_box.setChecked(bool(insert_default))
        self.preserve_box = QCheckBox("Preserve original file when possible", self)
        self.preserve_box.setChecked(True)
        self.preserve_box.setEnabled(info.kind == "image")
        self.preserve_box.setToolTip(
            "Off: a downloaded still is re-encoded as PNG. GIFs, videos and audio always keep "
            "their original file." if info.kind == "image" else
            "Animated GIFs, videos and audio are always kept as their original file.")
        for box in (self.add_box, self.insert_box, self.preserve_box):
            outer.addWidget(box)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.add_box.toggled.connect(self._sync)
        self.insert_box.toggled.connect(self._sync)
        outer.addWidget(self.buttons)
        self._sync()
        self.setToolTip(link_import.redact(info.url))

    def _text(self, value):
        label = QLabel(str(value), self)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _sync(self):
        """Import needs a destination: adding to the panel, the timeline, or both."""
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.add_box.isChecked() or self.insert_box.isChecked())

    def options(self):
        return {"add": self.add_box.isChecked(), "insert": self.insert_box.isChecked(),
                "preserve": self.preserve_box.isChecked()}


class DownloadProgressDialog(QDialog):
    """A non-blocking progress window with a Cancel button of its own."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent, info):
        super().__init__(parent)
        self.setWindowTitle("Importing Media")
        self.setModal(False)
        self.setMinimumWidth(380)
        self._finished = False
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        self.label = QLabel(f"Downloading {info.filename or info.host}", self)
        self.label.setWordWrap(True)
        outer.addWidget(self.label)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 0)                    # busy until the server declares a size
        self.bar.setTextVisible(False)
        outer.addWidget(self.bar)
        self.status = QLabel("Starting\u2026", self)
        self.status.setObjectName("dialogNote")
        outer.addWidget(self.status)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self._cancel)
        outer.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignRight)

    def set_progress(self, done, total):
        if self._finished:
            return
        if total > 0:
            self.bar.setRange(0, int(total))
            self.bar.setValue(min(int(done), int(total)))
            self.status.setText(f"{link_import.human_size(done)} of {link_import.human_size(total)}")
        else:
            self.status.setText(link_import.human_size(done))

    def _cancel(self):
        if self._finished:
            return
        self.cancel_button.setEnabled(False)
        self.status.setText("Cancelling\u2026")
        self.cancel_requested.emit()

    def closeEvent(self, event):
        # Closing the window is a cancel, not a silent "keep downloading in the background".
        if not self._finished:
            self._cancel()
        super().closeEvent(event)

    def finish(self):
        self._finished = True
        self.close()
        self.deleteLater()


# ------------------------------------------------------------------- controller
class SmartPaste(QObject):
    """The Ctrl+V dispatcher: text stays text, pictures, files and links become media."""

    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self._serial = 0
        self._internal_copy_serial = -1
        self._decoding = {}
        self._links = {}            # url -> running probe/download job (owned, never dropped)
        self._progress = {}         # url -> DownloadProgressDialog
        self._options = {}          # url -> the choices made in the import dialog
        self._youtube_jobs = {}     # video URL -> running metadata or import job (owned)
        self._youtube_dialogs = {}  # video URL -> the window still receiving metadata
        self._youtube_open = set()  # video URLs whose window is inside exec() right now
        self._retired = []          # finished runnables, released only after their thread ends
        self._youtube_progress = {}  # video URL -> YouTubeProgressDialog
        self._youtube_requests = {}  # video URL -> the ImportRequest being downloaded
        self._still_queue = []
        self._queue_frame = None
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.dataChanged.connect(self._clipboard_changed)

    # ---- state ---------------------------------------------------------
    def _retire(self, job):
        """Release a job a moment after it reported, never while its thread may still run.

        ``setAutoDelete(False)`` means Python owns the runnable; dropping the last reference
        during ``run()`` destroys it under the worker thread and takes the process down with
        no traceback. The signal is emitted before ``run()`` returns, so the reference is kept
        for a beat longer.
        """
        if job is None:
            return
        self._retired.append(job)

        def release():
            try:
                self._retired.remove(job)
            except ValueError:
                pass

        QTimer.singleShot(1500, release)

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
            text = mime.text()
            try:
                link = youtube.parse_url(text)
            except youtube.YouTubeError as exc:
                return {"kind": "youtube_error", "message": exc.message}
            if link is not None:
                return {"kind": "youtube", "link": link}
            try:
                info = link_import.classify(text)
            except link_import.UnsupportedLink:
                info = None
            if info is not None:
                return {"kind": "url", "info": info}
        return None

    def has_media(self):
        return self.payload() is not None

    def unsupported_link_note(self):
        """Why a pasted link cannot be imported, when the clipboard holds one at all."""
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None or not mime.hasText():
            return ""
        try:
            link_import.classify(mime.text())
        except link_import.UnsupportedLink as exc:
            return exc.message
        return ""

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
        note = self.unsupported_link_note()
        if note:
            self.w.toast_message(note, kind="info", duration_ms=6000)
            return True
        self.w.toast_message("Nothing on the clipboard to paste", kind="info")
        return False

    def _import_payload(self, payload):
        kind = payload.get("kind")
        if kind == "youtube_error":
            self.w.toast_message(payload["message"], kind="error", duration_ms=8000)
            return
        project = self.w.projects.project
        if project is None:
            self.w.toast_message("Open or create a project before pasting media", kind="error")
            return
        folder = managed_folder(project)
        if kind == "url":
            self._probe_link(payload["info"])
            return
        if kind == "youtube":
            self._open_youtube(payload["link"])
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

    # ---- links ---------------------------------------------------------
    def _probe_link(self, info):
        """Check the link's headers first, off the GUI thread, then ask what to do."""
        if info.url in self._links:
            self.w.toast_message("That link is already being imported", kind="info")
            return
        job = _ProbeJob(info)
        job.signals.ready.connect(self._link_probed)
        job.signals.failed.connect(self._link_failed)
        self._links[info.url] = job
        self.w.toast_message(f"Checking {info.host}\u2026", kind="info", duration_ms=1500)
        self.w.pool.start(job)

    def _link_probed(self, url, info):
        self._retire(self._links.pop(url, None))
        if self.w.projects.project is None:
            self.w.toast_message("Open or create a project before importing media", kind="error")
            return
        dialog = ImportLinkDialog(self.w, info,
                                  insert_default=not self._media_panel_active())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            log.info("link import cancelled before download: %s", link_import.redact(url))
            return
        self._start_download(info, dialog.options())

    def _link_failed(self, url, message):
        self._retire(self._links.pop(url, None))
        log.warning("link %s could not be imported: %s", link_import.redact(url), message)
        self.w.toast_message(message, kind="error", duration_ms=7000)

    def _start_download(self, info, options):
        project = self.w.projects.project
        if project is None:
            self.w.toast_message("Open or create a project before importing media", kind="error")
            return
        if info.url in self._links:
            self.w.toast_message("That link is already being imported", kind="info")
            return
        job = _DownloadJob(info, managed_folder(project), preserve=options.get("preserve", True))
        progress = DownloadProgressDialog(self.w, info)
        job.signals.progress.connect(progress.set_progress)
        job.signals.ready.connect(self._download_ready)
        job.signals.failed.connect(self._download_failed)
        job.signals.cancelled.connect(self._download_cancelled)
        progress.cancel_requested.connect(job.cancel)
        self._links[info.url] = job       # keeps the runnable alive while it runs
        self._progress[info.url] = progress
        self._options[info.url] = options
        progress.show()
        self.w.pool.start(job)

    def _end_link(self, url):
        self._retire(self._links.pop(url, None))
        self._options.pop(url, None)
        progress = self._progress.pop(url, None)
        if progress is not None:
            progress.finish()

    def _download_ready(self, url, result):
        options = self._options.get(url) or {}
        self._end_link(url)
        if self.w.projects.project is None:
            self.w.toast_message(f"Downloaded {os.path.basename(result.path)}",
                                 kind="info", duration_ms=4000)
            return
        summary = result.summary()
        self.w.toast_message(f"Imported {os.path.basename(result.path)}"
                             + (f" \u00b7 {summary}" if summary else ""))
        if options.get("add", True):
            self._add_to_library([result.path])
        if options.get("insert", True):
            self._insert([result.path], self.w.engine.session.playhead)
        elif options.get("add", True):
            self.w._refresh_panels()

    def _download_failed(self, url, message):
        self._end_link(url)
        log.warning("link download failed for %s: %s", link_import.redact(url), message)
        self.w.toast_message(f"Import failed: {message}", kind="error", duration_ms=8000)

    def _download_cancelled(self, url):
        self._end_link(url)
        log.info("link download cancelled: %s", link_import.redact(url))
        self.w.toast_message("Import cancelled", kind="info")

    # ---- YouTube -------------------------------------------------------
    def _open_youtube(self, link):
        """Show the window straight away, then fill it as the details arrive."""
        if link.url in self._youtube_jobs:
            self.w.toast_message("That video is already being imported", kind="info")
            return
        dialog = youtube_import.YouTubeImportDialog(
            self.w, link, insert_default=not self._media_panel_active())
        job = youtube_import.MetadataJob(link)
        job.signals.ready.connect(dialog.set_meta)
        job.signals.failed.connect(dialog.set_error)
        job.signals.ready.connect(lambda _meta, url=link.url: self._youtube_meta_finished(url))
        job.signals.failed.connect(lambda _text, url=link.url: self._youtube_meta_finished(url))
        self._youtube_jobs[link.url] = job      # owned while the worker runs
        self._youtube_dialogs[link.url] = dialog
        self.w.pool.start(job)
        self._youtube_open.add(link.url)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._youtube_open.discard(link.url)
        options = dialog.options() if accepted else None
        if link.url in self._youtube_jobs:
            dialog.hide()                       # details still arriving: keep it alive
        else:
            self._youtube_dialogs.pop(link.url, None)
            dialog.deleteLater()
        if not accepted or options is None:
            log.info("YouTube import cancelled before download: %s", link.video_id)
            return
        request = options["request"]
        if options.get("local_path"):
            self._import_local_choice([options["local_path"]], request)
            return
        self._start_youtube(link, dialog.meta, request)

    def _youtube_meta_finished(self, url):
        """The metadata job reported; delete the window only once it is really closed."""
        self._retire(self._youtube_jobs.pop(url, None))
        if url in self._youtube_open:
            return                              # still inside exec(): the window is in use
        dialog = self._youtube_dialogs.pop(url, None)
        if dialog is not None:
            dialog.deleteLater()

    def _import_local_choice(self, paths, request):
        """The way out when a video cannot be downloaded: import a file the user already has."""
        if request.add:
            added = self.w.project_panel.add_files(paths)
            if not added and request.insert:
                pass                            # already in the library: insert it anyway
        if request.insert:
            self._insert(paths)
        else:
            self.w._refresh_panels()

    def _start_youtube(self, link, meta, request):
        project = self.w.projects.project
        if project is None:
            self.w.toast_message("Open or create a project before importing media", kind="error")
            return
        if meta is None:
            self.w.toast_message("The video details were not ready, so nothing was imported",
                                 kind="error")
            return
        if link.url in self._youtube_jobs:
            self.w.toast_message("That video is already being imported", kind="info")
            return
        job = youtube_import.ImportJob(link, meta, request, managed_folder(project))
        progress = youtube_import.YouTubeProgressDialog(self.w, meta)
        job.signals.progress.connect(progress.set_progress)
        job.signals.ready.connect(self._youtube_ready)
        job.signals.failed.connect(self._youtube_failed)
        job.signals.cancelled.connect(self._youtube_cancelled)
        progress.cancel_requested.connect(job.cancel)
        self._youtube_jobs[link.url] = job
        self._youtube_progress[link.url] = progress
        self._youtube_requests[link.url] = request
        progress.show()
        self.w.pool.start(job)

    def _end_youtube(self, url):
        self._retire(self._youtube_jobs.pop(url, None))
        self._youtube_requests.pop(url, None)
        progress = self._youtube_progress.pop(url, None)
        if progress is not None:
            progress.finish()

    def _youtube_ready(self, url, imported):
        request = self._youtube_requests.get(url) or youtube.ImportRequest()
        self._end_youtube(url)
        if self.w.projects.project is None:
            self.w.toast_message(f"Downloaded {os.path.basename(imported.path)}",
                                 kind="info", duration_ms=5000)
            return
        summary = imported.summary()
        self.w.toast_message(f"Imported {os.path.basename(imported.path)}"
                             + (f" \u00b7 {summary}" if summary else ""))
        if request.add:
            self.w.project_panel.add_files([imported.path])
        if request.insert:
            self.w.import_paths([imported.path], self.w.engine.session.playhead, None, True)
        else:
            self.w._refresh_panels()
        self.w.projects.mark_dirty()
        self.w._refresh_audio()
        self.w._request_preview()
        self.w.timeline_panel.canvas.update()

    def _youtube_failed(self, url, message):
        self._end_youtube(url)
        log.warning("YouTube import failed: %s", message)
        self.w.toast_message(f"YouTube import failed: {message}", kind="error", duration_ms=9000)

    def _youtube_cancelled(self, url):
        self._end_youtube(url)
        log.info("YouTube import cancelled: %s", url.split("v=")[-1])
        self.w.toast_message("YouTube import cancelled", kind="info")

    # ---- insertion -----------------------------------------------------
    def _insert_frame(self):
        if PasteSettings.load().placement == "after":
            timeline = self.w.engine.timeline
            selected = [timeline.find_clip(i) for i in self.w.engine.session.selected]
            selected = [clip for clip in selected if clip is not None]
            if selected:
                return max(clip.end for clip in selected)
        return self.w.engine.session.playhead

    def _insert(self, paths, frame=None):
        stills = [p for p in paths if p.lower().endswith(STILLS)]
        moving = [p for p in paths if not p.lower().endswith(STILLS)]
        frame = self._insert_frame() if frame is None else frame
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
        self._retire(self._decoding.pop(path, None))
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
        self._retire(self._decoding.pop(path, None))
        self.w.toast_message(f"Paste failed: {os.path.basename(path)}: {message}", kind="error",
                             duration_ms=4000)
        self._next_still()

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
