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

# ── Built-in colour themes ───────────────────────────────────────────
# Each theme is a list of 16 hex strings:
#   indices 0-7  → normal colours  (SGR 30-37 / 40-47)
#   indices 8-15 → bright colours  (SGR 90-97 / 100-107)

THEMES: dict[str, list[str]] = {
    "xterm": [
        "#000000","#aa0000","#00aa00","#aa5500",
        "#0000aa","#aa00aa","#00aaaa","#aaaaaa",
        "#555555","#ff5555","#55ff55","#ffff55",
        "#5555ff","#ff55ff","#55ffff","#ffffff",
    ],
    "VGA Classic": [
        "#000000","#800000","#008000","#808000",
        "#000080","#800080","#008080","#c0c0c0",
        "#808080","#ff0000","#00ff00","#ffff00",
        "#0000ff","#ff00ff","#00ffff","#ffffff",
    ],
    "Tango": [
        "#000000","#cc0000","#4e9a06","#c4a000",
        "#3465a4","#75507b","#06989a","#d3d7cf",
        "#555753","#ef2929","#8ae234","#fce94f",
        "#729fcf","#ad7fa8","#34e2e2","#eeeeec",
    ],
    "Solarized Dark": [
        "#073642","#dc322f","#859900","#b58900",
        "#268bd2","#d33682","#2aa198","#eee8d5",
        "#002b36","#cb4b16","#586e75","#657b83",
        "#839496","#6c71c4","#93a1a1","#fdf6e3",
    ],
    "Dracula": [
        "#21222c","#ff5555","#50fa7b","#f1fa8c",
        "#bd93f9","#ff79c6","#8be9fd","#f8f8f2",
        "#6272a4","#ff6e6e","#69ff94","#ffffa5",
        "#d6acff","#ff92df","#a4ffff","#ffffff",
    ],
    "Gruvbox Dark": [
        "#282828","#cc241d","#98971a","#d79921",
        "#458588","#b16286","#689d6a","#a89984",
        "#928374","#fb4934","#b8bb26","#fabd2f",
        "#83a598","#d3869b","#8ec07c","#ebdbb2",
    ],
    "Monokai": [
        "#272822","#f92672","#a6e22e","#f4bf75",
        "#66d9e8","#ae81ff","#a1efe4","#f8f8f2",
        "#75715e","#f92672","#a6e22e","#f4bf75",
        "#66d9e8","#ae81ff","#a1efe4","#f9f8f5",
    ],
    "Nord": [
        "#3b4252","#bf616a","#a3be8c","#ebcb8b",
        "#81a1c1","#b48ead","#88c0d0","#e5e9f0",
        "#4c566a","#bf616a","#a3be8c","#ebcb8b",
        "#81a1c1","#b48ead","#8fbcbb","#eceff4",
    ],
    "Custom": None,   # placeholder; filled at runtime
}

# Active palette — mutable list, starts as xterm
_PALETTE: list[str] = list(THEMES["VGA Classic"])

_DEFAULT_FG = "#aaaaaa"
_DEFAULT_BG = ""          # transparent


def set_palette(colors: list[str]) -> None:
    """Replace the active 16-colour palette used by all AnsiState instances."""
    global _PALETTE, _DEFAULT_FG
    assert len(colors) == 16, "palette must have exactly 16 entries"
    _PALETTE[:] = colors
    _DEFAULT_FG = colors[7]   # normal white = default fg


def get_palette() -> list[str]:
    """Return a copy of the current active palette."""
    return list(_PALETTE)


def palette_name(colors: list[str]) -> str:
    """Return the theme name matching *colors*, or 'Custom'."""
    for name, pal in THEMES.items():
        if pal is not None and list(pal) == list(colors):
            return name
    return "Custom"


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


# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# AnsiParser / AnsiSpan / TextStyle — streaming byte-level parser
# Used by OutputWidget for the direct feed_raw/ingest path.
# Reads the active 16-colour palette from _PALETTE (shared with AnsiState)
# so theme changes via set_palette() affect both rendering paths.
# ═══════════════════════════════════════════════════════════════════════════

import re as _re2
from dataclasses import dataclass as _dc

def _hex_to_rgb(h: str) -> tuple:
    c = h.lstrip('#')
    return (int(c[0:2],16), int(c[2:4],16), int(c[4:6],16))

def _get_c16() -> list:
    """Return the current 16-colour palette as (r,g,b) tuples from _PALETTE."""
    return [_hex_to_rgb(h) for h in _PALETTE]

def _build_pal256():
    p = _get_c16()
    for r in range(6):
        for g in range(6):
            for b in range(6):
                p.append((0 if r==0 else 55+r*40,
                           0 if g==0 else 55+g*40,
                           0 if b==0 else 55+b*40))
    for i in range(24):
        v = 8 + i*10; p.append((v,v,v))
    return p

# _PAL256 is rebuilt dynamically via get_pal256() so theme changes take effect
def _get_pal256() -> list:
    return _build_pal256()


