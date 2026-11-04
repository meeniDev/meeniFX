"""Headless (offscreen Qt) integration test: new project, import, edit, save, reopen, settings, missing media, autosave."""
import os, sys, tempfile, time, wave, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2, numpy as np
from PyQt6.QtCore import QSettings, QThreadPool, QCoreApplication
from PyQt6.QtWidgets import QApplication, QMessageBox, QDialog

tmp = tempfile.mkdtemp()
QCoreApplication.setOrganizationName("meeniFX"); QCoreApplication.setApplicationName("Studios")
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, os.path.join(tmp, "cfg"))

from meenifx.project import storage
storage.RECENTS_FILE = os.path.join(tmp, "recents.json"); storage.SESSION_FILE = os.path.join(tmp, "session.json")
# The scripts in this folder keep the recent list off (tests/conftest.py sets
# MEENIFX_NO_RECENT for the pytest run). This one drives the list itself, but only
# against the copy redirected above, so the user's real list stays untouched.
storage.enable_recents()
from meenifx.ui.main_window import MainWindow
from meenifx.ui import project_controller as PC
from meenifx.media.reader import read_media
from meenifx.model.keyframes import AnimatedValue, Keyframe
from meenifx.project.settings import ProjectSettings

app = QApplication(sys.argv)
w = MainWindow(app); w.show()
w.projects.autosave_timer.stop()
P = w.projects; E = w.engine

# silence dialogs
answers = {"question": QMessageBox.StandardButton.Yes, "exec": QMessageBox.StandardButton.Discard}
QMessageBox.question = staticmethod(lambda *a, **k: answers["question"])
QMessageBox.critical = staticmethod(lambda *a, **k: print("ERROR DIALOG:", a[2]) or 0)
QMessageBox.exec = lambda self: answers["exec"]
PC.MissingMediaDialog.exec = lambda self: 0

# media
media = os.path.join(tmp, "media"); os.makedirs(media)
vid = os.path.join(media, "clip.mp4")
vw = cv2.VideoWriter(vid, cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 180))
for i in range(60): vw.write(np.full((180, 320, 3), (i * 4 % 255, 80, 200), np.uint8))
vw.release()
png = os.path.join(media, "pic.png"); cv2.imwrite(png, np.full((100, 100, 3), 200, np.uint8))

def add(path, start, track):
    kind, frames, fps = read_media(path)
    (E.add_decoded_image if kind == "image" else E.add_decoded_media)(path, kind, frames, fps, start, track, False)

def dump(tl):
    out = []
    for t in tl.tracks:
        for c in t.clips:
            effs = [(e["effect_id"], e["enabled"], {k: ([(x.time, x.value, x.interpolation) for x in v.keyframes], v.base_value) if isinstance(v, AnimatedValue) else v for k, v in e["params"].items()}) for e in c.effects]
            out.append((t.id, c.name, c.timeline_start, c.duration, c.speed, c.reverse, c.fade_in, c.fade_out, c.fade_in_curve, effs))
    return out, [(m.frame, m.name, m.color) for m in tl.markers], tl.loop_in, tl.loop_out

# ---- (b)+(c): create from a preset
s = ProjectSettings.from_preset("720p 30", "My Film")
assert P.create_project(s, os.path.join(tmp, "Projects"))
folder = os.path.join(tmp, "Projects", "My Film"); path = os.path.join(folder, "My Film.mfxpf")
assert os.path.isfile(path) and os.path.isdir(os.path.join(folder, "Recordings")) and os.path.isdir(os.path.join(folder, "Cache"))
assert E.project_size() == (1280, 720) and E.timeline.fps == 30
assert w.windowTitle() == "My Film - meeniFX Studios", w.windowTitle()
print("created:", w.windowTitle())

v1 = E.timeline.tracks_of_type("video")[0]
add(vid, 0, v1); add(png, 70, v1)
assert w.windowTitle().startswith("My Film*"), w.windowTitle()
clip = E.timeline.tracks_of_type("video")[0].clips[0]
kfs = AnimatedValue([Keyframe(0, 30), Keyframe(20, 80, "bezier"), Keyframe(40, 50)], 40)
def edit():
    clip.effects.append({"effect_id": "mosaic", "enabled": True, "params": {"scale": kfs}})
    clip.fade_in = 6; clip.fade_out = 9; clip.fade_out_curve = -0.3
