"""Qt plumbing between the release service, the windows and the settings.

The checks and the installer download run on the global thread pool, so a slow GitHub
response or a large download never blocks the editor. Every result comes back to the GUI
thread through a signal, and the settings that describe what the user chose (remind, skip,
channel) are written to the application INI, never to a project file.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from ..version import VERSION
from . import download, installer
from .service import ReleaseService
from .settings import UpdateSettings


def cache_path():
    """The cached release list, beside the app's other per-user data."""
    return str(Path.home() / ".meenifx" / "update-cache.json")


def download_folder():
    return Path(tempfile.gettempdir()) / "meeniFX-updates"


class _CheckSignals(QObject):
    done = pyqtSignal(object)


class _CheckJob(QRunnable):
    def __init__(self, service, current, channel, force):
        super().__init__()
        self.service = service
        self.current = current
        self.channel = channel
        self.force = force
        self.signals = _CheckSignals()

    def run(self):
        # ``check`` never raises for network or metadata problems; it returns an error.
        self.signals.done.emit(self.service.check(current=self.current, channel=self.channel,
                                                  force=self.force))


class _DownloadSignals(QObject):
    progress = pyqtSignal(int, int)
    done = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class _DownloadJob(QRunnable):
    """Fetch the checksum file, verify the installer, and only then report success."""

    def __init__(self, update, cancel, opener=None):
        super().__init__()
        self.update = update
        self.cancel = cancel
        self.opener = opener
        self.signals = _DownloadSignals()

    def _open(self):
        from urllib.request import urlopen
        return self.opener or urlopen

    def run(self):
        update = self.update
        try:
            if update.checksums is None:
                raise download.DownloadError(
                    "checksum", "This release does not publish a checksum file, so the "
                                "installer cannot be verified.")
            checksums = download.fetch_checksums(update.checksums.url, opener=self._open())
            digest = checksums.get(update.installer.name)
            if not digest:
                raise download.DownloadError(
                    "checksum", "The release checksum file does not list this installer.")
            target = download_folder() / update.installer.name
            if target.is_file():
                # A previous check may have completed this exact download already.
                ok, _actual = download.verify_file(target, digest)
                if ok:
                    self.signals.done.emit(str(target), update)
                    return
            path = download.download(
                update.installer.url, target, digest,
                expected_size=update.installer_size,
                progress=lambda done, total: self.signals.progress.emit(int(done), int(total)),
                cancel=self.cancel, opener=self._open())
        except download.DownloadError as exc:
            self.signals.failed.emit(exc.kind, exc.message)
            return
        except Exception as exc:                      # never lose a job to a surprise
            self.signals.failed.emit("network", f"The update could not be downloaded: {exc}")
            return
        self.signals.done.emit(str(path), update)


class UpdateManager(QObject):
    """Owns the current update state; one check and one download at a time."""

    check_started = pyqtSignal()
    check_finished = pyqtSignal(object)
    download_started = pyqtSignal()
    download_progress = pyqtSignal(int, int)
    download_ready = pyqtSignal(str, object)
    download_failed = pyqtSignal(str, str)
    update_available = pyqtSignal(object)

    def __init__(self, window=None, store=None, service=None, settings=None):
        super().__init__(window)
        self.window = window
        self.store = store
        self.settings = settings or UpdateSettings.load(store)
        self.service = service or ReleaseService(cache_path())
        self.pending_update = None
        self.last_result = None
        self.checked = False
        self.ready_path = None
        self.ready_update = None
        self.downloading = False
        self._check_job = None
        self._download_job = None
        self._cancel = None
        self._opener = None

    # ------------------------------------------------------------------ checking
    def check(self, manual=False):
        """Start a background check; returns False when one is already running."""
        if self._check_job is not None:
            return False
        self.check_started.emit()
        job = _CheckJob(self.service, VERSION, self.settings.channel, force=manual)
        self._check_job = job
        job.signals.done.connect(lambda result: self._on_checked(result, manual))
        QThreadPool.globalInstance().start(job)
        return True

    def _on_checked(self, result, manual):
        self._check_job = None
        self.last_result = result
        self.checked = True
        if result.ok:
            self.pending_update = result.update
            if result.update is None:
                self.ready_path = None
                self.ready_update = None
            self.settings.mark_checked()
            self.settings.save(self.store)
            if result.update is not None and self.settings.should_notify(result.update,
                                                                         manual=manual):
                self.update_available.emit(result.update)
        self.check_finished.emit(result)
        if result.ok and result.update is not None:
            # Start the transfer after the page has shown the available release, so its
            # status settles on "downloading" rather than being overwritten.
            self._maybe_auto_download(result.update, manual=manual)

    def _maybe_auto_download(self, update, manual=False):
        """Start the download as soon as an update is noticed, for installed builds."""
        if not self.settings.auto_download or not installer.can_auto_install():
            return
        if not self.settings.should_notify(update, manual=manual):
            return
        self.download(update)

    def remind_later(self):
        self.settings.remind_later()
        self.settings.save(self.store)

    def skip(self):
        if self.pending_update is not None:
            self.settings.skip(self.pending_update.version_text)
            self.settings.save(self.store)

    # ------------------------------------------------------------------ downloading
    def download(self, update=None, opener=None):
        """Download and verify the installer for ``update`` in the background."""
        update = update or self.pending_update
        if update is None or self._download_job is not None:
            return False
        if (self.ready_update is not None and self.ready_update.version_text == update.version_text
                and self.ready_path and Path(self.ready_path).is_file()):
            self.download_ready.emit(self.ready_path, update)
            return True
        if update.checksums is None:
            self.download_failed.emit(
                "checksum", "This release does not publish a checksum file, so the "
                            "installer cannot be verified.")
            return False
        self._cancel = threading.Event()
        job = _DownloadJob(update, self._cancel, opener=opener)
        self._download_job = job
        job.signals.progress.connect(self.download_progress.emit)
        job.signals.done.connect(self._on_downloaded)
        job.signals.failed.connect(self._on_download_failed)
        self.downloading = True
        self.download_started.emit()
        QThreadPool.globalInstance().start(job)
        return True

    def cancel_download(self):
        if self._cancel is not None:
            self._cancel.set()

    def _on_downloaded(self, path, update):
        self._download_job = None
        self._cancel = None
        self.downloading = False
        self.ready_path = str(path)
        self.ready_update = update
        self.download_ready.emit(path, update)

    def _on_download_failed(self, kind, message):
        self._download_job = None
        self._cancel = None
        self.downloading = False
        self.download_failed.emit(kind, message)
