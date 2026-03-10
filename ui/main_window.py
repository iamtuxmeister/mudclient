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
)
from PyQt6.QtCore  import Qt, QThread, pyqtSignal
from PyQt6.QtGui   import QFont, QKeyEvent, QAction, QColor, QPalette

from core.telnet_worker  import TelnetWorker
from core.script_engine  import ScriptEngine
from core.map_parser     import MapGraph, try_parse_gmcp_line
from core.ansi_parser    import set_palette, THEMES, get_palette
from core.debug          import dbg

from ui.output_widget   import OutputWidget
from ui.map_widget      import MapWidget
from ui.right_panel     import RightPanel
from ui.button_bar      import ButtonBar
from ui.session_manager import SessionManager, Session, _load_sessions, _save_sessions
from ui.config_dialog   import ConfigDialog


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
                font-size: 11pt;
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

        # First keypress: snapshot prefix and build the filtered match list
        if self._hist_idx == -1:
            self._hist_prefix = self.selectedText() if self.hasSelectedText()                                 else self.text()
            # Strip trailing selection so prefix is just what the user typed
            # (when the field shows a previously selected command, we want
            # the *current visible text* as the filter, not an empty string)
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
            self.selectAll()
            return

        self._hist_pos = new_pos
        self._hist_idx = self._hist_matches[new_pos]
        self.setText(self._history[self._hist_idx])
        self.selectAll()

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
        self._cmd_sep:   str = ";"   # configurable command separator
        self._line_buf:  str = ""    # partial line buffer for line-by-line rendering

        self._completer = _TabCompleter()
        self._map       = MapGraph()
        self._engine    = ScriptEngine(self)

        self._engine.send_command.connect(self._send_raw_command)
        self._engine.local_echo.connect(self._echo_local)
        self._engine.showme.connect(self._on_showme)
        self._engine.gui_message.connect(self._dispatch_gui_msg)

        self._build_palette()
        self._build_ui()
        self._build_menu()
        self._set_status("Disconnected")

        # Show session picker as soon as the event loop starts
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._show_sessions)

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

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # Horizontal splitter: output | right panel
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: #222; width: 3px; }")

        self._output = OutputWidget()
        splitter.addWidget(self._output)

        self._right = RightPanel()
        splitter.addWidget(self._right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        vbox.addWidget(splitter, 1)

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
                font-family: Monospace; font-size: 10pt;
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

        # Status bar
        self._status_bar = QStatusBar()
        self._status_bar.setStyleSheet("""
            QStatusBar {
                background: #111; color: #666;
                font-family: Monospace; font-size: 9pt;
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
        self._act_reconnect  = self._add_action(fm, "&Reconnect",  "Ctrl+R",  self._reconnect,  enabled=False)
        self._act_disconnect = self._add_action(fm, "&Disconnect", "Ctrl+D",  self._disconnect, enabled=False)
        fm.addSeparator()
        self._add_action(fm, "&Quit", "Ctrl+Q", self.close)

        # View
        vm = bar.addMenu("&View")
        self._add_action(vm, "&Clear",         "Ctrl+L",       self._output.clear_output)
        self._add_action(vm, "Font &Larger",   "Ctrl+=",       self._output.font_larger)
        self._add_action(vm, "Font S&maller",  "Ctrl+-",       self._output.font_smaller)
        self._add_action(vm, "Scroll to &Bottom", "Ctrl+End",  self._output.scroll_to_bottom)

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
        self._cmd_sep = config.get("cmd_separator", ";") or ";"
        self._apply_palette(config)
        if config:
            self._engine.load_config(config)

        # load button bar
        self._button_bar.load_buttons(config.get("buttons", []))

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
        try:
            self._send_signal.disconnect()
        except Exception:
            pass
        if self._worker:
            self._worker.disconnect()
        self._engine.stop()
        self._connected = False
        self._mccp_on   = False
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

        # Render partial line (prompt) immediately without trigger processing
        if self._line_buf:
            self._output.append_ansi_line(self._line_buf, newline=False)
            self._line_buf = ""

        dbg("gui", "_on_data() done")

    def _on_gmcp(self, package: str, payload: object):
        room_data = try_parse_gmcp_line(package, payload)
        if room_data:
            self._map.update(room_data)
            self._right.update_map(self._map.render_ascii())
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

    def _send_command(self, cmd: str):
        """Entry point for all user-initiated commands."""
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

    def _send_raw_command(self, cmd: str):
        """Send text directly to the server (no alias expansion)."""
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

    # ── Config dialog ────────────────────────────────────────────────

    def _show_config(self):
        current_config = self._session.config if self._session else {}
        dlg = ConfigDialog(current_config, self)
        if dlg.exec() == ConfigDialog.DialogCode.Accepted:
            new_cfg = dlg.get_config()
            if self._session:
                self._session.config = new_cfg
                sessions = _load_sessions()
                for i, s in enumerate(sessions):
                    if s.name == self._session.name:
                        sessions[i] = self._session
                        break
                _save_sessions(sessions)
            self._cmd_sep = new_cfg.get("cmd_separator", ";") or ";"
            self._apply_palette(new_cfg)
            self._engine.load_config(new_cfg)
            self._button_bar.load_buttons(new_cfg.get("buttons", []))

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
        self._disconnect()
        super().closeEvent(event)
