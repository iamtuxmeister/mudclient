"""
ConfigDialog — edit session configuration (aliases, actions, timers, buttons).

Returns a config dict matching what ScriptEngine.load_config() expects.
"""

from __future__ import annotations

import copy
import json

from PyQt6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
    QDialogButtonBox, QLabel, QLineEdit, QCheckBox, QSpinBox,
    QTextEdit, QFormLayout, QMessageBox,
)
from PyQt6.QtCore import Qt


_DARK = """
    QDialog    { background: #1a1a1a; color: #d0d0d0; }
    QTabWidget::pane { border: none; }
    QTabBar::tab {
        background: #222; color: #888; padding: 5px 12px;
    }
    QTabBar::tab:selected { background: #2a2a2a; color: #ddd; }
    QTableWidget {
        background: #111; color: #ccc; gridline-color: #333;
        border: 1px solid #333;
    }
    QHeaderView::section {
        background: #1e1e1e; color: #888;
        border: none; padding: 4px;
    }
    QLineEdit, QSpinBox {
        background: #222; color: #ddd;
        border: 1px solid #444; padding: 4px;
    }
    QCheckBox { color: #ccc; }
    QPushButton {
        background: #2a2a2a; color: #ccc;
        border: 1px solid #444; padding: 4px 10px;
    }
    QPushButton:hover  { background: #363636; }
    QPushButton:pressed{ background: #444; }
"""

_DEFAULT_CONFIG = {
    "aliases":    [],
    "actions":    [],
    "timers":     [],
    "highlights": [],
    "variables":  [],
    "buttons":    [],
}


def _make_table(columns: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(columns))
    t.setHorizontalHeaderLabels(columns)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    return t


def _table_to_list(table: QTableWidget, keys: list[str]) -> list[dict]:
    result = []
    for row in range(table.rowCount()):
        d = {}
        for col, key in enumerate(keys):
            item = table.item(row, col)
            val  = item.text() if item else ""
            if key == "enabled":
                val = item.checkState() == Qt.CheckState.Checked if item else True
            elif key in ("port", "interval"):
                try: val = int(val)
                except ValueError: val = 0
            d[key] = val
        result.append(d)
    return result


def _list_to_table(table: QTableWidget, rows: list[dict], keys: list[str]):
    table.setRowCount(0)
    for d in rows:
        r = table.rowCount()
        table.insertRow(r)
        for col, key in enumerate(keys):
            val = d.get(key, "")
            if key == "enabled":
                item = QTableWidgetItem()
                item.setCheckState(Qt.CheckState.Checked if val else Qt.CheckState.Unchecked)
            else:
                item = QTableWidgetItem(str(val))
            table.setItem(r, col, item)


class ConfigDialog(QDialog):
    """
    Edit aliases, actions, timers, highlight patterns, and button bar.
    """

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration")
        self.setModal(True)
        self.setMinimumSize(720, 520)
        self.setStyleSheet(_DARK)

        self._cfg = copy.deepcopy({**_DEFAULT_CONFIG, **config})

        vbox = QVBoxLayout(self)
        tabs = QTabWidget()
        vbox.addWidget(tabs)

        # ── Aliases ──────────────────────────────────────────────────
        self._alias_table = _make_table(["Name", "Body", "On"])
        _list_to_table(self._alias_table, self._cfg["aliases"], ["name", "body", "enabled"])
        tabs.addTab(self._wrap_table(self._alias_table, ["name", "body"]), "Aliases")

        # ── Actions ──────────────────────────────────────────────────
        self._action_table = _make_table(["Pattern", "Command", "GUI Target", "On"])
        _list_to_table(self._action_table, self._cfg["actions"],
                       ["pattern", "command", "gui_target", "enabled"])
        tabs.addTab(self._wrap_table(self._action_table, ["pattern", "command", "gui_target"]), "Actions")

        # ── Timers ───────────────────────────────────────────────────
        self._timer_table = _make_table(["Name", "Interval (s)", "Command", "On"])
        _list_to_table(self._timer_table, self._cfg["timers"],
                       ["name", "interval", "command", "enabled"])
        tabs.addTab(self._wrap_table(self._timer_table, ["name", "interval", "command"]), "Timers")

        # ── Buttons ──────────────────────────────────────────────────
        self._button_table = _make_table(["Label", "Command", "On"])
        _list_to_table(self._button_table, self._cfg["buttons"],
                       ["label", "command", "enabled"])
        tabs.addTab(self._wrap_table(self._button_table, ["label", "command"]), "Buttons")

        # ── Highlights ───────────────────────────────────────────────
        self._hl_table = _make_table(["Pattern", "Colour", "On"])
        _list_to_table(self._hl_table, self._cfg["highlights"],
                       ["pattern", "color", "enabled"])
        tabs.addTab(self._wrap_table(self._hl_table, ["pattern", "color"]), "Highlights")

        # ── OK / Cancel ──────────────────────────────────────────────
        bbox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bbox.accepted.connect(self._save_and_accept)
        bbox.rejected.connect(self.reject)
        vbox.addWidget(bbox)

    def _wrap_table(self, table: QTableWidget, add_keys: list[str]) -> QWidget:
        """Wrap a table with Add / Remove buttons."""
        w      = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(table)
        row = QHBoxLayout()
        add_btn = QPushButton("Add row")
        del_btn = QPushButton("Remove row")

        def _add():
            r = table.rowCount()
            table.insertRow(r)
            for col in range(table.columnCount()):
                hdr = table.horizontalHeaderItem(col)
                if hdr and hdr.text() == "On":
                    item = QTableWidgetItem()
                    item.setCheckState(Qt.CheckState.Checked)
                else:
                    item = QTableWidgetItem("")
                table.setItem(r, col, item)

        def _del():
            rows = sorted({i.row() for i in table.selectedItems()}, reverse=True)
            for r in rows:
                table.removeRow(r)

        add_btn.clicked.connect(_add)
        del_btn.clicked.connect(_del)
        row.addWidget(add_btn)
        row.addWidget(del_btn)
        row.addStretch()
        layout.addLayout(row)
        return w

    def _save_and_accept(self):
        self._cfg["aliases"]    = _table_to_list(self._alias_table,  ["name", "body", "enabled"])
        self._cfg["actions"]    = _table_to_list(self._action_table,  ["pattern", "command", "gui_target", "enabled"])
        self._cfg["timers"]     = _table_to_list(self._timer_table,   ["name", "interval", "command", "enabled"])
        self._cfg["buttons"]    = _table_to_list(self._button_table,  ["label", "command", "enabled"])
        self._cfg["highlights"] = _table_to_list(self._hl_table,      ["pattern", "color", "enabled"])
        self.accept()

    def get_config(self) -> dict:
        return copy.deepcopy(self._cfg)
