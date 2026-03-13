"""ButtonBar — a row of configurable macro buttons."""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PyQt6.QtCore    import pyqtSignal


class ButtonBar(QWidget):
    """
    A horizontal bar of up to 12 programmable macro buttons.
    Each button emits command_triggered with its configured command string.
    """

    command_triggered = pyqtSignal(str)

    _STYLE = """
        QPushButton {
            background: #1e1e1e;
            color: #999;
            border: 1px solid #333;
            padding: 3px 8px;
            font-family: Monospace;
            font-size:11pt;
        }
        QPushButton:hover { background: #2a2a2a; color: #ccc; border-color: #555; }
        QPushButton:pressed { background: #333; color: #eee; }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(4)
        self._buttons: list[QPushButton] = []
        self.setStyleSheet("QWidget { background: #111; border-top: 1px solid #222; }")

    def load_buttons(self, buttons: list[dict]):
        """
        *buttons* is a list of dicts with keys: label, command, enabled.
        """
        # remove old buttons
        for btn in self._buttons:
            self._layout.removeWidget(btn)
            btn.deleteLater()
        self._buttons.clear()
        self._layout.addStretch()

        for cfg in buttons[:12]:
            if not cfg.get("enabled", True):
                continue
            label   = cfg.get("label",   "?")
            # support both "body" (new) and "command" (legacy)
            command = cfg.get("body", cfg.get("command", ""))
            color   = cfg.get("color", "")
            btn = QPushButton(label)
            style = self._STYLE
            if color:
                style += f"QPushButton {{ background: {color}; }}"
            btn.setStyleSheet(style)
            btn.setToolTip(command)
            btn.clicked.connect(lambda checked=False, cmd=command: self.command_triggered.emit(cmd))
            self._layout.insertWidget(self._layout.count() - 1, btn)
            self._buttons.append(btn)
