"""
OutputWidget — scrollable ANSI-aware terminal-style text display.

Features
--------
  - Full ANSI SGR colour / style rendering via AnsiState
  - append_bytes()     : raw byte stream from telnet worker
  - append_ansi_line() : pre-processed single line (for trigger/gag flow)
  - append_ansi_text() : arbitrary ANSI string (for #showme)
  - append_local()     : styled italic client messages
  - Auto-scroll (sticks to bottom unless user scrolls up)
  - Font size zoom: Ctrl+= / Ctrl+-
"""

from __future__ import annotations

from PyQt6.QtCore    import Qt
from PyQt6.QtGui     import (
    QFont, QTextCharFormat, QTextCursor, QColor,
    QTextOption,
)
from PyQt6.QtWidgets import QTextEdit

from core.ansi_parser import AnsiState, split_ansi


_SCROLLBACK_LIMIT = 5_000


def _parse_codes(codes_str: str) -> list[int]:
    """Parse a semicolon-separated SGR parameter string to ints."""
    if not codes_str:
        return [0]
    out = []
    for tok in codes_str.replace(':', ';').split(';'):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return out or [0]


class OutputWidget(QTextEdit):
    """Read-only ANSI terminal display."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setUndoRedoEnabled(False)
        self.document().setMaximumBlockCount(_SCROLLBACK_LIMIT)
        # Never steal keyboard focus
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._font_size  = 11
        self._base_font  = self._make_font()
        self.setFont(self._base_font)

        self._ansi        = AnsiState()   # persists across calls
        self._auto_scroll = True

        self.setStyleSheet("""
            QTextEdit {
                background-color: #0d0d0d;
                color: #aaaaaa;
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

    # ── Font ─────────────────────────────────────────────────────────

    def _make_font(self) -> QFont:
        f = QFont('Monospace', self._font_size)
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
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    # ── Public text intake ───────────────────────────────────────────

    def append_ansi_line(self, text: str, newline: bool = True):
        """
        Render one ANSI-coloured line.  Called for each complete line
        from the telnet worker after gag filtering.
        The internal AnsiState is updated so colour bleeds correctly
        across chunk boundaries.
        """
        cursor = self._end_cursor()
        self._render_to(cursor, text)
        if newline:
            cursor.insertText('\n')
        self.setTextCursor(cursor)

    def append_ansi_text(self, text: str):
        """
        Render an arbitrary ANSI string (e.g. from #showme).
        Resets AnsiState before and after so showme output is isolated.
        """
        saved = AnsiState()  # snapshot not needed — just reset after
        cursor = self._end_cursor()
        self._render_to(cursor, text)
        # terminate with reset + newline
        self._ansi.reset()
        cursor.insertText('\n')
        self.setTextCursor(cursor)

    def append_local(self, text: str, color: str = '#5599ff'):
        """Inject a local client message in a distinct italic colour."""
        cursor = self._end_cursor()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        f = QFont(self._base_font)
        f.setItalic(True)
        fmt.setFont(f)
        cursor.setCharFormat(fmt)
        cursor.insertText(f'[{text}]\n')
        self.setTextCursor(cursor)

    def clear_output(self):
        self.clear()
        self._ansi.reset()

    # ── Internal rendering ───────────────────────────────────────────

    def _end_cursor(self) -> QTextCursor:
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        return c

    def _render_to(self, cursor: QTextCursor, text: str):
        """Render ANSI text into cursor, updating self._ansi state."""
        for codes_str, plain in split_ansi(text):
            if codes_str is not None:
                self._ansi.apply_codes(_parse_codes(codes_str))
            if plain:
                fmt = self._ansi.to_format(self._base_font)
                cursor.setCharFormat(fmt)
                cursor.insertText(plain)

    # ── Keyboard / wheel ─────────────────────────────────────────────

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
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.font_larger()
            else:
                self.font_smaller()
            return
        super().wheelEvent(event)
