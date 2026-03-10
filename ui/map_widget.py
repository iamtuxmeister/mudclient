"""MapWidget — simple ASCII map display panel."""

from __future__ import annotations

from PyQt6.QtWidgets import QTextEdit
from PyQt6.QtGui     import QFont, QTextOption


class MapWidget(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        f = QFont("Monospace", 9)
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        self.setFont(f)
        self.setStyleSheet("""
            QTextEdit {
                background: #0a0a12;
                color: #7ecfff;
                border: none;
                padding: 4px;
            }
        """)
        self.setPlainText("(no map data)")

    def update_map(self, ascii_text: str):
        self.setPlainText(ascii_text)
