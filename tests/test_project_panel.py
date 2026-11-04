"""Headless test of the Project panel and layout."""
import os, sys, tempfile, time, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cv2, numpy as np
from PyQt6.QtCore import QSettings, QCoreApplication, QMimeData, QUrl, QPointF, Qt
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

tmp = tempfile.mkdtemp()
QCoreApplication.setOrganizationName("meeniFX"); QCoreApplication.setApplicationName("Studios")
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, os.path.join(tmp, "cfg"))
from meenifx.project import storage
storage.RECENTS_FILE = os.path.join(tmp, "recents.json"); storage.SESSION_FILE = os.path.join(tmp, "session.json")
# The scripts in this folder keep the recent list off (tests/conftest.py sets
# MEENIFX_NO_RECENT for the pytest run). This one drives the list itself, but only
# against the copy redirected above, so the user's real list stays untouched.
storage.enable_recents()
from meenifx.project.settings import ProjectSettings
from meenifx.ui.main_window import MainWindow
from meenifx.ui import layout, project_panel as PP

app = QApplication(sys.argv)
def pump(n=15):
    for _ in range(n): app.processEvents(); time.sleep(0.05)
w = MainWindow(app); w.show(); P = w.projects; P.autosave_timer.stop(); E = w.engine
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
from meenifx.ui import project_controller as PC
PC.MissingMediaDialog.exec = lambda self: 0
assert layout.LAYOUT_VERSION == 8
panel = w.project_panel
assert "project" in w.docks and "controls" not in w.docks and w.dockWidgetArea(w.docks["project"]) == Qt.DockWidgetArea.LeftDockWidgetArea
P.create_project(ProjectSettings.from_preset("720p 30", "PanelTest"), os.path.join(tmp, "p")); pump(2)

media = os.path.join(tmp, "m"); os.makedirs(media)
vid = os.path.join(media, "a.mp4"); vw = cv2.VideoWriter(vid, cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 180))
for i in range(60): vw.write(np.full((180, 320, 3), (i * 4, 90, 200), np.uint8))
vw.release()
png = os.path.join(media, "b.png"); cv2.imwrite(png, np.full((100, 200, 3), 120, np.uint8))
txt = os.path.join(media, "c.txt"); open(txt, "w").write("x")

assert panel.add_files([vid, png, txt]) == 2
assert P.is_dirty() and len(P.project.library) == 2
pump(40)
items = {panel.list.item(i).text().split("\n")[0]: panel.list.item(i) for i in range(panel.list.count())}
print({k: v.text().replace("\n", " | ") for k, v in items.items()})
v = P.project.library.by_path(vid); assert (v.width, v.height) == (320, 180) and 1.9 < v.duration < 2.1 and round(v.fps) == 30, v
assert not panel.list.item(0).icon().isNull()
assert os.path.isdir(os.path.join(os.path.dirname(P.project.path), "Cache", "thumbs")) and os.listdir(os.path.join(os.path.dirname(P.project.path), "Cache", "thumbs"))
assert "320x180" in items["a.mp4"].text()

# list/grid toggle
panel.toggle_view(); assert panel.list.viewMode() == panel.list.ViewMode.ListMode; panel.toggle_view()

# search
panel.search.setText("b.p"); pump(3); assert panel.list.count() == 1; panel.search.setText(""); pump(3)

# mime for dragging onto timeline/preview carries file URLs
sel = [panel.list.item(i) for i in range(panel.list.count()) if panel.list.item(i).data(Qt.ItemDataRole.UserRole)[1] == v.id]
md = panel.list.mimeData(sel); assert os.path.normcase(os.path.normpath(md.urls()[0].toLocalFile())) == os.path.normcase(os.path.normpath(vid)) and md.hasFormat(PP.MEDIA_MIME)

# double click inserts at playhead
E.session.playhead = 10
panel._double_clicked(sel[0]); pump(30)
clips = list(E.timeline.all_clips()); assert clips and clips[0].source_path == vid and clips[0].timeline_start == 10, [(c.source_path, c.timeline_start) for c in clips]
panel.rebuild(force=True)
used_item = [panel.list.item(i) for i in range(panel.list.count()) if panel.list.item(i).data(Qt.ItemDataRole.UserRole)[1] == v.id][0]
assert "Used in the timeline" in used_item.toolTip()

# bins: create, move by drop, navigate
panel.project.library.add_bin("Footage"); panel.rebuild(force=True)
bin_id = list(P.project.library.bins)[0]
panel._items_moved([v.id], bin_id); assert P.project.library.items[v.id].bin == bin_id
assert all(panel.list.item(i).data(Qt.ItemDataRole.UserRole)[1] != v.id for i in range(panel.list.count()))
panel._current_bin = bin_id; panel.rebuild(force=True)
assert any(panel.list.item(i).data(Qt.ItemDataRole.UserRole)[0] == "up" for i in range(panel.list.count()))
panel._items_moved([v.id], PP.ROOT_BIN); assert P.project.library.items[v.id].bin == ""
panel._current_bin = ""; panel.rebuild(force=True)

# OS file drop onto list adds to library
extra = os.path.join(media, "d.png"); cv2.imwrite(extra, np.zeros((20, 20, 3), np.uint8))
panel._files_dropped([extra], ""); assert P.project.library.by_path(extra)

# save / reopen keeps library, bins, names
panel._rename(P.project.library.by_path(extra)) if False else None
P.project.library.by_path(extra).name = "renamed.png"
assert P.save(); P.close_project(); assert P.open_path(P.project.path if P.project else storage.load_recents()[0]); pump(5)
lib = P.project.library
assert lib.by_path(extra).name == "renamed.png" and bin_id in lib.bins and len(lib) == 3

# missing -> warning state + relink
os.rename(png, png + ".x"); P.close_project(); P.open_path(storage.load_recents()[0]); pump(5)
item = lib = P.project.library.by_path(png); assert panel._is_missing(item)
assert any("missing" in panel.list.item(i).text() or panel.list.item(i).toolTip().endswith("File not found") for i in range(panel.list.count()))
os.rename(png + ".x", png); P.relink(item.id, png); pump(3); assert not panel._is_missing(item)

# remove used media warns and deletes clips (one undo step)
n = E.session.stack.undo_stack.count()
assert panel.remove([P.project.library.by_path(vid)]); assert not list(E.timeline.all_clips()) and P.project.library.by_path(vid) is None
E.session.undo(); assert list(E.timeline.all_clips())

# timeline missing icon painting does not crash
os.rename(vid, vid + ".x"); P.save(); P.close_project(); P.open_path(storage.load_recents()[0]); pump(3)
w.timeline_panel.canvas.grab(); assert w.timeline_panel.canvas.is_missing(vid)
os.rename(vid + ".x", vid)

# saved layout version
P.shutdown(); w._save_layout_now(); assert str(w._settings.value("layoutVersion")) == "5"
print("PANEL OK"); sys.stdout.flush()
shutil.rmtree(tmp, ignore_errors=True)
os._exit(0)
