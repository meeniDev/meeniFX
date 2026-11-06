import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from meenifx import theme, tokens
from meenifx.icons import icon


def test_switch_updates_tokens_and_icons():
    app = QApplication.instance() or QApplication([])
    theme.configure_theme(app, "light")
    light_bg = tokens.COLORS["bg"]
    assert not icon("undo").pixmap(16, 16).isNull()
    theme.configure_theme(app, "dark")
    assert tokens.COLORS["bg"] != light_bg
    assert not icon("undo").pixmap(16, 16).isNull()
    theme.configure_theme(app, "light")
    assert tokens.COLORS["bg"] == light_bg
