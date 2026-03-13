"""
MainWindow — top-level application window.

Layout
------
┌───────────────────────────────────────────────┐
│  Menu bar                                     │
├────────────────────────┬──────────────────────┤
│                        │                      │
│  OutputWidget          │  RightPanel          │
│  (scrollback)          │  (map / info / log)  │
│                        │                      │
├────────────────────────┴──────────────────────┤
│  Input line  [Send]                           │
├───────────────────────────────────────────────┤
│  ButtonBar (macro buttons)                    │
├───────────────────────────────────────────────┤
│  Status bar                                   │
└───────────────────────────────────────────────┘

Key features
------------
- TelnetWorker in a QThread — full IAC + MCCP2 + GMCP
- ScriptEngine — aliases, triggers, timers (main thread)
- Tab completion from MUD output words
- Prefix-search command history (↑/↓)
- Session profiles (sessions.json)
- Config dialog for aliases / actions / timers / buttons
- MCCP2 status indicator in status bar
"""

from __future__ import annotations

import collections
import re
import sys
import os

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QLineEdit, QPushButton, QStatusBar,
    QLabel, QApplication, QMessageBox, QInputDialog,
    QDockWidget, QSizePolicy,
)
from PyQt6.QtCore  import Qt, QThread, QObject, QEvent, pyqtSignal, QByteArray, QTimer
from PyQt6.QtGui   import QFont, QFontMetrics, QKeyEvent, QAction, QColor, QPalette

from core.telnet_worker  import TelnetWorker
from core.script_engine  import ScriptEngine
from core.map_data import MapData, try_parse_gmcp_line
from core.room_detector import TorilRoomDetector
from core.client_commands import ClientCommandDispatcher
from core.ansi_parser    import set_palette, THEMES, get_palette
from core.debug          import dbg

from ui.output_widget   import OutputWidget
from ui.map_widget      import MapWidget
from ui.right_panel     import PaneSet, _AnsiPane
from ui.button_bar      import ButtonBar
from ui.session_manager import SessionManager, Session, _load_sessions, _save_sessions
from ui.config_dialog   import ConfigDialog, _migrate_legacy
from ui.window_settings import save_geometry, restore_geometry


# ── Special-exit matching helper ────────────────────────────────────────

_VERB_RE = re.compile(
    r'^(?:enter|en|go|jump|climb|crawl|swim|ride|board)\s*',
    re.IGNORECASE,
)

def _strip_verb(s: str) -> str:
    return _VERB_RE.sub('', s).strip()

def _special_exit_matches(exit_name: str, sent: str) -> bool:
    """Match a queued/typed command against a map special-exit name.

    Exit names may use | for aliases: 'enter portal|liquid|gate'
    sent is the normalised lower-case command, e.g. 'enter portal'

    Strategy: strip leading movement-verb from both sides and compare
    the target noun. Falls back to exact full/token match.
    """
    if exit_name == sent:
        return True
    tokens = [t.strip() for t in re.split(r'[|,]', exit_name)]
    if sent in tokens:
        return True
    # Compare verb-stripped targets
    sent_noun = _strip_verb(sent)
    if sent_noun:
        for tok in tokens:
            if sent_noun == _strip_verb(tok) or sent_noun == tok:
                return True
    return False


# ── Tab completer ─────────────────────────────────────────────────────

class _TabCompleter:
    WINDOW   = 500
    # 4+ letter words (allow hyphens inside)
    _WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{3,}")

    def __init__(self):
        # key = lowercase, value = (original_word, line_number)
        # We keep the original casing for display but match on lowercase.
        self._words: dict[str, tuple[str, int]] = collections.OrderedDict()
        self._line  = 0

    def feed(self, text: str):
        self._line += text.count("\n")
        for w in self._WORD_RE.findall(text):
            lw = w.lower()
            # Always refresh recency; prefer the most-recently-seen casing
            self._words.pop(lw, None)
            self._words[lw] = (w, self._line)
        cutoff = self._line - self.WINDOW
        stale = [k for k, (_, ln) in self._words.items() if ln < cutoff]
        for k in stale:
            del self._words[k]

    def complete(self, prefix: str) -> list[str]:
        """
        Return completions (most-recent first) whose lowercase form starts
        with the lowercase prefix.  If the prefix is all-lowercase the
        original-cased word is returned so the user can tab-cycle through
        capitalised variants; if the prefix has capitals we preserve them.
        """
        p = prefix.lower()
        results = []
        for lw, (orig, _) in reversed(list(self._words.items())):
            if lw.startswith(p):
                results.append(orig)
        return results


# ── Command input ─────────────────────────────────────────────────────

