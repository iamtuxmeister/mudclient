"""
right_panel.py — Pane widgets used inside QDockWidgets.

The old fixed RightPanel widget is replaced by individual QDockWidgets
created in MainWindow._build_ui().  This module exports:

  _AnsiPane  — scrolling ANSI-aware text pane (Info / Log)
  PaneSet    — thin shim wired to the dock widgets so the rest of
               main_window.py can keep using self._right.*
"""

from __future__ import annotations

from PyQt6.QtCore    import Qt
from PyQt6.QtGui     import QFont, QTextOption, QTextCharFormat, QTextCursor, QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QSizePolicy

from core.ansi_parser import AnsiState, split_ansi
from ui.map_widget    import MapWidget

_PANEL_SCROLLBACK = 2_000


def _parse_codes(codes_str: str) -> list[int]:
    if not codes_str:
        return [0]
    out = []
    for tok in codes_str.replace(':', ';').split(';'):
        if tok.strip().isdigit():
            out.append(int(tok.strip()))
    return out or [0]


class _AnsiPane(QTextEdit):
    """Scrolling, ANSI-aware read-only text pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setUndoRedoEnabled(False)
        self.document().setMaximumBlockCount(_PANEL_SCROLLBACK)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        f = QFont('Monospace', 11)
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        self.setFont(f)
        self._base_font = f

        self.setStyleSheet("""
            QTextEdit {
                background: #0d0d0d; color: #aaaaaa;
                border: none; padding: 4px;
            }
            QScrollBar:vertical { background:#111; width:8px; }
            QScrollBar::handle:vertical { background:#444; min-height:16px; border-radius:3px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """)

        self._ansi = AnsiState()
        self.verticalScrollBar().rangeChanged.connect(
            lambda _mn, _mx: self.verticalScrollBar().setValue(_mx)
        )
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Ignored)

    def append_ansi(self, text: str):
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        for codes_str, plain in split_ansi(text):
            if codes_str is not None:
                self._ansi.apply_codes(_parse_codes(codes_str))
            if plain:
                fmt = self._ansi.to_format(self._base_font)
                c.setCharFormat(fmt)
                c.insertText(plain)
        self._ansi.reset()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor('#aaaaaa'))
        c.setCharFormat(fmt)
        c.insertText('\n')
        self.setTextCursor(c)

    def append_plain(self, text: str):
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor('#aaaaaa'))
        c.setCharFormat(fmt)
        c.insertText(text + '\n')
        self.setTextCursor(c)


class PaneSet:
    """
    Compatibility shim: provides the same API as the old RightPanel
    but delegates to individual dock-widget contents.

    Wired up in MainWindow._build_ui() after the docks are created.
    """

    def __init__(self, map_widget: MapWidget, info_pane: _AnsiPane,
                 log_pane: _AnsiPane, map_dock, info_dock, log_dock):
        self.map_widget = map_widget
        self._info      = info_pane
        self._log       = log_pane
        self._map_dock  = map_dock
        self._info_dock = info_dock
        self._log_dock  = log_dock

    def on_gmcp_room(self, data: dict):
        self.map_widget.on_gmcp_room(data)
        # Raise the map dock if it's tabbed
        self._map_dock.raise_()

    def update_map(self, ascii_text: str):
        self.map_widget.update_map(ascii_text)

    def write_ansi(self, target: str, ansi_text: str):
        t = target.lower()
        if 'info' in t:
            self._info.append_ansi(ansi_text)
            self._info_dock.raise_()
        elif 'log' in t:
            self._log.append_ansi(ansi_text)
            self._log_dock.raise_()

    def write_info(self, text: str):
        self._info.append_plain(text)
        self._info_dock.raise_()

    def write_log(self, text: str):
        self._log.append_plain(text)
