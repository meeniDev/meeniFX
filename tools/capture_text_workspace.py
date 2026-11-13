"""Capture real Qt typography UI for the README without touching user projects/settings."""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
os.environ["MEENIFX_NO_RECENT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QSettings, QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QComboBox, QWidget
from meenifx.project.settings import ProjectSettings
from meenifx.ui import layout, font_picker
from meenifx.ui.main_window import MainWindow


def pump(seconds=0.5):
    loop = QEventLoop()
    QTimer.singleShot(round(seconds * 1000), loop.quit)
    loop.exec()


def main():
    app = QApplication.instance() or QApplication([])
    output = ROOT / "docs" / "design"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meenifx-readme-") as scratch:
        settings = QSettings(str(Path(scratch) / "settings.ini"), QSettings.Format.IniFormat)
        layout.app_settings = lambda: settings
        font_picker._store = lambda: settings
        MainWindow._load_demo_project = lambda self: None
        window = MainWindow(app)
        window.resize(1440, 960)
        window.show()
        window.projects.create_project(ProjectSettings(
            name="Typography Studio", width=1280, height=720,
            background="#152435"), scratch)
        cid = window.engine.session.add_text_clip("MAKE IT MOVE", 0, 120)
        clip = window.engine.timeline.find_clip(cid)
        clip.text.update(font="Arial", size=100, bold=True,
                         color="#F0F7FF", shadow_opacity=0.5)
        window.engine.session.apply_text_animation(cid, "in", "Slide In Down Left", 20)
        window.engine.session.apply_text_animation(cid, "out", "Slide Out Up Right", 20)
        window.engine.session.set_selection({cid})
        window.engine.session.set_playhead(45)
        window.effects_panel.refresh_properties()
        window._request_preview()
        pump(1.5)
        window.grab().save(str(output / "alpha10-text-workspace.png"))
        panel = window.effects_panel
        box = next(child for child in panel.properties.findChildren(QComboBox)
                   if child.objectName() == "text_animation_in_direction" and child.isVisible())
        assert box is not None
        panel.props_scroll.ensureWidgetVisible(box)
        pump()
        section = next(child for child in panel.properties.findChildren(QWidget)
                       if child.objectName() == "textAnimationSection" and child.isVisible())
        section.grab().save(str(output / "alpha10-text-animation.png"))
        picker = font_picker._FontPopup("Arial", window)
        picker.show()
        pump()
        picker.grab().save(str(output / "alpha10-font-picker.png"))
        picker.close()
        window.projects._set_clean()
        window.close()
        pump()


if __name__ == "__main__":
    main()