_CSI_PAT = _re2.compile(rb'\x1b\[[?!>]?[0-9;]*[a-zA-Z@`]')
_OSC_PAT = _re2.compile(rb'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


@_dc
class TextStyle:
    fg: object = None           # (r,g,b) tuple or None
    bg: object = None
    _fg_base_idx: int = -1      # 0-7 if set via SGR 30-37 (for bold-bright)
    bold:          bool = False
    dim:           bool = False
    italic:        bool = False
    underline:     bool = False
    blink:         bool = False
    reverse:       bool = False
    strikethrough: bool = False

    def reset(self):
        self.fg = self.bg = None
        self._fg_base_idx = -1
        self.bold = self.dim = self.italic = self.underline = False
        self.blink = self.reverse = self.strikethrough = False

    def copy(self):
        s = TextStyle(fg=self.fg, bg=self.bg, bold=self.bold, dim=self.dim,
                      italic=self.italic, underline=self.underline,
                      blink=self.blink, reverse=self.reverse,
                      strikethrough=self.strikethrough)
        s._fg_base_idx = self._fg_base_idx
        return s


@_dc
class AnsiSpan:
    text:  str
    style: TextStyle


class AnsiParser:
    """Stateful streaming ANSI/SGR byte-stream → AnsiSpan list converter."""

    def __init__(self):
        self._style  = TextStyle()
        self._buf    = b""

    def feed(self, data: bytes) -> list:
        data = self._buf + data
        self._buf = b""
        spans = []
        pos   = 0
        n     = len(data)

        while pos < n:
            esc = data.find(b'\x1b', pos)
            if esc == -1:
                t = data[pos:].decode('utf-8', errors='replace')
                if t: spans.append(AnsiSpan(t, self._style.copy()))
                break
            if esc > pos:
                t = data[pos:esc].decode('utf-8', errors='replace')
                if t: spans.append(AnsiSpan(t, self._style.copy()))
            pos = esc
            if pos + 1 >= n:
                self._buf = data[pos:]; break
            nb = data[pos+1:pos+2]
            if nb == b'[':
                m = _CSI_PAT.match(data, pos)
                if m:
                    if data[m.end()-1:m.end()] == b'm':
                        self._sgr(data[pos+2:m.end()-1].lstrip(b'?!>'))
                    pos = m.end()
                else:
                    rem = data[pos:]
                    if len(rem) > 32: pos += 1
                    else: self._buf = rem; break
            elif nb == b']':
                m = _OSC_PAT.match(data, pos)
                if m: pos = m.end()
                else:
                    rem = data[pos:]
                    if len(rem) > 256: pos += 1
                    else: self._buf = rem; break
            else:
                pos += 2

        return [s for s in spans if s.text]

    def _sgr(self, raw: bytes):
        s = raw.decode('ascii', errors='replace').strip(';')
        if not s:
            self._style.reset(); return
        params = [int(p) if p.isdigit() else 0 for p in s.split(';')]
        i = 0
        while i < len(params):
            p = params[i]
            if   p == 0:  self._style.reset()
            elif p == 1:  self._style.bold = True
            elif p == 2:  self._style.dim = True
            elif p == 3:  self._style.italic = True
            elif p == 4:  self._style.underline = True
            elif p in (5, 6): self._style.blink = True
            elif p == 7:  self._style.reverse = True
            elif p == 9:  self._style.strikethrough = True
            elif p == 22: self._style.bold = self._style.dim = False
            elif p == 23: self._style.italic = False
            elif p == 24: self._style.underline = False
            elif p == 25: self._style.blink = False
            elif p == 27: self._style.reverse = False
            elif p == 29: self._style.strikethrough = False
            elif p == 39: self._style.fg = None; self._style._fg_base_idx = -1
            elif p == 49: self._style.bg = None
            elif 30 <= p <= 37:
                self._style.fg = _get_c16()[p - 30]
                self._style._fg_base_idx = p - 30
            elif p == 38:
                c, n = self._ext(params, i+1); i += n
                if c: self._style.fg = c; self._style._fg_base_idx = -1
            elif 40 <= p <= 47:
                self._style.bg = _get_c16()[p - 40]
            elif p == 48:
                c, n = self._ext(params, i+1); i += n
                if c: self._style.bg = c
            elif 90 <= p <= 97:
                self._style.fg = _get_c16()[p - 82]; self._style._fg_base_idx = -1
            elif 100 <= p <= 107:
                self._style.bg = _get_c16()[p - 92]
            i += 1

    @staticmethod
    def _ext(params, start):
        if start >= len(params): return None, 0
        m = params[start]
        if m == 5 and start+1 < len(params):
            return _get_pal256()[params[start+1] % 256], 2
        if m == 2 and start+3 < len(params):
            return (params[start+1]&0xFF, params[start+2]&0xFF, params[start+3]&0xFF), 4
        return None, 1
