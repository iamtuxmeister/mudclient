"""RightPanel — collapsible panel with a map pane and ANSI-aware text panes."""

from __future__ import annotations

from PyQt6.QtCore    import Qt
from PyQt6.QtGui     import QFont, QTextOption, QTextCharFormat, QTextCursor, QColor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTabWidget, QTextEdit, QSizePolicy

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
    """A read-only, ANSI-rendering text pane for the right panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setUndoRedoEnabled(False)
        self.document().setMaximumBlockCount(_PANEL_SCROLLBACK)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        f = QFont('Monospace', 9)
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

    def append_ansi(self, text: str):
        """Render an ANSI-coloured string and append a newline."""
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        for codes_str, plain in split_ansi(text):
            if codes_str is not None:
                self._ansi.apply_codes(_parse_codes(codes_str))
            if plain:
                fmt = self._ansi.to_format(self._base_font)
                c.setCharFormat(fmt)
                c.insertText(plain)
        # reset + newline
        self._ansi.reset()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor('#aaaaaa'))
        c.setCharFormat(fmt)
        c.insertText('\n')
        self.setTextCursor(c)

    def append_plain(self, text: str):
        """Append plain text with a newline."""
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor('#aaaaaa'))
        c.setCharFormat(fmt)
        c.insertText(text + '\n')
        self.setTextCursor(c)


class RightPanel(QWidget):
    """
    Vertical side panel:
      Tab 0 — ASCII map  (MapWidget)
      Tab 1 — Info pane  (ANSI-aware, for trigger #showme {text} {info})
      Tab 2 — Log pane   (ANSI-aware, for GMCP events and #showme {text} {log})
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(160)
        self.setMaximumWidth(400)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                background: #1a1a1a; color: #888;
                padding: 4px 8px; font-size: 9pt;
            }
            QTabBar::tab:selected { background: #222; color: #ccc; }
        """)
        layout.addWidget(self._tabs)

        self.map_widget = MapWidget()
        self._tabs.addTab(self.map_widget, 'Map')

        self._info = _AnsiPane()
        self._tabs.addTab(self._info, 'Info')

        self._log = _AnsiPane()
        self._tabs.addTab(self._log, 'Log')

    # ── Public write API ─────────────────────────────────────────────

    def update_map(self, ascii_text: str):
        self.map_widget.update_map(ascii_text)

    def on_gmcp_room(self, data: dict):
        """Called on every Room.Info GMCP packet."""
        self.map_widget.on_gmcp_room(data)
        self._tabs.setCurrentIndex(0)

    def write_ansi(self, target: str, ansi_text: str):
        """
        Route #showme output to the correct pane.
        target: '' or 'main' → caller handles (main window)
                'info'       → Info tab
                'log'        → Log tab
        """
        t = target.lower()
        if 'info' in t:
            self._info.append_ansi(ansi_text)
            self._tabs.setCurrentIndex(1)
        elif 'log' in t:
            self._log.append_ansi(ansi_text)
            self._tabs.setCurrentIndex(2)
        # 'map' and '' are handled by caller

    def write_info(self, text: str):
        """Write plain text to the Info pane (GMCP / status messages)."""
        self._info.append_plain(text)
        self._tabs.setCurrentIndex(1)

    def write_log(self, text: str):
        """Write plain text to the Log pane."""
        self._log.append_plain(text)
