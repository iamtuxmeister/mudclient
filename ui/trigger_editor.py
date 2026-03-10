"""
TriggerEditor — folder/trigger tree + script editor widget.

Data model
----------
trigger_folders: list of
  {
    "name":     str,
    "enabled":  bool,
    "triggers": list of
      {
        "name":     str,
        "patterns": list[str],   # one or more regex / substring patterns
        "body":     str,         # TinTin++ script body
        "enabled":  bool,
      }
  }

UI layout (QSplitter, horizontal)
-----------------------------------
Left pane  — QTreeWidget
              • folder items  (bold, folder icon)
              • trigger items (indented, strikethrough when disabled)
              • Disabled folder = all children appear struck-through
Right pane — stacked editor
              • Folder selected  → name + enabled checkbox
              • Trigger selected → name, pattern list, body editor
              • Nothing selected → empty placeholder
"""

from __future__ import annotations

import copy
from typing import Optional

from PyQt6.QtCore    import Qt, QSize
from PyQt6.QtGui     import (
    QFont, QColor, QBrush, QTextOption,
    QFontMetrics,
)
from PyQt6.QtWidgets import (
    QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTreeWidget, QTreeWidgetItem, QPushButton, QLineEdit,
    QTextEdit, QLabel, QCheckBox, QScrollArea, QFrame,
    QStackedWidget, QSizePolicy,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_STYLE = """
    QTreeWidget {
        background: #111; color: #ccc;
        border: 1px solid #333;
        font-size: 10pt;
    }
    QTreeWidget::item { padding: 3px 2px; }
    QTreeWidget::item:selected { background: #1e3a5a; color: #eee; }
    QLineEdit {
        background: #1e1e1e; color: #ddd;
        border: 1px solid #444; padding: 4px;
        font-family: Monospace; font-size: 10pt;
    }
    QTextEdit {
        background: #141414; color: #ddd;
        border: 1px solid #444; padding: 6px;
        font-family: Monospace; font-size: 10pt;
    }
    QCheckBox { color: #ccc; font-size: 10pt; }
    QPushButton {
        background: #2a2a2a; color: #ccc;
        border: 1px solid #444; padding: 3px 10px;
        font-size: 9pt;
    }
    QPushButton:hover  { background: #363636; }
    QPushButton:pressed{ background: #444; }
    QLabel { color: #aaa; font-size: 9pt; }
    QLabel#section_label {
        color: #888; font-size: 8pt; letter-spacing: 1px;
        border-bottom: 1px solid #333; padding-bottom: 3px;
    }
"""

_FOLDER_ICON = "📁"
_ROOT_NAME   = "All Triggers"   # undeletable master toggle
_TRIGGER_ICON = "⚡"

def _folder_font() -> QFont:
    f = QFont()
    f.setBold(True)
    f.setPointSize(10)
    return f

def _trigger_font(enabled: bool, folder_enabled: bool) -> QFont:
    f = QFont()
    f.setPointSize(10)
    f.setStrikeOut(not enabled or not folder_enabled)
    return f

def _strike_color(enabled: bool, folder_enabled: bool) -> QBrush:
    if not enabled or not folder_enabled:
        return QBrush(QColor("#555"))
    return QBrush(QColor("#ccc"))


# ── TriggerEditor ─────────────────────────────────────────────────────────────

class TriggerEditor(QWidget):
    """Full trigger management widget — embed in a tab."""

    def __init__(self, folders: list[dict], parent=None):
        super().__init__(parent)
        self.setStyleSheet(_STYLE)

        # Working copy of the data
        self._folders: list[dict] = copy.deepcopy(folders) if folders else []
        self._ensure_root_folder()

        # Currently selected item info
        self._sel_folder_idx:  Optional[int] = None
        self._sel_trigger_idx: Optional[int] = None

        self._build_ui()
        self._populate_tree()

    # ── Root folder guarantee ────────────────────────────────────────

    def _ensure_root_folder(self):
        """Make sure the first folder is the undeletable root, inserting if absent."""
        if self._folders and self._folders[0].get("_root"):
            return
        # Check if one already exists anywhere (migration)
        for i, f in enumerate(self._folders):
            if f.get("_root"):
                self._folders.insert(0, self._folders.pop(i))
                return
        # No root found — create one wrapping any existing top-level triggers
        root = {
            "_root":   True,
            "name":    _ROOT_NAME,
            "enabled": True,
            "triggers": [],
        }
        self._folders.insert(0, root)

    # ── Build UI ─────────────────────────────────────────────────────

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #2a2a2a; width: 3px; }")
        root.addWidget(splitter)

        # ── Left: tree + toolbar ──────────────────────────────────────
        left = QWidget()
        lv   = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.setSpacing(4)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self._tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._tree.itemSelectionChanged.connect(self._on_selection)
        self._tree.itemChanged.connect(self._on_item_changed)
        lv.addWidget(self._tree, 1)

        # Toolbar
        tb = QHBoxLayout()
        tb.setSpacing(3)
        for label, slot in [
            ("+ Folder",  self._add_folder),
            ("+ Trigger", self._add_trigger),
            ("Delete",    self._delete_selected),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            tb.addWidget(b)
        tb.addStretch()
        lv.addLayout(tb)

        splitter.addWidget(left)
        splitter.setStretchFactor(0, 0)

        # ── Right: stacked editor ─────────────────────────────────────
        self._stack = QStackedWidget()

        # Page 0 — empty placeholder
        ph = QLabel("Select a trigger or folder to edit")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setStyleSheet("color:#555; font-size:11pt;")
        self._stack.addWidget(ph)

        # Page 1 — folder editor
        self._stack.addWidget(self._build_folder_editor())

        # Page 2 — trigger editor
        self._stack.addWidget(self._build_trigger_editor())

        splitter.addWidget(self._stack)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 600])

    def _build_folder_editor(self) -> QWidget:
        w    = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setContentsMargins(16, 16, 16, 16)
        vbox.setSpacing(10)

        lbl = QLabel("FOLDER")
        lbl.setObjectName("section_label")
        vbox.addWidget(lbl)

        form = QFormLayout()
        form.setSpacing(8)
        self._folder_name = QLineEdit()
        self._folder_name.setPlaceholderText("Folder name")
        self._folder_name.editingFinished.connect(self._save_folder)
        form.addRow("Name:", self._folder_name)
        self._folder_enabled = QCheckBox("Enabled  (disabling stops all triggers in this folder)")
        self._folder_enabled.setChecked(True)
        self._folder_enabled.stateChanged.connect(self._save_folder)
        form.addRow("", self._folder_enabled)
        vbox.addLayout(form)
        vbox.addStretch()
        return w

    def _build_trigger_editor(self) -> QWidget:
        """
        Layout:
          outer vbox
            ├─ TRIGGER header + name row  (fixed height)
            └─ QSplitter (vertical, stretches to fill)
                 ├─ top pane: PATTERNS label + scrollable pattern rows + +/- buttons
                 └─ bottom pane: SCRIPT label + body editor
        Dragging the splitter handle resizes patterns vs script.
        Resizing the config window grows/shrinks the script box.
        """
        w    = QWidget()
        vbox = QVBoxLayout(w)
        vbox.setContentsMargins(16, 12, 16, 12)
        vbox.setSpacing(6)

        # ── Fixed top: section label + name/enabled row ───────────────
        lbl = QLabel("TRIGGER")
        lbl.setObjectName("section_label")
        vbox.addWidget(lbl)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self._trig_name = QLineEdit()
        self._trig_name.setPlaceholderText("Trigger name")
        self._trig_name.editingFinished.connect(self._save_trigger)
        name_row.addWidget(self._trig_name, 1)
        self._trig_enabled = QCheckBox("Enabled")
        self._trig_enabled.setChecked(True)
        self._trig_enabled.stateChanged.connect(self._save_trigger)
        name_row.addWidget(self._trig_enabled)
        vbox.addLayout(name_row)

        # ── Vertical splitter: patterns (top) ↕ script (bottom) ───────
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.setStyleSheet(
            "QSplitter::handle:vertical {"
            "  background: #3a3a3a;"
            "  height: 5px;"
            "  border-top: 1px solid #555;"
            "  border-bottom: 1px solid #555;"
            "}"
        )
        vsplit.setHandleWidth(5)
        vbox.addWidget(vsplit, 1)   # splitter takes all remaining vertical space

        # ── Top pane: patterns ────────────────────────────────────────
        pat_pane = QWidget()
        pat_pane.setStyleSheet("background: transparent;")
        pat_vbox = QVBoxLayout(pat_pane)
        pat_vbox.setContentsMargins(0, 4, 0, 2)
        pat_vbox.setSpacing(4)

        pat_lbl = QLabel("PATTERNS  (regex or plain text — %1–%9 capture groups)")
        pat_lbl.setObjectName("section_label")
        pat_lbl.setWordWrap(True)
        pat_vbox.addWidget(pat_lbl)

        self._pat_scroll = QScrollArea()
        self._pat_scroll.setWidgetResizable(True)
        self._pat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._pat_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._pat_container = QWidget()
        self._pat_container.setStyleSheet("background: transparent;")
        self._pat_layout = QVBoxLayout(self._pat_container)
        self._pat_layout.setContentsMargins(0, 0, 0, 0)
        self._pat_layout.setSpacing(3)
        self._pat_layout.addStretch()
        self._pat_scroll.setWidget(self._pat_container)
        pat_vbox.addWidget(self._pat_scroll, 1)

        pat_btns = QHBoxLayout()
        add_p = QPushButton("+ Pattern")
        add_p.clicked.connect(lambda: self._add_pattern_row(""))
        del_p = QPushButton("− Pattern")
        del_p.clicked.connect(self._remove_last_pattern)
        pat_btns.addWidget(add_p)
        pat_btns.addWidget(del_p)
        pat_btns.addStretch()
        pat_vbox.addLayout(pat_btns)

        vsplit.addWidget(pat_pane)

        # ── Bottom pane: script body ──────────────────────────────────
        body_pane = QWidget()
        body_pane.setStyleSheet("background: transparent;")
        body_vbox = QVBoxLayout(body_pane)
        body_vbox.setContentsMargins(0, 4, 0, 0)
        body_vbox.setSpacing(4)

        body_lbl = QLabel("SCRIPT  (TinTin++ — #gag / #var / #showme / #send / #if …)")
        body_lbl.setObjectName("section_label")
        body_lbl.setWordWrap(True)
        body_vbox.addWidget(body_lbl)

        self._trig_body = QTextEdit()
        self._trig_body.setPlaceholderText(
            "# Examples:\n"
            "#gag\n"
            "#var {hp} {%1}\n"
            "#showme {<125>HP: $hp}\n"
            "#if {$hp < 50} {#send flee}"
        )
        self._trig_body.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self._trig_body.setMinimumHeight(60)
        self._trig_body.textChanged.connect(self._save_trigger)
        body_vbox.addWidget(self._trig_body, 1)

        vsplit.addWidget(body_pane)

        # Default split: patterns ~35%, script ~65%
        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 2)

        return w

    # ── Tree population ──────────────────────────────────────────────

    def _populate_tree(self):
        self._tree.blockSignals(True)
        self._tree.clear()
        for fi, folder in enumerate(self._folders):
            f_item = self._make_folder_item(folder)
            self._tree.addTopLevelItem(f_item)
            for ti, trig in enumerate(folder.get("triggers", [])):
                t_item = self._make_trigger_item(trig, folder.get("enabled", True))
                f_item.addChild(t_item)
            f_item.setExpanded(True)
        self._tree.blockSignals(False)

    def _make_folder_item(self, folder: dict) -> QTreeWidgetItem:
        enabled  = folder.get("enabled", True)
        is_root  = folder.get("_root", False)
        icon     = "🌐" if is_root else _FOLDER_ICON
        name     = folder.get("name", "Folder")
        label    = f"{icon}  {name}"
        if is_root and not enabled:
            label += "  (all triggers disabled)"
        item = QTreeWidgetItem([label])
        item.setFont(0, _folder_font())
        col  = QColor("#ffcc66") if is_root else QColor("#ddd" if enabled else "#555")
        item.setForeground(0, QBrush(col if enabled else QColor("#555")))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(0, Qt.ItemDataRole.UserRole, ("folder",))
        return item

    def _make_trigger_item(self, trig: dict, folder_enabled: bool) -> QTreeWidgetItem:
        enabled = trig.get("enabled", True)
        name    = trig.get("name") or (trig.get("patterns", [""])[:1] or [""])[0] or "Trigger"
        item    = QTreeWidgetItem([f"  {_TRIGGER_ICON}  {name}"])
        item.setFont(0, _trigger_font(enabled, folder_enabled))
        item.setForeground(0, _strike_color(enabled, folder_enabled))
        item.setData(0, Qt.ItemDataRole.UserRole, ("trigger",))
        return item

    def _refresh_folder_item(self, fi: int):
        """Re-render a folder and all its children after a data change."""
        folder = self._folders[fi]
        f_item = self._tree.topLevelItem(fi)
        if not f_item:
            return
        self._tree.blockSignals(True)
        enabled = folder.get("enabled", True)
        is_root = folder.get("_root", False)
        icon    = "🌐" if is_root else _FOLDER_ICON
        label   = f"{icon}  {folder.get('name','Folder')}"
        if is_root and not enabled:
            label += "  (all triggers disabled)"
        f_item.setText(0, label)
        base_col = QColor("#ffcc66") if is_root else QColor("#ddd")
        f_item.setForeground(0, QBrush(base_col if enabled else QColor("#555")))
        for ti, trig in enumerate(folder.get("triggers", [])):
            c = f_item.child(ti)
            if c:
                name = trig.get("name") or (trig.get("patterns", [""])[:1] or [""])[0] or "Trigger"
                c.setText(0, f"  {_TRIGGER_ICON}  {name}")
                c.setFont(0, _trigger_font(trig.get("enabled", True), enabled))
                c.setForeground(0, _strike_color(trig.get("enabled", True), enabled))
        self._tree.blockSignals(False)

    def _refresh_trigger_item(self, fi: int, ti: int):
        folder = self._folders[fi]
        trig   = folder["triggers"][ti]
        f_item = self._tree.topLevelItem(fi)
        c      = f_item.child(ti) if f_item else None
        if not c:
            return
        self._tree.blockSignals(True)
        folder_en = folder.get("enabled", True)
        name = trig.get("name") or (trig.get("patterns", [""])[:1] or [""])[0] or "Trigger"
        c.setText(0, f"  {_TRIGGER_ICON}  {name}")
        c.setFont(0, _trigger_font(trig.get("enabled", True), folder_en))
        c.setForeground(0, _strike_color(trig.get("enabled", True), folder_en))
        self._tree.blockSignals(False)

    # ── Selection handling ───────────────────────────────────────────

    def _on_selection(self):
        items = self._tree.selectedItems()
        if not items:
            self._sel_folder_idx  = None
            self._sel_trigger_idx = None
            self._stack.setCurrentIndex(0)
            return
        item = items[0]
        role = item.data(0, Qt.ItemDataRole.UserRole)
        if role and role[0] == "folder":
            fi = self._tree.indexOfTopLevelItem(item)
            self._sel_folder_idx  = fi
            self._sel_trigger_idx = None
            self._load_folder_editor(fi)
        elif role and role[0] == "trigger":
            parent = item.parent()
            fi     = self._tree.indexOfTopLevelItem(parent)
            ti     = parent.indexOfChild(item)
            self._sel_folder_idx  = fi
            self._sel_trigger_idx = ti
            self._load_trigger_editor(fi, ti)

    def _on_item_changed(self, item, col):
        # Drag-drop reorder — resync _folders from tree
        self._sync_from_tree()

    # ── Editors ──────────────────────────────────────────────────────

    def _load_folder_editor(self, fi: int):
        folder = self._folders[fi]
        self._folder_name.blockSignals(True)
        self._folder_enabled.blockSignals(True)
        self._folder_name.setText(folder.get("name", ""))
        self._folder_name.setEnabled(not folder.get("_root", False))
        self._folder_enabled.setChecked(folder.get("enabled", True))
        tip = ("Toggle to quickly disable ALL triggers." if folder.get("_root")
               else "Disabling stops all triggers in this folder.")
        self._folder_enabled.setText(f"Enabled  ({tip})")
        self._folder_name.blockSignals(False)
        self._folder_enabled.blockSignals(False)
        self._stack.setCurrentIndex(1)

    def _load_trigger_editor(self, fi: int, ti: int):
        trig = self._folders[fi]["triggers"][ti]

        self._trig_name.blockSignals(True)
        self._trig_enabled.blockSignals(True)
        self._trig_body.blockSignals(True)

        self._trig_name.setText(trig.get("name", ""))
        self._trig_enabled.setChecked(trig.get("enabled", True))

        # Rebuild pattern rows
        self._clear_pattern_rows()
        for p in trig.get("patterns", []):
            self._add_pattern_row(p, emit=False)
        if not trig.get("patterns"):
            self._add_pattern_row("", emit=False)

        self._trig_body.setPlainText(trig.get("body", ""))

        self._trig_name.blockSignals(False)
        self._trig_enabled.blockSignals(False)
        self._trig_body.blockSignals(False)

        self._stack.setCurrentIndex(2)

    def _save_folder(self):
        fi = self._sel_folder_idx
        if fi is None or fi >= len(self._folders):
            return
        if not self._folders[fi].get("_root"):
            self._folders[fi]["name"] = self._folder_name.text().strip() or "Folder"
        self._folders[fi]["enabled"] = self._folder_enabled.isChecked()
        self._refresh_folder_item(fi)

    def _save_trigger(self):
        fi = self._sel_folder_idx
        ti = self._sel_trigger_idx
        if fi is None or ti is None:
            return
        if fi >= len(self._folders):
            return
        trigs = self._folders[fi].get("triggers", [])
        if ti >= len(trigs):
            return
        trigs[ti]["name"]     = self._trig_name.text().strip()
        trigs[ti]["enabled"]  = self._trig_enabled.isChecked()
        trigs[ti]["patterns"] = self._collect_patterns()
        trigs[ti]["body"]     = self._trig_body.toPlainText()
        self._refresh_trigger_item(fi, ti)

    # ── Pattern rows ─────────────────────────────────────────────────

    def _clear_pattern_rows(self):
        # Remove all widgets before the stretch
        while self._pat_layout.count() > 1:
            item = self._pat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_pattern_row(self, text: str = "", emit: bool = True):
        edit = QLineEdit(text)
        edit.setPlaceholderText("Pattern (regex or plain text)")
        edit.editingFinished.connect(self._save_trigger)
        # Insert before the stretch
        pos = self._pat_layout.count() - 1
        self._pat_layout.insertWidget(pos, edit)
        if emit:
            self._save_trigger()

    def _remove_last_pattern(self):
        count = self._pat_layout.count() - 1   # -1 for stretch
        if count <= 1:
            return    # keep at least one row
        item = self._pat_layout.takeAt(count - 1)
        if item and item.widget():
            item.widget().deleteLater()
        self._save_trigger()

    def _collect_patterns(self) -> list[str]:
        pats = []
        for i in range(self._pat_layout.count() - 1):  # -1 for stretch
            w = self._pat_layout.itemAt(i).widget()
            if isinstance(w, QLineEdit):
                t = w.text().strip()
                if t:
                    pats.append(t)
        return pats

    # ── Tree toolbar actions ─────────────────────────────────────────

    def _add_folder(self):
        folder = {"name": "New Folder", "enabled": True, "triggers": []}
        self._folders.append(folder)
        f_item = self._make_folder_item(folder)
        self._tree.addTopLevelItem(f_item)
        f_item.setExpanded(True)
        self._tree.setCurrentItem(f_item)
        self._sel_folder_idx  = len(self._folders) - 1
        self._sel_trigger_idx = None
        self._load_folder_editor(self._sel_folder_idx)
        self._folder_name.setFocus()
        self._folder_name.selectAll()

    def _add_trigger(self):
        fi = self._sel_folder_idx
        if fi is None:
            if self._folders:
                fi = 0
            else:
                self._add_folder()
                fi = 0
        trig = {"name": "", "patterns": [""], "body": "", "enabled": True}
        self._folders[fi].setdefault("triggers", []).append(trig)
        ti     = len(self._folders[fi]["triggers"]) - 1
        f_item = self._tree.topLevelItem(fi)
        folder_en = self._folders[fi].get("enabled", True)
        t_item = self._make_trigger_item(trig, folder_en)
        f_item.addChild(t_item)
        f_item.setExpanded(True)
        self._tree.setCurrentItem(t_item)
        self._sel_folder_idx  = fi
        self._sel_trigger_idx = ti
        self._load_trigger_editor(fi, ti)
        self._trig_name.setFocus()

    def _delete_selected(self):
        fi = self._sel_folder_idx
        ti = self._sel_trigger_idx
        if fi is None:
            return
        if ti is not None:
            # delete trigger
            del self._folders[fi]["triggers"][ti]
            f_item = self._tree.topLevelItem(fi)
            if f_item:
                child = f_item.child(ti)
                if child:
                    f_item.removeChild(child)
            self._sel_trigger_idx = None
            self._stack.setCurrentIndex(0)
        else:
            # block deleting the root folder
            if self._folders[fi].get("_root"):
                return
            # delete folder
            del self._folders[fi]
            item = self._tree.topLevelItem(fi)
            if item:
                self._tree.takeTopLevelItem(fi)
            self._sel_folder_idx  = None
            self._stack.setCurrentIndex(0)

    # ── Drag-drop sync ───────────────────────────────────────────────

    def _sync_from_tree(self):
        """Rebuild _folders list from current tree order after a drag-drop."""
        new_folders = []
        # Keep a flat lookup by old index signature
        for fi in range(self._tree.topLevelItemCount()):
            f_item = self._tree.topLevelItem(fi)
            # find matching folder by scanning (drag-drop may reorder)
            # We match by the text which includes the name
            name_in_tree = f_item.text(0).replace(f"{_FOLDER_ICON}  ", "").strip()
            # find original folder with this name (first match)
            orig = next(
                (f for f in self._folders if f.get("name") == name_in_tree),
                {"name": name_in_tree, "enabled": True, "triggers": []}
            )
            new_trigs = []
            for ti in range(f_item.childCount()):
                t_item   = f_item.child(ti)
                t_name   = t_item.text(0).replace(f"  {_TRIGGER_ICON}  ", "").strip()
                orig_t   = next(
                    (t for t in orig.get("triggers", []) if
                     (t.get("name") or "") == t_name or
                     (t.get("patterns", [""])[0] if t.get("patterns") else "") == t_name),
                    None
                )
                if orig_t:
                    new_trigs.append(orig_t)
            orig["triggers"] = new_trigs
            new_folders.append(orig)
        self._folders = new_folders

    # ── Public API ───────────────────────────────────────────────────

    def get_folders(self) -> list[dict]:
        """Return the current folder/trigger data."""
        # Flush any unsaved changes from the open editors
        if self._sel_folder_idx is not None and self._sel_trigger_idx is None:
            self._save_folder()
        if self._sel_folder_idx is not None and self._sel_trigger_idx is not None:
            self._save_trigger()
        return copy.deepcopy(self._folders)
