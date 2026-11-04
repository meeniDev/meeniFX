"""The compact recorder panel: three source cards, one big Record button and a small settings gear."""

import os

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider, QSpinBox, QToolButton,
                             QVBoxLayout, QWidget)

from .. import tokens
from ..icons import icon
from ..recording import audiocap, devices
from ..recording.capture import SourceCapture
from ..recording.disk import CAMERA_BLOCKED, MIC_BLOCKED
from ..recording.hotkey import parse_hotkey
from ..recording.settings import RecorderSettings
from .filedialogs import choose_folder

C = tokens.COLORS


class LevelBar(QWidget):
    """Thin horizontal input meter; green, then amber, never red (red means recording)."""

    def __init__(self, parent=None, vertical=False):
        super().__init__(parent)
        self.value = 0.0
        self.vertical = vertical
        self.setFixedHeight(8) if not vertical else self.setFixedWidth(8)
        self.setToolTip("Input level")

    def set_level(self, value):
        value = max(0.0, min(1.0, float(value)))
        # fast attack, slow release
        self.value = value if value > self.value else self.value * 0.82 + value * 0.18
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C["panel_soft"]))
        p.drawRoundedRect(self.rect(), 4, 4)
        color = C["success"] if self.value < 0.85 else C["warning"]
        p.setBrush(QColor(color))
        w = int(self.width() * self.value)
        if w > 0:
            p.drawRoundedRect(0, 0, w, self.height(), 4, 4)


class SourceCard(QFrame):
    """A titled card with an on/off switch and a body."""

    toggled = pyqtSignal(bool)

    def __init__(self, icon_name, title, parent=None):
        super().__init__(parent)
        self.setObjectName("recorderCard")
        self.setStyleSheet(
            f"#recorderCard{{background:{C['panel_alt']};border:1px solid {C['border']};border-radius:{tokens.RADIUS + 2}px;}}")
        root = QVBoxLayout(self)
        root.setContentsMargins(tokens.SP3, tokens.SP2, tokens.SP3, tokens.SP3)
        root.setSpacing(tokens.SP2)
        head = QHBoxLayout()
        head.setSpacing(tokens.SP2)
        glyph = QLabel()
        glyph.setPixmap(icon(icon_name).pixmap(QSize(16, 16)))
        self.title = QLabel(title)
        self.title.setStyleSheet(f"font-size:{tokens.FONT_TITLE}px;font-weight:600;")
        self.switch = QCheckBox()
        self.switch.setToolTip(f"{title} on/off")
        self.switch.toggled.connect(self._toggled)
        head.addWidget(glyph)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.switch)
        root.addLayout(head)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(tokens.SP2)
        root.addWidget(self.body)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{C['warning']};font-size:{tokens.FONT_SMALL}px;")
        self.note.hide()
        root.addWidget(self.note)

    def _toggled(self, on):
        self.body.setVisible(on)
        self.toggled.emit(on)

    def set_note(self, text):
        self.note.setText(text or "")
        self.note.setVisible(bool(text))

    def is_on(self):
        return self.switch.isChecked()

    def set_on(self, on):
        self.switch.blockSignals(True)
        self.switch.setChecked(on)
        self.switch.blockSignals(False)
        self.body.setVisible(on)


def _combo(tip):
    box = QComboBox()
    box.setFixedHeight(tokens.CONTROL_H)
    box.setToolTip(tip)
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(14)
    return box


def _icon_button(name, tip):
    b = QToolButton()
    b.setProperty("role", "icon")
    b.setIcon(icon(name))
    b.setToolTip(tip)
    b.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
    return b


def _preview():
    w = QVideoWidget()
    w.setFixedHeight(92)
    w.setStyleSheet(f"background:{C['bg']};")
    return w


