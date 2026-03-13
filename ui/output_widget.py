"""
OutputWidget — ANSI-aware split scrollback display.

Architecture
------------
Two panes, two separate documents:

  _LivePane (bottom)
    - No vertical scrollbar; always pinned to latest output
    - Capped at _LIVE_MAX_LINES
    - Wheel events open the split instead of scrolling

  _ScrollbackPane (top, hidden until split is active)
    - Full vertical scrollbar, user can browse freely
    - Shows a "tail" of history immediately when opened (synchronous)
    - Older history is prepended lazily via a zero-interval QTimer
    - Scrolling to the bottom closes the split

Key fixes vs the old single-document version
--------------------------------------------
FIX A  prepend_spans: iterate reversed(spans), re-seek to Start each time so
       oldest span ends up at the top and chronological order is preserved.

FIX B  _live_queue: spans that arrive *while the split is open* are buffered
       here instead of being written into the visible scrollback QTextEdit.
       Appending to a large visible document triggers repaints on every MUD
       line and makes typing feel laggy.  The queue is flushed (with updates
       blocked) when close_split() is called, or incrementally when the user
       scrolls back to the bottom (_on_sb_value_changed).

FIX C  _on_sb_value_changed: connected to the scrollback scrollbar in
       open_split(), disconnected in close_split().  Flushes _live_queue
       when near the bottom so content is current before the split closes.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QSplitter, QTextEdit
from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import (
    QTextCharFormat, QTextCursor, QColor, QFont, QKeyEvent,
    QPalette, QWheelEvent,
)

from core.ansi_parser import AnsiParser, AnsiSpan, TextStyle


_LIVE_MAX_LINES    = 500
_SCROLLBACK_MAX    = 10_000
_BG                = QColor(10, 10, 10)
_FG                = QColor(200, 200, 200)

# Bright variants of the 8 standard ANSI colours (indices 8-15)
_ANSI_BRIGHT = [
    QColor( 85,  85,  85),  # bright black  (dark grey)
    QColor(255,  85,  85),  # bright red
    QColor( 85, 255,  85),  # bright green
    QColor(255, 255,  85),  # bright yellow
    QColor( 85,  85, 255),  # bright blue
    QColor(255,  85, 255),  # bright magenta
    QColor( 85, 255, 255),  # bright cyan
    QColor(255, 255, 255),  # bright white
]


def _make_fmt(style: TextStyle, font: QFont) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setFont(font)
    fg, bg = style.fg, style.bg

    # Bold-as-bright: bold + standard colour → bright variant
    if style.bold and style._fg_base_idx >= 0:
        fg = _ANSI_BRIGHT[style._fg_base_idx]
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


# ── Pane base ─────────────────────────────────────────────────────────────────

class _Pane(QTextEdit):
    """Base read-only pane with ANSI append/prepend support."""

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
        """Insert spans at the very beginning of the document.

        FIX A: iterate in REVERSE order, seeking to Start before each insert.
        This way the oldest span (first in the list) ends up at the top of the
        document and chronological order is fully preserved.
        """
        c = QTextCursor(self.document())
        for span in reversed(spans):
            text = span.text.replace('\r\n', '\n').replace('\r', '')
            if not text:
                continue
            self._line_count += text.count('\n')
            c.movePosition(QTextCursor.MoveOperation.Start)
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
    """Always pinned to bottom, no scrollbar ever."""

    def __init__(self, output_widget: "OutputWidget", parent=None):
        super().__init__(parent)
        self._ow = output_widget

    def wheelEvent(self, event: QWheelEvent):
        self._ow._on_wheel(event.angleDelta().y())


# ── Scrollback pane ───────────────────────────────────────────────────────────

class _ScrollbackPane(_Pane):
    """Freely scrollable history pane. Visible only when split is active."""

    def __init__(self, output_widget: "OutputWidget", parent=None):
        super().__init__(parent)
        self._ow = output_widget
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def wheelEvent(self, event: QWheelEvent):
        # Handled by _WheelRedirectFilter in main_window; this is the fallback.
        self._ow._on_wheel(event.angleDelta().y())


# ── OutputWidget ──────────────────────────────────────────────────────────────

class OutputWidget(QWidget):
    """
    Split-scrollback output widget.

    Lazy-load strategy:
      While split is closed, all incoming spans accumulate in _pending_spans.
      On open_split():
        1. Synchronously write the last _TAIL_LINES lines to the scrollback
           pane and pin to bottom — user sees recent content instantly.
        2. A zero-interval QTimer prepends the older history in _FLUSH_CHUNK
           batches, end-to-start, so chronological order is maintained without
           blocking the event loop.

    While split is open (FIX B — _live_queue):
      New spans are buffered in _live_queue instead of being written to the
      visible scrollback widget.  Writing to a large visible QTextEdit on
      every MUD line causes repaints/cursor ops that make input feel laggy.
      The queue is flushed when the user scrolls to the bottom
      (_on_sb_value_changed) or when close_split() is called.
    """

    _TAIL_LINES       = 100
    _FLUSH_CHUNK      = 300
    _PENDING_LINE_CAP = _SCROLLBACK_MAX

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parser       = AnsiParser()
        self._split_active = False
        self._flush_timer  = None
        self._split_tail: list = []

        self._pending_spans: list = []
        self._pending_lines: int  = 0

        # FIX B: buffer spans that arrive while the split is open
        self._live_queue: list = []

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

    # ── Public ingest API ─────────────────────────────────────────────

    def feed_raw(self, data: bytes):
        spans = self._parser.feed(data)
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
            # FIX B: never touch the visible scrollback document while the
            # user is browsing — buffer instead and flush later.
            self._live_queue.extend(spans)
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

    # ── Tail split ────────────────────────────────────────────────────

    def _split_off_tail(self):
        lines = 0
        for i in range(len(self._pending_spans) - 1, -1, -1):
            lines += self._pending_spans[i].text.count('\n')
            if lines >= self._TAIL_LINES:
                return self._pending_spans[:i], self._pending_spans[i:]
        return [], list(self._pending_spans)

    # ── Async prepend flush ───────────────────────────────────────────

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
        self._live_queue = []
        if self._split_tail:
            self._pending_spans.extend(self._split_tail)
            for span in self._split_tail:
                self._pending_lines += span.text.count('\n')
            self._split_tail = []

    # ── Split control ─────────────────────────────────────────────────

    def open_split(self):
        if self._split_active:
            return
        self._split_active = True
        self._scrollback.setVisible(True)

        # FIX C: connect scrollbar so we can flush live_queue at bottom
        self._scrollback.verticalScrollBar().valueChanged.connect(
            self._on_sb_value_changed)

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

        # FIX C: disconnect scrollbar signal
        try:
            self._scrollback.verticalScrollBar().valueChanged.disconnect(
                self._on_sb_value_changed)
        except Exception:
            pass

        # FIX B: flush buffered live spans invisibly before hiding the pane
        if self._live_queue:
            self._scrollback.setUpdatesEnabled(False)
            self._scrollback.append_spans(self._live_queue)
            self._live_queue = []
            self._scrollback.trim_to(_SCROLLBACK_MAX)
            self._scrollback.setUpdatesEnabled(True)

        self._split_active = False
        self._scrollback.setVisible(False)
        self._live.pin_to_bottom()

    def toggle_split(self):
        if self._split_active:
            self.close_split()
        else:
            self.open_split()

    # Alias used by old View → "Scroll to Bottom" menu action
    def scroll_to_bottom(self):
        self.close_split()

    def clear(self):
        self._stop_flush()
        self._split_active = False
        self._scrollback.setVisible(False)
        self._split_tail = []
        for pane in (self._live, self._scrollback):
            pane.clear()
            pane._line_count = 0
        self._pending_spans.clear()
        self._pending_lines = 0
        self._live_queue = []
        self._parser = AnsiParser()

    # ── Font ──────────────────────────────────────────────────────────

    @property
    def font_size(self) -> int:
        return self._live.base_font.pointSize()

    @font_size.setter
    def font_size(self, pt: int):
        self._live.set_font_size(pt)
        self._scrollback.set_font_size(pt)

    # ── Internal ──────────────────────────────────────────────────────

    def _flush_live_queue(self):
        """Append buffered live spans to the scrollback document.
        Safe to call multiple times; no-op if queue is empty."""
        if not self._live_queue:
            return
        self._scrollback.append_spans(self._live_queue)
        self._live_queue = []
        self._scrollback.trim_to(_SCROLLBACK_MAX)

    def _on_sb_value_changed(self, value: int):
        """FIX C: flush live queue when scrollbar is near the bottom."""
        sb = self._scrollback.verticalScrollBar()
        if value >= sb.maximum() - 5:
            self._flush_live_queue()

    def _on_wheel(self, delta_y: int):
        """Fallback handler (when _WheelRedirectFilter is not installed)."""
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
