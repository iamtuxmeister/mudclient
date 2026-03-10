"""
ANSI / VT100 parser.

Converts raw text containing ANSI escape sequences into a list of
(format, text) pairs where format is a QTextCharFormat ready to pass
to a QTextCursor.

Supported:
  - SGR (ESC [ … m): reset, bold, italic, underline, blink, dim
  - 8-colour FG/BG     (30-37, 40-47, 90-97, 100-107)
  - 256-colour FG/BG   (38;5;n, 48;5;n)
  - 24-bit colour FG/BG (38;2;r;g;b, 48;2;r;g;b)
  - Cursor / erase sequences: consumed silently
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from PyQt6.QtGui import QColor, QFont, QTextCharFormat

# ── Colour tables ────────────────────────────────────────────────────
_FG = {
    30: "#2e2e2e",  31: "#cc0000",  32: "#4e9a06",  33: "#c4a000",
    34: "#3465a4",  35: "#75507b",  36: "#06989a",  37: "#d3d7cf",
    90: "#555753",  91: "#ef2929",  92: "#8ae234",  93: "#fce94f",
    94: "#729fcf",  95: "#ad7fa8",  96: "#34e2e2",  97: "#eeeeec",
}
_BG = {
    40: _FG[30], 41: _FG[31], 42: _FG[32], 43: _FG[33],
    44: _FG[34], 45: _FG[35], 46: _FG[36], 47: _FG[37],
   100: _FG[90],101: _FG[91],102: _FG[92],103: _FG[93],
   104: _FG[94],105: _FG[95],106: _FG[96],107: _FG[97],
}

_DEFAULT_FG = "#d8d8d8"
_DEFAULT_BG = ""          # empty = transparent

# Matches ESC [ <params> <letter>
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")


def _256color(n: int) -> str:
    """xterm 256-colour index → hex string."""
    if n < 16:
        table = [
            "#000000","#800000","#008000","#808000",
            "#000080","#800080","#008080","#c0c0c0",
            "#808080","#ff0000","#00ff00","#ffff00",
            "#0000ff","#ff00ff","#00ffff","#ffffff",
        ]
        return table[min(n, 15)]
    if n < 232:
        n -= 16
        b = n % 6; n //= 6
        g = n % 6; r = n // 6
        def v(x): return 0 if x == 0 else 55 + x * 40
        return f"#{v(r):02x}{v(g):02x}{v(b):02x}"
    grey = 8 + (n - 232) * 10
    return f"#{grey:02x}{grey:02x}{grey:02x}"


@dataclass
class AnsiState:
    """Current SGR rendering state."""
    fg:        str  = _DEFAULT_FG
    bg:        str  = _DEFAULT_BG
    bold:      bool = False
    italic:    bool = False
    underline: bool = False
    dim:       bool = False

    def reset(self):
        self.fg = _DEFAULT_FG
        self.bg = _DEFAULT_BG
        self.bold = self.italic = self.underline = self.dim = False

    def apply_codes(self, codes: list[int]):
        i = 0
        while i < len(codes):
            c = codes[i]
            if   c == 0:  self.reset()
            elif c == 1:  self.bold      = True
            elif c == 2:  self.dim       = True
            elif c == 3:  self.italic    = True
            elif c == 4:  self.underline = True
            elif c == 22: self.bold = self.dim = False
            elif c == 23: self.italic    = False
            elif c == 24: self.underline = False
            elif c == 39: self.fg = _DEFAULT_FG
            elif c == 49: self.bg = _DEFAULT_BG
            elif c in _FG: self.fg = _FG[c]
            elif c in _BG: self.bg = _BG[c]
            elif c == 38:
                if i+1 < len(codes) and codes[i+1] == 5 and i+2 < len(codes):
                    self.fg = _256color(codes[i+2]); i += 2
                elif i+1 < len(codes) and codes[i+1] == 2 and i+4 < len(codes):
                    r,g,b = codes[i+2],codes[i+3],codes[i+4]
                    self.fg = f"#{r:02x}{g:02x}{b:02x}"; i += 4
            elif c == 48:
                if i+1 < len(codes) and codes[i+1] == 5 and i+2 < len(codes):
                    self.bg = _256color(codes[i+2]); i += 2
                elif i+1 < len(codes) and codes[i+1] == 2 and i+4 < len(codes):
                    r,g,b = codes[i+2],codes[i+3],codes[i+4]
                    self.bg = f"#{r:02x}{g:02x}{b:02x}"; i += 4
            i += 1

    def to_format(self, base_font: QFont) -> QTextCharFormat:
        fmt  = QTextCharFormat()
        font = QFont(base_font)
        font.setBold(self.bold)
        font.setItalic(self.italic)
        fmt.setFont(font)
        fg = self.fg
        if self.dim and fg == _DEFAULT_FG:
            fg = "#999999"
        fmt.setForeground(QColor(fg))
        if self.bg:
            fmt.setBackground(QColor(self.bg))
        else:
            fmt.clearBackground()
        if self.underline:
            fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
        return fmt


def split_ansi(text: str) -> list[tuple[str | None, str]]:
    """
    Split *text* into [(sgr_codes_str_or_None, plain_text), …].

    sgr_codes_str is the raw parameter string from the escape, or None
    for plain segments.  Callers typically iterate and update an AnsiState.

    Non-SGR escapes (cursor movement etc.) produce ('', '') tuples and
    should be skipped by callers.
    """
    result: list[tuple[str | None, str]] = []
    pos = 0
    for m in _SGR_RE.finditer(text):
        if m.start() > pos:
            result.append((None, text[pos:m.start()]))
        cmd = m.group(2)
        if cmd == 'm':
            result.append((m.group(1), ""))
        # else: cursor/erase — discard
        pos = m.end()
    if pos < len(text):
        result.append((None, text[pos:]))
    return result