class RecorderSettingsDialog(QDialog):
    """Advanced options: recordings folder, bitrate and hotkeys."""

    def __init__(self, settings, default_folder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recorder settings")
        self.settings = settings
        form = QFormLayout(self)
        form.setContentsMargins(tokens.SP4, tokens.SP4, tokens.SP4, tokens.SP4)
        row = QHBoxLayout()
        self.folder = QLineEdit(settings.folder)
        self.folder.setPlaceholderText(default_folder)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        form.addRow("Folder", row)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(0, 100)
        self.bitrate.setSpecialValueText("Auto")
        self.bitrate.setSuffix(" Mbps")
        self.bitrate.setValue(settings.video_bitrate_mbps)
        self.bitrate.setToolTip("Video bitrate (0 = automatic)")
        form.addRow("Bitrate", self.bitrate)
        self.rate = QComboBox()
        for value in audiocap.RATES:
            self.rate.addItem(f"{value} Hz", value)
        self.rate.setCurrentIndex(max(0, self.rate.findData(settings.audio_rate)))
        self.rate.setToolTip("Sample rate of the recorded WAV files")
        form.addRow("Audio rate", self.rate)
        self.channels = QComboBox()
        self.channels.addItem("Stereo", 2)
        self.channels.addItem("Mono", 1)
        self.channels.setCurrentIndex(max(0, self.channels.findData(settings.audio_channels)))
        form.addRow("Audio channels", self.channels)
        self.mux = QCheckBox("Mix the sound into the video file instead of separate clips")
        self.mux.setChecked(settings.mux_audio)
        self.mux.setToolTip("Off (recommended): microphone and system sound are added as two separate \n"
                            "linked clips you can edit, mute and process independently.\n"
                            "On: they are mixed inside the video, and those separate clips are skipped.")
        form.addRow(self.mux)
        self.stop_key = QLineEdit(settings.hotkey_stop)
        self.pause_key = QLineEdit(settings.hotkey_pause)
        self.stop_key.setToolTip("e.g. Ctrl+Shift+R")
        form.addRow("Stop hotkey", self.stop_key)
        form.addRow("Pause hotkey", self.pause_key)
        self.minimize = QCheckBox("Minimize editor while recording")
        self.minimize.setChecked(settings.minimize_while_recording)
        form.addRow(self.minimize)
        self.encoder = QLabel("Automatic (hardware when available)")
        self.encoder.setToolTip("Chosen by the system's Media Foundation H.264 encoder")
        form.addRow("Encoder", self.encoder)
        self.error = QLabel()
        self.error.setStyleSheet(f"color:{C['warning']};")
        form.addRow(self.error)
        buttons = QHBoxLayout()
        ok, cancel = QPushButton("OK"), QPushButton("Cancel")
        ok.setProperty("primary", True)
        ok.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        form.addRow(buttons)

    def _browse(self):
        folder = choose_folder(self, "Recordings folder", self.folder.text() or "")
        if folder:
            self.folder.setText(folder)

    def _accept(self):
        for edit in (self.stop_key, self.pause_key):
            if parse_hotkey(edit.text()) is None:
                self.error.setText("Hotkeys need a modifier and a key, e.g. Ctrl+Shift+R.")
                return
        if self.stop_key.text().strip().lower() == self.pause_key.text().strip().lower():
            self.error.setText("Stop and pause need different hotkeys.")
            return
        s = self.settings
        s.folder = self.folder.text().strip()
        s.video_bitrate_mbps = self.bitrate.value()
        s.hotkey_stop = self.stop_key.text().strip()
        s.hotkey_pause = self.pause_key.text().strip()
        s.minimize_while_recording = self.minimize.isChecked()
        s.audio_rate = int(self.rate.currentData() or 48000)
        s.audio_channels = int(self.channels.currentData() or 2)
        s.mux_audio = self.mux.isChecked()
        self.accept()


class RecorderPanel(QDialog):
    """Small tool window; stays open next to the editor until Record is pressed."""

    record_clicked = pyqtSignal()
    settings_changed = pyqtSignal()

    def __init__(self, parent=None, default_folder=""):
        super().__init__(parent)
        self.setWindowTitle("Record")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(400)
        self.default_folder = default_folder
        self.settings = RecorderSettings.load()
        self.captures_by_kind = {"webcam": SourceCapture("webcam", self), "screen": SourceCapture("screen", self),
                                 "mic": audiocap.AudioCapture("mic", False, self),
                                 "system": audiocap.AudioCapture("system", True, self)}
        self.busy = False
        self._loading = True

        root = QVBoxLayout(self)
        root.setContentsMargins(tokens.SP3, tokens.SP3, tokens.SP3, tokens.SP3)
        root.setSpacing(tokens.SP2)

        # Webcam
        self.cam_card = SourceCard("camera", "Webcam")
        row = QHBoxLayout()
        self.cam_combo = _combo("Camera")
        self.cam_refresh = _icon_button("refresh", "Refresh devices")
        row.addWidget(self.cam_combo, 1)
        row.addWidget(self.cam_refresh)
        self.cam_card.body_layout.addLayout(row)
        self.mode_combo = _combo("Resolution and frame rate")
        self.cam_card.body_layout.addWidget(self.mode_combo)
        self.cam_preview = _preview()
        self.cam_card.body_layout.addWidget(self.cam_preview)
        root.addWidget(self.cam_card)

        # Screen
        self.screen_card = SourceCard("screen", "Screen")
        row = QHBoxLayout()
        self.screen_mode = _combo("What to capture")
        self.screen_mode.addItem("Entire screen", "screen")
        self.screen_mode.addItem("Window", "window")
        self.screen_target = _combo("Screen or window")
        self.screen_refresh = _icon_button("refresh", "Refresh windows")
        row.addWidget(self.screen_mode)
        row.addWidget(self.screen_target, 1)
        row.addWidget(self.screen_refresh)
        self.screen_card.body_layout.addLayout(row)
        self.screen_preview = _preview()
        self.screen_card.body_layout.addWidget(self.screen_preview)
        root.addWidget(self.screen_card)

        # Microphone
        self.mic_card = SourceCard("mic", "Microphone")
        row = QHBoxLayout()
        self.mic_combo = _combo("Microphone")
        self.mic_refresh = _icon_button("refresh", "Refresh devices")
        row.addWidget(self.mic_combo, 1)
        row.addWidget(self.mic_refresh)
        self.mic_card.body_layout.addLayout(row)
        self.level = LevelBar()
        self.mic_card.body_layout.addWidget(self.level)
        vol = QHBoxLayout()
        vol_icon = QLabel()
        vol_icon.setPixmap(icon("volume").pixmap(QSize(16, 16)))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setToolTip("Microphone gain")
        vol.addWidget(vol_icon)
        vol.addWidget(self.volume, 1)
        self.mic_card.body_layout.addLayout(vol)
        root.addWidget(self.mic_card)

        # System audio (what the speakers play, captured through WASAPI loopback)
        self.system_card = SourceCard("volume", "System audio")
        row = QHBoxLayout()
        self.system_combo = _combo("Output device to capture")
        self.system_refresh = _icon_button("refresh", "Refresh devices")
        row.addWidget(self.system_combo, 1)
        row.addWidget(self.system_refresh)
        self.system_card.body_layout.addLayout(row)
        self.system_level = LevelBar()
        self.system_card.body_layout.addWidget(self.system_level)
        system_vol = QHBoxLayout()
        system_icon = QLabel()
        system_icon.setPixmap(icon("volume").pixmap(QSize(16, 16)))
        self.system_volume = QSlider(Qt.Orientation.Horizontal)
        self.system_volume.setRange(0, 100)
        self.system_volume.setToolTip("System sound gain")
        system_vol.addWidget(system_icon)
        system_vol.addWidget(self.system_volume, 1)
        self.system_card.body_layout.addLayout(system_vol)
        root.addWidget(self.system_card)

        # Footer
        foot = QHBoxLayout()
        foot.setSpacing(tokens.SP2)
        self.voice_over = QCheckBox("Voice-over")
        self.voice_over.setToolTip("Record only the microphone while the timeline plays from the playhead")
        self.countdown = QCheckBox("3-2-1")
        self.countdown.setToolTip("Countdown before recording")
        self.gear = _icon_button("gear", "Recorder settings")
        foot.addWidget(self.voice_over)
        foot.addWidget(self.countdown)
        foot.addStretch(1)
        foot.addWidget(self.gear)
        root.addLayout(foot)
        self.record_button = QPushButton("  Record")
        self.record_button.setIcon(icon("record", C["red"], 20))
        self.record_button.setIconSize(QSize(20, 20))
        self.record_button.setFixedHeight(44)
        self.record_button.setStyleSheet(
            f"QPushButton{{font-size:15px;font-weight:600;background:{C['panel_soft']};border:1px solid {C['red']};"
            f"border-radius:{tokens.RADIUS + 2}px;color:{C['text']};}}"
            f"QPushButton:hover{{background:#3a1f26;}}QPushButton:disabled{{border-color:{C['border']};color:{C['disabled']};}}")
        self.record_button.setToolTip("Start recording")
        root.addWidget(self.record_button)

        self._wire()
        self._load_into_controls()
        self._loading = False

    # --------------------------------------------------------------- wiring
    def _wire(self):
        self.cam_card.toggled.connect(lambda _on: self._source_toggled("webcam"))
        self.screen_card.toggled.connect(lambda _on: self._source_toggled("screen"))
        self.mic_card.toggled.connect(lambda _on: self._source_toggled("mic"))
        self.system_card.toggled.connect(lambda _on: self._source_toggled("system"))
        self.cam_combo.currentIndexChanged.connect(self._camera_changed)
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._apply_webcam())
        self.cam_refresh.clicked.connect(self.refresh_devices)
        self.screen_mode.currentIndexChanged.connect(lambda _i: self._fill_screen_targets())
        self.screen_target.currentIndexChanged.connect(lambda _i: self._apply_screen())
        self.screen_refresh.clicked.connect(self._fill_screen_targets)
        self.mic_combo.currentIndexChanged.connect(lambda _i: self._apply_mic())
        self.mic_refresh.clicked.connect(self.refresh_devices)
        self.system_combo.currentIndexChanged.connect(lambda _i: self._apply_system())
        self.system_refresh.clicked.connect(self.refresh_devices)
        self.volume.valueChanged.connect(self._volume_changed)
        self.system_volume.valueChanged.connect(self._system_volume_changed)
        self.voice_over.toggled.connect(self._voice_over_toggled)
        self.countdown.toggled.connect(lambda _on: self._save())
        self.gear.clicked.connect(self.open_settings)
        self.record_button.clicked.connect(self._record)
        self.captures_by_kind["mic"].levelChanged.connect(self.level.set_level)
        self.captures_by_kind["system"].levelChanged.connect(self.system_level.set_level)
        for kind in ("webcam", "screen", "mic", "system"):
            self.captures_by_kind[kind].failed.connect(self._source_failed)

    def _load_into_controls(self):
        s = self.settings
        self.cam_card.set_on(s.webcam_on)
        self.screen_card.set_on(s.screen_on)
        self.mic_card.set_on(s.mic_on)
        self.system_card.set_on(s.system_on)
        self.countdown.setChecked(s.countdown)
        self.voice_over.setChecked(s.voice_over)
        self.volume.setValue(s.mic_volume)
        self.system_volume.setValue(s.system_volume)
        self.screen_mode.setCurrentIndex(max(0, self.screen_mode.findData(s.screen_mode)))
        self._fill_cameras()
        self._fill_mics()
        self._fill_system()
        self._fill_screen_targets()
        self._sync_voice_over_ui()

    # --------------------------------------------------------------- device lists
    def refresh_devices(self):
        self._loading = True
        self._fill_cameras()
        self._fill_mics()
        self._fill_system()
        self._fill_screen_targets()
        self._loading = False
        self._restart_previews()

    def _fill_cameras(self):
        keep = self.cam_combo.currentData() or self.settings.camera_id
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()
        for d in devices.cameras():
            self.cam_combo.addItem(d.description(), devices.device_id(d))
        picked = devices.pick_camera(keep)
        if picked is not None:
            self.cam_combo.setCurrentIndex(max(0, self.cam_combo.findData(devices.device_id(picked))))
        self.cam_combo.blockSignals(False)
        if self.cam_combo.count() == 0:
            self.cam_card.set_note("No camera found.")
        else:
            self.cam_card.set_note("")
        self._fill_modes()

    def _fill_modes(self):
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        dev = devices.find_by_id(devices.cameras(), self.cam_combo.currentData() or "")
        modes = devices.camera_modes(dev)
        for m in modes:
            self.mode_combo.addItem(m.label, m)
        wanted = devices.pick_mode(modes, self.settings.camera_format)
        if wanted is not None:
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findText(wanted.label)))
        self.mode_combo.blockSignals(False)

    def _fill_endpoints(self, combo, loopback, saved_id, card):
        """Fill a device picker with WASAPI endpoints; keep the saved one when it is there."""
        keep = combo.currentData() or saved_id
        combo.blockSignals(True)
        combo.clear()
        for endpoint in audiocap.endpoints(loopback, refresh=True):
            combo.addItem(endpoint.label, endpoint.id)
        picked = audiocap.pick_endpoint(loopback, keep)
        if picked is not None:
            combo.setCurrentIndex(max(0, combo.findData(picked.id)))
        combo.blockSignals(False)
        return picked

    def _fill_mics(self):
        picked = self._fill_endpoints(self.mic_combo, False, self.settings.mic_id, self.mic_card)
        if picked is None:
            self.mic_card.set_note("No microphone found.")

    def _fill_system(self):
        picked = self._fill_endpoints(self.system_combo, True, self.settings.system_id, self.system_card)
        if picked is None:
            self.system_card.set_note("No output device found.")

    def _fill_screen_targets(self):
        self.screen_target.blockSignals(True)
        self.screen_target.clear()
        if self.screen_mode.currentData() == "window":
            for w in devices.windows():
                self.screen_target.addItem(w.description()[:60], w)
            index = max(0, next((i for i in range(self.screen_target.count())
                                 if self.screen_target.itemData(i).id() == self.settings.window_id), 0))
            self.screen_target.setCurrentIndex(index)
        else:
            for s in devices.screens():
                self.screen_target.addItem(f"{s.name()}  {s.size().width()}x{s.size().height()}", s)
            picked = devices.pick_screen(self.settings.screen_name)
            for i in range(self.screen_target.count()):
                if self.screen_target.itemData(i) is picked:
                    self.screen_target.setCurrentIndex(i)
        self.screen_target.blockSignals(False)
        if not self._loading:
            self._apply_screen()

    # --------------------------------------------------------------- applying selections
    def _permission_note(self, kind):
        from PyQt6.QtCore import QCameraPermission, QMicrophonePermission, Qt as _Qt
        perm = QMicrophonePermission() if kind == "mic" else QCameraPermission()
        status = QApplication.instance().checkPermission(perm)
        if status == _Qt.PermissionStatus.Denied:
            return MIC_BLOCKED if kind == "mic" else CAMERA_BLOCKED
        if status == _Qt.PermissionStatus.Undetermined:
            QApplication.instance().requestPermission(perm, self, lambda *_: self._restart_previews())
        return ""

    def _apply_webcam(self):
        cap = self.captures_by_kind["webcam"]
        if not self.cam_card.is_on() or self.settings.voice_over:
            cap.release()
            return
        note = self._permission_note("webcam")
        dev = devices.find_by_id(devices.cameras(), self.cam_combo.currentData() or "")
        if dev is None:
            cap.release()
            self.cam_card.set_note("No camera found.")
            return
        self.cam_card.set_note(note)
        if note:
            return
        cap.use_camera(dev, self.mode_combo.currentData())
        cap.set_preview(self.cam_preview)
        cap.start_capture()

    def _apply_screen(self):
        cap = self.captures_by_kind["screen"]
        if not self.screen_card.is_on() or self.settings.voice_over:
            cap.release()
            return
        target = self.screen_target.currentData()
        if target is None:
            cap.release()
            self.screen_card.set_note("Nothing to capture here.")
            return
        self.screen_card.set_note("")
        if self.screen_mode.currentData() == "window":
            cap.use_window(target)
        else:
            cap.use_screen(target)
        cap.set_preview(self.screen_preview)
        cap.start_capture()

    def _apply_mic(self):
        cap = self.captures_by_kind["mic"]
        wants = self.mic_card.is_on() or self.settings.voice_over
        if not wants:
            cap.release()
            return
        note = self._permission_note("mic")
        self.mic_card.set_note(note)
        endpoint = audiocap.pick_endpoint(False, self.mic_combo.currentData() or "")
        if endpoint is None or note:
            cap.release()
            if endpoint is None and not note:
                self.mic_card.set_note("No microphone found.")
            return
        cap.use_endpoint(endpoint)
        cap.set_format(self.settings.audio_rate, self.settings.audio_channels)
        cap.set_gain(max(0.0, self.volume.value() / 100.0))
        if not self.busy:
            cap.monitor()

    def _apply_system(self):
        cap = self.captures_by_kind["system"]
        if not self.system_card.is_on() or self.settings.voice_over:
            cap.release()
            return
        endpoint = audiocap.pick_endpoint(True, self.system_combo.currentData() or "")
        if endpoint is None:
            cap.release()
            self.system_card.set_note("No output device found.")
            return
        self.system_card.set_note("")
        cap.use_endpoint(endpoint)
        cap.set_format(self.settings.audio_rate, self.settings.audio_channels)
        cap.set_gain(max(0.0, self.system_volume.value() / 100.0))
        if not self.busy:
            cap.monitor()

    def _restart_previews(self):
        if self.busy or not self.isVisible():
            return
        self._apply_webcam()
        self._apply_screen()
        self._apply_mic()
        self._apply_system()

    def stop_previews(self):
        for cap in self.captures_by_kind.values():
            cap.release()

    def _camera_changed(self):
        self._fill_modes()
        self._apply_webcam()

    def _source_toggled(self, kind):
        self._save()
        {"webcam": self._apply_webcam, "screen": self._apply_screen, "mic": self._apply_mic,
         "system": self._apply_system}[kind]()

    def _volume_changed(self, value):
        self.captures_by_kind["mic"].set_gain(value / 100.0)
        self._save()

    def _system_volume_changed(self, value):
        self.captures_by_kind["system"].set_gain(value / 100.0)
        self._save()

    def _voice_over_toggled(self, on):
        self.settings.voice_over = on
        self._sync_voice_over_ui()
        self._save()
        self._restart_previews()

    def _sync_voice_over_ui(self):
        on = self.settings.voice_over
        self.cam_card.setEnabled(not on)
        self.screen_card.setEnabled(not on)
        self.system_card.setEnabled(not on)
        self.mic_card.setEnabled(True)

    def _source_failed(self, kind, message):
        card = {"webcam": self.cam_card, "screen": self.screen_card, "mic": self.mic_card,
                "system": self.system_card}[kind]
        card.set_note(message)

    # --------------------------------------------------------------- settings
    def collect(self):
        s = self.settings
        s.webcam_on, s.screen_on, s.mic_on = self.cam_card.is_on(), self.screen_card.is_on(), self.mic_card.is_on()
        s.camera_id = self.cam_combo.currentData() or s.camera_id
        mode = self.mode_combo.currentData()
        s.camera_format = mode.key if mode is not None else s.camera_format
        s.screen_mode = self.screen_mode.currentData() or "screen"
        target = self.screen_target.currentData()
        if target is not None:
            if s.screen_mode == "window":
                s.window_id = int(target.id())
            else:
                s.screen_name = target.name()
        s.mic_id = self.mic_combo.currentData() or s.mic_id
        s.mic_volume = self.volume.value()
        s.system_on = self.system_card.is_on()
        s.system_id = self.system_combo.currentData() or s.system_id
        s.system_volume = self.system_volume.value()
        s.countdown = self.countdown.isChecked()
        s.voice_over = self.voice_over.isChecked()
        return s

    def _save(self):
        if self._loading:
            return
        self.collect().save()
        self.settings_changed.emit()

    def open_settings(self):
        dialog = RecorderSettingsDialog(self.settings, self.default_folder, self)
        if dialog.exec():
            self.settings.save()
            self.settings_changed.emit()
            self._restart_previews()

    # --------------------------------------------------------------- recording
    def active_sources(self):
        return self.collect().sources()

    def captures(self):
        """The ready captures for the sources that are switched on.

        Audio captures are given their endpoint from the picker here rather than relying on
        the preview cycle: Record can be pressed while a preview has failed to start, and the
        device a card is showing is what the user asked to record.
        """
        out = {}
        for kind in self.active_sources():
            cap = self.captures_by_kind[kind]
            if cap.device is None and kind in ("mic", "system"):
                loopback = kind == "system"
                combo = self.system_combo if loopback else self.mic_combo
                volume = self.system_volume if loopback else self.volume
                cap.use_endpoint(audiocap.pick_endpoint(loopback, combo.currentData() or ""))
                cap.set_format(self.settings.audio_rate, self.settings.audio_channels)
                cap.set_gain(max(0.0, volume.value() / 100.0))
            if cap.device is not None:
                out[kind] = cap
        return out

    def _record(self):
        self.collect().save()
        self.record_clicked.emit()

    def set_busy(self, busy):
        self.busy = busy
        self.record_button.setEnabled(not busy)
        for w in (self.cam_card, self.screen_card, self.mic_card, self.system_card):
            w.setEnabled(not busy)

    # --------------------------------------------------------------- lifecycle
    def showEvent(self, event):
        super().showEvent(event)
        if not self.busy:
            self._restart_previews()

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self.busy:
            self.stop_previews()
