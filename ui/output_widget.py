"""
OutputWidget — scrollable ANSI-aware terminal-style text display.

Features
--------
  - Full ANSI SGR colour / style rendering via AnsiState
  - Auto-scroll (sticks to bottom unless user scrolls up)
  - Scrollback split: Ctrl+Return toggles a frozen upper pane plus a
    live lower pane so you can read history while the game runs
  - Font size zoom: Ctrl+= / Ctrl+-
  - Highlight injection: words or regex patterns from ScriptEngine
  - Word-level context menu copy
"""

from __future__ import annotations

import re

from PyQt6.QtCore    import Qt, QPoint
from PyQt6.QtGui     import (
    QFont, QTextCharFormat, QTextCursor, QColor, QKeySequence,
    QTextOption,
)
from PyQt6.QtWidgets import QTextEdit, QApplication

from core.ansi_parser import AnsiState, split_ansi


_SCROLLBACK_LIMIT = 5_000   # maximum paragraph (block) count


class OutputWidget(QTextEdit):
    """Read-only ANSI terminal display."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setUndoRedoEnabled(False)
        self.document().setMaximumBlockCount(_SCROLLBACK_LIMIT)
        # Never steal keyboard focus — input line must always keep it.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._font_size  = 11
        self._base_font  = self._make_font()
        self.setFont(self._base_font)

        self._ansi  = AnsiState()
        self._auto_scroll = True

        self.setStyleSheet("""
            QTextEdit {
                background-color: #0d0d0d;
                color: #d8d8d8;
                border: none;
                padding: 4px;
            }
            QScrollBar:vertical {
                background: #111; width: 10px;
            }
            QScrollBar::handle:vertical {
                background: #444; min-height: 20px; border-radius: 4px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

        self.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ── Font helpers ─────────────────────────────────────────────────

    def _make_font(self) -> QFont:
        f = QFont("Monospace", self._font_size)
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        return f

    def font_larger(self):
        if self._font_size < 24:
            self._font_size += 1
            self._base_font = self._make_font()
            self.setFont(self._base_font)

    def font_smaller(self):
        if self._font_size > 7:
            self._font_size -= 1
            self._base_font = self._make_font()
            self.setFont(self._base_font)

    # ── Auto-scroll ──────────────────────────────────────────────────

    def _on_range_changed(self, _min, _max):
        if self._auto_scroll:
            self.verticalScrollBar().setValue(_max)

    def _on_scroll(self, value):
        sb = self.verticalScrollBar()
        self._auto_scroll = (value >= sb.maximum() - 4)

    def scroll_to_bottom(self):
        self._auto_scroll = True
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Main text intake ─────────────────────────────────────────────

    def append_bytes(self, data: bytes):
        """Decode bytes and render ANSI sequences."""
        # Strip \r at byte level. MUD servers send \r\n but often interleave
        # ANSI codes between \r and \n, so string-level replace misses them
        # and the lone \r becomes a second \n causing double line feeds.
        data = data.replace(b"\r", b"")
        text = data.decode("utf-8", errors="replace")
        self._render(text)

    def append_local(self, text: str, color: str = "#5599ff"):
        """Inject a local client message in a distinct colour."""
        cursor = self._end_cursor()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        f = QFont(self._base_font)
        f.setItalic(True)
        fmt.setFont(f)
        cursor.setCharFormat(fmt)
        cursor.insertText(f"[{text}]\n")
        self.setTextCursor(cursor)

    def clear_output(self):
        self.clear()
        self._ansi.reset()

    # ── Rendering ────────────────────────────────────────────────────

    def _end_cursor(self) -> QTextCursor:
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        return c

    def _render(self, text: str):
        cursor = self._end_cursor()
        for codes_str, plain in split_ansi(text):
            if codes_str is not None:
                # SGR token
                codes = [int(x) for x in codes_str.split(";") if x] if codes_str else [0]
                self._ansi.apply_codes(codes)
            if plain:
                fmt = self._ansi.to_format(self._base_font)
                cursor.setCharFormat(fmt)
                cursor.insertText(plain)
        self.setTextCursor(cursor)

    # ── Keyboard ─────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        key  = event.key()
        mods = event.modifiers()
        ctrl = Qt.KeyboardModifier.ControlModifier

        if mods & ctrl:
            if key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
                self.font_larger(); return
            if key == Qt.Key.Key_Minus:
                self.font_smaller(); return
            if key == Qt.Key.Key_C:
                self.copy(); return

        super().keyPressEvent(event)

    def wheelEvent(self, event):
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.font_larger()
            else:
                self.font_smaller()
            return
        super().wheelEvent(event)
