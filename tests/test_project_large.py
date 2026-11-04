"""Large project through the real controller: 6000 clips / 36000 keyframes. Measures UI-thread stalls and memory."""
import os, sys, tempfile, time, tracemalloc
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtCore import QSettings, QCoreApplication, QThreadPool
from PyQt6.QtWidgets import QApplication, QMessageBox

tmp = tempfile.mkdtemp()
QCoreApplication.setOrganizationName("meeniFX"); QCoreApplication.setApplicationName("Studios")
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, os.path.join(tmp, "cfg"))
from meenifx.project import storage
storage.RECENTS_FILE = os.path.join(tmp, "recents.json"); storage.SESSION_FILE = os.path.join(tmp, "session.json")
from meenifx.project.settings import ProjectSettings
from meenifx.model.keyframes import AnimatedValue, Keyframe
from meenifx.ui.main_window import MainWindow
from meenifx.ui import project_controller as PC
PC.MissingMediaDialog.exec = lambda self: 0
app = QApplication(sys.argv)
w = MainWindow(app); w.show(); P = w.projects; P.autosave_timer.stop(); E = w.engine
P.create_project(ProjectSettings.from_preset("YouTube 1080p 30", "Big"), os.path.join(tmp, "p"))
tl = E.timeline; v = tl.tracks_of_type("video")[0]; a = tl.tracks_of_type("audio")[0]
N = 6000
for i in range(N):
    track = v if i % 2 == 0 else a
    eff = [{"effect_id": "mosaic", "enabled": True, "params": {"scale": AnimatedValue([Keyframe(j * 5, 30.0 + j, "bezier", 0.2, 0.4, 0.1, None) for j in range(6)], 40)}}]
    tl.new_clip(f"c{i}", os.path.join(tmp, f"f{i % 80}.mp4"), track, (i // 2) * 40, 30, effects=eff, fade_in=5, fade_out=7)
for m in range(500): tl.add_marker(m * 10, f"m{m}", "#ff0000")
P.mark_dirty()

t = time.perf_counter(); data = P._collect(); stall = time.perf_counter() - t
print(f"collect (UI thread): {stall*1000:.0f} ms")
assert stall < 1.5, "collect must not freeze the UI"
t = time.perf_counter(); assert P.save(); st = time.perf_counter() - t
size = os.path.getsize(P.project.path)
print(f"save: {st:.2f}s, file {size/1e3:.0f} KB")
P.close_project()
tracemalloc.start()
t = time.perf_counter(); assert P.open_path(P.project.path if P.project else storage.load_recents()[0]); lt = time.perf_counter() - t
cur, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
clips = list(E.timeline.all_clips())
assert len(clips) == N and sum(len(c.effects[0]["params"]["scale"].keyframes) for c in clips) == N * 6 and len(E.timeline.markers) == 500
print(f"load+apply: {lt:.2f}s, python peak during load {peak/1e6:.0f} MB")
assert lt < 10
# panel with 80 distinct media items stays responsive
t = time.perf_counter(); w.project_panel.rebuild(force=True); print(f"panel rebuild: {(time.perf_counter()-t)*1000:.0f} ms, items {len(P.project.library)}")
t = time.perf_counter(); w.timeline_panel.canvas.grab(); print(f"timeline repaint: {(time.perf_counter()-t)*1000:.0f} ms")
# autosave write is off the UI thread
t = time.perf_counter(); P.autosave(); ui = time.perf_counter() - t
QThreadPool.globalInstance().waitForDone(20000); print(f"autosave UI-thread cost: {ui*1000:.0f} ms")
print("LARGE OK"); sys.stdout.flush(); os._exit(0)
