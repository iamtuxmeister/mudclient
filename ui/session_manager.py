"""
SessionManager — load/save connection profiles from sessions.json.

A Session holds: name, host, port, config (aliases, actions, timers, …).
The SessionManager dialog lets the user pick, create, edit, and delete them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QDialogButtonBox, QInputDialog, QMessageBox,
)
from PyQt6.QtCore import Qt


_SESSIONS_FILE = os.path.join(
    os.path.expanduser("~"), ".config", "mud-client", "sessions.json"
)


@dataclass
class Session:
    name:   str
    host:   str = ""
    port:   int = 4000
    config: dict = field(default_factory=dict)


def _load_sessions() -> list[Session]:
    if not os.path.exists(_SESSIONS_FILE):
        return []
    try:
        with open(_SESSIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        sessions = []
        for item in raw:
            s = Session(
                name=item.get("name", "Unnamed"),
                host=item.get("host", ""),
                port=int(item.get("port", 4000)),
                config=item.get("config", {}),
            )
            sessions.append(s)
        return sessions
    except Exception:
        return []


def _save_sessions(sessions: list[Session]):
    os.makedirs(os.path.dirname(_SESSIONS_FILE), exist_ok=True)
    with open(_SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(s) for s in sessions], f, indent=2)


class SessionManager(QDialog):
    """Pick a session to connect to, or manage sessions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sessions")
        self.setModal(True)
        self.setMinimumSize(360, 300)
        self._sessions = _load_sessions()
        self.selected: Optional[Session] = None

        self.setStyleSheet("""
            QDialog    { background: #1a1a1a; color: #d8d8d8; }
            QListWidget {
                background: #111; color: #ccc;
                border: 1px solid #333; font-family: Monospace;
            }
            QListWidget::item:selected { background: #2a5a8a; color: #fff; }
            QPushButton {
                background: #2a2a2a; color: #ccc;
                border: 1px solid #444; padding: 5px 12px;
            }
            QPushButton:hover  { background: #363636; }
            QPushButton:pressed{ background: #444; }
        """)

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._on_connect)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        self._connect_btn = None
        for label, slot in [
            ("Connect",   self._on_connect),
            ("New",       self._on_new),
            ("Delete",    self._on_delete),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            if label == "Connect":
                b.setDefault(True)   # pressing Enter triggers this button
                b.setAutoDefault(True)
                self._connect_btn = b
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._refresh_list()
        # Focus the list so arrow keys work immediately; Enter goes to
        # the default Connect button via the dialog's key handling.
        self._list.setFocus()

    # ── Helpers ─────────────────────────────────────────────────────

    def _refresh_list(self):
        self._list.clear()
        for s in self._sessions:
            item = QListWidgetItem(f"{s.name}  ({s.host}:{s.port})")
            item.setData(Qt.ItemDataRole.UserRole, s)
            self._list.addItem(item)
        if self._sessions:
            self._list.setCurrentRow(0)

    def _selected_session(self) -> Optional[Session]:
        item = self._list.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

    # ── Slots ────────────────────────────────────────────────────────

    def _on_connect(self):
        s = self._selected_session()
        if s:
            self.selected = s
            self.accept()

    def _on_new(self):
        name, ok = QInputDialog.getText(self, "New Session", "Session name:")
        if not ok or not name.strip():
            return
        host, ok = QInputDialog.getText(self, "New Session", "Host:")
        if not ok:
            return
        port_str, ok = QInputDialog.getText(self, "New Session", "Port:", text="4000")
        if not ok:
            return
        try:
            port = int(port_str)
        except ValueError:
            port = 4000
        s = Session(name=name.strip(), host=host.strip(), port=port)
        self._sessions.append(s)
        _save_sessions(self._sessions)
        self._refresh_list()

    def _on_delete(self):
        s = self._selected_session()
        if s is None:
            return
        reply = QMessageBox.question(
            self, "Delete Session", f"Delete '{s.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._sessions = [x for x in self._sessions if x is not s]
            _save_sessions(self._sessions)
            self._refresh_list()
