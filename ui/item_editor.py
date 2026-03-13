"""
ItemEditor — unified folder/item browser for triggers, aliases, variables,
timers, and buttons.

Data model
----------
"folders": list of {
    "_root":   bool   (optional — root folder is undeletable)
    "name":    str
    "enabled": bool
    "items":   list of item dicts, each with a "type" key:

        trigger  — name, patterns: list[str], body: str, enabled: bool
        alias    — name, match: str, body: str, enabled: bool
        variable — name, value: str
        timer    — name, interval: int (seconds), body: str, enabled: bool
        button   — name, label: str, color: str, body: str, enabled: bool
}

UI layout
---------
 ┌─ filter tabs (All / Triggers / Aliases / Variables / Timers / Buttons)
 ├── QSplitter (horizontal) ─────────────────────────────────────────────
 │   left: folder+item tree + toolbar
 │   right: QStackedWidget
 │           0  placeholder
 │           1  folder editor
 │           2  trigger editor  (vertical splitter: patterns / script)
 │           3  alias editor    (match field + script)
 │           4  variable editor (name + value only)
 │           5  timer editor    (interval spinbox + script)
 │           6  button editor   (label, color, script)
 └────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import copy
from typing import Optional

from PyQt6.QtCore    import Qt, QSize
from PyQt6.QtGui     import QColor, QBrush, QFont, QTextOption
from PyQt6.QtWidgets import (
    QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTreeWidget, QTreeWidgetItem, QPushButton, QLineEdit,
    QTextEdit, QLabel, QCheckBox, QScrollArea, QFrame,
    QStackedWidget, QSizePolicy, QSpinBox, QTabBar,
    QColorDialog,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_ROOT_NAME = "All"

_TYPE_ICON = {
    "trigger":  "⚡",
    "alias":    "↩",
    "variable": "$",
    "timer":    "⏱",
    "button":   "🔘",
}

_FILTER_LABELS = ["All", "Triggers", "Aliases", "Variables", "Timers", "Buttons"]
_FILTER_TYPES  = [None, "trigger", "alias", "variable", "timer", "button"]

_STYLE = """
    QWidget       { background: #1a1a1a; color: #d0d0d0; }
    QTreeWidget {
        background: #111; color: #ccc;
        border: 1px solid #333; font-size:12pt;
    }
    QTreeWidget::item { padding: 3px 2px; }
    QTreeWidget::item:selected { background: #1e3a5a; color: #eee; }
    QLineEdit, QSpinBox {
        background: #1e1e1e; color: #ddd;
        border: 1px solid #444; padding: 4px;
        font-family: Monospace; font-size:12pt;
    }
    QTextEdit {
        background: #141414; color: #ddd;
        border: 1px solid #444; padding: 6px;
        font-family: Monospace; font-size:12pt;
    }
    QCheckBox { color: #ccc; font-size:12pt; }
    QPushButton {
        background: #2a2a2a; color: #ccc;
        border: 1px solid #444; padding: 3px 10px; font-size:11pt;
    }
    QPushButton:hover  { background: #363636; }
    QPushButton:pressed{ background: #444; }
    QLabel { color: #aaa; font-size:11pt; }
    QLabel#section_label {
        color: #888; font-size:10pt; letter-spacing: 1px;
        border-bottom: 1px solid #333; padding-bottom: 3px;
    }
    QTabBar::tab {
        background: #1e1e1e; color: #888;
        padding: 4px 12px; border: 1px solid #333;
        border-bottom: none; margin-right: 2px;
    }
    QTabBar::tab:selected { background: #2a2a2a; color: #ddd; }
    QTabBar::tab:hover    { background: #252525; color: #bbb; }
    QSplitter::handle:horizontal { background: #2a2a2a; width: 3px; }
    QSplitter::handle:vertical   {
        background: #3a3a3a; height: 5px;
        border-top: 1px solid #555; border-bottom: 1px solid #555;
    }
"""


# ── Font helpers ─────────────────────────────────────────────────────────────

def _folder_font(bold: bool = True) -> QFont:
    f = QFont(); f.setBold(bold); f.setPointSize(12); return f

def _item_font(strike: bool) -> QFont:
    f = QFont(); f.setPointSize(12); f.setStrikeOut(strike); return f

def _item_color(active: bool) -> QBrush:
    return QBrush(QColor("#ccc") if active else QColor("#555"))

def _folder_color(enabled: bool, is_root: bool) -> QBrush:
    if not enabled:
        return QBrush(QColor("#555"))
    return QBrush(QColor("#ffcc66") if is_root else QColor("#ddd"))


# ── Section label factory ────────────────────────────────────────────────────

def _section(text: str) -> QLabel:
    l = QLabel(text); l.setObjectName("section_label"); l.setWordWrap(True)
    return l


# ── ItemEditor ────────────────────────────────────────────────────────────────

class ItemEditor(QWidget):

    def __init__(self, folders: list[dict], parent=None):
        super().__init__(parent)
        self.setStyleSheet(_STYLE)
        self._folders: list[dict] = copy.deepcopy(folders) if folders else []
        self._ensure_root()
        self._filter: Optional[str] = None   # None = show all
        self._sel_fi:  Optional[int] = None
        self._sel_ii:  Optional[int] = None  # item index within folder
        self._build_ui()
        self._populate_tree()

    # ── Root guarantee ───────────────────────────────────────────────

    def _ensure_root(self):
        for i, f in enumerate(self._folders):
            if f.get("_root"):
                if i != 0:
                    self._folders.insert(0, self._folders.pop(i))
                return
        self._folders.insert(0, {
            "_root": True, "name": _ROOT_NAME,
            "enabled": True, "items": []
        })

    # ── Build UI ─────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Filter tab bar ────────────────────────────────────────────
        self._tab_bar = QTabBar()
        self._tab_bar.setExpanding(False)
        for label in _FILTER_LABELS:
            self._tab_bar.addTab(label)
        self._tab_bar.currentChanged.connect(self._on_filter_changed)
        outer.addWidget(self._tab_bar)

        # ── Horizontal splitter ───────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter)

        # Left pane
        left = QWidget()
        lv   = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.setSpacing(4)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self._tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._tree.itemSelectionChanged.connect(self._on_selection)
        self._tree.itemChanged.connect(self._sync_from_tree)
        lv.addWidget(self._tree, 1)

        tb = QHBoxLayout(); tb.setSpacing(3)
        for label, slot in [
            ("+ Folder",  self._add_folder),
            ("+ Item",    self._add_item_menu),
            ("Delete",    self._delete_selected),
        ]:
            b = QPushButton(label); b.clicked.connect(slot); tb.addWidget(b)
        tb.addStretch()
        lv.addLayout(tb)
        splitter.addWidget(left)
        splitter.setStretchFactor(0, 0)

        # Right pane stack
        self._stack = QStackedWidget()
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 600])

        # Page 0 — placeholder
        ph = QLabel("Select a folder or item to edit")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setStyleSheet("color:#555; font-size:13pt;")
        self._stack.addWidget(ph)                           # idx 0

        # Page 1 — folder editor
        self._stack.addWidget(self._build_folder_page())   # idx 1

        # Page 2 — trigger
        self._stack.addWidget(self._build_trigger_page())  # idx 2

        # Page 3 — alias
        self._stack.addWidget(self._build_alias_page())    # idx 3

        # Page 4 — variable
        self._stack.addWidget(self._build_variable_page()) # idx 4

        # Page 5 — timer
        self._stack.addWidget(self._build_timer_page())    # idx 5

        # Page 6 — button
        self._stack.addWidget(self._build_button_page())   # idx 6

    # ── Page builders ────────────────────────────────────────────────

    def _build_folder_page(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 16); v.setSpacing(10)
        v.addWidget(_section("FOLDER"))
        form = QFormLayout(); form.setSpacing(8)
        self._folder_name_edit = QLineEdit()
        self._folder_name_edit.setPlaceholderText("Folder name")
        self._folder_name_edit.editingFinished.connect(self._save_folder)
        form.addRow("Name:", self._folder_name_edit)
        self._folder_enabled_chk = QCheckBox("Enabled")
        self._folder_enabled_chk.stateChanged.connect(self._save_folder)
        form.addRow("", self._folder_enabled_chk)
        v.addLayout(form); v.addStretch()
        return w

    def _build_trigger_page(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(16, 12, 16, 12); v.setSpacing(6)
        v.addWidget(_section("TRIGGER"))

        nr = QHBoxLayout()
        nr.addWidget(QLabel("Name:"))
        self._trig_name = QLineEdit()
        self._trig_name.setPlaceholderText("Trigger name")
        self._trig_name.editingFinished.connect(self._save_item)
        nr.addWidget(self._trig_name, 1)
        self._trig_enabled = QCheckBox("Enabled")
        self._trig_enabled.stateChanged.connect(self._save_item)
        nr.addWidget(self._trig_enabled)
        v.addLayout(nr)

        vsplit = self._make_vsplit()
        v.addWidget(vsplit, 1)

        # top: patterns
        pp = QWidget(); pp.setStyleSheet("background:transparent;")
        pv = QVBoxLayout(pp); pv.setContentsMargins(0,4,0,2); pv.setSpacing(4)
        pv.addWidget(_section("PATTERNS  (regex or plain text — %1–%9 capture groups)"))
        self._pat_scroll = QScrollArea()
        self._pat_scroll.setWidgetResizable(True)
        self._pat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._pat_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self._pat_container = QWidget()
        self._pat_container.setStyleSheet("background:transparent;")
        self._pat_layout = QVBoxLayout(self._pat_container)
        self._pat_layout.setContentsMargins(0,0,0,0)
        self._pat_layout.setSpacing(3)
        self._pat_layout.addStretch()
        self._pat_scroll.setWidget(self._pat_container)
        pv.addWidget(self._pat_scroll, 1)
        pb = QHBoxLayout()
        ap = QPushButton("+ Pattern"); ap.clicked.connect(lambda: self._add_pat_row(""))
        dp = QPushButton("− Pattern"); dp.clicked.connect(self._del_pat_row)
        pb.addWidget(ap); pb.addWidget(dp); pb.addStretch()
        pv.addLayout(pb)
        vsplit.addWidget(pp)

        # bottom: script
        bp = QWidget(); bp.setStyleSheet("background:transparent;")
        bv = QVBoxLayout(bp); bv.setContentsMargins(0,4,0,0); bv.setSpacing(4)

        # Header row: mode badge + section label
        hdr = QHBoxLayout(); hdr.setContentsMargins(0,0,0,0); hdr.setSpacing(6)
        self._trig_mode_label = QLabel("SCRIPT")
        self._trig_mode_label.setStyleSheet(
            "font:bold 9px; letter-spacing:1px; color:#888;")
        self._trig_hint_label = QLabel("TinTin++ — #gag / #showme / #send … | start with #python for Python")
        self._trig_hint_label.setStyleSheet("font:9px; color:#666; font-style:italic;")
        hdr.addWidget(self._trig_mode_label)
        hdr.addWidget(self._trig_hint_label, 1)
        bv.addLayout(hdr)

        self._trig_body = QTextEdit()
        self._trig_body.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self._trig_body.setMinimumHeight(60)
        self._trig_body.textChanged.connect(self._on_trig_body_changed)
        bv.addWidget(self._trig_body, 1)
        vsplit.addWidget(bp)
        vsplit.setStretchFactor(0, 1); vsplit.setStretchFactor(1, 2)
        return w

    def _build_alias_page(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(16, 12, 16, 12); v.setSpacing(6)
        v.addWidget(_section("ALIAS"))

        nr = QHBoxLayout()
        nr.addWidget(QLabel("Name:"))
        self._alias_name = QLineEdit()
        self._alias_name.setPlaceholderText("Alias name (identifier)")
        self._alias_name.editingFinished.connect(self._save_item)
        nr.addWidget(self._alias_name, 1)
        self._alias_enabled = QCheckBox("Enabled")
        self._alias_enabled.stateChanged.connect(self._save_item)
        nr.addWidget(self._alias_enabled)
        v.addLayout(nr)

        v.addWidget(_section("MATCH  (command word to intercept, e.g. 'k')"))
        self._alias_match = QLineEdit()
        self._alias_match.setPlaceholderText("Command word or phrase")
        self._alias_match.editingFinished.connect(self._save_item)
        v.addWidget(self._alias_match)

        vsplit = self._make_vsplit()
        v.addWidget(vsplit, 1)

        # top: match hint pane (just a label + the match field again at larger view)
        mp = QWidget(); mp.setStyleSheet("background:transparent;")
        mv = QVBoxLayout(mp); mv.setContentsMargins(0,4,0,2); mv.setSpacing(4)
        mv.addWidget(_section("MATCH PREVIEW"))
        self._alias_match_preview = QLabel()
        self._alias_match_preview.setStyleSheet(
            "background:#1e1e1e; border:1px solid #333; padding:6px; color:#aaa; font-family:Monospace;")
        self._alias_match_preview.setWordWrap(True)
        mv.addWidget(self._alias_match_preview)
        mv.addStretch()
        vsplit.addWidget(mp)

        # bottom: body
        bp = QWidget(); bp.setStyleSheet("background:transparent;")
        bv = QVBoxLayout(bp); bv.setContentsMargins(0,4,0,0); bv.setSpacing(4)
        bv.addWidget(_section("SCRIPT  (TinTin++ — #send / #var / #showme / #if …)"))
        self._alias_body = QTextEdit()
        self._alias_body.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self._alias_body.setMinimumHeight(60)
        self._alias_body.textChanged.connect(self._save_item)
        bv.addWidget(self._alias_body, 1)
        vsplit.addWidget(bp)
        vsplit.setStretchFactor(0, 0); vsplit.setStretchFactor(1, 1)
        return w

    def _build_variable_page(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(16, 16, 16, 16); v.setSpacing(10)
        v.addWidget(_section("VARIABLE"))
        form = QFormLayout(); form.setSpacing(8)
        self._var_name = QLineEdit()
        self._var_name.setPlaceholderText("Variable name")
        self._var_name.editingFinished.connect(self._save_item)
        form.addRow("Name:", self._var_name)
        self._var_value = QLineEdit()
        self._var_value.setPlaceholderText("Value")
        self._var_value.editingFinished.connect(self._save_item)
        form.addRow("Value:", self._var_value)
        v.addLayout(form); v.addStretch()
        return w

    def _build_timer_page(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(16, 12, 16, 12); v.setSpacing(6)
        v.addWidget(_section("TIMER"))

        nr = QHBoxLayout()
        nr.addWidget(QLabel("Name:"))
        self._timer_name = QLineEdit()
        self._timer_name.setPlaceholderText("Timer name")
        self._timer_name.editingFinished.connect(self._save_item)
        nr.addWidget(self._timer_name, 1)
        self._timer_enabled = QCheckBox("Enabled")
        self._timer_enabled.stateChanged.connect(self._save_item)
        nr.addWidget(self._timer_enabled)
        v.addLayout(nr)

        ir = QHBoxLayout()
        ir.addWidget(QLabel("Interval:"))
        self._timer_interval = QSpinBox()
        self._timer_interval.setRange(1, 86400)
        self._timer_interval.setValue(30)
        self._timer_interval.setSuffix("  seconds")
        self._timer_interval.setMaximumWidth(160)
        self._timer_interval.valueChanged.connect(self._save_item)
        ir.addWidget(self._timer_interval); ir.addStretch()
        v.addLayout(ir)

        v.addWidget(_section("SCRIPT  (runs every interval — #send / #var / #showme …)"))
        self._timer_body = QTextEdit()
        self._timer_body.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self._timer_body.setMinimumHeight(60)
        self._timer_body.textChanged.connect(self._save_item)
        v.addWidget(self._timer_body, 1)
        return w

    def _build_button_page(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(16, 12, 16, 12); v.setSpacing(6)
        v.addWidget(_section("BUTTON"))

        nr = QHBoxLayout()
        nr.addWidget(QLabel("Name:"))
        self._btn_name = QLineEdit()
        self._btn_name.setPlaceholderText("Internal name")
        self._btn_name.editingFinished.connect(self._save_item)
        nr.addWidget(self._btn_name, 1)
        self._btn_enabled = QCheckBox("Enabled")
        self._btn_enabled.stateChanged.connect(self._save_item)
        nr.addWidget(self._btn_enabled)
        v.addLayout(nr)

        lr = QHBoxLayout()
        lr.addWidget(QLabel("Label:"))
        self._btn_label = QLineEdit()
        self._btn_label.setPlaceholderText("Text shown on button")
        self._btn_label.editingFinished.connect(self._save_item)
        lr.addWidget(self._btn_label, 1)
        lr.addWidget(QLabel("  Colour:"))
        self._btn_color_btn = QPushButton()
        self._btn_color_btn.setFixedSize(40, 24)
        self._btn_color_btn.setProperty("color", "#1e3a1e")
        self._btn_color_btn.setStyleSheet(
            "background:#1e3a1e; border:1px solid #555; border-radius:3px;")
        def _pick_btn_color():
            c = QColorDialog.getColor(
                QColor(self._btn_color_btn.property("color")), self, "Button colour")
            if c.isValid():
                h = c.name()
                self._btn_color_btn.setProperty("color", h)
                self._btn_color_btn.setStyleSheet(
                    f"background:{h}; border:1px solid #555; border-radius:3px;")
                self._save_item()
        self._btn_color_btn.clicked.connect(_pick_btn_color)
        lr.addWidget(self._btn_color_btn)
        v.addLayout(lr)

        v.addWidget(_section("SCRIPT  (executed when clicked — #send / #var / #showme …)"))
        self._btn_body = QTextEdit()
        self._btn_body.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self._btn_body.setMinimumHeight(60)
        self._btn_body.textChanged.connect(self._save_item)
        v.addWidget(self._btn_body, 1)
        return w

    # ── Shared vertical splitter factory ────────────────────────────

    def _make_vsplit(self) -> QSplitter:
        s = QSplitter(Qt.Orientation.Vertical)
        s.setHandleWidth(5)
        return s

    # ── Filter tab ───────────────────────────────────────────────────

    def _on_filter_changed(self, idx: int):
        self._filter = _FILTER_TYPES[idx]
        self._apply_filter()

    def _apply_filter(self):
        ftype = self._filter
        for fi in range(self._tree.topLevelItemCount()):
            f_item = self._tree.topLevelItem(fi)
            any_visible = False
            for ci in range(f_item.childCount()):
                child = f_item.child(ci)
                item_type = child.data(0, Qt.ItemDataRole.UserRole + 1)
                hidden = (ftype is not None and item_type != ftype)
                child.setHidden(hidden)
                if not hidden:
                    any_visible = True
            # Hide folder only if nothing passes filter (except root always visible)
            folder_data = f_item.data(0, Qt.ItemDataRole.UserRole)
            is_root = folder_data and folder_data[0] == "root"
            f_item.setHidden(not is_root and not any_visible and ftype is not None)

    # ── Tree population ──────────────────────────────────────────────

    def _populate_tree(self):
        self._tree.blockSignals(True)
        self._tree.clear()
        for folder in self._folders:
            f_item = self._make_folder_tree_item(folder)
            self._tree.addTopLevelItem(f_item)
            for item in folder.get("items", []):
                f_item.addChild(self._make_item_tree_item(item, folder.get("enabled", True)))
            f_item.setExpanded(True)
        self._tree.blockSignals(False)
        self._apply_filter()

    def _make_folder_tree_item(self, folder: dict) -> QTreeWidgetItem:
        is_root = folder.get("_root", False)
        enabled = folder.get("enabled", True)
        icon    = "🌐" if is_root else "📁"
        name    = folder.get("name", "Folder")
        label   = f"{icon}  {name}"
        if is_root and not enabled:
            label += "  (all disabled)"
        it = QTreeWidgetItem([label])
        it.setFont(0, _folder_font())
        it.setForeground(0, _folder_color(enabled, is_root))
        it.setData(0, Qt.ItemDataRole.UserRole, ("root",) if is_root else ("folder",))
        return it

    def _make_item_tree_item(self, item: dict, folder_enabled: bool) -> QTreeWidgetItem:
        itype   = item.get("type", "?")
        enabled = item.get("enabled", True) if itype != "variable" else True
        active  = enabled and folder_enabled
        # Python triggers get a snake icon
        if itype == "trigger":
            body  = item.get("body", "")
            first = body.lstrip().split('\n', 1)[0].strip().lower()
            icon  = "🐍" if first in ("#python", "# python") else _TYPE_ICON.get(itype, "•")
        else:
            icon = _TYPE_ICON.get(itype, "•")
        name    = self._item_display_name(item)
        it = QTreeWidgetItem([f"  {icon}  {name}"])
        it.setFont(0, _item_font(not active))
        it.setForeground(0, _item_color(active))
        it.setData(0, Qt.ItemDataRole.UserRole,     ("item",))
        it.setData(0, Qt.ItemDataRole.UserRole + 1, itype)
        return it

    def _item_display_name(self, item: dict) -> str:
        itype = item.get("type", "")
        name  = item.get("name", "").strip()
        if name:
            return name
        if itype == "trigger":
            pats = item.get("patterns", [])
            return pats[0][:40] if pats else "Trigger"
        if itype == "alias":
            return item.get("match", "Alias")
        if itype == "variable":
            return f'{item.get("name","var")} = {item.get("value","")}'[:40]
        if itype == "timer":
            return f'{item.get("interval",0)}s timer'
        if itype == "button":
            return item.get("label", "Button")
        return "Item"

    def _refresh_folder_item(self, fi: int):
        folder = self._folders[fi]
        f_item = self._tree.topLevelItem(fi)
        if not f_item:
            return
        self._tree.blockSignals(True)
        is_root = folder.get("_root", False)
        enabled = folder.get("enabled", True)
        icon    = "🌐" if is_root else "📁"
        label   = f"{icon}  {folder.get('name','Folder')}"
        if is_root and not enabled:
            label += "  (all disabled)"
        f_item.setText(0, label)
        f_item.setForeground(0, _folder_color(enabled, is_root))
        # refresh children
        for ci in range(f_item.childCount()):
            child  = f_item.child(ci)
            if ci < len(folder.get("items", [])):
                item   = folder["items"][ci]
                itype  = item.get("type", "")
                active = item.get("enabled", True) and enabled
                if itype == "variable":
                    active = True
                child.setText(0, f"  {_TYPE_ICON.get(itype,'•')}  {self._item_display_name(item)}")
                child.setFont(0, _item_font(not active))
                child.setForeground(0, _item_color(active))
        self._tree.blockSignals(False)
        self._apply_filter()

    # ── Selection ────────────────────────────────────────────────────

    def _on_selection(self):
        items = self._tree.selectedItems()
        if not items:
            self._sel_fi = None; self._sel_ii = None
            self._stack.setCurrentIndex(0)
            return
        it    = items[0]
        role  = it.data(0, Qt.ItemDataRole.UserRole)
        if not role:
            return
        kind = role[0]
        if kind in ("folder", "root"):
            fi = self._tree.indexOfTopLevelItem(it)
            self._sel_fi = fi; self._sel_ii = None
            self._load_folder_page(fi)
        elif kind == "item":
            parent = it.parent()
            fi     = self._tree.indexOfTopLevelItem(parent)
            ii     = parent.indexOfChild(it)
            self._sel_fi = fi; self._sel_ii = ii
            self._load_item_page(fi, ii)

    def _sync_from_tree(self, *args):
        pass   # drag-drop handled lazily on get_folders()

    # ── Load pages ───────────────────────────────────────────────────

    def _load_folder_page(self, fi: int):
        folder  = self._folders[fi]
        is_root = folder.get("_root", False)
        self._folder_name_edit.blockSignals(True)
        self._folder_enabled_chk.blockSignals(True)
        self._folder_name_edit.setText(folder.get("name", ""))
        self._folder_name_edit.setEnabled(not is_root)
        tip = ("Toggle to disable ALL items globally."
               if is_root else "Disabling stops all items in this folder.")
        self._folder_enabled_chk.setText(f"Enabled  ({tip})")
        self._folder_enabled_chk.setChecked(folder.get("enabled", True))
        self._folder_name_edit.blockSignals(False)
        self._folder_enabled_chk.blockSignals(False)
        self._stack.setCurrentIndex(1)

    def _load_item_page(self, fi: int, ii: int):
        items = self._folders[fi].get("items", [])
        if ii >= len(items):
            return
        item  = items[ii]
        itype = item.get("type", "")
        _PAGE = {"trigger":2, "alias":3, "variable":4, "timer":5, "button":6}
        page  = _PAGE.get(itype, 0)
        self._stack.setCurrentIndex(page)

        def block(widgets, yes=True):
            for w in widgets:
                if w: w.blockSignals(yes)

        if itype == "trigger":
            block([self._trig_name, self._trig_enabled, self._trig_body])
            self._trig_name.setText(item.get("name", ""))
            self._trig_enabled.setChecked(item.get("enabled", True))
            self._clear_pat_rows()
            for p in item.get("patterns", [""]):
                self._add_pat_row(p, emit=False)
            if not item.get("patterns"):
                self._add_pat_row("", emit=False)
            self._trig_body.setPlainText(item.get("body", ""))
            block([self._trig_name, self._trig_enabled, self._trig_body], False)

        elif itype == "alias":
            block([self._alias_name, self._alias_enabled, self._alias_match, self._alias_body])
            self._alias_name.setText(item.get("name", ""))
            self._alias_enabled.setChecked(item.get("enabled", True))
            self._alias_match.setText(item.get("match", ""))
            self._alias_match_preview.setText(
                f"When you type:  <b>{item.get('match','…')}</b>")
            self._alias_body.setPlainText(item.get("body", ""))
            block([self._alias_name, self._alias_enabled, self._alias_match, self._alias_body], False)

        elif itype == "variable":
            block([self._var_name, self._var_value])
            self._var_name.setText(item.get("name", ""))
            self._var_value.setText(item.get("value", ""))
            block([self._var_name, self._var_value], False)

        elif itype == "timer":
            block([self._timer_name, self._timer_enabled, self._timer_interval, self._timer_body])
            self._timer_name.setText(item.get("name", ""))
            self._timer_enabled.setChecked(item.get("enabled", True))
            self._timer_interval.setValue(int(item.get("interval", 30)))
            self._timer_body.setPlainText(item.get("body", ""))
            block([self._timer_name, self._timer_enabled, self._timer_interval, self._timer_body], False)

        elif itype == "button":
            block([self._btn_name, self._btn_enabled, self._btn_label, self._btn_body])
            self._btn_name.setText(item.get("name", ""))
            self._btn_enabled.setChecked(item.get("enabled", True))
            self._btn_label.setText(item.get("label", ""))
            col = item.get("color", "#1e3a1e")
            self._btn_color_btn.setProperty("color", col)
            self._btn_color_btn.setStyleSheet(
                f"background:{col}; border:1px solid #555; border-radius:3px;")
            self._btn_body.setPlainText(item.get("body", ""))
            block([self._btn_name, self._btn_enabled, self._btn_label, self._btn_body], False)

    # ── Save callbacks ───────────────────────────────────────────────

    def _save_folder(self):
        fi = self._sel_fi
        if fi is None or fi >= len(self._folders):
            return
        folder = self._folders[fi]
        if not folder.get("_root"):
            folder["name"] = self._folder_name_edit.text().strip() or "Folder"
        folder["enabled"] = self._folder_enabled_chk.isChecked()
        self._refresh_folder_item(fi)

    def _on_trig_body_changed(self):
        """Update the Python/TinTin++ mode badge and then save."""
        body = self._trig_body.toPlainText()
        first = body.lstrip().split('\n', 1)[0].strip().lower()
        is_python = first in ('#python', '# python')
        if is_python:
            self._trig_mode_label.setText("🐍 PYTHON")
            self._trig_mode_label.setStyleSheet(
                "font:bold 9px; letter-spacing:1px; color:#5af;")
            self._trig_hint_label.setText(
                "m[1]…m[9]  m1…m9  send()  showme()  gag()  vars  raw  — full Python + imports")
        else:
            self._trig_mode_label.setText("SCRIPT")
            self._trig_mode_label.setStyleSheet(
                "font:bold 9px; letter-spacing:1px; color:#888;")
            self._trig_hint_label.setText(
                "TinTin++ — #gag / #showme / #send … | start with #python for Python")
        self._save_item()

    def _save_item(self):
        fi = self._sel_fi; ii = self._sel_ii
        if fi is None or ii is None:
            return
        items = self._folders[fi].get("items", [])
        if ii >= len(items):
            return
        item  = items[ii]
        itype = item.get("type", "")
        if itype == "trigger":
            item["name"]     = self._trig_name.text().strip()
            item["enabled"]  = self._trig_enabled.isChecked()
            item["patterns"] = self._collect_pat_rows()
            item["body"]     = self._trig_body.toPlainText()
        elif itype == "alias":
            item["name"]    = self._alias_name.text().strip()
            item["enabled"] = self._alias_enabled.isChecked()
            item["match"]   = self._alias_match.text().strip()
            item["body"]    = self._alias_body.toPlainText()
            self._alias_match_preview.setText(
                f"When you type:  <b>{item['match'] or '…'}</b>")
        elif itype == "variable":
            item["name"]  = self._var_name.text().strip()
            item["value"] = self._var_value.text()
        elif itype == "timer":
            item["name"]     = self._timer_name.text().strip()
            item["enabled"]  = self._timer_enabled.isChecked()
            item["interval"] = self._timer_interval.value()
            item["body"]     = self._timer_body.toPlainText()
        elif itype == "button":
            item["name"]    = self._btn_name.text().strip()
            item["enabled"] = self._btn_enabled.isChecked()
            item["label"]   = self._btn_label.text().strip()
            item["color"]   = self._btn_color_btn.property("color")
            item["body"]    = self._btn_body.toPlainText()
        self._refresh_folder_item(fi)

    # ── Pattern rows ─────────────────────────────────────────────────

    def _clear_pat_rows(self):
        while self._pat_layout.count() > 1:
            it = self._pat_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _add_pat_row(self, text: str = "", emit: bool = True):
        e = QLineEdit(text)
        e.setPlaceholderText("Pattern (regex or plain text)")
        e.editingFinished.connect(self._save_item)
        pos = self._pat_layout.count() - 1
        self._pat_layout.insertWidget(pos, e)
        if emit:
            self._save_item()

    def _del_pat_row(self):
        count = self._pat_layout.count() - 1
        if count <= 1:
            return
        it = self._pat_layout.takeAt(count - 1)
        if it and it.widget():
            it.widget().deleteLater()
        self._save_item()

    def _collect_pat_rows(self) -> list[str]:
        pats = []
        for i in range(self._pat_layout.count() - 1):
            w = self._pat_layout.itemAt(i).widget()
            if isinstance(w, QLineEdit):
                t = w.text().strip()
                if t:
                    pats.append(t)
        return pats

    # ── Toolbar actions ──────────────────────────────────────────────

    def _add_folder(self):
        folder = {"name": "New Folder", "enabled": True, "items": []}
        self._folders.append(folder)
        f_item = self._make_folder_tree_item(folder)
        self._tree.addTopLevelItem(f_item)
        f_item.setExpanded(True)
        self._tree.setCurrentItem(f_item)
        self._sel_fi  = len(self._folders) - 1
        self._sel_ii  = None
        self._load_folder_page(self._sel_fi)
        self._folder_name_edit.setFocus()
        self._folder_name_edit.selectAll()

    def _add_item_menu(self):
        """Cycle through item types via a small floating menu or add trigger by default."""
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet("QMenu{background:#222;color:#ccc;border:1px solid #444;}"
                           "QMenu::item:selected{background:#1e3a5a;}")
        types = [("⚡  Trigger",  "trigger"),
                 ("↩  Alias",    "alias"),
                 ("$  Variable", "variable"),
                 ("⏱  Timer",    "timer"),
                 ("🔘  Button",  "button")]
        for label, itype in types:
            menu.addAction(label, lambda t=itype: self._add_item(t))
        btn = self.sender()
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _add_item(self, itype: str):
        fi = self._sel_fi
        if fi is None:
            if self._folders:
                fi = 0
            else:
                self._add_folder(); fi = 0
        defaults = {
            "trigger":  {"type":"trigger",  "name":"", "patterns":[""], "body":"", "enabled":True},
            "alias":    {"type":"alias",    "name":"", "match":"",      "body":"", "enabled":True},
            "variable": {"type":"variable", "name":"", "value":""},
            "timer":    {"type":"timer",    "name":"", "interval":30,   "body":"", "enabled":True},
            "button":   {"type":"button",   "name":"", "label":"",   "color":"#1e3a1e", "body":"", "enabled":True},
        }
        item = copy.deepcopy(defaults[itype])
        folder = self._folders[fi]
        folder.setdefault("items", []).append(item)
        ii     = len(folder["items"]) - 1
        f_item = self._tree.topLevelItem(fi)
        t_item = self._make_item_tree_item(item, folder.get("enabled", True))
        f_item.addChild(t_item)
        f_item.setExpanded(True)
        self._apply_filter()
        self._tree.setCurrentItem(t_item)
        self._sel_fi = fi; self._sel_ii = ii
        self._load_item_page(fi, ii)

    def _delete_selected(self):
        fi = self._sel_fi; ii = self._sel_ii
        if fi is None:
            return
        if ii is not None:
            del self._folders[fi]["items"][ii]
            f_item = self._tree.topLevelItem(fi)
            if f_item:
                child = f_item.child(ii)
                if child:
                    f_item.removeChild(child)
            self._sel_ii = None
            self._stack.setCurrentIndex(0)
        else:
            if self._folders[fi].get("_root"):
                return
            del self._folders[fi]
            item = self._tree.topLevelItem(fi)
            if item:
                self._tree.takeTopLevelItem(fi)
            self._sel_fi = None
            self._stack.setCurrentIndex(0)

    # ── Public API ───────────────────────────────────────────────────

    def get_folders(self) -> list[dict]:
        if self._sel_fi is not None and self._sel_ii is None:
            self._save_folder()
        if self._sel_fi is not None and self._sel_ii is not None:
            self._save_item()
        return copy.deepcopy(self._folders)
