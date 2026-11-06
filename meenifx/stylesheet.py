"""The application stylesheet, generated from the tokens of the active palette.

One function, ``build_stylesheet``, so a theme switch is a single ``setStyleSheet`` call on
the application. Controls are distinguished by dynamic properties (``role``, ``kind``,
``card``...) rather than per-widget stylesheets, which keeps widgets cheap to create and
avoids re-polishing them one by one.
"""

import os
import tempfile

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

from . import tokens
from .tokens import (COLORS, CONTROL_H, FONT_BODY, FONT_SMALL, FONT_STACK, FONT_TITLE, RADIUS,
                     RADIUS_LG, RADIUS_SM, SP1, SP2, SP3)

_IMAGE_DIR = os.path.join(tempfile.gettempdir(), "meenifx-ui")


def _glyph(name, color, size, draw):
    """Write ``name`` at 1x and @2x so Qt picks the sharp one on high-DPI screens."""
    os.makedirs(_IMAGE_DIR, exist_ok=True)
    paths = {}
    for scale, suffix in ((1, ""), (2, "@2x")):
        path = os.path.join(_IMAGE_DIR, f"{name}{suffix}.png")
        pixels = size * scale
        image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(scale, scale)
        draw(painter, QColor(color or "#000000"))
        painter.end()
        image.save(path, "PNG")
        paths[scale] = path
    return paths[1].replace("\\", "/")


