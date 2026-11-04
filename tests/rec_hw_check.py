"""Real-device check of the recording core (needs the real 'windows' Qt platform).

usage: python tests/rec_hw_check.py <sources e.g. webcam,screen,mic> <seconds> [pause]
"""
import os
import sys
import tempfile
import wave

import cv2
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from meenifx.recording import audiocap, devices
from meenifx.recording.capture import RecordingSession, SourceCapture

kinds = sys.argv[1].split(",")
seconds = float(sys.argv[2])
with_pause = len(sys.argv) > 3 and sys.argv[3] == "pause"
app = QApplication(sys.argv)
folder = tempfile.mkdtemp(prefix="rec_")
caps = {}
for kind in kinds:
    cap = SourceCapture(kind)
    if kind == "webcam":
        dev = devices.pick_camera("")
        cap.use_camera(dev, devices.default_mode(devices.camera_modes(dev)))
    elif kind == "screen":
        cap.use_screen(devices.pick_screen(""))
    else:
        cap = audiocap.AudioCapture(kind, loopback=(kind == "system"))
        cap.use_endpoint(audiocap.pick_endpoint(kind == "system", ""))
    cap.start_capture()
    caps[kind] = cap
session = RecordingSession(caps, folder, "2026-01-01 00-00-00", 0)
session.warning.connect(lambda m: print("WARNING", m, flush=True))


def report(result):
    for kind, segs in result.segments.items():
        for s in segs:
            if s.path.endswith(".wav"):
                with wave.open(s.path) as w:
                    dur = w.getnframes() / w.getframerate()
            else:
                cap = cv2.VideoCapture(s.path)
                dur = cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 1)
                size = (int(cap.get(3)), int(cap.get(4)), round(cap.get(5), 2))
                cap.release()
                dur = (round(dur, 3), size)
            print(kind, os.path.basename(s.path), "offset", round(s.offset, 2), "wall", round(s.length, 2), "file", dur, flush=True)
    print("DONE", flush=True)
    os._exit(0)


session.finished.connect(report)
session.failed.connect(lambda m: (print("FAILED", m, flush=True), os._exit(1)))
QTimer.singleShot(2500, session.start)
if with_pause:
    QTimer.singleShot(2500 + int(seconds * 500), session.pause)
    QTimer.singleShot(2500 + int(seconds * 500) + 1500, session.resume)
    QTimer.singleShot(2500 + int(seconds * 1000) + 1500, session.stop)
else:
    QTimer.singleShot(2500 + int(seconds * 1000), session.stop)
QTimer.singleShot(int((seconds + 40) * 1000), lambda: (print("TIMEOUT", flush=True), os._exit(2)))
app.exec()
