"""
ConfigDialog — edit session configuration (aliases, triggers, timers, buttons).

Returns a config dict matching what ScriptEngine.load_config() expects.
"""

from __future__ import annotations

import copy
import json

from PyQt6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
    QDialogButtonBox, QLabel, QLineEdit, QCheckBox, QSpinBox,
    QTextEdit, QFormLayout, QMessageBox, QColorDialog,
    QComboBox, QGridLayout, QFrame,
)
from PyQt6.QtGui import QColor
from PyQt6.QtCore import Qt, pyqtSignal

from ui.window_settings import save_geometry

from ui.trigger_editor import TriggerEditor


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
    "aliases":       [],
    "trigger_folders": [],
    "timers":        [],
    "highlights":    [],
    "variables":     [],
    "buttons":       [],
    "cmd_separator": ";",
    "cmd_echo":       True,
    "cmd_echo_color": "#e8d44d",   # light yellow
    "variables":      [],
    "palette": None,        # None = use theme name instead
    "palette_theme": "xterm",
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


def _migrate_actions(actions: list[dict]) -> list[dict]:
    """Convert old flat actions list to the new folder-based structure."""
    if not actions:
        return []
    triggers = []
    for a in actions:
        pattern = a.get("pattern", "") or a.get("body", "")
        body    = a.get("body", "") or a.get("command", "")
        # support old key names
        if not body:
            body = a.get("command", "")
        triggers.append({
            "name":     pattern[:40] if pattern else "Trigger",
            "patterns": [pattern] if pattern else [],
            "body":     body,
            "enabled":  a.get("enabled", True),
        })
    return [{"name": "Imported", "enabled": True, "triggers": triggers}]