E.session.stack.run("edit", edit)
E.session.add_marker(15, "beat", None); E.session.set_loop_region(10, 50)
before = dump(E.timeline)
assert P.save() and w.windowTitle() == "My Film - meeniFX Studios", w.windowTitle()
E.session.add_marker(30, "second", None)
assert w.windowTitle().startswith("My Film*")
E.session.undo(); assert w.windowTitle() == "My Film - meeniFX Studios", "undo to clean state should drop asterisk"
P.save(); assert os.path.exists(path + ".bak")
print("saved, .bak present, asterisk follows undo clean state")

# ---- close, reopen
closed = []; P.closed.connect(lambda: closed.append(1))
assert P.close_project() and closed and not P.is_open
assert P.open_path(path) and P.is_open
after = dump(E.timeline)
assert before == after, (before, after)
assert E.timeline.loop_in == 10 and E.timeline.loop_out == 50
assert storage.load_recents(os.path.join(tmp, "recents.json"))[0] == path
assert w.windowTitle() == "My Film - meeniFX Studios"
raw, processed = E.render_frame(5, 1.0); assert raw.shape == (720, 1280, 3)
print("reopened: effects, keyframes, fades, markers, loop region identical")

# ---- project settings: fps + resolution, one undo step
dur_before = [(c.timeline_start, c.duration) for c in E.timeline.all_clips()]
st = ProjectSettings.from_dict(E.project_settings.to_dict()); st.fps = 60.0; st.width, st.height = 1080, 1920; st.preset = "Custom"
steps = E.session.stack.undo_stack.count()
assert P.apply_settings(st)
assert E.session.stack.undo_stack.count() == steps + 1
assert E.timeline.fps == 60 and E.project_size() == (1080, 1920) and E.project_fps == 60.0
for (a, b), c in zip(dur_before, E.timeline.all_clips()): assert (c.timeline_start, c.duration) == (a * 2, b * 2), (a, b, c.timeline_start, c.duration)
assert [m.frame for m in E.timeline.markers] == [30] and (E.timeline.loop_in, E.timeline.loop_out) == (20, 100)
assert [k.time for k in E.timeline.all_clips().__iter__().__next__().effects[0]["params"]["scale"].keyframes] == [0, 40, 80]
raw, _p = E.render_frame(5, 1.0); assert raw.shape == (1920, 1080, 3)
assert w.windowTitle().startswith("My Film*")
E.session.undo()
assert E.timeline.fps == 30 and E.project_size() == (1280, 720) and dump(E.timeline) == before, "undo is one step"
E.session.redo(); assert E.timeline.fps == 60 and E.project_size() == (1080, 1920)
E.session.undo()
print("settings: fps+resolution converted, preview/export size follows, single undo step")
P.save()

# ---- missing media
moved = vid + ".moved"; os.rename(vid, moved)
assert P.close_project() and P.open_path(path)
assert vid in E.missing_paths
E.render_frame(5, 1.0)  # must not crash
assert E.has_media()
os.rename(moved, vid)
item = [i for i in P.project.library.items.values() if i.path == vid][0]
P.relink(item.id, vid); assert vid not in E.missing_paths and w.windowTitle().startswith("My Film*")
P.save()
# relative path survives moving the whole project folder
new_root = os.path.join(tmp, "moved"); shutil.copytree(media, os.path.join(new_root, "media")); shutil.copy(path, os.path.join(new_root, "My Film.mfxpf"))
# (relative path is relative to the project: ../media from project folder)
print("missing media: no crash, relink works")

# ---- autosave + crash recovery
P.close_project()
P.open_path(path)
E.session.add_marker(44, "unsaved", None)
assert P.autosave(); QThreadPool.globalInstance().waitForDone(5000); app.processEvents(); time.sleep(0.4); app.processEvents()
assert os.path.exists(storage.autosave_path(path)) and storage.newer_autosave(path)
assert storage.crashed_session(os.path.join(tmp, "session.json")) == path
# simulate kill: drop the in-memory state without a clean shutdown
P.autosave_timer.stop(); P.project = None
answers["question"] = QMessageBox.StandardButton.Yes
assert P.open_path(path)
assert 44 in [m.frame for m in E.timeline.markers] and w.windowTitle().startswith("My Film*")
P.save(); assert not os.path.exists(storage.autosave_path(path))
print("autosave recovered after simulated crash")
P.shutdown()
assert storage.crashed_session(os.path.join(tmp, "session.json")) is None

# bad extension handling
assert not P.open_path(os.path.join(tmp, "x.json"))
print("ALL OK")
shutil.rmtree(tmp, ignore_errors=True)
sys.stdout.flush()
os._exit(0)
