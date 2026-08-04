from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

COLORS = {
    "background": "#0d0f12",
    "surface": "#15181d",
    "surface_alt": "#1b1f25",
    "border": "#292e36",
    "text": "#f4f5f7",
    "muted": "#949ca8",
    "red": "#e2383f",
    "red_hover": "#f14b52",
    "green": "#43c778",
    "yellow": "#f5bd4f",
    "blue": "#4d9df8",
    "gray": "#707986",
}


APP_STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 15px;
    color: {COLORS["text"]};
}}
QMainWindow, QDialog, QWidget#AppRoot {{
    background: {COLORS["background"]};
}}
QFrame#Sidebar {{
    background: #111317;
    border-right: 1px solid {COLORS["border"]};
}}
QLabel#Brand {{
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#BrandMark {{
    background: {COLORS["red"]};
    border-radius: 8px;
    font-size: 18px;
    font-weight: 800;
    padding: 8px;
}}
QLabel#PageTitle {{
    font-size: 30px;
    font-weight: 700;
}}
QLabel#PageSubtitle, QLabel[muted="true"] {{
    color: {COLORS["muted"]};
}}
QLabel#SectionTitle {{
    font-size: 18px;
    font-weight: 650;
}}
QLabel#MetricValue {{
    font-size: 25px;
    font-weight: 700;
}}
QLabel#MetricLabel {{
    color: {COLORS["muted"]};
    font-size: 12px;
    text-transform: uppercase;
}}
QFrame[card="true"] {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 12px;
}}
QFrame[card="true"]:hover {{
    border-color: #3a414c;
}}
QFrame[accent="red"] {{
    background: #211518;
    border: 1px solid #5a252a;
    border-left: 4px solid {COLORS["red"]};
    border-radius: 12px;
}}
QFrame[accent="blue"] {{
    background: #131c27;
    border: 1px solid #243e5c;
    border-left: 4px solid {COLORS["blue"]};
    border-radius: 12px;
}}
QFrame[accent="orange"] {{
    background: #241c12;
    border: 1px solid #604621;
    border-left: 4px solid {COLORS["yellow"]};
    border-radius: 12px;
}}
QLabel[emptyState="true"] {{
    color: {COLORS["muted"]};
    background: #111419;
    border: 1px dashed #343a43;
    border-radius: 10px;
    padding: 14px 16px;
}}
QPushButton {{
    background: {COLORS["surface_alt"]};
    border: 1px solid #343a43;
    border-radius: 8px;
    min-height: 38px;
    padding: 2px 14px;
}}
QPushButton:hover {{
    background: #252a31;
    border-color: #4a515d;
}}
QPushButton:pressed {{
    background: #101216;
}}
QPushButton:disabled {{
    color: #5f6670;
    background: #16191d;
    border-color: #24282e;
}}
QPushButton[primary="true"] {{
    background: {COLORS["red"]};
    border-color: {COLORS["red"]};
    color: white;
    font-weight: 650;
}}
QPushButton[primary="true"]:hover {{
    background: {COLORS["red_hover"]};
    border-color: {COLORS["red_hover"]};
}}
QPushButton[nav="true"] {{
    text-align: left;
    background: transparent;
    border: 0;
    border-radius: 8px;
    min-height: 44px;
    color: {COLORS["muted"]};
    font-weight: 600;
    padding-left: 14px;
}}
QPushButton[nav="true"]:hover {{
    color: white;
    background: #1a1d22;
}}
QPushButton[nav="true"]:checked {{
    color: white;
    background: #28171a;
    border-left: 3px solid {COLORS["red"]};
}}
QLineEdit, QSpinBox, QComboBox, QListWidget {{
    background: #101216;
    border: 1px solid #343a43;
    border-radius: 8px;
    min-height: 38px;
    padding: 2px 10px;
    selection-background-color: {COLORS["red"]};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QListWidget:focus {{
    border-color: {COLORS["red"]};
}}
QComboBox::drop-down {{
    border: 0;
    width: 28px;
}}
QComboBox QAbstractItemView {{
    background: {COLORS["surface_alt"]};
    border: 1px solid #3a414a;
    selection-background-color: #322024;
}}
QListWidget#SettingsSections::item {{
    min-height: 32px;
    padding: 4px 8px;
    border-radius: 6px;
}}
QListWidget#SettingsSections::item:selected {{
    background: #322024;
    color: white;
}}
QCheckBox {{ spacing: 9px; }}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border: 1px solid #525a65;
    border-radius: 4px;
    background: #101216;
}}
QCheckBox::indicator:checked {{
    background: {COLORS["red"]};
    border-color: {COLORS["red"]};
}}
QProgressBar {{
    background: #0e1013;
    border: 0;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {COLORS["green"]};
    border-radius: 4px;
}}
QScrollArea {{ border: 0; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent;
    width: 9px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #343a43;
    min-height: 30px;
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{
    color: {COLORS["muted"]};
    padding: 10px 16px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: white;
    border-bottom: 2px solid {COLORS["red"]};
}}
QToolTip {{
    background: #22262d;
    color: white;
    border: 1px solid #3a414a;
    padding: 6px;
}}
"""


def apply_theme(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["background"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["surface_alt"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["red"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLESHEET)
    app.setWindowIcon(create_app_icon())


def create_app_icon() -> QIcon:
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(COLORS["red"]))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(16, 16, 224, 224, 48, 48)
    painter.setPen(QColor("white"))
    font = QFont("Segoe UI", 72, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "CS")
    painter.end()
    return QIcon(pixmap)
