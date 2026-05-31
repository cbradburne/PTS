"""
EStopButton — large, always-visible emergency stop button.

Sends E-STOP broadcast to all mounts immediately (bypasses send queue).
"""
from __future__ import annotations

from PyQt6.QtWidgets import QPushButton
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont


class EStopButton(QPushButton):

    def __init__(self, parent=None):
        super().__init__("■  E-STOP", parent)
        self.setFixedHeight(52)
        self.setMinimumWidth(120)
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        self.setFont(font)
        self.setStyleSheet("""
            QPushButton {
                background: #B71C1C;
                color: white;
                border: 2px solid #FF1744;
                border-radius: 8px;
            }
            QPushButton:pressed {
                background: #7F0000;
                border-color: #B71C1C;
            }
        """)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
