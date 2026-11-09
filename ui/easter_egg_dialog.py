"""
DynoGatorLabs Classified Wasteland SIGINT Terminal Easter Egg Dialog
Fallout / Pip-Boy / RobCo Phosphor Green CRT Aesthetic for Picto-Sight H8
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

ASCII_BANNER = r"""
  ____                    ____       _             _           _         
 |  _ \ _   _ _ __   ___ / ___| __ _| |_ ___  _ __| |    __ _| |__  ___ 
 | | | | | | | '_ \ / _ \ |  _ / _` | __/ _ \| '__| |   / _` | '_ \/ __|
 | |_| | |_| | | | | (_) | |_| | (_| | || (_) | |  | |__| (_| | |_) \__ \
 |____/ \__, |_| |_|\___/ \____|\__,_|\__\___/|_|  |_____\__,_|_.__/|___/
        |___/                                                            
       [ VAULT-TEC CLASSIFIED SIGINT OVERSEER CONSOLE • TERMINAL 101 ]
"""

LORE_TEXT = """
================================================================================
ROBCO UNIFIED OPERATING SYSTEM (C) 2077-2026 ROBCO INDUSTRIES / DYNOGATORLABS
SECURITY CLEARANCE: OVERSEER // SIGINT OPERATOR
RADS BACKGROUND: 0.003 mR/h (NOMINAL • RAD-SHIELDING ACTIVE)
================================================================================

[+] HARDWARE AFFIRMATION:
    • Primary Node: Raspberry Pi 5 Model B (Rev 1.0)
    • Neural Coprocessor: Hailo-8 M.2 AI Acceleration Module (26 TOPS)
    • Rad-Hardened Transceiver: Great Scott Gadgets HackRF One
    • Mobile Handset Link: Google Pixel 9 Pro XL (GrapheneOS Sandboxed)

[+] WASTELAND EMERGENCY FREQUENCY MONITOR:
    • 146.5200 MHz [FM]  -> DynoGatorLabs Wasteland Calling Net (+1V6)
    • 101.3000 MHz [WFM] -> Vault 101 Emergency Distress Beacon
    •   1.2400 MHz [AM]  -> Civil Defense CONELRAD Emergency Net (640/1240)
    • 145.8000 MHz [FM]  -> Enclave Eyebot Presidential Broadcast
    • 539.0000 MHz [RAW] -> ATSC 8VSB Emergency Video Broadcast (Channel 25)
    • 1090.000 MHz [RAW] -> Tactical Airspace Radar (Cheyenne Mountain / Peterson)

[+] OPERATOR NOTES:
    "If you are reading this, the bombs have dropped, but the RF spectrum remains.
     Keep your HackRF grounded, your Hailo-8 heat dissipation steady, and remember:
     The spectrum never sleeps. 73 de DynoGatorLabs."
================================================================================
"""


class DynoGatorEasterEggDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, tune_callback=None) -> None:
        super().__init__(parent)
        self.tune_callback = tune_callback
        self.setWindowTitle("⚡ DynoGatorLabs • Vault-Tec SIGINT Overseer Terminal")
        self.resize(760, 580)
        self.setModal(True)
        self._init_ui()

    def _init_ui(self) -> None:
        self.setStyleSheet("""
            QDialog {
                background-color: #040804;
                border: 2px solid #00ff66;
            }
            QLabel {
                color: #00ff66;
                font-family: 'Courier New', Courier, monospace;
            }
            QTextEdit {
                background-color: #020502;
                color: #00ff66;
                border: 1px solid #00aa44;
                border-radius: 4px;
                font-family: 'Courier New', Courier, monospace;
                font-size: 11px;
                line-height: 1.4;
            }
            QPushButton {
                background-color: #061a06;
                color: #00ff66;
                border: 1px solid #00ff66;
                border-radius: 4px;
                padding: 6px 12px;
                font-family: 'Courier New', Courier, monospace;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #00ff66;
                color: #000000;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Header with cover image thumbnail + title
        hdr = QHBoxLayout()
        cover_path = Path(__file__).resolve().parent.parent / "docs" / "images" / "cover.jpg"
        if cover_path.exists():
            img_lbl = QLabel(self)
            pix = QPixmap(str(cover_path)).scaled(
                120, 68, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            img_lbl.setPixmap(pix)
            img_lbl.setStyleSheet("border: 1px solid #00ff66; border-radius: 4px;")
            hdr.addWidget(img_lbl)

        title_vbox = QVBoxLayout()
        t1 = QLabel("DYNOGATOR LABS // WASTELAND DIVISION", self)
        t1.setStyleSheet("font-size: 15px; font-weight: bold; color: #00ff66; letter-spacing: 1px;")
        t2 = QLabel("VAULT-TEC SIGINT OPERATING SYSTEM v3.2.0 • PROTOCOL DGL-73", self)
        t2.setStyleSheet("font-size: 11px; color: #33cc66;")
        title_vbox.addWidget(t1)
        title_vbox.addWidget(t2)
        hdr.addLayout(title_vbox, 1)

        layout.addLayout(hdr)

        # Terminal text area
        self.term_edit = QTextEdit(self)
        self.term_edit.setReadOnly(True)
        self.term_edit.setPlainText(ASCII_BANNER + LORE_TEXT)
        layout.addWidget(self.term_edit, 1)

        # Quick tune buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self.btn_tune_dgl = QPushButton("▶ TUNE DGL NET (146.520 MHz FM)", self)
        self.btn_tune_dgl.clicked.connect(lambda: self._tune(146_520_000, "FM"))
        btn_layout.addWidget(self.btn_tune_dgl)

        self.btn_tune_vault = QPushButton("📻 VAULT 101 (101.3 MHz)", self)
        self.btn_tune_vault.clicked.connect(lambda: self._tune(101_300_000, "WFM"))
        btn_layout.addWidget(self.btn_tune_vault)

        self.btn_tune_conelrad = QPushButton("⚠ CONELRAD (1240 kHz)", self)
        self.btn_tune_conelrad.clicked.connect(lambda: self._tune(1_240_000, "AM"))
        btn_layout.addWidget(self.btn_tune_conelrad)

        btn_close = QPushButton("✕ CLOSE TERMINAL", self)
        btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(btn_close)

        layout.addLayout(btn_layout)

    def _tune(self, hz: int, mode: str) -> None:
        if self.tune_callback:
            try:
                self.tune_callback(hz, mode)
            except Exception:
                pass
        self.accept()
