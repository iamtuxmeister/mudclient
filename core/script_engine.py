"""
ScriptEngine — client-side alias expansion, trigger matching, and timers.

All processing runs on the Qt main thread (no locking needed).

Features
--------
Aliases    exact first-word match; body supports %0-%9, ${var}; semicolons
Actions    Python regex (fallback: substring); %0-%9 substitution
Timers     QTimer-based; restarted per load_config()
Variables  ${name} substitution in alias bodies
"""

from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class ScriptEngine(QObject):
    """
    Signals
    -------
    send_command(str)     engine wants to send a command to the MUD
    local_echo(str)       display a local message in the output window
    gui_message(str,str)  (target, text) for status bar / panel routing
    """

    send_command = pyqtSignal(str)
    local_echo   = pyqtSignal(str)
    gui_message  = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._aliases:    list[dict] = []
        self._actions:    list[dict] = []
        self._highlights: list[dict] = []
        self._variables:  dict[str, str] = {}
        self._timers:     dict[str, QTimer] = {}

    # ── Config ──────────────────────────────────────────────────────

    def load_config(self, config: dict):
        self._aliases    = list(config.get("aliases",    []))
        self._actions    = list(config.get("actions",    []))
        self._highlights = list(config.get("highlights", []))
        self._variables  = {
            v["name"]: v.get("value", "")
            for v in config.get("variables", [])
            if v.get("name")
        }
        self._restart_timers(config.get("timers", []))

    def stop(self):
        self._stop_all_timers()

    def clear(self):
        self.stop()
        self._aliases.clear()
        self._actions.clear()
        self._highlights.clear()
        self._variables.clear()

    # ── Alias expansion ─────────────────────────────────────────────

    def expand_alias(self, command: str) -> Optional[str]:
        """
        Return expanded string if an alias matches, else None.
        """
        parts = command.strip().split(None, 1)
        if not parts:
            return None
        word = parts[0]
        args_str = parts[1] if len(parts) > 1 else ""
        args = args_str.split()

        for alias in self._aliases:
            if not alias.get("enabled", True):
                continue
            if alias.get("name", "") != word:
                continue
            body = alias.get("body", "")
            # variable substitution
            for k, v in self._variables.items():
                body = body.replace(f"${{{k}}}", v)
            # argument substitution: %0=full, %1-%9=individual
            body = body.replace("%0", args_str)
            for idx, a in enumerate(args[:9], 1):
                body = body.replace(f"%{idx}", a)
            return body
        return None

    def process_alias(self, command: str) -> bool:
        """
        Try to expand and emit alias.  Returns True if an alias fired.
        """
        expanded = self.expand_alias(command)
        if expanded is None:
            return False
        for part in expanded.split(";"):
            part = part.strip()
            if part:
                self.send_command.emit(part)
        return True

    # ── Trigger / action matching ────────────────────────────────────

    def process_line(self, line: str):
        """
        Run all enabled actions against a single line of MUD output.
        """
        for action in self._actions:
            if not action.get("enabled", True):
                continue
            pattern = action.get("pattern", "")
            if not pattern:
                continue
            cmd_template = action.get("command", "")
            target = action.get("gui_target", "")

            # Try regex first, then substring
            groups: list[str] = []
            matched = False
            try:
                m = re.search(pattern, line)
                if m:
                    matched = True
                    groups = list(m.groups())
            except re.error:
                if pattern in line:
                    matched = True

            if not matched:
                continue

            cmd = cmd_template.replace("%0", line)
            for idx, g in enumerate(groups[:9], 1):
                cmd = cmd.replace(f"%{idx}", g or "")

            if target:
                self.gui_message.emit(target, cmd)
            else:
                for part in cmd.split(";"):
                    part = part.strip()
                    if part:
                        self.send_command.emit(part)

    # ── Highlights ──────────────────────────────────────────────────

    def get_highlights(self) -> list[dict]:
        return list(self._highlights)

    # ── Timers ──────────────────────────────────────────────────────

    def _restart_timers(self, timer_configs: list[dict]):
        self._stop_all_timers()
        for tc in timer_configs:
            if not tc.get("enabled", True):
                continue
            name     = tc.get("name", "")
            interval = int(tc.get("interval", 0))
            command  = tc.get("command", "")
            if interval <= 0 or not command:
                continue
            t = QTimer(self)
            t.setInterval(interval * 1000)
            t.timeout.connect(lambda cmd=command: self._timer_fire(cmd))
            t.start()
            self._timers[name or str(id(t))] = t

    def _timer_fire(self, command: str):
        for part in command.split(";"):
            part = part.strip()
            if part:
                self.send_command.emit(part)

    def _stop_all_timers(self):
        for t in self._timers.values():
            t.stop()
        self._timers.clear()
