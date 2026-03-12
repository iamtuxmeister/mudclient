"""
OutputWidget — ANSI-aware split scrollback display.

Ported directly from the reference tintin-gui implementation.

Architecture
------------
Two panes share content via an AnsiParser / AnsiSpan pipeline:

  _LivePane (bottom)
    - No vertical scrollbar; always pinned to latest output
    - Capped at _LIVE_MAX_LINES to stay fast
    - Never scrolls on wheel events — wheel opens the split instead

  _ScrollbackPane (top, hidden until split is active)
    - Full vertical scrollbar, user can browse freely
    - Shows a "tail" of history immediately when opened (synchronous)
    - Older history is prepended lazily via a zero-interval QTimer so the
      event loop stays live and the UI never freezes
    - Scrolling to the bottom closes the split

Public API (same as old single-pane widget):
    append_ansi_line(text, newline=True)   — MUD line with ANSI codes
    append_ansi_text(text)                 — #showme / arbitrary ANSI
    append_local(text, color, brackets)    — italic client status message
    feed_raw(data: bytes)                  — raw bytes (ANSI handled internally)
    clear_output()
    scroll_to_bottom()  / open_split() / close_split() / toggle_split()
    font_size  property (int)

Wheel / keyboard
    _WheelRedirectFilter installed in main_window routes all wheel events here.
    Ctrl+Return (menu action "Toggle scrollback split") calls toggle_split().
    PageUp / PageDown on the widget also work.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QSplitter, QTextEdit
from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import (
    QTextCharFormat, QTextCursor, QColor, QFont, QKeyEvent,
    QPalette, QWheelEvent,
)

from core.ansi_parser import AnsiParser, AnsiSpan, TextStyle, _get_c16


_LIVE_MAX_LINES    = 500
_SCROLLBACK_MAX    = 10_000
_BG                = QColor(10, 10, 10)
_FG                = QColor(200, 200, 200)

def _ansi_bright(idx: int) -> QColor:
    """Return the bright variant (index 8-15) of a standard colour from the
    active palette. Reads _PALETTE live so theme changes take effect."""
    return QColor(*_get_c16()[idx + 8])


def _make_fmt(style: TextStyle, font: QFont) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setFont(font)
    fg, bg = style.fg, style.bg

    # Bold-as-bright: bold + standard colour → bright variant
    if style.bold and style._fg_base_idx >= 0:
        fg = _ansi_bright(style._fg_base_idx)
    elif fg:
        fg = QColor(*fg)
    else:
        fg = _FG

    if style.reverse:
        fg, bg = (QColor(*bg) if bg else _BG), fg

    fmt.setForeground(fg if isinstance(fg, QColor) else (QColor(*fg) if fg else _FG))
    if bg:
        fmt.setBackground(QColor(*bg) if isinstance(bg, tuple) else bg)

    if style.bold:        fmt.setFontWeight(QFont.Weight.Bold)
    if style.italic:      fmt.setFontItalic(True)
    if style.underline:   fmt.setFontUnderline(True)
    if style.strikethrough: fmt.setFontStrikeOut(True)
    return fmt


def _local_spans(text: str, color: str, brackets: bool, font: QFont) -> list:
    """Produce a single AnsiSpan for a local (italic, coloured) message."""
    line = ('[' + text + ']' if brackets else text) + '\n'
    style = TextStyle()
    style.italic = True
    # Convert hex color to (r,g,b)
    c = QColor(color)
    style.fg = (c.red(), c.green(), c.blue())
    return [AnsiSpan(line, style)]


# ── Pane base ─────────────────────────────────────────────────────────────────

class _Pane(QTextEdit):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, _BG)
        pal.setColor(QPalette.ColorRole.Text, _FG)
        self.setPalette(pal)

        self._font = QFont("Monospace")
        self._font.setStyleHint(QFont.StyleHint.TypeWriter)
        self._font.setPointSize(11)
        self.setFont(self._font)

        self._cur        = QTextCursor(self.document())
        self._line_count = 0

    @property
    def base_font(self) -> QFont:
        return self._font

    def set_font_size(self, pt: int):
        self._font.setPointSize(pt)
        self.setFont(self._font)

    def append_spans(self, spans: list):
        self._cur.movePosition(QTextCursor.MoveOperation.End)
        for span in spans:
            text = span.text.replace('\r\n', '\n').replace('\r', '')
            if not text:
                continue
            self._line_count += text.count('\n')
            self._cur.insertText(text, _make_fmt(span.style, self._font))

    def prepend_spans(self, spans: list):
        """Insert spans at the very beginning of the document."""
        c = QTextCursor(self.document())
        c.movePosition(QTextCursor.MoveOperation.Start)
        for span in spans:
            text = span.text.replace('\r\n', '\n').replace('\r', '')
            if not text:
                continue
            self._line_count += text.count('\n')
            c.insertText(text, _make_fmt(span.style, self._font))

    def trim_to(self, max_lines: int):
        excess = self._line_count - max_lines
        if excess <= 0:
            return
        c = QTextCursor(self.document())
        c.movePosition(QTextCursor.MoveOperation.Start)
        c.movePosition(QTextCursor.MoveOperation.Down,
                       QTextCursor.MoveMode.KeepAnchor, excess)
        c.removeSelectedText()
        self._line_count = max_lines

    def pin_to_bottom(self):
        self._cur.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(self._cur)
        self.ensureCursorVisible()


# ── Live pane ─────────────────────────────────────────────────────────────────

class _LivePane(_Pane):
    """Always pinned to bottom, no scrollbar."""

    def __init__(self, output_widget: "OutputWidget", parent=None):
        super().__init__(parent)
        self._ow = output_widget

    def wheelEvent(self, event: QWheelEvent):
        # Route wheel to OutputWidget instead of scrolling
        self._ow._on_wheel(event.angleDelta().y())


# ── Scrollback pane ───────────────────────────────────────────────────────────

class _ScrollbackPane(_Pane):
    """Freely scrollable history pane. Scrolling to bottom closes the split."""

    def __init__(self, output_widget: "OutputWidget", parent=None):
        super().__init__(parent)
        self._ow = output_widget
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def wheelEvent(self, event: QWheelEvent):
        # Handled by _WheelRedirectFilter in main_window; fallback:
        self._ow._on_wheel(event.angleDelta().y())


# ── OutputWidget ──────────────────────────────────────────────────────────────

class OutputWidget(QWidget):
    """
    Split-scrollback output widget.

    Lazy-load strategy (identical to reference implementation):
      While split is closed, all incoming spans accumulate in _pending_spans.
      On open_split():
        1. Synchronously write the last _TAIL_LINES lines (tail) to the
           scrollback pane and pin to bottom — user sees recent content instantly.
        2. A zero-interval QTimer prepends the older history in _FLUSH_CHUNK
           batches, end-to-start, so chronological order is maintained without
           blocking the event loop.
    """

    _TAIL_LINES       = 100
    _FLUSH_CHUNK      = 300
    _PENDING_LINE_CAP = _SCROLLBACK_MAX

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parser        = AnsiParser()
        self._line_parser   = AnsiParser()   # for line-by-line append_ansi_line
        self._split_active  = False
        self._flush_timer   = None
        self._split_tail: list = []

        self._pending_spans: list = []
        self._pending_lines: int  = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.setHandleWidth(5)
        self._splitter.setStyleSheet(
            "QSplitter::handle { background: #3c3c50; }"
        )

        self._scrollback = _ScrollbackPane(self)
        self._scrollback.setVisible(False)

        self._live = _LivePane(self)

        self._splitter.addWidget(self._scrollback)
        self._splitter.addWidget(self._live)
        self._splitter.setSizes([300, 200])

        layout.addWidget(self._splitter)

    # ── Public ingest API ─────────────────────────────────────────────────

    def feed_raw(self, data: bytes):
        """Parse raw bytes and ingest resulting spans."""
        spans = self._parser.feed(data)
        self.ingest(spans)

    def append_ansi_line(self, text: str, newline: bool = True):
        """Render one ANSI-coloured MUD line (with optional trailing newline)."""
        payload = text + ('\n' if newline else '')
        spans   = self._line_parser.feed(payload.encode('utf-8', errors='replace'))
        self.ingest(spans)

    def append_ansi_text(self, text: str):
        """Render an arbitrary ANSI string (#showme). Resets parser state after."""
        spans = self._line_parser.feed((text + '\n').encode('utf-8', errors='replace'))
        # Reset so showme colors don't bleed into subsequent MUD output
        self._line_parser = AnsiParser()
        self.ingest(spans)

    def append_local(self, text: str, color: str = '#5599ff', brackets: bool = True):
        """Inject a local (italic, coloured) client message."""
        spans = _local_spans(text, color, brackets, self._live.base_font)
        self.ingest(spans)

    def ingest(self, spans: list):
        if not spans:
            return

        # Live pane: always append, cap, pin
        self._live.append_spans(spans)
        self._live.trim_to(_LIVE_MAX_LINES)
        self._live.pin_to_bottom()

        # Scrollback handling
        if self._split_active:
            self._scrollback.append_spans(spans)
            self._scrollback.trim_to(_SCROLLBACK_MAX)
        else:
            self._pending_spans.extend(spans)
            for span in spans:
                self._pending_lines += span.text.count('\n')
            if self._pending_lines > self._PENDING_LINE_CAP:
                self._trim_pending()

    def _trim_pending(self):
        excess = self._pending_lines - self._PENDING_LINE_CAP
        while self._pending_spans and excess > 0:
            dropped = self._pending_spans.pop(0)
            excess -= dropped.text.count('\n')
            self._pending_lines -= dropped.text.count('\n')
        self._pending_lines = max(0, self._pending_lines)

    # ── Tail split ────────────────────────────────────────────────────────

    def _split_off_tail(self):
        lines = 0
        for i in range(len(self._pending_spans) - 1, -1, -1):
            lines += self._pending_spans[i].text.count('\n')
            if lines >= self._TAIL_LINES:
                return self._pending_spans[:i], self._pending_spans[i:]
        return [], list(self._pending_spans)

    # ── Async prepend flush ───────────────────────────────────────────────

    def _start_prepend_flush(self):
        if self._flush_timer is not None:
            return
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(False)
        self._flush_timer.setInterval(0)
        self._flush_timer.timeout.connect(self._prepend_chunk)
        self._flush_timer.start()

    def _prepend_chunk(self):
        if not self._pending_spans:
            self._flush_timer.stop()
            self._flush_timer.deleteLater()
            self._flush_timer = None
            self._split_tail = []
            self._scrollback.trim_to(_SCROLLBACK_MAX)
            return

        sb      = self._scrollback.verticalScrollBar()
        old_max = sb.maximum()
        old_val = sb.value()

        chunk = self._pending_spans[-self._FLUSH_CHUNK:]
        del self._pending_spans[-self._FLUSH_CHUNK:]
        for span in chunk:
            self._pending_lines -= span.text.count('\n')
        self._pending_lines = max(0, self._pending_lines)

        self._scrollback.prepend_spans(chunk)

        new_max = sb.maximum()
        sb.setValue(old_val + (new_max - old_max))

    def _stop_flush(self):
        if self._flush_timer is None:
            return
        self._flush_timer.stop()
        self._flush_timer.deleteLater()
        self._flush_timer = None
        self._scrollback.clear()
        self._scrollback._line_count = 0
        if self._split_tail:
            self._pending_spans.extend(self._split_tail)
            for span in self._split_tail:
                self._pending_lines += span.text.count('\n')
            self._split_tail = []

    # ── Split control ─────────────────────────────────────────────────────

    def open_split(self):
        if self._split_active:
            return
        self._split_active = True
        self._scrollback.setVisible(True)

        if self._pending_spans:
            older, tail = self._split_off_tail()
            self._split_tail    = tail
            self._pending_spans = older
            self._pending_lines = sum(s.text.count('\n') for s in older)

            self._scrollback.append_spans(tail)
            self._scrollback.pin_to_bottom()

            if self._pending_spans:
                self._start_prepend_flush()
        else:
            self._scrollback.pin_to_bottom()

        QTimer.singleShot(0, self._live.pin_to_bottom)

    def close_split(self):
        if not self._split_active:
            return
        self._stop_flush()
        self._split_active = False
        self._scrollback.setVisible(False)
        self._live.pin_to_bottom()

    def toggle_split(self):
        if self._split_active:
            self.close_split()
        else:
            self.open_split()

    # Alias used by old View→"Scroll to Bottom" menu action
    def scroll_to_bottom(self):
        self.close_split()

    def clear_output(self):
        self._stop_flush()
        self._split_active = False
        self._scrollback.setVisible(False)
        self._split_tail = []
        for pane in (self._live, self._scrollback):
            pane.clear()
            pane._line_count = 0
        self._pending_spans.clear()
        self._pending_lines = 0
        # Reset both parsers so color state is clean
        self._parser      = AnsiParser()
        self._line_parser = AnsiParser()

    # ── Font ──────────────────────────────────────────────────────────────

    @property
    def font_size(self) -> int:
        return self._live.base_font.pointSize()

    @font_size.setter
    def font_size(self, pt: int):
        self._live.set_font_size(pt)
        self._scrollback.set_font_size(pt)

    def font_larger(self):
        if self.font_size < 24:
            self.font_size = self.font_size + 1

    def font_smaller(self):
        if self.font_size > 7:
            self.font_size = self.font_size - 1

    # ── Internal ──────────────────────────────────────────────────────────

    def _on_wheel(self, delta_y: int):
        """Called by _WheelRedirectFilter (and _LivePane fallback)."""
        if not self._split_active:
            if delta_y > 0:
                self.open_split()
        else:
            sb = self._scrollback.verticalScrollBar()
            if delta_y < 0 and sb.value() >= sb.maximum() - 5:
                self.close_split()

    def keyPressEvent(self, event: QKeyEvent):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.toggle_split()
        elif event.key() == Qt.Key.Key_PageUp:
            self.open_split()
            sb = self._scrollback.verticalScrollBar()
            sb.setValue(sb.value() - self._scrollback.height())
        elif event.key() == Qt.Key.Key_PageDown:
            if self._split_active:
                sb = self._scrollback.verticalScrollBar()
                sb.setValue(sb.value() + self._scrollback.height())
                if sb.value() >= sb.maximum() - 5:
                    self.close_split()
        else:
            super().keyPressEvent(event)