class _InputLine(QLineEdit):
    """Command input with history (prefix-search) and Tab completion."""

    def __init__(self, completer: _TabCompleter, parent=None):
        super().__init__(parent)
        self._history:      list[str]  = []
        self._hist_idx:     int        = -1
        self._hist_prefix:  str        = ""
        self._hist_matches: list[int]  = []   # indices into _history
        self._hist_pos:     int        = -1   # position within _hist_matches
        self._completer   = completer
        self._tab_matches: list[str]  = []
        self._tab_idx:     int        = -1
        self._tab_anchor:  int        = 0

        self.setStyleSheet("""
            QLineEdit {
                background: #111;
                color: #e8e8e8;
                border: none;
                border-top: 1px solid #333;
                padding: 6px 8px;
                font-family: Monospace;
                font-size:13pt;
            }
            QLineEdit:focus { border-top: 1px solid #555; }
        """)
        self.setPlaceholderText("Enter command…")

    def add_history(self, text: str):
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._hist_idx    = -1
        self._hist_prefix = ""
        self._clear_tab()
        # Leave text in field but select all so the user can type over it
        # or just press Enter again to resend.
        self.selectAll()

    def event(self, event):
        # Tab must be caught here, before Qt routes it to the focus chain.
        if event.type() == event.Type.KeyPress and event.key() == Qt.Key.Key_Tab:
            self._tab_complete()
            return True   # consumed
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()

        if key == Qt.Key.Key_Up:
            self._history_step(-1); return
        if key == Qt.Key.Key_Down:
            self._history_step(+1); return

        # any typing resets history position and tab state
        self._hist_idx = -1
        self._hist_prefix = ""
        self._clear_tab()
        super().keyPressEvent(event)

    # history
    #
    # _hist_prefix  — the text that was in the box when Up was first pressed;
    #                 all searches are filtered to entries starting with this.
    # _hist_idx     — index into self._history of the currently shown entry,
    #                 or -1 when showing the live (user-typed) text.
    # _hist_matches — indices of history entries that match _hist_prefix,
    #                 in chronological order (oldest first).
    # _hist_pos     — position within _hist_matches (-1 = live text end).

    def _history_step(self, direction: int):
        # direction: -1 = Up (go back in time), +1 = Down (go forward)
        if not self._history:
            return

        # First keypress: snapshot prefix from the text *before* the cursor,
        # ignoring any trailing selection so that pressing Up right after
        # sending a command (which selectAll()s the field) starts an
        # unfiltered search rather than filtering to that exact command.
        if self._hist_idx == -1:
            if self.hasSelectedText():
                # Selection spans the whole field → treat prefix as empty
                # so Up simply walks backwards through all history.
                self._hist_prefix = ""
            else:
                # No selection → use text left of cursor as the prefix filter.
                self._hist_prefix = self.text()[:self.cursorPosition()]
            self._hist_matches = [
                i for i, cmd in enumerate(self._history)
                if cmd.startswith(self._hist_prefix)
            ]
            self._hist_pos = len(self._hist_matches)  # one past end = live

        if not self._hist_matches:
            return

        new_pos = self._hist_pos + direction
        if new_pos < 0:
            new_pos = 0          # clamp at oldest match
        elif new_pos >= len(self._hist_matches):
            # Scrolled past newest → restore live text
            self._hist_idx = -1
            self._hist_pos = len(self._hist_matches)
            self.setText(self._hist_prefix)
            self.setCursorPosition(len(self._hist_prefix))
            return

        self._hist_pos = new_pos
        self._hist_idx = self._hist_matches[new_pos]
        full = self._history[self._hist_idx]
        self.setText(full)
        # Place cursor at end; select only the suffix beyond the typed prefix
        # so the user can see exactly what was matched without the whole
        # line being highlighted.
        pre_len = len(self._hist_prefix)
        if pre_len < len(full):
            self.setSelection(len(full), -(len(full) - pre_len))
        else:
            self.setCursorPosition(len(full))

    # tab completion

    def _tab_complete(self):
        text   = self.text()
        cursor = self.cursorPosition()
        before = text[:cursor]

        # find word anchor
        m = re.search(r"(\S+)$", before)
        if not m:
            return
        word   = m.group(1)
        anchor = m.start()

        if self._tab_idx == -1 or self._tab_anchor != anchor:
            self._tab_matches = self._completer.complete(word)
            self._tab_idx     = -1
            self._tab_anchor  = anchor

        if not self._tab_matches:
            return

        self._tab_idx = (self._tab_idx + 1) % len(self._tab_matches)
        replacement   = self._tab_matches[self._tab_idx]
        new_text      = text[:anchor] + replacement + text[cursor:]
        self.setText(new_text)
        self.setCursorPosition(anchor + len(replacement))

    def _clear_tab(self):
        self._tab_matches = []
        self._tab_idx     = -1


# ── Main window ───────────────────────────────────────────────────────



class _WheelRedirectFilter(QObject):
    """
    Application-level event filter that:
    - Routes all mouse wheel events to the OutputWidget
    - Refocuses the input line when the user clicks anywhere in the
      main window (except on widgets that legitimately need focus like
      the input itself, config dialog, item editor, etc.)
    """

    def __init__(self, output_widget, input_widget, main_window, canvas=None, parent=None):
        super().__init__(parent)
        self._output = output_widget
        self._input  = input_widget
        self._main   = main_window
        self._canvas = canvas   # _MapCanvas — if wheel is over this widget, let it zoom

    def eventFilter(self, obj, event):
        # ── Click → refocus input ─────────────────────────────────────
        if event.type() == QEvent.Type.MouseButtonPress:
            self._maybe_refocus(obj)
            return False   # don't consume — let the click reach its target

        if event.type() != QEvent.Type.Wheel:
            return False

        # ── Wheel → route to scrollback ──────────────────────────────
        out        = self._output
        scrollback = out._scrollback

        if obj is scrollback or obj is scrollback.viewport():
            return False

        # Let the map canvas handle its own wheel (zoom).
        # Check by global screen position rather than parent-walking — Qt may
        # deliver the wheel event to an ancestor (dock container, etc.) rather
        # than the canvas widget itself, making identity/isinstance checks miss.
        if self._canvas is not None and self._canvas.isVisible():
            from PyQt6.QtCore import QRect, QPoint
            canvas_global = self._canvas.mapToGlobal(QPoint(0, 0))
            canvas_rect   = QRect(canvas_global, self._canvas.size())
            cursor_pos    = event.globalPosition().toPoint()
            if canvas_rect.contains(cursor_pos):
                return False   # pass through — canvas wheelEvent will zoom

        sb = scrollback.verticalScrollBar()

        if out._split_active:
            px = event.pixelDelta().y()
            if px != 0:
                sb.setValue(sb.value() - px)
            else:
                angle = event.angleDelta().y()
                steps = angle / 120.0 * sb.singleStep() * 3
                sb.setValue(sb.value() - int(steps))
            if sb.value() >= sb.maximum() - 5:
                if (event.pixelDelta().y() < 0 or
                        (event.pixelDelta().y() == 0 and event.angleDelta().y() < 0)):
                    out.close_split()
            return True
        else:
            opens = (event.pixelDelta().y() > 0 or
                     (event.pixelDelta().y() == 0 and event.angleDelta().y() > 0))
            if opens:
                out.open_split()
            return True

    def _maybe_refocus(self, obj):
        """Refocus the input unless the click is on a widget that needs its own focus."""
        from PyQt6.QtWidgets import (QLineEdit, QTextEdit, QPlainTextEdit,
                                     QAbstractItemView, QComboBox, QSpinBox,
                                     QAbstractButton, QDialog, QMenu)
        # Don't steal focus from: the input itself, any dialog, any editable
        # widget inside a dialog, menus, or the scrollback pane
        if obj is self._input:
            return
        # Walk up the widget hierarchy — if any ancestor is a QDialog, skip
        w = obj
        while w is not None:
            if isinstance(w, QDialog):
                return
            w = w.parent() if hasattr(w, 'parent') else None

        # Don't steal from focusable input-type widgets in the main window
        if isinstance(obj, (QLineEdit, QTextEdit, QPlainTextEdit,
                             QAbstractItemView, QComboBox, QSpinBox)):
            return

        # Refocus
        self._input.setFocus()


