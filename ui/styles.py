"""
Beautified Cyber-SIGINT Dark Theme & Responsive Touch/Studio Stylesheet (PyQt6)
Designed for crisp legibility on both the 5-inch 800x480 DSI multitouch deck
and high-resolution 1080p/1440p external HDMI monitors.
"""

from __future__ import annotations


def build_theme_qss(compact: bool = True) -> str:
    base_font = "11px" if compact else "12px"
    btn_pad = "4px 9px" if compact else "7px 14px"
    input_pad = "2px 5px" if compact else "5px 8px"
    row_pad = "2px 5px" if compact else "5px 8px"

    return f"""
QMainWindow, QWidget {{
    background-color: #070b12;
    color: #e2e8f0;
    font-family: 'Inter', 'Segoe UI', 'Ubuntu', 'Cantarell', sans-serif;
    font-size: {base_font};
}}

/* Card Panels */
QFrame#cardPanel, QGroupBox {{
    background-color: #0c1320;
    border: 1px solid #1b2d48;
    border-radius: 6px;
}}

QGroupBox {{
    margin-top: 10px;
    padding-top: 10px;
    font-weight: bold;
    color: #38bdf8;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 5px;
    background-color: #0c1320;
    color: #00f0ff;
}}

QFrame#highlightBox {{
    background-color: #0f1a2c;
    border: 1px solid #0284c7;
    border-radius: 6px;
}}

QFrame#headerBar {{
    background-color: #0a101b;
    border: 1px solid #192a44;
    border-radius: 6px;
}}

QFrame#progressStrip {{
    background-color: #0b1320;
    border: 1px solid #1d3354;
    border-radius: 6px;
}}

/* Typography */
QLabel {{
    background: transparent;
    color: #cbd5e1;
}}
QLabel#brandTitle {{
    color: #00f0ff;
    font-weight: 800;
    font-size: {"13px" if compact else "16px"};
    letter-spacing: 0.6px;
}}
QLabel#sectionHeader {{
    color: #38bdf8;
    font-weight: 700;
    font-size: {"11px" if compact else "12px"};
    letter-spacing: 0.4px;
}}
QLabel#valuePillCyan {{
    background-color: #082f49;
    border: 1px solid #00f0ff;
    border-radius: 4px;
    padding: 2px 7px;
    color: #00f0ff;
    font-family: monospace;
    font-weight: bold;
}}
QLabel#valuePillAmber {{
    background-color: #3b2106;
    border: 1px solid #f59e0b;
    border-radius: 4px;
    padding: 2px 7px;
    color: #fbbf24;
    font-family: monospace;
    font-weight: bold;
}}
QLabel#valuePillGreen {{
    background-color: #062e1e;
    border: 1px solid #10b981;
    border-radius: 4px;
    padding: 2px 7px;
    color: #34d399;
    font-family: monospace;
    font-weight: bold;
}}
QLabel#valuePillMagenta {{
    background-color: #3b0926;
    border: 1px solid #ec4899;
    border-radius: 4px;
    padding: 2px 7px;
    color: #f472b6;
    font-family: monospace;
    font-weight: bold;
}}

/* Navigation Deck Tabs */
QPushButton#navTabBtn {{
    background-color: #0e1726;
    border: 1px solid #1e3250;
    border-radius: 5px;
    padding: 5px 8px;
    color: #94a3b8;
    font-weight: 700;
}}
QPushButton#navTabBtn:checked {{
    background-color: #0b2d48;
    border: 2px solid #00f0ff;
    color: #ffffff;
}}
QPushButton#navTabBtn:hover {{
    border-color: #38bdf8;
    color: #e2e8f0;
}}

/* Scan Mode Selector Cards (QUICK / DEEP / CUSTOM) */
QPushButton#modeCardBtn {{
    background-color: #101a2b;
    border: 1px solid #243b5e;
    border-radius: 6px;
    padding: {"6px 8px" if compact else "8px 12px"};
    color: #94a3b8;
    font-weight: 700;
    text-align: center;
}}
QPushButton#modeCardBtn:checked {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #063b38, stop:1 #0b2e4f);
    border: 2px solid #00ffaa;
    color: #00ffaa;
}}
QPushButton#modeCardBtn:hover {{
    border-color: #00f0ff;
}}

/* General & Action Buttons */
QPushButton {{
    background-color: #152238;
    border: 1px solid #284166;
    border-radius: 5px;
    padding: {btn_pad};
    color: #e2e8f0;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: #1e3150;
    border-color: #00f0ff;
}}
QPushButton:pressed {{
    background-color: #0c1524;
}}
QPushButton:disabled {{
    background-color: #0f1520;
    border-color: #192233;
    color: #475569;
}}

QPushButton#btnStartScan {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #059669, stop:1 #0284c7);
    border: 1px solid #34d399;
    color: #ffffff;
    font-weight: 800;
    padding: {"6px 12px" if compact else "8px 18px"};
}}
QPushButton#btnStartScan:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #0ea5e9);
    border-color: #6ee7b7;
}}
QPushButton#btnStartScan:disabled {{
    background-color: #122923;
    border-color: #1b3d34;
    color: #4b7368;
}}

QPushButton#btnAbort {{
    background-color: #7f1d1d;
    border: 1px solid #ef4444;
    color: #ffffff;
    font-weight: 800;
    padding: 2px 8px;
}}
QPushButton#btnAbort:hover {{
    background-color: #991b1b;
}}

QPushButton#btnTuneRow {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #2563eb);
    border: 1px solid #38bdf8;
    border-radius: 4px;
    color: #ffffff;
    font-weight: 800;
    padding: 3px 8px;
}}
QPushButton#btnTuneRow:hover {{
    background-color: #0ea5e9;
    border-color: #7dd3fc;
}}

QPushButton#btnReleaseGqrx {{
    background-color: #451a03;
    border: 1px solid #f59e0b;
    color: #fde68a;
    font-weight: 700;
    padding: 3px 8px;
}}

QPushButton#btnResetSdr {{
    background-color: #082f49;
    border: 1px solid #0ea5e9;
    color: #7dd3fc;
    font-weight: 700;
    padding: 2px 7px;
}}
QPushButton#btnResetSdr:hover {{
    background-color: #0c4a6e;
    border-color: #00f0ff;
    color: #ffffff;
}}
QPushButton#btnResetSdr:disabled {{
    background-color: #0f1520;
    border-color: #192233;
    color: #475569;
}}

/* Highlighted Inputs, Combos, Spinboxes & Sliders */
QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: #091526;
    border: 1px solid #0284c7;
    border-radius: 4px;
    padding: {input_pad};
    color: #00f0ff;
    font-family: monospace;
    font-weight: bold;
    min-height: {"19px" if compact else "26px"};
}}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid #00ffaa;
}}
QComboBox QAbstractItemView {{
    background-color: #0b1628;
    border: 1px solid #00f0ff;
    selection-background-color: #0369a1;
    selection-color: #ffffff;
    color: #e2e8f0;
    padding: 4px;
}}

QSlider::groove:horizontal {{
    border: 1px solid #1e3a5f;
    height: 8px;
    background: #091322;
    border-radius: 4px;
}}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #00ffaa);
    border-radius: 4px;
}}
QSlider::handle:horizontal {{
    background: #00f0ff;
    border: 2px solid #ffffff;
    width: {"18px" if compact else "16px"};
    margin: -6px 0;
    border-radius: 9px;
}}

QCheckBox {{
    color: #e2e8f0;
    font-weight: 600;
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #0284c7;
    background-color: #091526;
}}
QCheckBox::indicator:checked {{
    background-color: #10b981;
    border: 1px solid #6ee7b7;
}}

/* Tables */
QTableWidget {{
    background-color: #09101c;
    alternate-background-color: #0c1524;
    border: 1px solid #1b2d48;
    border-radius: 5px;
    gridline-color: #152238;
    selection-background-color: #0c365a;
    selection-color: #00f0ff;
}}
QTableWidget::item {{
    padding: {row_pad};
    border-bottom: 1px solid #121d30;
}}
QHeaderView::section {{
    background-color: #101b2d;
    color: #7dd3fc;
    font-weight: 700;
    padding: {"4px 5px" if compact else "6px 8px"};
    border: none;
    border-bottom: 2px solid #0284c7;
}}

/* Touch-Friendly Scrollbars */
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: #080d16;
    width: {"12px" if compact else "10px"};
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: #1e3a5f;
    min-height: 28px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: #00f0ff;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QSplitter::handle {{
    background-color: #1b2d48;
}}
QSplitter::handle:hover {{
    background-color: #00f0ff;
}}
"""


DARK_THEME_QSS = build_theme_qss(compact=False)
