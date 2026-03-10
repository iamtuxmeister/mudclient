"""
ANSI / VT100 SGR parser.

Models behaviour of xterm with the standard 16-color VGA palette — the
same palette TinTin++ and virtually every MUD expect.

Supported SGR codes
  0          reset
  1          bold  (also makes 30-37 → bright variant, matching xterm)
  2          dim / faint
  3          italic
  4          underline
  5          blink (stored but not rendered in Qt)
  7          reverse video
  21/22      bold off
  23         italic off
  24         underline off
  25         blink off
  27         reverse off
  30-37      standard foreground (dark set)
  38;5;n     256-colour foreground
  38;2;r;g;b true-colour foreground
  39         default foreground
  40-47      standard background (dark set)
  48;5;n     256-colour background
  48;2;r;g;b true-colour background
  49         default background
  90-97      bright foreground (light set)
  100-107    bright background
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from PyQt6.QtGui import QColor, QFont, QTextCharFormat

# ── Standard xterm/VGA 16-colour palette ─────────────────────────────
# Indices 0-7: normal colours (SGR 30-37 foreground, 40-47 background)
# Indices 8-15: bright colours (SGR 90-97 foreground, 100-107 background)
# These values match xterm's hard-coded defaults which MUDs were designed for.
_PALETTE = [
    "#000000",  #  0 black
    "#aa0000",  #  1 red
    "#00aa00",  #  2 green
    "#aa5500",  #  3 brown / dark yellow
    "#0000aa",  #  4 blue
    "#aa00aa",  #  5 magenta
    "#00aaaa",  #  6 cyan
    "#aaaaaa",  #  7 white (light grey)
    # bright variants
    "#555555",  #  8 bright black (dark grey)
    "#ff5555",  #  9 bright red
    "#55ff55",  # 10 bright green
    "#ffff55",  # 11 bright yellow
    "#5555ff",  # 12 bright blue
    "#ff55ff",  # 13 bright magenta
    "#55ffff",  # 14 bright cyan
    "#ffffff",  # 15 bright white
]

_DEFAULT_FG = "#aaaaaa"   # same as palette[7], xterm default
_DEFAULT_BG = ""          # transparent


def _256color(n: int) -> str:
    """xterm 256-colour index → hex string.

    0-15  : standard palette (same as _PALETTE)
    16-231: 6×6×6 RGB cube  (component values: 0, 95, 135, 175, 215, 255)
    232-255: 24-step greyscale (8, 18, 28, … 238)
    """
    n = max(0, min(255, n))
    if n < 16:
        return _PALETTE[n]
    if n < 232:
        n -= 16
        b_idx = n % 6;  n //= 6
        g_idx = n % 6;  r_idx = n // 6
        def _v(i: int) -> int:
            return 0 if i == 0 else 55 + i * 40
        return f"#{_v(r_idx):02x}{_v(g_idx):02x}{_v(b_idx):02x}"
    grey = 8 + (n - 232) * 10
    return f"#{grey:02x}{grey:02x}{grey:02x}"


# Matches ESC [ <params> <letter>  (we only act on letter == 'm')
_SGR_RE = re.compile(r"\x1b\[([0-9;:]*)([A-Za-z])")


@dataclass
class AnsiState:
    """Current SGR rendering state — mirrors how xterm tracks attributes."""
    bold:       bool = False
    dim:        bool = False
    italic:     bool = False
    underline:  bool = False
    blink:      bool = False
    reverse:    bool = False

    # Foreground and background stored as resolved hex strings.
    # _fg_idx is the raw 0-15 palette index, or -1 for 256/true-colour.
    # We need it to apply the bold-as-bright rule on 30-37 (idx 0-7).
    fg:       str = _DEFAULT_FG
    bg:       str = _DEFAULT_BG
    _fg_idx:  int = 7    # default = palette[7]
    _bg_idx:  int = -1   # -1 = transparent / no bg

    def reset(self):
        self.bold = self.dim = self.italic = False
        self.underline = self.blink = self.reverse = False
        self.fg = _DEFAULT_FG
        self.bg = _DEFAULT_BG
        self._fg_idx = 7
        self._bg_idx = -1

    def apply_codes(self, codes: list[int]):
        i = 0
        while i < len(codes):
            c = codes[i]
            if   c == 0:  self.reset()
            elif c == 1:  self.bold      = True
            elif c == 2:
                # '2' is dim unless it follows 38 or 48 (handled below in
                # 256/true-colour sub-parsing)
                self.dim = True
            elif c == 3:  self.italic    = True
            elif c == 4:  self.underline = True
            elif c == 5:  self.blink     = True
            elif c == 7:  self.reverse   = True
            elif c in (21, 22):
                self.bold = False
                self.dim  = False
            elif c == 23: self.italic    = False
            elif c == 24: self.underline = False
            elif c == 25: self.blink     = False
            elif c == 27: self.reverse   = False
            elif c == 39:
                self.fg      = _DEFAULT_FG
                self._fg_idx = 7
            elif c == 49:
                self.bg      = _DEFAULT_BG
                self._bg_idx = -1
            elif 30 <= c <= 37:
                self._fg_idx = c - 30          # 0-7
                self.fg = _PALETTE[self._fg_idx]
            elif 40 <= c <= 47:
                self._bg_idx = c - 40          # 0-7
                self.bg = _PALETTE[self._bg_idx]
            elif 90 <= c <= 97:
                self._fg_idx = (c - 90) + 8   # 8-15 (already bright)
                self.fg = _PALETTE[self._fg_idx]
            elif 100 <= c <= 107:
                self._bg_idx = (c - 100) + 8  # 8-15
                self.bg = _PALETTE[self._bg_idx]
            elif c == 38:
                # 256-colour: 38;5;n   true-colour: 38;2;r;g;b
                if i + 2 < len(codes) and codes[i+1] == 5:
                    self.fg = _256color(codes[i+2])
                    self._fg_idx = -1
                    i += 2
                elif i + 4 < len(codes) and codes[i+1] == 2:
                    r, g, b = codes[i+2], codes[i+3], codes[i+4]
                    self.fg = f"#{r:02x}{g:02x}{b:02x}"
                    self._fg_idx = -1
                    i += 4
            elif c == 48:
                if i + 2 < len(codes) and codes[i+1] == 5:
                    self.bg = _256color(codes[i+2])
                    self._bg_idx = -1
                    i += 2
                elif i + 4 < len(codes) and codes[i+1] == 2:
                    r, g, b = codes[i+2], codes[i+3], codes[i+4]
                    self.bg = f"#{r:02x}{g:02x}{b:02x}"
                    self._bg_idx = -1
                    i += 4
            i += 1

    def to_format(self, base_font: QFont) -> QTextCharFormat:
        fmt  = QTextCharFormat()
        font = QFont(base_font)
        font.setItalic(self.italic)
        fmt.setFont(font)

        fg = self.fg
        bg = self.bg

        # ── Bold-as-bright rule (xterm / classic terminal behaviour) ──
        # If bold is set AND the foreground is one of the 8 normal palette
        # colours (index 0-7), substitute the bright variant (index 8-15).
        # This is exactly what xterm does: ESC[1;31m → bright red, not
        # bold dark-red.  For 256/true-colour fg (_fg_idx == -1) bold does
        # not change the colour.
        if self.bold and 0 <= self._fg_idx <= 7:
            fg = _PALETTE[self._fg_idx + 8]

        # ── Dim rule ──────────────────────────────────────────────────
        # Dim darkens the resolved fg colour slightly.
        if self.dim and not self.bold:
            c = QColor(fg)
            fg = QColor(c.red()//2, c.green()//2, c.blue()//2).name()

        # ── Reverse video ─────────────────────────────────────────────
        if self.reverse:
            fg, bg = (bg if bg else _PALETTE[0]), fg

        fmt.setForeground(QColor(fg))
        if bg:
            fmt.setBackground(QColor(bg))
        else:
            fmt.clearBackground()
        if self.underline:
            fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
        return fmt


def split_ansi(text: str) -> list[tuple[str | None, str]]:
    """
    Split *text* into [(sgr_codes_str | None, plain_text), …].

    sgr_codes_str is the raw parameter string from ESC[…m sequences.
    None means the segment is plain text with no preceding escape.
    Non-SGR escapes (cursor movement, etc.) are silently consumed.
    """
    result: list[tuple[str | None, str]] = []
    pos = 0
    for m in _SGR_RE.finditer(text):
        if m.start() > pos:
            result.append((None, text[pos:m.start()]))
        if m.group(2) == 'm':
            result.append((m.group(1), ""))
        pos = m.end()
    if pos < len(text):
        result.append((None, text[pos:]))
    return result
