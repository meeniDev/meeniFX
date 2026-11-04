"""End-to-end recorder check inside the real MainWindow (needs the real 'windows' Qt platform).

usage: python tests/rec_app_check.py <webcam,screen,mic> <seconds> [flags]
flags: pause  hotkey  voice  unplug  countdown
"""
import ctypes
import os
import sys
import time

import numpy as np
from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QApplication

kinds = sys.argv[1].split(",")
seconds = float(sys.argv[2])
flags = set(sys.argv[3:])
app = QApplication(sys.argv)
app.setOrganizationName("meeniFX")
app.setApplicationName("Studios")

from meenifx.media.cache import MediaEntry  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

MainWindow._load_demo_project = lambda self: None
win = MainWindow(app)
win.resize(1500, 950)
win.show()
out = []


def log(*a):
    print(*a, flush=True)


def pump(s):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


pump(0.5)
tl = win.engine.timeline
if "voice" in flags:
    path = os.path.join(os.environ["TEMP"], "vo_base.png")
    entry = MediaEntry("video", 600, 30)
    entry.frames = [np.full((360, 640, 3), 90, np.uint8)] * 600
    win.engine.media._entries[path] = entry
    win.engine.source_width, win.engine.source_height = 640, 360
    tl.tracks.clear()
    track = tl.add_track("video")
    tl.new_clip("base", path, track, 0, 600, source_out=600, source_fps=30)
win.engine.session.set_playhead(30)
win.open_recorder()
rc = win.recorder
panel = rc.panel
pump(1.0)
panel.cam_card.switch.setChecked("webcam" in kinds)
panel.screen_card.switch.setChecked("screen" in kinds)
panel.mic_card.switch.setChecked("mic" in kinds)
panel.countdown.setChecked("countdown" in flags)
panel.voice_over.setChecked("voice" in flags)
pump(1.5)
log("panel visible", panel.isVisible(), "level after 1.5s", round(panel.level.value, 2))
log("devices", {k: (c.device is not None) for k, c in panel.captures_by_kind.items()}, "target", panel.screen_target.currentText(), panel.screen_card.note_text() if hasattr(panel.screen_card, "note_text") else "")
stack = win.qt_undo_stack
undo_before = len(stack._undo) if hasattr(stack, "_undo") else None
before_clips = len(tl.all_clips())
gaps = []
last = [time.monotonic()]
hb = QTimer()
hb.setInterval(20)
def _beat():
    now = time.monotonic()
    if now - last[0] > 0.25:
        log("GAP", round((now - last[0]) * 1000), "ms at t=", round(now - t0, 2), "state", rc.state)
    gaps.append(now - last[0])
    last[0] = now


hb.timeout.connect(_beat)
t0 = time.monotonic()
hb.start()
rc.start()
t0 = time.monotonic()
log("state", rc.state, "sources", panel.active_sources())
pump(3.5 if "countdown" in flags else 0.8)
log("state after start", rc.state, "bar visible", rc.bar.isVisible(), "minimized", win.isMinimized())
if "pause" in flags:
    pump(seconds / 2)
    rc.toggle_pause()
    pump(1.5)
    log("paused ->", rc.session.state)
    rc.toggle_pause()
    pump(seconds / 2)
elif "unplug" in flags:
    pump(seconds / 2)
    rc.session._source_failed("webcam", "Camera was disconnected.")
else:
    pump(seconds)
if "hotkey" in flags:
    u = ctypes.windll.user32
    for vk, down in ((0x11, 0), (0x10, 0), (0x52, 0), (0x52, 2), (0x10, 2), (0x11, 2)):
        u.keybd_event(vk, 0, down, 0)
else:
    rc.stop()
deadline = time.monotonic() + 40
while time.monotonic() < deadline and (rc.state != "idle" or not len(tl.all_clips()) > before_clips):
    pump(0.1)
pump(0.5)
hb.stop()
log("state", rc.state, "recording wall", round(time.monotonic() - t0, 1))
log("max UI gap ms", round(max(gaps[5:] or [0]) * 1000), "gaps>250ms:", sum(1 for g in gaps[5:] if g > 0.25))
fps = tl.fps
for t in tl.tracks:
    for c in t.clips:
        log(" ", t.type, t.name, "|", c.name, "start", c.timeline_start, "dur", c.duration, f"({c.duration / fps:.2f}s)")
log("toast:", win.toast.label.text() if hasattr(win.toast, "label") else "?")
log("undo entries added:", (len(stack._undo) - undo_before) if undo_before is not None else "?")
if "undo" in flags:
    win.engine.session.undo()
    log("after undo clips:", len(tl.all_clips()))
log("DONE")
os._exit(0)
