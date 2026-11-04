"""Real-window check: open the sample project, export an mp4 through the app and verify it has sound."""
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
app.setOrganizationName("meeniFX")
app.setApplicationName("Studios")

from meenifx.audio.export_mix import ffmpeg_exe  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

win = MainWindow(app)
win.show()


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


win.projects.open_sample()
pump(6)
win._refresh_audio()
pump(6)
done = {}
win.bridge.export_done.connect(lambda r: done.setdefault("ok", r))
win.bridge.export_failed.connect(lambda m: done.setdefault("err", m))
target = os.path.join(tempfile.gettempdir(), "export_app_check.mp4")
if os.path.exists(target):
    os.remove(target)
win._start_export({"output_path": target, "format": "mp4", "use_source_fps": True, "use_source_size": True,
                   "range_start": 0, "range_end": 90})
for _ in range(600):
    pump(0.2)
    if done:
        break
print("result", done, flush=True)
out = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", target, "-vn", "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                     capture_output=True)
samples = np.frombuffer(out.stdout, dtype=np.int16)
print("audio samples", samples.size, "peak", int(np.abs(samples).max()) if samples.size else 0, flush=True)
os._exit(0)
