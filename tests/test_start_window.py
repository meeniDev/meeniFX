"""Headless test of the start window and AppController flow."""
import os, sys, tempfile, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtCore import QSettings, QCoreApplication, QMimeData, QUrl, QPoint, QPointF, Qt
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

tmp = tempfile.mkdtemp()
QCoreApplication.setOrganizationName("meeniFX"); QCoreApplication.setApplicationName("Studios")
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, os.path.join(tmp, "cfg"))
from meenifx.project import storage
storage.RECENTS_FILE = os.path.join(tmp, "recents.json"); storage.SESSION_FILE = os.path.join(tmp, "session.json")
from meenifx.project.settings import ProjectSettings
from meenifx.app import AppController
from meenifx.ui import start_window as SW

app = QApplication(sys.argv)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARN", a[2]) or 0)
c = AppController(app, [])
assert c.start.isVisible() and not c.window.isVisible(), "app starts on the start window"
assert not c.projects.is_open, "no demo project at startup"
assert c.start.grid.count() == 0

P = c.projects; P.autosave_timer.stop()
P.create_project(ProjectSettings.from_preset("Vertical 9:16 1080x1920", "Alpha"), os.path.join(tmp, "p"))
assert c.window.isVisible() and not c.start.isVisible()
P.create_project(ProjectSettings.from_preset("HD 1080p 60", "Beta"), os.path.join(tmp, "p"))
P.close_project()
assert c.start.isVisible() and not c.window.isVisible(), "Close Project returns to start window"
S = c.start
assert S.grid.count() == 2
texts = [S.grid.item(i).text() for i in range(2)]
print(texts)
assert texts[0].startswith("Beta") and "1920x1080" in texts[0] and "60" in texts[0]
assert "1080x1920" in S.grid.item(1).text(), texts
assert S.grid.item(0).icon().isNull() is False
S.search.setText("alp"); assert S.grid.count() == 1; S.search.setText("")
S.sort.setCurrentIndex(1); assert S.grid.item(0).text().startswith("Alpha"); S.sort.setCurrentIndex(0)

# thumbnail comes from the container
meta, thumb = SW.container.read_summary(storage.load_recents()[0]); assert thumb and thumb[:2] == b"\xff\xd8"

# rename / duplicate keep extension
beta = storage.load_recents()[0]
info = SW.ProjectInfo(beta)
SW.QInputDialog.getText = staticmethod(lambda *a, **k: ("Gamma", True))
S._rename(info); rec = storage.load_recents(); assert os.path.basename(rec[0]) == "Gamma.mfxpf" and os.path.isfile(rec[0]), rec
S._duplicate(SW.ProjectInfo(rec[0])); rec = storage.load_recents(); assert rec[0].endswith("Gamma copy.mfxpf") and os.path.isfile(rec[0])
assert S.grid.count() == 3

# missing project dimmed
os.remove(rec[0]); S.reload(); assert S.grid.count() == 3
miss = [i for i in S._infos if not i.exists]; assert len(miss) == 1
S._remove(miss[0]); assert S.grid.count() == 2

# open by click
S.grid.itemClicked.emit(S.grid.item(0))
assert P.is_open and c.window.isVisible() and not S.isVisible(), "card click opens"
P.close_project()

# drag and drop
md = QMimeData(); md.setUrls([QUrl.fromLocalFile(storage.load_recents()[0])])
got = []; S.open_requested.connect(got.append)
S.dropEvent(QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
assert got and got[0].endswith(".mfxpf")
md2 = QMimeData(); md2.setUrls([QUrl.fromLocalFile(os.path.join(tmp, "x.json"))]); got.clear()
S.dropEvent(QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, md2, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
assert not got, "non-mfxpf ignored"

# show on startup off -> opens most recent directly
P.shutdown() if P.is_open else None
SW.set_show_on_startup(False)
c.window.close() if c.window.isVisible() else None
c2 = AppController(app, [])
app.processEvents()
import time; [app.processEvents() or time.sleep(0.05) for _ in range(10)]
assert c2.projects.is_open and c2.window.isVisible(), "show-on-startup off opens last project"
# argv
SW.set_show_on_startup(True); c2.projects.shutdown()
print("START OK"); sys.stdout.flush(); os._exit(0)