class ConfigDialog(QDialog):
    """
    Edit aliases, triggers, timers, highlight patterns, and button bar.
    Non-modal, always-on-top — stays open while you play.
    """
    config_saved = pyqtSignal(dict)   # emitted when OK is pressed


    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumSize(720, 520)
        self.setStyleSheet(_DARK)

        self._cfg = copy.deepcopy({**_DEFAULT_CONFIG, **config})

        vbox = QVBoxLayout(self)
        tabs = QTabWidget()
        vbox.addWidget(tabs)

        # ── General ──────────────────────────────────────────────────
        gen_widget = QWidget()
        gen_layout = QFormLayout(gen_widget)
        gen_layout.setContentsMargins(12, 12, 12, 12)
        gen_layout.setSpacing(10)
        self._sep_edit = QLineEdit(self._cfg.get("cmd_separator", ";"))
        self._sep_edit.setMaximumWidth(60)
        self._sep_edit.setToolTip(
            "Character(s) used to split stacked commands in the input line.\n"
            "e.g. with ';' you can type  north;kill orc;loot  to send 3 commands."
        )
        gen_layout.addRow("Command separator:", self._sep_edit)

        # Command echo — checkbox only; color lives on the Colors tab
        self._echo_check = QCheckBox("Echo sent commands to output window")
        self._echo_check.setChecked(self._cfg.get("cmd_echo", True))
        gen_layout.addRow("Command echo:", self._echo_check)
        tabs.addTab(gen_widget, "General")

        # ── Colors ───────────────────────────────────────────────────
        tabs.addTab(self._build_colors_tab(), "Colors")

        # ── Variables ────────────────────────────────────────────────
        self._var_table = _make_table(["Variable", "Value"])
        _list_to_table(self._var_table, self._cfg.get("variables", []), ["name", "value"])
        tabs.addTab(self._wrap_table(self._var_table, ["name", "value"]), "Variables")

        # ── Aliases ──────────────────────────────────────────────────
        self._alias_table = _make_table(["Name", "Body", "On"])
        _list_to_table(self._alias_table, self._cfg["aliases"], ["name", "body", "enabled"])
        tabs.addTab(self._wrap_table(self._alias_table, ["name", "body"]), "Aliases")

        # ── Triggers ─────────────────────────────────────────────────
        folders = self._cfg.get("trigger_folders") or _migrate_actions(self._cfg.get("actions", []))
        self._trigger_editor = TriggerEditor(folders)
        tabs.addTab(self._trigger_editor, "Triggers")

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

    def _build_colors_tab(self) -> QWidget:
        """Build the Colors tab: theme dropdown + 16 interactive swatches."""
        from core.ansi_parser import THEMES, get_palette, palette_name

        w      = QWidget()
        vbox   = QVBoxLayout(w)
        vbox.setContentsMargins(16, 16, 16, 16)
        vbox.setSpacing(14)

        # ── Theme dropdown ────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Theme:"))
        self._theme_combo = QComboBox()
        self._theme_combo.setMaximumWidth(200)
        for name in THEMES:
            if name != "Custom":
                self._theme_combo.addItem(name)
        self._theme_combo.addItem("Custom")

        # Determine starting theme from saved config
        saved_pal   = self._cfg.get("palette")
        saved_theme = self._cfg.get("palette_theme", "xterm")
        if saved_pal and len(saved_pal) == 16:
            start_colors = saved_pal
            start_theme  = palette_name(saved_pal)
        else:
            start_colors = list(THEMES.get(saved_theme, THEMES["xterm"]))
            start_theme  = saved_theme

        idx = self._theme_combo.findText(start_theme)
        self._theme_combo.setCurrentIndex(max(idx, 0))

        hdr.addWidget(self._theme_combo)
        hdr.addStretch()
        vbox.addLayout(hdr)

        # ── Colour grid ───────────────────────────────────────────────
        grid = QGridLayout()
        grid.setSpacing(6)
        _LABELS = [
            "Black","Red","Green","Yellow",
            "Blue","Magenta","Cyan","White",
            "Br.Black","Br.Red","Br.Green","Br.Yellow",
            "Br.Blue","Br.Magenta","Br.Cyan","Br.White",
        ]
        self._color_btns: list[QPushButton] = []
        for i, (label, color) in enumerate(zip(_LABELS, start_colors)):
            row_i, col_i = divmod(i, 8)
            lbl = QLabel(label)
            lbl.setStyleSheet("color:#aaa; font-size:8pt;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(lbl, row_i * 2, col_i)

            btn = QPushButton()
            btn.setFixedSize(48, 28)
            btn.setProperty("color", color)
            btn.setStyleSheet(f"background:{color}; border:1px solid #555; border-radius:3px;")
            btn.setToolTip(f"{label}: {color}")

            def _make_clicker(b):
                def _click():
                    current = QColor(b.property("color"))
                    chosen  = QColorDialog.getColor(current, self, "Pick colour",
                                QColorDialog.ColorDialogOption.ShowAlphaChannel.__class__
                                .__bases__[0](0))   # no alpha
                    chosen  = QColorDialog.getColor(current, self, "Pick colour")
                    if chosen.isValid():
                        hex_col = chosen.name()
                        b.setProperty("color", hex_col)
                        b.setStyleSheet(
                            f"background:{hex_col}; border:1px solid #555; border-radius:3px;"
                        )
                        b.setToolTip(hex_col)
                        # Mark combo as Custom
                        ci = self._theme_combo.findText("Custom")
                        if ci >= 0:
                            self._theme_combo.setCurrentIndex(ci)
                return _click

            btn.clicked.connect(_make_clicker(btn))
            self._color_btns.append(btn)
            grid.addWidget(btn, row_i * 2 + 1, col_i)

        vbox.addLayout(grid)

        # ── Preview strip ─────────────────────────────────────────────
        prev_lbl = QLabel("Preview:")
        prev_lbl.setStyleSheet("color:#aaa; font-size:8pt; margin-top:6px;")
        vbox.addWidget(prev_lbl)
        self._preview_strip = QFrame()
        self._preview_strip.setFixedHeight(24)
        self._preview_strip.setFrameShape(QFrame.Shape.StyledPanel)
        vbox.addWidget(self._preview_strip)
        self._refresh_preview()

        # When theme dropdown changes → reload all swatches
        def _on_theme(theme_name: str):
            if theme_name == "Custom":
                return
            pal = THEMES.get(theme_name)
            if not pal:
                return
            for btn, color in zip(self._color_btns, pal):
                btn.setProperty("color", color)
                btn.setStyleSheet(
                    f"background:{color}; border:1px solid #555; border-radius:3px;"
                )
                btn.setToolTip(color)
            self._refresh_preview()

        self._theme_combo.currentTextChanged.connect(_on_theme)

        # ── Command-echo colour ───────────────────────────────────────
        sep_line = QFrame()
        sep_line.setFrameShape(QFrame.Shape.HLine)
        sep_line.setStyleSheet("color: #333;")
        vbox.addWidget(sep_line)

        echo_row = QHBoxLayout()
        echo_lbl = QLabel("Command echo colour:")
        echo_lbl.setStyleSheet("color:#aaa; font-size:9pt;")
        echo_row.addWidget(echo_lbl)
        echo_c = self._cfg.get("cmd_echo_color", "#e8d44d")
        self._echo_color_btn = QPushButton()
        self._echo_color_btn.setFixedSize(48, 22)
        self._echo_color_btn.setProperty("color", echo_c)
        self._echo_color_btn.setStyleSheet(
            f"background:{echo_c}; border:1px solid #555; border-radius:3px;")
        self._echo_color_btn.setToolTip(echo_c)
        def _pick_echo_color():
            chosen = QColorDialog.getColor(
                QColor(self._echo_color_btn.property("color")), self, "Echo colour")
            if chosen.isValid():
                c = chosen.name()
                self._echo_color_btn.setProperty("color", c)
                self._echo_color_btn.setStyleSheet(
                    f"background:{c}; border:1px solid #555; border-radius:3px;")
                self._echo_color_btn.setToolTip(c)
        self._echo_color_btn.clicked.connect(_pick_echo_color)
        echo_row.addWidget(self._echo_color_btn)
        echo_row.addStretch()
        vbox.addLayout(echo_row)
        vbox.addStretch()
        return w

    def _refresh_preview(self):
        """Paint 16 colour squares across the preview strip."""
        colors = [btn.property("color") for btn in self._color_btns]
        squares = "".join(
            f"<span style=\"background:{c}; color:{c}; padding:0 6px;\">&nbsp;&nbsp;</span>"
            for c in colors
        )
        # Use a QLabel trick: set the strip's layout
        layout = self._preview_strip.layout()
        if layout is None:
            from PyQt6.QtWidgets import QHBoxLayout as _HBL
            layout = _HBL(self._preview_strip)
            layout.setContentsMargins(2, 2, 2, 2)
            layout.setSpacing(1)
            self._prev_labels: list[QLabel] = []
            for c in colors:
                lbl = QLabel()
                lbl.setFixedSize(14, 18)
                lbl.setStyleSheet(f"background:{c}; border:none;")
                layout.addWidget(lbl)
                self._prev_labels.append(lbl)
            layout.addStretch()
        else:
            for lbl, c in zip(self._prev_labels, colors):
                lbl.setStyleSheet(f"background:{c}; border:none;")

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
        sep = self._sep_edit.text()
        self._cfg["cmd_separator"] = sep if sep else ";"
        self._cfg["cmd_echo"]       = self._echo_check.isChecked()
        self._cfg["cmd_echo_color"] = self._echo_color_btn.property("color")
        self._cfg["palette"]       = [btn.property("color") for btn in self._color_btns]
        self._cfg["palette_theme"] = self._theme_combo.currentText()
        self._cfg["variables"]  = _table_to_list(self._var_table,    ["name", "value"])
        self._cfg["aliases"]    = _table_to_list(self._alias_table,  ["name", "body", "enabled"])
        self._cfg["trigger_folders"] = self._trigger_editor.get_folders()
        self._cfg["timers"]     = _table_to_list(self._timer_table,   ["name", "interval", "command", "enabled"])
        self._cfg["buttons"]    = _table_to_list(self._button_table,  ["label", "command", "enabled"])
        self._cfg["highlights"] = _table_to_list(self._hl_table,      ["pattern", "color", "enabled"])
        self.config_saved.emit(copy.deepcopy(self._cfg))
        self.accept()

    def closeEvent(self, event):
        save_geometry("config_dialog", self)
        super().closeEvent(event)

    def get_config(self) -> dict:
        return copy.deepcopy(self._cfg)
