"""Render the main window in both themes and several sizes for visual review.

Run: python tests/ui_prototype.py [output_dir]   (QT_QPA_PLATFORM=offscreen works)
Screenshots land in docs/design/ by default.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from meenifx import theme, tokens
from meenifx.project.settings import ProjectSettings
from meenifx.ui import layout as dock_layout
from meenifx.ui.main_window import MainWindow

SIZES = ((1000, 600), (1420, 880))


def pump(seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "docs", "design")
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp()
    dock_layout.app_settings = lambda: QSettings(os.path.join(tmp, "layout.ini"), QSettings.Format.IniFormat)
    app = QApplication.instance() or QApplication(sys.argv)
    MainWindow._load_demo_project = lambda self: None
    window = MainWindow(app)
    window.projects.create_project(ProjectSettings.from_preset("720p 30", "Prototype"), os.path.join(tmp, "p"))
    pump(0.5)
    for name in ("light", "dark"):
        theme.configure_theme(app, name)
        for width, height in SIZES:
            window.resize(width, height)
            window.show()
            pump(0.5)
            path = os.path.join(out, f"window-{name}-{width}x{height}.png")
            window.grab().save(path)
            print(path)
        panel = window.project_panel
        panel.grab().save(os.path.join(out, f"project-{name}.png"))
    window.projects._set_clean()
    window.projects.autosave_timer.stop()
    window.close()


if __name__ == "__main__":
    main()
