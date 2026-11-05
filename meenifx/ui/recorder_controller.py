"""Glue between the main window, the recorder panel and the recording session."""

import os
import time

from PyQt6.QtCore import QTimer

from ..recording.capture import RecordingSession
from ..recording.disk import low_space_message
from ..recording.hotkey import GlobalHotkeys
from ..recording.insert import insert_recording, recordings_folder
from ..recording.settings import RecorderSettings
from .recorder_bar import Countdown, FloatingBar
from .recorder_panel import RecorderPanel


class RecorderController:
    def __init__(self, window):
        self.w = window
        self.panel = None
        self.bar = FloatingBar()
        self.countdown = Countdown()
        self.hotkeys = GlobalHotkeys(window)
        self.session = None
        self.state = "idle"            # idle | countdown | recording
        self._minimized = False
        self._voice_started_playback = False
        self._start_frame = 0
        self._jobs = []
        self.bar.pause_clicked.connect(self.toggle_pause)
        self.bar.stop_clicked.connect(self.stop)
        self.countdown.finished.connect(self._begin)
        self.countdown.cancelled.connect(self._abort_countdown)
        self.hotkeys.triggered.connect(self._hotkey)

    # ---------------------------------------------------------------- panel
    def folder(self):
        project = self.w.projects.project if hasattr(self.w.projects, "project") else None
        folder = getattr(project, "folder", None) if project is not None else None
        configured = self.panel.settings.folder if self.panel else ""
        return recordings_folder(folder, configured)

    def open_panel(self):
        if self.state != "idle":
            return
        if self.panel is None:
            self.panel = RecorderPanel(self.w, default_folder=self.folder())
            self.panel.record_clicked.connect(self.start)
            # Both audio sources feed their own meter and the shared level history, so a take
            # with voice and system sound shows both levels side by side from the first second.
            for kind in ("mic", "system"):
                self.panel.captures_by_kind[kind].levelChanged.connect(
                    lambda value, k=kind: self.bar.push_level(k, value))
        self.panel.settings.folder = RecorderSettings.load().folder   # Preferences may have changed it
        self.panel.default_folder = self.folder()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    # ---------------------------------------------------------------- start
    def start(self):
        if self.state != "idle" or self.panel is None:
            return
        panel = self.panel
        settings = panel.collect()
        sources = settings.sources()
        if not sources:
            self.w.toast_message("Turn on a source to record", kind="error")
            return
        captures = panel.captures()
        missing = [k for k in sources if k not in captures]
        if missing:
            names = {"webcam": "No camera available", "screen": "No screen to capture",
                     "mic": "No microphone available", "system": "No audio output device to capture"}
            self.w.toast_message(names[missing[0]] + (" - check the card" if panel.captures_by_kind[missing[0]].device is None else ""),
                                 kind="error")
            return
        for kind, note in ((k, c.note.text()) for k, c in (("webcam", panel.cam_card), ("screen", panel.screen_card),
                                                           ("mic", panel.mic_card), ("system", panel.system_card))
                           if k in sources):
            if note and ("blocked" in note or "in use" in note or "found" in note):
                self.w.toast_message(note, kind="error", duration_ms=6000)
                return
        folder = self.folder()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self.w.toast_message(f"Cannot use recordings folder: {exc.strerror or exc}", kind="error")
            return
        warn = low_space_message(folder, sources)
        if warn:
            self.w.toast_message(warn, kind="error", duration_ms=5000)
        self._start_frame = self.w.engine.session.playhead
        self.state = "countdown"
        panel.set_busy(True)
        panel.hide()
        self._register_hotkeys(settings)
        if settings.countdown:
            self.countdown.begin()
        else:
            self._begin()

    def _register_hotkeys(self, settings):
        ok_stop = self.hotkeys.register("stop", settings.hotkey_stop)
        ok_pause = self.hotkeys.register("pause", settings.hotkey_pause)
        if not (ok_stop and ok_pause):
            self.w.toast_message("A recording hotkey is already used by another app - use the bar", kind="error")

    def _abort_countdown(self):
        self._finish_ui()

    def _begin(self):
        if self.state != "countdown":
            return
        panel, settings = self.panel, self.panel.settings
        captures = panel.captures()
        stamp = time.strftime("%Y-%m-%d %H-%M-%S")
        voice = bool(settings.voice_over)
        self.session = RecordingSession(captures, self.folder(), stamp, self._start_frame, voice_over=voice)
        self.session.finished.connect(self._finished)
        self.session.failed.connect(self._failed)
        self.session.warning.connect(self._warning)
        self.session.elapsed_changed.connect(self.bar.set_elapsed)
        self.session.elapsed_changed.connect(self.w.set_recording_status)
        self.session.state_changed.connect(self._state_changed)
        self.state = "recording"
        self.bar.set_elapsed(0)
        self.bar.set_paused(False)
        self.bar.set_sources(settings.sources())
        if settings.floating_bar:
            self.bar.present()
        else:
            # The main window shows the time and the stop control; no always-on-top bar.
            self.bar.dismiss()
        self.w.set_recording_status(0)
        # Minimizing only makes sense when the floating bar is there to control the take;
        # without it the window itself is the only place with the timer and Stop.
        if settings.minimize_while_recording and not voice and settings.floating_bar:
            self.w.showMinimized()
            self._minimized = True
        self.session.start(settings.video_bitrate_mbps)
        if voice:
            self._voice_started_playback = not self.w._playing and bool(self.w._begin_playback(self._start_frame))

    # ---------------------------------------------------------------- controls
    def toggle_pause(self):
        if self.session is not None and self.state == "recording":
            self.session.toggle_pause()

    def stop(self):
        if self.state == "countdown":
            self.countdown.cancel()
        elif self.session is not None and self.state == "recording":
            self.session.stop()

    def _hotkey(self, name):
        self.stop() if name == "stop" else self.toggle_pause()

    def _state_changed(self, state):
        self.bar.set_paused(state == "paused")
        if self.w._playing and state == "paused" and self._voice_started_playback:
            self.w.stop_playback()

    def _warning(self, message):
        self._warnings = getattr(self, "_warnings", []) + [message]

    # ---------------------------------------------------------------- end
    def _finish_ui(self):
        self.hotkeys.clear()
        self.bar.dismiss()
        self.w.set_recording_status(None)
        if self._voice_started_playback and self.w._playing:
            self.w.stop_playback()
        self._voice_started_playback = False
        if self._minimized:
            self.w.showNormal()
            self.w.raise_()
            self.w.activateWindow()
            self._minimized = False
        self.state = "idle"
        if self.panel is not None:
            self.panel.set_busy(False)

    def _release_later(self):
        # Closing a webcam blocks for ~2.5 s inside Qt, so let the new clips and the toast paint first.
        QTimer.singleShot(400, self._release_previews)

    def _release_previews(self):
        if self.panel is not None and self.state == "idle":
            self.panel.stop_previews()

    def _failed(self, message):
        self._finish_ui()
        self.session = None
        self.w.toast_message(message, kind="error", duration_ms=5000)
        self._release_later()

    def _finished(self, result):
        warnings = list(getattr(self, "_warnings", []))
        self._warnings = []
        self._finish_ui()
        self.session = None
        self._place(result, warnings)

    def _place(self, result, warnings):
        """Mux sound, decode short clips off the GUI thread, then add one undo step."""
        from .main_window import FunctionJob
        from ..recording.mux import mux_recording
        engine = self.w.engine
        wants_mux = bool(self.panel is None or self.panel.settings.mux_audio) and bool(result.segments)

        def preload():
            notes = []
            if wants_mux:
                _kinds, notes = mux_recording(result)
            paths = [s.path for segs in result.segments.values() for s in segs
                     if s.path.lower().endswith((".mkv", ".mp4", ".mov"))]
            for path in paths:
                engine.media.load(path)
            return notes

        job = FunctionJob(preload)
        self._jobs.append(job)

        def done(notes):
            self._jobs.remove(job)
            self._insert(result, warnings + list(notes or []))
            self._release_later()

        def failed(message):
            self._jobs.remove(job)
            self._insert(result, warnings + [f"Could not read a recording: {message}"])
            self._release_later()

        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        self.w.pool.start(job)

    def _insert(self, result, warnings):
        w = self.w
        ids = insert_recording(w.engine.timeline, w.engine.session.stack, result)
        if not ids:
            w.toast_message("Nothing usable was recorded", kind="error")
            return
        w.engine.session.set_selection(set(ids))
        w.engine.session.reveal_requested.emit(result.start_frame)
        for path in {c.source_path for c in map(w.engine.timeline.find_clip, ids) if c is not None and c.source_path.endswith(".wav")}:
            w.waveforms.request(path)
        w._refresh_audio()
        w._refresh_panels()
        w._request_preview()
        if warnings:
            w.toast_message(f"{warnings[0]} What was recorded is kept and added.", kind="error", duration_ms=6000)
        else:
            w.toast_message("Recording added to the timeline")