class MainWindow(QMainWindow):

    # Internal signal to send text to the worker thread
    _send_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MUD Client")
        self.resize(1100, 700)

        self._thread:    QThread | None      = None
        self._worker:    TelnetWorker | None = None
        self._connected: bool                = False
        self._mccp_on:   bool                = False

        self._last_host: str = ""
        self._last_port: int = 4000
        self._session:   Session | None = None
        self._config_dlg: object = None     # persistent config dialog
        self._cmd_sep:    str    = ";"    # configurable command separator
        self._cmd_echo:       bool = True   # echo sent commands to output
        self._cmd_echo_color: str  = "#e8d44d"  # light yellow
        self._cmd_char:       str  = "#"   # client command prefix
        self._line_buf:  str = ""    # partial line buffer for line-by-line rendering

        self._completer    = _TabCompleter()
        self._map          = MapData()
        # Movement queue: list of ("dir","north") or ("special","portal") tuples
        # Each Exits: confirmation pops the front entry.
        self._move_queue:  list = []
        self._walk_path:   list = []   # remaining dirs for #map walk speedwalk
        self._room_det     = TorilRoomDetector(self._on_text_room)
        self._commands     = ClientCommandDispatcher(self)
        self._engine       = ScriptEngine(self)

        self._engine.send_command.connect(self._send_raw_command)
        self._engine.triggered_send.connect(self._send_triggered_command)
        self._engine.local_echo.connect(self._echo_local)
        self._engine.showme.connect(self._on_showme)
        self._engine.gui_message.connect(self._dispatch_gui_msg)

        self._build_palette()
        self._build_ui()
        self._build_menu()
        self._set_status("Disconnected")

        # Show session picker as soon as the event loop starts
        from PyQt6.QtCore import QTimer
        restore_geometry("main_window", self)

        # Wheel filter — route all wheel events through OutputWidget
        self._wheel_filter = _WheelRedirectFilter(
            self._output, self._input, self,
            canvas=self._right.map_widget._canvas)
        QApplication.instance().installEventFilter(self._wheel_filter)

        # Restore dock layout after Qt has finished its initial layout pass.
        # 150 ms is enough for the event loop to process resize/paint events
        # that commit the default sizes — then we override with saved state.
        def _after_show():
            self._restore_dock_layout()
            self._show_sessions()
        QTimer.singleShot(150, _after_show)

    # ── Palette ──────────────────────────────────────────────────────

    def _build_palette(self):
        p = QPalette()
        def c(role, hex_):
            p.setColor(role, QColor(hex_))
        R = QPalette.ColorRole
        c(R.Window,          "#1a1a1a"); c(R.WindowText,     "#d8d8d8")
        c(R.Base,            "#111111"); c(R.AlternateBase,  "#1e1e1e")
        c(R.Text,            "#e0e0e0"); c(R.Button,         "#2a2a2a")
        c(R.ButtonText,      "#d0d0d0"); c(R.Highlight,      "#2a5a8a")
        c(R.HighlightedText, "#ffffff"); c(R.Link,           "#5599ff")
        QApplication.instance().setPalette(p)

    # ── UI construction ──────────────────────────────────────────────

    # ── Dock stylesheet (shared) ──────────────────────────────────────
    _DOCK_SS = """
        QDockWidget {
            color: #bbb;
            font-size:11pt;
            titlebar-close-icon: none;
        }
        QDockWidget::title {
            background: #111;
            padding: 3px 6px;
            border-bottom: 1px solid #333;
            text-align: left;
        }
        QDockWidget > QWidget {
            border: none;
        }
    """

    def _make_dock(self, title: str, widget: QWidget,
                   area=Qt.DockWidgetArea.RightDockWidgetArea) -> QDockWidget:
        """Create a styled, closable-but-restorable dock."""
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title.lower().replace(' ', '_')}")
        dock.setWidget(widget)
        dock.setStyleSheet(self._DOCK_SS)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable |
            QDockWidget.DockWidgetFeature.DockWidgetFloatable |
            QDockWidget.DockWidgetFeature.DockWidgetClosable,
        )
        self.addDockWidget(area, dock)
        return dock

    def _build_ui(self):
        # ── Allow nested/split docks ──────────────────────────────
        self.setDockNestingEnabled(True)

        # ── Central widget: output + input ────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self._output = OutputWidget()

        # Minimum width = 100 monospace chars
        _f = QFont("Monospace", 12)
        _f.setStyleHint(QFont.StyleHint.TypeWriter)
        _cw = QFontMetrics(_f).averageCharWidth()
        self._output.setMinimumWidth(_cw * 100)
        self._output.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        vbox.addWidget(self._output, 1)

        # Input row
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(0)

        self._input = _InputLine(self._completer)
        self._input.returnPressed.connect(self._on_return)
        input_row.addWidget(self._input, 1)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(60)
        send_btn.setStyleSheet("""
            QPushButton {
                background: #1e4a6e; color: #cde;
                border: none; border-left: 1px solid #333;
                font-family: Monospace; font-size:12pt;
            }
            QPushButton:hover   { background: #255a84; }
            QPushButton:pressed { background: #1a3a58; }
        """)
        send_btn.clicked.connect(self._on_return)
        input_row.addWidget(send_btn)
        vbox.addLayout(input_row)

        # Button bar
        self._button_bar = ButtonBar()
        self._button_bar.command_triggered.connect(self._send_command)
        vbox.addWidget(self._button_bar)

        # ── Dock widgets ──────────────────────────────────────────
        from ui.map_widget import MapWidget
        _map_widget = MapWidget()

        _info_pane = _AnsiPane()
        _log_pane  = _AnsiPane()

        _map_dock  = self._make_dock("Map",  _map_widget)
        _info_dock = self._make_dock("Info", _info_pane)
        _log_dock  = self._make_dock("Log",  _log_pane)

        # Default layout: Map on top-right, Info+Log tabbed below it
        self.splitDockWidget(_map_dock, _info_dock,
                             Qt.Orientation.Vertical)
        self.tabifyDockWidget(_info_dock, _log_dock)
        _map_dock.raise_()

        # Expose PaneSet shim so self._right.* keeps working everywhere
        self._right = PaneSet(
            map_widget=_map_widget,
            info_pane=_info_pane,
            log_pane=_log_pane,
            map_dock=_map_dock,
            info_dock=_info_dock,
            log_dock=_log_dock,
        )
        self._dock_map  = _map_dock
        self._dock_info = _info_dock
        self._dock_log  = _log_dock

        # Debounced save — any dock move/resize triggers a save 500ms later
        self._dock_save_timer = QTimer(self)
        self._dock_save_timer.setSingleShot(True)
        self._dock_save_timer.setInterval(500)
        self._dock_save_timer.timeout.connect(self._save_dock_layout)
        def _schedule_save(_ignored=None):
            # Guard: don't restart the timer if the window is closing
            if not self.isVisible():
                return
            self._dock_save_timer.start()

        for dock in (_map_dock, _info_dock, _log_dock):
            dock.dockLocationChanged.connect(_schedule_save)
            dock.topLevelChanged.connect(_schedule_save)
            dock.visibilityChanged.connect(_schedule_save)

        # Status bar
        self._status_bar = QStatusBar()
        self._status_bar.setStyleSheet("""
            QStatusBar {
                background: #111; color: #666;
                font-family: Monospace; font-size:11pt;
                border-top: 1px solid #1e1e1e;
            }
        """)
        self.setStatusBar(self._status_bar)
        self._mccp_label = QLabel("MCCP: off")
        self._mccp_label.setStyleSheet("color: #444; padding-right: 8px;")
        self._status_bar.addPermanentWidget(self._mccp_label)

    def _build_menu(self):
        bar = self.menuBar()
        bar.setStyleSheet("""
            QMenuBar { background: #111; color: #aaa; }
            QMenuBar::item:selected { background: #2a2a2a; color: #eee; }
            QMenu { background: #1a1a1a; color: #ccc; border: 1px solid #444; }
            QMenu::item:selected { background: #2a5a8a; color: #fff; }
            QMenu::separator { height: 1px; background: #333; margin: 2px 0; }
        """)

        # File
        fm = bar.addMenu("&File")
        self._add_action(fm, "&Sessions…",   "Ctrl+Shift+N", self._show_sessions)
        self._add_action(fm, "&Quick Connect…", "Ctrl+O",    self._quick_connect)
        fm.addSeparator()
        self._add_action(fm, "Load &Map File…", "Ctrl+M",   self._load_map_file)
        fm.addSeparator()
        self._act_reconnect  = self._add_action(fm, "&Reconnect",  "Ctrl+R",  self._reconnect,  enabled=False)
        self._act_disconnect = self._add_action(fm, "&Disconnect", "Ctrl+D",  self._disconnect, enabled=False)
        fm.addSeparator()
        self._add_action(fm, "&Quit", "Ctrl+Q", self.close)

        # View — toggle docks, reset layout
        vm = bar.addMenu("&View")
        vm.addAction(self._dock_map.toggleViewAction())
        vm.addAction(self._dock_info.toggleViewAction())
        vm.addAction(self._dock_log.toggleViewAction())
        vm.addSeparator()
        self._add_action(vm, "Reset Layout", None, self._reset_dock_layout)

        # View
        vm = bar.addMenu("&View")
        self._add_action(vm, "&Clear",         "Ctrl+L",       self._output.clear_output)
        self._add_action(vm, "Font &Larger",   "Ctrl+=",       self._output.font_larger)
        self._add_action(vm, "Font S&maller",  "Ctrl+-",       self._output.font_smaller)
        self._add_action(vm, "Scroll to &Bottom", "Ctrl+End",  self._output.scroll_to_bottom)
        self._add_action(vm, "Toggle &Scrollback", "Ctrl+Return", self._output.toggle_split)

        # Tools
        tm = bar.addMenu("&Tools")
        self._add_action(tm, "&Config…", "Ctrl+,", self._show_config)

        # Help
        hm = bar.addMenu("&Help")
        self._add_action(hm, "&About", callback=self._show_about)

    def _add_action(self, menu, label, shortcut=None, callback=None, enabled=True) -> QAction:
        a = QAction(label, self)
        if shortcut:
            a.setShortcut(shortcut)
        if callback:
            a.triggered.connect(callback)
        a.setEnabled(enabled)
        menu.addAction(a)
        return a

    # ── Connection management ────────────────────────────────────────

    def _show_sessions(self):
        dlg = SessionManager(self)
        if dlg.exec() == SessionManager.DialogCode.Accepted and dlg.selected:
            s = dlg.selected
            self._session = s
            self._do_connect(s.host, s.port, s.config)

    def _quick_connect(self):
        host, ok = QInputDialog.getText(self, "Quick Connect", "Host:")
        if not ok or not host.strip():
            return
        port_str, ok = QInputDialog.getText(self, "Quick Connect", "Port:", text=str(self._last_port))
        if not ok:
            return
        try:
            port = int(port_str)
        except ValueError:
            port = 4000
        self._do_connect(host.strip(), port, {})

    def _do_connect(self, host: str, port: int, config: dict):
        if not host:
            dbg("gui", "_do_connect: empty host, aborting")
            return
        dbg("gui", f"_do_connect({host!r}, {port})")
        if self._connected:
            dbg("gui", "already connected — disconnecting first")
            self._disconnect()

        self._last_host = host
        self._last_port = port
        self._map.clear()

        # load scripting config
        self._engine.clear()
        self._cmd_sep        = config.get("cmd_separator", ";") or ";"
        self._cmd_echo       = config.get("cmd_echo", True)
        self._cmd_echo_color = config.get("cmd_echo_color", "#e8d44d") or "#e8d44d"
        self._cmd_char       = config.get("cmd_char", "#") or "#"
        self._commands.set_command_char(self._cmd_char)
        self._apply_palette(config)
        # Migrate old-format config to unified folders if needed
        if config and "folders" not in config:
            config = dict(config)
            migrated = _migrate_legacy(config)
            if migrated:
                config["folders"] = migrated
        self._engine.load_config(config)
        self._button_bar.load_buttons(self._engine.get_buttons())

        # Defer map auto-load to after the event loop has committed the dock
        # state.  A singleShot(0) runs once the current call stack unwinds.
        map_path = config.get("map_file")
        last_room = config.get("map_last_room")
        if map_path and os.path.exists(map_path):
            def _deferred_map_load(mp=map_path, lr=last_room):
                ok, msg = self._right.map_widget.load_map_file(mp)
                if ok:
                    self._output.append_local(
                        f"Map: auto-loaded {os.path.basename(mp)}", "#44cc88")
                    if lr and lr in self._right.map_widget._data.rooms:
                        self._right.on_gmcp_room({"num": lr})
                        self._output.append_local(
                            f"Map: restored position to room #{lr}", "#44aacc")
                # Re-apply dock state after map load — _rebuild_area_combo
                # triggers a Qt layout pass that clobbers the restored widths.
                QTimer.singleShot(150, self._restore_dock_layout)
            QTimer.singleShot(0, _deferred_map_load)

        self._output.append_local(f"Connecting to {host}:{port}…", "#c4a000")

        dbg("gui", "creating TelnetWorker and QThread")
        worker = TelnetWorker()
        thread = QThread(self)
        thread.setObjectName(f"TelnetThread-{host}")

        # set_target before moveToThread so host/port are stored safely
        worker.set_target(host, port)
        worker.moveToThread(thread)
        dbg("gui", "worker moved to thread; connecting signals")
        worker.connected.connect(self._on_connected)
        worker.disconnected.connect(self._on_disconnected)
        worker.error.connect(self._on_error)
        worker.data_received.connect(self._on_data)
        worker.gmcp_received.connect(self._on_gmcp)
        worker.mccp_active.connect(self._on_mccp_active)

        # DirectConnection: executes worker.send() on the calling
        # (GUI) thread rather than queuing on the worker thread.
        # Necessary because the worker is blocked in recv() and
        # its event queue is never drained while connected.
        self._send_signal.connect(
            worker.send, Qt.ConnectionType.DirectConnection
        )
        # Connect to worker.start (a real bound slot), NOT a lambda.
        # A lambda bypasses Qt's thread-dispatch and runs on the GUI thread,
        # blocking the event loop with the recv() loop.
        thread.started.connect(worker.start)
        dbg("gui", "calling thread.start()")
        thread.start()
        dbg("gui", "thread.start() returned — worker now running in background")

        self._thread = thread
        self._worker = worker

        # Give the input focus now so typing works immediately,
        # even before _on_connected fires from the worker thread.
        self._input.setFocus()

    def _reconnect(self):
        if self._last_host:
            cfg = self._session.config if self._session else {}
            self._do_connect(self._last_host, self._last_port, cfg)

    def _disconnect(self):
        dbg("gui", "_disconnect() called")
        # Step 1: sever ALL Qt signal connections from the worker FIRST.
        # This prevents any already-queued signals (disconnected/data/etc.)
        # from being delivered to our slots after this point.
        try:
            self._send_signal.disconnect()
        except Exception:
            pass
        if self._worker:
            for sig in (self._worker.connected, self._worker.disconnected,
                        self._worker.error, self._worker.data_received,
                        self._worker.gmcp_received, self._worker.mccp_active):
                try:
                    sig.disconnect()
                except Exception:
                    pass
            # Step 2: tell the socket to close (triggers recv → EOF in thread)
            self._worker.disconnect()

        self._engine.stop()
        self._connected = False
        self._mccp_on   = False

        # Step 3: quit + wait so the thread is dead before we return.
        # Safe because signals are already disconnected above.
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(2000):
                dbg("gui", "thread did not stop in 2 s — terminating")
                self._thread.terminate()
                self._thread.wait(500)
        self._thread = None
        self._worker = None

        self._act_disconnect.setEnabled(False)
        self._act_reconnect.setEnabled(bool(self._last_host))
        self._set_status("Disconnected")
        self._mccp_label.setText("MCCP: off")
        self._mccp_label.setStyleSheet("color: #444; padding-right: 8px;")

    # ── Worker slots ─────────────────────────────────────────────────

    def _on_connected(self):
        dbg("gui", "_on_connected() slot fired on GUI thread")
        self._connected = True
        self._act_disconnect.setEnabled(True)
        self._act_reconnect.setEnabled(True)
        self._set_status(f"Connected  {self._last_host}:{self._last_port}")
        self._output.append_local(f"Connected to {self._last_host}:{self._last_port}", "#4e9a06")
        self._input.setFocus()

    def _on_disconnected(self, reason: str):
        dbg("gui", f"_on_disconnected({reason!r}) slot fired")
        self._connected = False
        if not self.isVisible():
            # Window is closing — don't touch widgets, just stop the thread
            if self._thread:
                self._thread.quit()
            return
        self._act_disconnect.setEnabled(False)
        self._act_reconnect.setEnabled(bool(self._last_host))
        self._set_status("Disconnected")
        self._output.append_local(reason, "#cc0000")
        self._mccp_label.setText("MCCP: off")
        self._mccp_label.setStyleSheet("color: #444; padding-right: 8px;")
        if self._thread:
            self._thread.quit()

    def _on_error(self, msg: str):
        dbg("gui", f"_on_error({msg!r}) slot fired")
        self._connected = False
        if not self.isVisible():
            if self._thread:
                self._thread.quit()
            return
        self._act_disconnect.setEnabled(False)
        self._act_reconnect.setEnabled(bool(self._last_host))
        self._set_status(f"Error: {msg}")
        self._output.append_local(f"Error: {msg}", "#ef2929")
        if self._thread:
            self._thread.quit()

    def _on_data(self, data: bytes):
        dbg("gui", f"_on_data(): {len(data)} bytes arriving on GUI thread")
        data = data.replace(b"\r", b"")
        text = data.decode("utf-8", errors="replace")

        # Feed plain words to tab completer
        self._completer.feed(re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", text))

        # Line-by-line processing: prepend buffered partial, split on \n
        text = self._line_buf + text
        self._line_buf = ""

        lines = text.split("\n")
        # Last element is partial (or empty if text ended with \n)
        self._line_buf = lines[-1]
        complete = lines[:-1]

        for line_ansi in complete:
            plain = re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", line_ansi).strip()
            gagged = self._engine.process_line(plain, line_ansi)
            if not gagged:
                self._output.append_ansi_line(line_ansi)
            # Detect involuntary movement (flee, escape, retreat, etc.)
            self._check_forced_move(plain)
            # Always feed room detector — it fires on every Exits: line
            plain_raw = re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", line_ansi)
            self._room_det.feed_line(plain_raw)

        # Render partial line (prompt) immediately without trigger processing
        if self._line_buf:
            self._output.append_ansi_line(self._line_buf, newline=False)
            self._line_buf = ""

        dbg("gui", "_on_data() done")

    def _on_gmcp(self, package: str, payload: object):
        room_data = try_parse_gmcp_line(package, payload)
        if room_data:
            self._set_map_room(room_data)   # updates map widget's MapData + persists
        self._right.write_log(f"GMCP {package}")

    def _on_mccp_active(self, enabled: bool):
        self._mccp_on = enabled
        if enabled:
            self._mccp_label.setText("MCCP2: ✓")
            self._mccp_label.setStyleSheet("color: #8ae234; padding-right: 8px;")
            self._output.append_local("MCCP2 compression active", "#8ae234")
        else:
            self._mccp_label.setText("MCCP: off")
            self._mccp_label.setStyleSheet("color: #444; padding-right: 8px;")

    # ── Sending ──────────────────────────────────────────────────────

    def _on_return(self):
        cmd = self._input.text().strip()
        # Don't clear — add_history() keeps the text and selects it so
        # the user can retype or just press Enter again to resend.
        if cmd:
            self._input.add_history(cmd)
        else:
            self._input.selectAll()
        self._send_command(cmd)

    # Matches: #99 flee  or  #99flee  (cmd_char + digits + optional space + rest)
    _REPEAT_RE = re.compile(r'^(?P<count>\d+)\s*(?P<body>.+)$')

    # Speedwalk tokens — two patterns tried in order:
    #   1. digit(s) + two-char diagonal  (e.g. 2ne, 3sw)
    #   2. optional digit(s) + single direction char
    # This means bare "sw" in "sws" is parsed as s+w+s not sw+s.
    _SW_DIAG_RE   = re.compile(r'(\d+)(ne|nw|se|sw)',           re.IGNORECASE)
    _SW_SINGLE_RE = re.compile(r'(\d*)(n|s|e|w|u|d)',           re.IGNORECASE)

    _SW_EXPAND: dict = {
        'n': 'north', 's': 'south', 'e': 'east', 'w': 'west',
        'u': 'up',    'd': 'down',
        'ne': 'northeast', 'nw': 'northwest',
        'se': 'southeast', 'sw': 'southwest',
    }

    def _expand_speedwalk(self, text: str) -> list[str] | None:
        """Parse a .speedwalk string into a list of direction commands.
        Returns None if the string isn't a valid speedwalk (unknown chars remain).

        Two-char diagonals (ne/nw/se/sw) only match when an explicit digit count
        precedes them, so 'sws' → s,w,s and '2sw' → sw,sw.

        e.g. '.2s5esws'  → s,s,e,e,e,e,e,s,w,s
             '.3ne2sw'   → ne,ne,ne,sw,sw
        """
        if not text.startswith('.'):
            return None
        body = text[1:]
        if not body:
            return None
        dirs = []
        pos  = 0
        while pos < len(body):
            # Try digit+diagonal first
            m = self._SW_DIAG_RE.match(body, pos)
            if m:
                count = int(m.group(1))
                dirs.extend([self._SW_EXPAND[m.group(2).lower()]] * count)
                pos = m.end()
                continue
            # Try optional-digit + single char
            m = self._SW_SINGLE_RE.match(body, pos)
            if m:
                count = int(m.group(1)) if m.group(1) else 1
                dirs.extend([self._SW_EXPAND[m.group(2).lower()]] * count)
                pos = m.end()
                continue
            return None   # unexpected character — not a speedwalk
        return dirs if dirs else None

    def _send_command(self, cmd: str):
        """Entry point for all user-initiated commands."""
        if not cmd:
            return

        # ── .speedwalk expansion ─────────────────────────────────────
        # Check before cmd_char dispatch so ".2n3s" isn't treated as text.
        # Only applies when the whole input is a single speedwalk token.
        stripped = cmd.strip()
        dirs = self._expand_speedwalk(stripped)
        if dirs is not None:
            if not self._connected:
                self._output.append_local("Not connected — use File → Sessions or Quick Connect", "#c4a000")
                return
            for d in dirs:
                self._send_raw_command(d)
            return

        # ── #N repeat expansion ──────────────────────────────────────
        # #99 flee  → send "flee" 99 times.
        # Only fires when the rest of the token is NOT a known client command.
        if stripped.startswith(self._cmd_char):
            after = stripped[len(self._cmd_char):]
            m = self._REPEAT_RE.match(after)
            if m:
                count = min(int(m.group('count')), 999)  # sanity cap
                body  = m.group('body').strip()
                # Make sure it's not a real client command like #map
                if not self._commands.dispatch(stripped):
                    if not self._connected:
                        self._output.append_local("Not connected — use File → Sessions or Quick Connect", "#c4a000")
                        return
                    parts = [p.strip() for p in body.split(self._cmd_sep) if p.strip()]
                    for _ in range(count):
                        for part in parts:
                            if not self._engine.process_alias(part):
                                self._send_raw_command(part)
                return

        # Client commands (start with command char) are handled locally
        if self._commands.dispatch(cmd):
            return
        if not self._connected:
            self._output.append_local("Not connected — use File → Sessions or Quick Connect", "#c4a000")
            return
        # Split on the configured separator so "go north;kill orc" sends two commands
        parts = [p.strip() for p in cmd.split(self._cmd_sep) if p.strip()]
        if not parts:
            return
        for part in parts:
            if not self._engine.process_alias(part):
                self._send_raw_command(part)

    # Direction abbreviation → full name
    # ── Movement tracking ─────────────────────────────────────────────

    # ── Direction tables ──────────────────────────────────────────────

    _DIR_ABBREVS: dict = {
        "n": "north", "s": "south", "e": "east", "w": "west",
        "ne": "northeast", "nw": "northwest",
        "se": "southeast", "sw": "southwest",
        "u": "up", "d": "down",
        "north": "north", "south": "south", "east": "east", "west": "west",
        "northeast": "northeast", "northwest": "northwest",
        "southeast": "southeast", "southwest": "southwest",
        "up": "up", "down": "down", "in": "in", "out": "out",
    }

    _FORCED_MOVE_RE = re.compile(
        r'^You \w+ (north|south|east|west|northeast|northwest|southeast|southwest|up|down)ward',
        re.IGNORECASE,
    )

    _ENTER_RE = re.compile(
        r'^(?:enter|en|go|jump|climb|crawl|swim)\s+(.+)',
        re.IGNORECASE,
    )

    # ── Movement queue ────────────────────────────────────────────────

    def _queue_movement(self, kind: str, value: str):
        """Append a movement intent to the queue."""
        self._move_queue.append((kind, value))

    def _record_sent_command(self, cmd: str):
        """Classify a sent command and push onto the movement queue."""
        cl = cmd.strip().lower()
        direction = self._DIR_ABBREVS.get(cl)
        if direction:
            self._queue_movement("dir", direction)
            return
        if self._ENTER_RE.match(cmd.strip()):
            # Store the whole normalised command ("enter portal", "go gate")
            # so matching against map exit names works correctly.
            self._queue_movement("special", cl)
            return
        # Non-movement: no queue entry — won't produce an Exits: line

    def _check_forced_move(self, plain: str):
        """Detect involuntary movement echoes and push onto queue."""
        m = self._FORCED_MOVE_RE.match(plain)
        if m:
            # Forced move bypasses the send path — insert at front
            self._move_queue.insert(0, ("dir", m.group(1).lower()))

    def _apply_move_from_exits(self, name: str, exits: frozenset, forced: bool):
        """
        Called every time the room detector sees a complete Exits: block.

        Priority order (most reliable → least):
        1. Direction-following: current_room.exits[direction] → dest
           Trusted unconditionally when we have a queued direction and a
           current room — the map graph is always more reliable than name
           matching, especially with duplicate room names.
        2. Name+exits lookup restricted to adjacent rooms only.
           Used only when there is no queued direction (look, scan, teleport).

        Stationary guard: if the detected room equals the current room
        (look, scan, failed move) we do NOT pop the queue.
        """
        map_data = self._right.map_widget._data
        cur      = map_data.current
        cur_id   = map_data.current_id

        # Peek at the queue without popping yet
        pending = self._move_queue[0] if self._move_queue else None

        # ── Step 1: direction-following (primary, unconditional) ──────
        # Trust the map graph edge — never fall through to name search when
        # we have a queued direction, as duplicate names cause wrong jumps.
        if not forced and pending and cur is not None:
            kind, value = pending
            dest_id = None
            if kind == "dir":
                dest_id = cur.exits.get(value)
            elif kind == "special":
                for exit_name, eid in cur.exits.items():
                    if _special_exit_matches(exit_name.lower(), value):
                        dest_id = eid
                        break
            # Consume the queue entry regardless of whether the exit is mapped
            self._move_queue.pop(0)
            if dest_id and dest_id in map_data.rooms:
                self._set_map_room({"num": dest_id})
                self._walk_tick()
            # If exit not in map, position unknown — don't jump elsewhere
            return

        # ── Step 2: name+exits lookup (no queued direction) ───────────
        # Only runs when no direction was queued: teleport, script move, etc.
        # Restrict to adjacent rooms to minimise false positives.
        near_id     = None if forced else cur_id
        detected_id = map_data.find_by_name_and_exits(name, exits, near_id=near_id)

        # Stationary guard — same room as current → look/scan/failed move
        if (not forced
                and detected_id is not None
                and detected_id == cur_id):
            return

        if detected_id is not None:
            self._set_map_room({"num": detected_id})
            self._walk_tick()
            return

        # ── Step 3: nothing worked ────────────────────────────────────
        if forced:
            self._output.append_local(
                f"Map: could not find '{name}' with exits "
                f"{', '.join(sorted(exits)) or 'none'} in loaded map.",
                "#cc9944",
            )

    # ── Speedwalk engine ──────────────────────────────────────────────

    def start_walk(self, path: list):
        """
        Confirmed walk: send one step, wait for Exits: confirmation, repeat.
        Safe for paths with doors or special exits.
        """
        self._walk_path = list(path)
        self._move_queue.clear()
        self._walk_send_next()

    def start_fwalk(self, path: list):
        """
        Fast walk: blast all directions to the MUD at once without waiting
        for confirmation.  Fastest for open paths with no doors.
        The move queue is still populated so the map tracker stays in sync.
        """
        self.stop_walk()   # clear any in-progress walk first
        if not path:
            return
        summary = self._compress_path(path)
        self._output.append_local(
            f"Map: fwalk {len(path)} steps  [{summary}]",
            "#44aacc",
        )
        for step in path:
            is_dir = step in self._DIR_ABBREVS.values()
            self._queue_movement("dir" if is_dir else "special", step)
            mud_cmd = step.split("|")[0].strip() if "|" in step else step
            self._send_signal.emit(mud_cmd)

    def stop_walk(self):
        """Abort any in-progress walk."""
        self._walk_path.clear()
        self._move_queue.clear()

    def _walk_send_next(self):
        """Send the next step of the confirmed walk."""
        if not self._walk_path:
            return
        step = self._walk_path.pop(0)
        is_dir = step in self._DIR_ABBREVS.values()
        self._queue_movement("dir" if is_dir else "special", step)
        mud_cmd = step.split("|")[0].strip() if "|" in step else step
        if self._cmd_echo:
            self._output.append_local(mud_cmd, self._cmd_echo_color, brackets=False)
        self._send_signal.emit(mud_cmd)

    def _walk_tick(self):
        """Called after each confirmed room change — sends next confirmed-walk step."""
        if self._walk_path:
            self._walk_send_next()

    @staticmethod
    def _compress_path(path: list) -> str:
        """Turn ['north','north','north','east'] into '3n e'."""
        _abbr = {
            "north": "n", "south": "s", "east": "e", "west": "w",
            "northeast": "ne", "northwest": "nw",
            "southeast": "se", "southwest": "sw",
            "up": "u", "down": "d", "in": "in", "out": "out",
        }
        parts = []
        i = 0
        while i < len(path):
            step = path[i]
            abbr = _abbr.get(step)
            if abbr:
                count = 1
                while i + count < len(path) and path[i + count] == step:
                    count += 1
                parts.append(f"{count}{abbr}" if count > 1 else abbr)
                i += count
            else:
                # Special exit — use first token before |
                parts.append(step.split("|")[0].strip())
                i += 1
        return " ".join(parts)

    def _send_raw_command(self, cmd: str):
        """Send user-typed text to the server — echo as bare text, no brackets."""
        if self._cmd_echo:
            self._output.append_local(cmd, self._cmd_echo_color, brackets=False)
        self._record_sent_command(cmd)
        self._send_signal.emit(cmd)

    def _send_triggered_command(self, cmd: str):
        """Send a trigger/alias-fired command — echo with brackets."""
        if self._cmd_echo:
            self._output.append_local(cmd, self._cmd_echo_color, brackets=True)
        self._record_sent_command(cmd)
        self._send_signal.emit(cmd)


    def _echo_local(self, msg: str):
        self._output.append_local(msg, "#5599ff")

    def _apply_palette(self, config: dict):
        """Apply the colour palette from config to the ANSI renderer."""
        pal = config.get("palette")
        if pal and len(pal) == 16:
            set_palette(pal)
        else:
            theme = config.get("palette_theme", "xterm")
            theme_pal = THEMES.get(theme, THEMES["xterm"])
            set_palette(list(theme_pal))

    def _on_showme(self, target: str, ansi_text: str):
        """Route #showme output — empty target goes to main output."""
        t = target.lower()
        if not t or t in ("main", "output"):
            self._output.append_ansi_text(ansi_text)
        else:
            self._right.write_ansi(t, ansi_text)

    def _dispatch_gui_msg(self, target: str, message: str):
        t = target.lower()
        if "status" in t:
            self._set_status(message)
        elif "info" in t:
            self._right.write_info(message)
        else:
            self._right.write_log(message)

    # ── Map ──────────────────────────────────────────────────────────

    def _on_text_room(self, name: str, exits: frozenset, forced: bool = False):
        """Called by TorilRoomDetector when a room name+exits is detected."""
        self._apply_move_from_exits(name, exits, forced)

    def _load_map_file(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Mudlet Map File", "",
            "JSON Map Files (*.json);;All Files (*)")
        if path:
            ok, msg = self._right.map_widget.load_map_file(path)
            self._set_status(f"Map: {msg}")
            if ok:
                self._output.append_local(f"Map loaded: {msg}", "#44cc88")
                self._save_session_map(map_path=path)
            else:
                self._output.append_local(f"Map error: {msg}", "#cc4444")

    # ── Config dialog ────────────────────────────────────────────────

    def _show_config(self):
        # Reuse the dialog if already open — just bring it to front
        if self._config_dlg is not None and self._config_dlg.isVisible():
            self._config_dlg.raise_()
            self._config_dlg.activateWindow()
            return
        current_config = self._session.config if self._session else {}
        dlg = ConfigDialog(current_config, self)
        dlg.config_saved.connect(self._apply_config)
        self._config_dlg = dlg
        dlg.show()
        restore_geometry("config_dialog", dlg)

    def _set_map_room(self, data: dict):
        """Set current map room and persist room ID to session."""
        self._right.on_gmcp_room(data)
        room_id = data.get("num") or data.get("vnum") or data.get("id")
        if room_id is not None:
            self._save_session_map(room_id=int(room_id))

    # ── Dock layout persistence ──────────────────────────────────────

    def _save_dock_layout(self):
        """Save dock state + geometry to window_settings.json.
        Called synchronously on close and debounced on dock moves."""
        try:
            from ui.window_settings import load_settings, save_settings
            geom  = self.saveGeometry()
            state = self.saveState(version=1)
            data  = load_settings()
            data["main_window"] = geom.toHex().data().decode()
            data["dock_state"]  = state.toBase64().data().decode()
            save_settings(data)
            dbg("gui", f"dock layout saved ({len(state)} bytes state)")
        except Exception as e:
            dbg("gui", f"_save_dock_layout error: {e}")

    def _reset_dock_layout(self):
        """Restore the default dock arrangement."""
        # Re-dock everything to right, then re-split
        for dock in (self._dock_map, self._dock_info, self._dock_log):
            dock.setFloating(False)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            dock.show()
        self.splitDockWidget(self._dock_map, self._dock_info,
                             Qt.Orientation.Vertical)
        self.tabifyDockWidget(self._dock_info, self._dock_log)
        self._dock_map.raise_()

    def _restore_dock_layout(self):
        """Restore dock state from window_settings.json."""
        try:
            from ui.window_settings import load_settings
            data = load_settings()
            state_b64 = data.get("dock_state")
            if state_b64:
                ok = self.restoreState(
                    QByteArray.fromBase64(state_b64.encode()), version=1)
                dbg("gui", f"restoreState → {ok}")
        except Exception as e:
            dbg("gui", f"_restore_dock_layout error: {e}")

    def _save_session_map(self, map_path: str = None, room_id: int = None):
        """Persist map file path and/or last room ID into the current session."""
        if self._session is None:
            return
        if map_path is not None:
            self._session.config["map_file"] = map_path
        if room_id is not None:
            self._session.config["map_last_room"] = room_id
        sessions = _load_sessions()
        for i, s in enumerate(sessions):
            if s.name == self._session.name:
                sessions[i] = self._session
                break
        _save_sessions(sessions)

    def _apply_config(self, new_cfg: dict):
        """Apply a saved config dict from the ConfigDialog signal."""
        if self._session:
            self._session.config = new_cfg
            sessions = _load_sessions()
            for i, s in enumerate(sessions):
                if s.name == self._session.name:
                    sessions[i] = self._session
                    break
            _save_sessions(sessions)
        self._cmd_sep        = new_cfg.get("cmd_separator", ";") or ";"
        self._cmd_echo       = new_cfg.get("cmd_echo", True)
        self._cmd_echo_color = new_cfg.get("cmd_echo_color", "#e8d44d") or "#e8d44d"
        self._cmd_char       = new_cfg.get("cmd_char", "#") or "#"
        self._commands.set_command_char(self._cmd_char)
        self._apply_palette(new_cfg)
        if "folders" not in new_cfg:
            migrated = _migrate_legacy(new_cfg)
            if migrated:
                new_cfg["folders"] = migrated
        self._engine.load_config(new_cfg)
        self._button_bar.load_buttons(self._engine.get_buttons())

    # ── Misc ────────────────────────────────────────────────────────

    def _set_status(self, msg: str):
        self._status_bar.showMessage(f"  {msg}")

    def _show_about(self):
        QMessageBox.about(
            self, "About MUD Client",
            "<b>MUD Client</b><br>"
            "A full-featured MUD client built with Python 3 and PyQt6.<br><br>"
            "Features: ANSI colours · Telnet IAC · MCCP2 compression · "
            "GMCP · Aliases · Triggers · Timers · Tab completion · "
            "Session profiles · Configurable macro buttons",
        )

    def closeEvent(self, event):
        # 0. Stop script engine timers FIRST — they can fire triggered_send
        #    which touches output/status widgets during teardown (segfault).
        try:
            self._engine.stop()
        except Exception:
            pass

        # 1. Kill the debounced save timer immediately so it cannot fire
        #    while Qt is tearing down widgets (causes segfault via
        #    visibilityChanged → timer.start → _save_dock_layout on dead widget).
        try:
            self._dock_save_timer.stop()
            self._dock_save_timer.timeout.disconnect()
        except Exception:
            pass
        # Disconnect dock signals that would restart the timer
        for dock in (self._dock_map, self._dock_info, self._dock_log):
            try:
                dock.dockLocationChanged.disconnect()
                dock.topLevelChanged.disconnect()
                dock.visibilityChanged.disconnect()
            except Exception:
                pass

        # 2. Save synchronously now while all widgets still exist
        self._save_dock_layout()

        # 3. Close child dialogs
        try:
            if self._config_dlg is not None:
                self._config_dlg.close()
        except Exception:
            pass

        # 4. Disconnect telnet
        try:
            self._disconnect()
        except Exception:
            pass

        super().closeEvent(event)