def _indicator_images():
    """Check box, radio and arrow artwork in the active palette (cached on disk per theme)."""
    theme = tokens.theme_name()
    c = COLORS
    size = 16

    def box(painter, fill, outline, tick=None, radius=4.0):
        painter.setPen(QPen(QColor(outline), 1.4))
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(QRectF(1.0, 1.0, size - 2.0, size - 2.0), radius, radius)
        if tick:
            painter.setPen(QPen(QColor(tick), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([QPointF(4.3, 8.3), QPointF(7.0, 11.0), QPointF(11.8, 5.2)]))

    def dash(painter, color):
        painter.setPen(QPen(QColor(color), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(4.6, 8), QPointF(11.4, 8))

    def radio(painter, fill, outline, dot=None):
        painter.setPen(QPen(QColor(outline), 1.4))
        painter.setBrush(QColor(fill))
        painter.drawEllipse(QPointF(8, 8), 6.3, 6.3)
        if dot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(dot))
            painter.drawEllipse(QPointF(8, 8), 3.0, 3.0)

    def chevron(direction):
        def draw(painter, color):
            painter.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            y0, y1 = (6.0, 10.0) if direction == "down" else (10.0, 6.0)
            painter.drawPolyline(QPolygonF([QPointF(4.5, y0), QPointF(8, y1), QPointF(11.5, y0)]))
        return draw

    def make(name, draw):
        return _glyph(f"{theme}-{name}", None, size, draw)

    def with_color(name, color, draw):
        return _glyph(f"{theme}-{name}", color, size, draw)

    images = {
        "check_off": make("check-off", lambda p, _c: box(p, c["panel_alt"], c["border_strong"])),
        "check_off_hover": make("check-off-h", lambda p, _c: box(p, c["accent_soft"], c["accent"])),
        "check_on": make("check-on", lambda p, _c: box(p, c["accent_fill"], c["accent_fill"], "#FFFFFF")),
        "check_part": make("check-part", lambda p, _c: (box(p, c["accent_fill"], c["accent_fill"]),
                                                       dash(p, "#FFFFFF"))),
        "check_dis": make("check-dis", lambda p, _c: box(p, c["panel_soft"], c["border"])),
        "radio_off": make("radio-off", lambda p, _c: radio(p, c["panel_alt"], c["border_strong"])),
        "radio_on": make("radio-on", lambda p, _c: radio(p, c["panel_alt"], c["accent_fill"], c["accent_fill"])),
        "down": with_color("down", c["muted"], chevron("down")),
        "up": with_color("up", c["muted"], chevron("up")),
    }
    return images


def build_stylesheet():
    c = COLORS
    img = _indicator_images()
    return f"""
        QWidget {{ background: {c['panel']}; color: {c['text']}; font: {FONT_BODY}px {FONT_STACK}; }}
        QWidget:disabled {{ color: {c['disabled']}; }}
        QMainWindow, QMainWindow::separator {{ background: {c['bg']}; }}
        QMainWindow::separator {{ width: 5px; height: 5px; }}
        QMainWindow::separator:hover {{ background: {c['accent_soft']}; }}
        QLabel {{ background: transparent; }}
        QLabel[quiet="true"] {{ color: {c['muted']}; font-size: {FONT_SMALL}px; }}
        QLabel[role="heading"] {{ font-size: {FONT_TITLE}px; font-weight: 600; }}
        QLabel[role="section"] {{ color: {c['muted']}; font-size: {FONT_SMALL}px; font-weight: 600; }}
        QToolTip {{ background-color: {c['popup']}; color: {c['popup_text']};
                    border: 1px solid {c['popup_border']}; border-radius: {RADIUS_SM}px;
                    padding: 5px 7px; font: {FONT_BODY}px {FONT_STACK}; }}

        /* Menus */
        QMenuBar {{ background: {c['panel']}; color: {c['text']}; border-bottom: 1px solid {c['border']}; }}
        QMenuBar::item {{ background: transparent; padding: 5px 10px; border-radius: {RADIUS_SM}px; margin: 2px 1px; }}
        QMenuBar::item:selected {{ background: {c['accent_soft']}; }}
        QMenu {{ background: {c['popup']}; color: {c['popup_text']}; border: 1px solid {c['popup_border']};
                 border-radius: {RADIUS}px; padding: 5px; }}
        QMenu::item {{ padding: 6px 26px 6px 12px; border-radius: {RADIUS_SM}px; background: transparent; }}
        QMenu::item:selected {{ background: {c['accent_fill']}; color: #FFFFFF; }}
        QMenu::item:disabled {{ color: {c['disabled']}; background: transparent; }}
        QMenu::separator {{ height: 1px; background: {c['border']}; margin: 5px 6px; }}

        /* Toolbars and docks */
        QToolBar {{ background: {c['panel']}; border: 0; border-bottom: 1px solid {c['border']};
                    spacing: {SP2}px; padding: {SP1}px {SP2}px; }}
        QToolBar::separator {{ background: {c['border']}; width: 1px; margin: 5px {SP1}px; }}
        QDockWidget {{ border: 1px solid {c['border']}; titlebar-close-icon: none; }}
        QDockWidget::title {{ background: {c['panel']}; padding: 7px 10px; font-size: {FONT_TITLE}px;
                              font-weight: 600; border-bottom: 1px solid {c['border']}; }}
        QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: {RADIUS_SM}px; }}
        QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 6px {SP3}px 5px {SP3}px;
                        font-size: {FONT_TITLE}px; border: 0; border-bottom: 2px solid transparent; }}
        QTabBar::tab:hover {{ color: {c['text']}; background: {c['accent_soft']}; }}
        QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['accent']}; font-weight: 600; }}

        /* Buttons */
        QPushButton, QToolButton, QComboBox {{ background: {c['panel_soft']}; border: 1px solid {c['border']};
                                              padding: 0 {SP3}px; border-radius: {RADIUS}px; min-height: {CONTROL_H - 2}px; }}
        QPushButton:hover, QToolButton:hover {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
        QPushButton:pressed, QToolButton:pressed {{ background: {c['border']}; }}
        QPushButton:focus, QToolButton:focus {{ border: 1px solid {c['focus']}; }}
        QPushButton:disabled, QToolButton:disabled {{ color: {c['disabled']}; background: {c['panel']}; border-color: {c['border']}; }}
        QPushButton[primary="true"], QPushButton[kind="primary"] {{ background: {c['accent']}; color: {c['on_accent']};
                                                                   border: 1px solid {c['accent']}; font-weight: 600; }}
        QPushButton[primary="true"]:hover, QPushButton[kind="primary"]:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
        QPushButton[kind="danger"] {{ color: {c['red']}; font-weight: 600; border-color: {c['red']}; }}
        QPushButton[kind="danger"]:hover {{ background: {c['red']}; color: {c['panel_alt']}; border-color: {c['red']}; }}
        QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; }}
        QPushButton[kind="ghost"]:hover {{ background: {c['accent_soft']}; border-color: transparent; }}
        /* Icon buttons: flat until hovered or active. "On" is a soft sage tint with a sage
           outline, so the icon keeps its normal colour and stays readable. */
        QToolButton[role="icon"], QToolButton[role="transport"] {{ background: transparent; border: 1px solid transparent;
                                   padding: 0; min-width: {CONTROL_H - 2}px; color: {c['text']}; }}
        QToolButton[role="icon"]:hover, QToolButton[role="transport"]:hover {{ background: {c['accent_soft']}; border-color: transparent; }}
        QToolButton[role="icon"]:checked, QToolButton[role="transport"]:checked {{ background: {c['accent_soft']}; border-color: {c['accent']}; }}
        QToolButton[role="icon"]:focus, QToolButton[role="transport"]:focus {{ border-color: {c['focus']}; }}
        QToolButton[role="icon"]:disabled {{ background: transparent; border-color: transparent; }}
        QToolButton[role="primary-icon"] {{ background: {c['accent']}; border: 0; border-radius: 16px;
                                            min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; padding: 0; }}
        QToolButton[role="primary-icon"]:hover {{ background: {c['accent_hover']}; }}
        QToolButton::menu-indicator {{ image: none; width: 0; }}
        /* Tool toggles (Select / Razor / Snap / Loop): flat when off, tinted when on. */
        QToolButton[role="tool"] {{ background: {c['panel_soft']}; color: {c['muted']};
                                   border: 1px solid {c['border']}; padding: 0 {SP2}px; font-weight: normal; }}
        QToolButton[role="tool"]:hover {{ background: {c['accent_soft']}; color: {c['text']}; border-color: {c['accent']}; }}
        QToolButton[role="tool"]:checked {{ background: {c['accent_soft']}; color: {c['text']};
                                            border: 1px solid {c['accent']}; font-weight: 600; }}

        /* Fields */
        QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{ background: {c['panel_alt']};
                      border: 1px solid {c['border_strong']}; border-radius: {RADIUS}px; padding: 2px {SP2}px;
                      selection-background-color: {c['accent_fill']}; selection-color: #FFFFFF; }}
        QLineEdit {{ min-height: {CONTROL_H - 8}px; }}
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus,
        QComboBox:focus {{ border: 1px solid {c['focus']}; }}
        QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ background: {c['panel']}; border-color: {c['border']}; }}
        QLineEdit[role="search"] {{ border-radius: {RADIUS_LG + 4}px; padding-left: {SP3}px; border-color: {c['border']}; }}
        QLineEdit[role="search"]:focus {{ border-color: {c['focus']}; }}
        QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
            width: 16px; border: 0; background: transparent; }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({img['up']}); width: 10px; height: 10px; }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({img['down']}); width: 10px; height: 10px; }}
        QComboBox {{ background: {c['panel_alt']}; border-color: {c['border_strong']}; padding: 0 {SP2}px 0 {SP3}px; }}
        QComboBox:hover {{ border-color: {c['accent']}; }}
        QComboBox::drop-down {{ border: 0; width: 22px; }}
        QComboBox::down-arrow {{ image: url({img['down']}); width: 12px; height: 12px; }}
        QComboBox QAbstractItemView {{ background: {c['popup']}; color: {c['popup_text']};
                                       border: 1px solid {c['popup_border']}; outline: 0; padding: 3px;
                                       selection-background-color: {c['accent_fill']}; selection-color: #FFFFFF; }}
        QComboBox QAbstractItemView::item {{ min-height: 24px; padding: 2px 8px; border-radius: {RADIUS_SM}px; }}
        QComboBox QAbstractItemView::item:hover {{ background: {c['accent_soft']}; color: {c['popup_text']}; }}

        /* Lists and trees */
        QListWidget, QListView, QTreeWidget, QTreeView {{ background: {c['panel_alt']}; border: 1px solid {c['border']};
                                  border-radius: {RADIUS}px; selection-background-color: {c['accent_fill']};
                                  selection-color: #FFFFFF; outline: 0; }}
        QListWidget::item, QTreeWidget::item {{ border-radius: {RADIUS_SM}px; padding: 2px; }}
        QListWidget::item:hover, QTreeWidget::item:hover {{ background: {c['accent_soft']}; color: {c['text']}; }}
        QListWidget::item:selected, QTreeWidget::item:selected {{ background: {c['accent_fill']}; color: #FFFFFF; }}
        QHeaderView::section {{ background: {c['panel_soft']}; color: {c['muted']}; border: 0; padding: 4px 8px; }}

        /* Cards and groups */
        QFrame[card="true"] {{ background: {c['panel_alt']}; border: 1px solid {c['border']}; border-radius: {RADIUS_LG}px; }}
        QFrame[card="true"] QWidget {{ background: transparent; }}
        QFrame[card="true"] QLineEdit, QFrame[card="true"] QComboBox, QFrame[card="true"] QSpinBox,
        QFrame[card="true"] QDoubleSpinBox {{ background: {c['panel_alt']}; }}
        QFrame[subCard="true"] {{ background: {c['panel_soft']}; border: 1px solid {c['border']}; border-radius: {RADIUS}px; }}
        QFrame[subCard="true"] QWidget {{ background: transparent; }}
        QFrame[role="separator"] {{ background: {c['border']}; border: 0; max-height: 1px; min-height: 1px; }}
        QGroupBox {{ border: 1px solid {c['border']}; border-radius: {RADIUS}px; margin-top: 10px; padding-top: 8px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['muted']}; }}

        /* Sliders, check boxes, scroll bars, progress */
        QSlider::groove:vertical {{ width: 5px; background: {c['border']}; border-radius: 2px; }}
        QSlider::sub-page:vertical {{ background: {c['border']}; border-radius: 2px; }}
        QSlider::add-page:vertical {{ background: {c['accent']}; border-radius: 2px; }}
        QSlider::handle:vertical {{ background: {c['panel_alt']}; border: 2px solid {c['accent']}; height: 10px;
            margin: 0 -5px; border-radius: 7px; }}
        QSlider::groove:horizontal {{ height: 5px; background: {c['border']}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {c['panel_alt']}; border: 2px solid {c['accent']}; width: 10px;
                                      margin: -6px 0; border-radius: 7px; }}
        QSlider::handle:horizontal:hover {{ background: {c['accent_soft']}; }}
        QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
        QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
        QCheckBox::indicator:unchecked {{ image: url({img['check_off']}); }}
        QCheckBox::indicator:unchecked:hover {{ image: url({img['check_off_hover']}); }}
        QCheckBox::indicator:checked {{ image: url({img['check_on']}); }}
        QCheckBox::indicator:indeterminate {{ image: url({img['check_part']}); }}
        QCheckBox::indicator:disabled {{ image: url({img['check_dis']}); }}
        QRadioButton::indicator:unchecked {{ image: url({img['radio_off']}); }}
        QRadioButton::indicator:checked {{ image: url({img['radio_on']}); }}
        QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; border: 0; }}
        QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; border: 0; }}
        QScrollBar::handle:vertical {{ background: {c['scroll']}; min-height: 28px; border-radius: 4px; margin: 2px; }}
        QScrollBar::handle:horizontal {{ background: {c['scroll']}; min-width: 28px; border-radius: 4px; margin: 2px; }}
        QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {c['scroll_hover']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: 0; background: transparent; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QStatusBar {{ background: {c['panel']}; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
        QProgressBar {{ background: {c['panel_soft']}; border: 1px solid {c['border']}; border-radius: {RADIUS_SM}px;
                        text-align: center; min-height: 14px; }}
        QProgressBar::chunk {{ background: {c['accent']}; border-radius: {RADIUS_SM}px; }}

        /* Dialogs, message boxes and the (non-native) file dialog follow the same theme. */
        QDialog, QMessageBox {{ background: {c['panel']}; color: {c['text']}; }}
        QMessageBox QLabel {{ color: {c['text']}; }}
        QDialogButtonBox QPushButton {{ min-width: 78px; }}
        QFileDialog {{ background: {c['panel']}; }}
        QFileDialog QListView, QFileDialog QTreeView {{ background: {c['panel_alt']}; color: {c['text']};
                                                        border: 1px solid {c['border']}; }}
        QFileDialog QLabel {{ color: {c['text']}; }}
    """
