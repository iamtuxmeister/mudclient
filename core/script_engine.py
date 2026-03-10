"""
ScriptEngine — TinTin++-compatible alias, action, variable, and timer engine.

Supported TinTin++ features
---------------------------
Aliases     #alias {name} {body}  — first-word match; body supports
            %0 (full args), %1-%9 (individual args), $var / ${var}
Actions     regex or substring trigger; captures %1-%9; action bodies
            are TinTin++ command strings
Variables   $name or ${name} expansion everywhere; set with #var
Timers      interval-based command firing

Inline commands supported in alias/action bodies
-------------------------------------------------
  #send {cmd}              send cmd to MUD  (also: bare text without #)
  #gag                     suppress the triggering line from display
  #showme {text}           display ANSI text in main output
  #showme {text} {window}  display in named side panel (info / log / map)
  #var {name} {value}      set a variable
  #nop {anything}          comment / no-op
  #echo {text}             alias for #showme (main window)
  #if {cond} {then}        basic conditional (cond: Python expression,
  #if {cond} {then} {else} $var and %n substituted before eval)

Color codes (TinTin++ <xyz> format converted to ANSI on the fly)
-----------------------------------------------------------------
  x = attribute: 0=reset 1=bold 2=dim 3=italic 4=underline 5=blink 7=rev 8=skip
  y = fg color:  0=blk 1=red 2=grn 3=yel 4=blu 5=mag 6=cyn 7=wht 8=skip 9=dflt
  z = bg color:  same mapping as fg
  <reset>        → ESC[0m
"""

from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


# ── TinTin++ <xyz> colour-code → ANSI ────────────────────────────────────────

_TT_ATTR  = {'0':'0','1':'1','2':'2','3':'3','4':'4','5':'5','7':'7','8':None}
_TT_COLOR = {'0':'0','1':'1','2':'2','3':'3','4':'4','5':'5','6':'6','7':'7',
             '8':None,'9':'9'}

def tt_color_to_ansi(text: str) -> str:
    """Convert TinTin++ <xyz> and <reset> colour tags to ANSI escape sequences."""
    def _sub(m: re.Match) -> str:
        code = m.group(1)
        if code.lower() == 'reset':
            return '\x1b[0m'
        if len(code) == 3:
            attr  = _TT_ATTR.get(code[0])
            fg    = _TT_COLOR.get(code[1])
            bg    = _TT_COLOR.get(code[2])
            parts: list[str] = []
            if attr  is not None: parts.append(attr)
            if fg    is not None and fg != '9': parts.append(f'3{fg}')
            if bg    is not None and bg != '9': parts.append(f'4{bg}')
            return f'\x1b[{";".join(parts)}m' if parts else '\x1b[0m'
        return m.group(0)      # unknown tag — leave unchanged
    return re.sub(r'<([a-zA-Z0-9]+)>', _sub, text)


_ANSI_STRIP_RE = re.compile(r'\x1b\[[^a-zA-Z]*[a-zA-Z]')

def strip_ansi(text: str) -> str:
    return _ANSI_STRIP_RE.sub('', text)


# ── Brace-aware text utilities ────────────────────────────────────────────────

def _split_semi(text: str) -> list[str]:
    """Split on ';' but not inside {} braces."""
    parts: list[str] = []
    depth   = 0
    current: list[str] = []
    for ch in text:
        if   ch == '{':
            depth += 1
            current.append(ch)
        elif ch == '}':
            depth -= 1
            current.append(ch)
        elif ch == ';' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current))
    return parts


def _parse_args(text: str) -> list[str]:
    """
    Extract brace-delimited or whitespace-delimited arguments.
    '{hello world} foo' → ['hello world', 'foo']
    """
    args: list[str] = []
    i = 0
    text = text.strip()
    while i < len(text):
        if text[i] == '{':
            depth = 1
            j = i + 1
            while j < len(text) and depth:
                if   text[j] == '{': depth += 1
                elif text[j] == '}': depth -= 1
                j += 1
            args.append(text[i+1:j-1])
            i = j
        elif text[i].isspace():
            i += 1
        else:
            j = i
            while j < len(text) and not text[j].isspace():
                j += 1
            args.append(text[i:j])
            i = j
    return args


# ── Variable / capture substitution ──────────────────────────────────────────

def _subst(text: str, variables: dict[str, str], captures: list[str]) -> str:
    """
    Apply variable and capture substitutions:
      %0   full first capture (or whole match if no groups)
      %1–%9  numbered captures
      $name or ${name}  variable lookup
    """
    # captures: index 0 = %0, 1 = %1 …
    for idx in range(min(10, len(captures))):
        text = text.replace(f'%{idx}', captures[idx] if captures[idx] is not None else '')
    # ${name} first, then $name (longest-match avoidance)
    text = re.sub(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',
        lambda m: variables.get(m.group(1), ''),
        text,
    )
    text = re.sub(
        r'\$([A-Za-z_][A-Za-z0-9_]*)',
        lambda m: variables.get(m.group(1), ''),
        text,
    )
    return text


# ── ScriptEngine ─────────────────────────────────────────────────────────────

class ScriptEngine(QObject):
    """
    Signals
    -------
    send_command(str)         engine wants to send text to the MUD
    local_echo(str)           plain local message for the output window
    showme(str, str)          (target, ansi_text): richly coloured display
                              target: '' = main, 'info'/'log'/'map' = panel
    gui_message(str, str)     legacy: (target, plain text) for status bar etc.
    """

    send_command = pyqtSignal(str)
    local_echo   = pyqtSignal(str)
    showme       = pyqtSignal(str, str)   # (window-target, ansi-coloured text)
    gui_message  = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._aliases:    list[dict] = []
        self._actions:    list[dict] = []
        self._highlights: list[dict] = []
        self._variables:  dict[str, str] = {}
        self._timers:     dict[str, QTimer] = {}
        self._cmd_sep:    str = ';'

    # ── Config ───────────────────────────────────────────────────────

    def load_config(self, config: dict):
        self._aliases    = list(config.get('aliases',    []))
        self._actions    = list(config.get('actions',    []))
        self._highlights = list(config.get('highlights', []))
        self._variables  = {
            v['name']: v.get('value', '')
            for v in config.get('variables', [])
            if v.get('name')
        }
        self._cmd_sep = config.get('cmd_separator', ';') or ';'
        self._restart_timers(config.get('timers', []))

    def set_cmd_sep(self, sep: str):
        self._cmd_sep = sep or ';'

    def stop(self):
        self._stop_all_timers()

    def clear(self):
        self.stop()
        self._aliases.clear()
        self._actions.clear()
        self._highlights.clear()
        self._variables.clear()

    # ── Public: alias processing ─────────────────────────────────────

    def process_alias(self, command: str) -> bool:
        """Try alias expansion. Returns True if an alias fired."""
        parts    = command.strip().split(None, 1)
        if not parts:
            return False
        word     = parts[0]
        args_str = parts[1] if len(parts) > 1 else ''
        args     = args_str.split()

        for alias in self._aliases:
            if not alias.get('enabled', True):
                continue
            if alias.get('name', '') != word:
                continue
            body     = alias.get('body', '')
            captures = [''] + args[:9]           # %0 unused; %1-%9=args
            captures[0] = args_str               # %0 = full args string
            body     = _subst(body, self._variables, captures)
            self._exec_body(body, captures, '')
            return True
        return False

    # ── Public: trigger processing ───────────────────────────────────

    def process_line(self, plain: str, raw_ansi: str = '') -> bool:
        """
        Run all enabled actions against one line of MUD output.
        Returns True if any action gagged the line (suppress display).

        plain    — ANSI-stripped text (for matching)
        raw_ansi — original coloured text (for %0 in #showme etc.)
        """
        gagged = False
        for action in self._actions:
            if not action.get('enabled', True):
                continue
            pattern = action.get('pattern', '')
            if not pattern:
                continue

            captures: list[str] = []
            matched = False
            try:
                m = re.search(pattern, plain)
                if m:
                    matched  = True
                    all_g    = list(m.groups())
                    captures = [plain] + [g or '' for g in all_g]
            except re.error:
                if pattern in plain:
                    matched  = True
                    captures = [plain]

            if not matched:
                continue

            body   = action.get('body', action.get('command', ''))
            body   = _subst(body, self._variables, captures)
            # Make raw ANSI available as special %%raw substitution
            body   = body.replace('%%raw', raw_ansi)
            gagged = self._exec_body(body, captures, raw_ansi) or gagged

        return gagged

    # ── Public: highlights ───────────────────────────────────────────

    def get_highlights(self) -> list[dict]:
        return list(self._highlights)

    # ── Internal: command execution ──────────────────────────────────

    def _exec_body(self, body: str, captures: list[str], raw_ansi: str) -> bool:
        """
        Execute a TinTin++ command body string.
        Returns True if any #gag was encountered.
        """
        gagged = False
        for cmd in _split_semi(body):
            cmd = cmd.strip()
            if not cmd:
                continue
            if cmd.startswith('#'):
                if self._exec_tt_cmd(cmd, captures, raw_ansi):
                    gagged = True
            else:
                # Bare text → send to MUD
                self.send_command.emit(cmd)
        return gagged

    def _exec_tt_cmd(self, cmd: str, captures: list[str], raw_ansi: str) -> bool:
        """
        Execute one #command.  Returns True only for #gag.
        """
        # Split into keyword + rest
        m = re.match(r'#(\w+)\s*(.*)', cmd, re.DOTALL)
        if not m:
            return False
        keyword  = m.group(1).lower()
        rest     = m.group(2).strip()
        args     = _parse_args(rest)

        if keyword in ('nop', 'comment'):
            return False

        if keyword == 'gag':
            return True

        if keyword in ('send', 'cr'):
            text = args[0] if args else rest
            text = _subst(text, self._variables, captures)
            for part in _split_semi(text):
                part = part.strip()
                if part:
                    self.send_command.emit(part)
            return False

        if keyword in ('showme', 'echo'):
            ansi_text = args[0] if args else rest
            ansi_text = _subst(ansi_text, self._variables, captures)
            ansi_text = tt_color_to_ansi(ansi_text)
            target    = args[1].lower() if len(args) > 1 else ''
            self.showme.emit(target, ansi_text)
            return False

        if keyword in ('var', 'variable'):
            if len(args) >= 2:
                name  = args[0]
                value = _subst(args[1], self._variables, captures)
                self._variables[name] = value
            elif len(args) == 1:
                # #var name  → delete
                self._variables.pop(args[0], None)
            return False

        if keyword in ('unvar', 'unvariable'):
            for a in args:
                self._variables.pop(a, None)
            return False

        if keyword == 'if':
            # #if {condition} {then-body} [{else-body}]
            if len(args) < 2:
                return False
            cond      = _subst(args[0], self._variables, captures)
            then_body = args[1]
            else_body = args[2] if len(args) > 2 else ''
            # Replace $var and %n in condition before eval
            try:
                result = bool(eval(cond, {'__builtins__': {}},
                                   dict(self._variables)))
            except Exception:
                result = bool(cond.strip())
            chosen = _subst(then_body if result else else_body,
                            self._variables, captures)
            return self._exec_body(chosen, captures, raw_ansi)

        if keyword in ('local', 'localecho'):
            text = args[0] if args else rest
            text = _subst(text, self._variables, captures)
            self.local_echo.emit(text)
            return False

        if keyword == 'status':
            text = args[0] if args else rest
            text = _subst(text, self._variables, captures)
            self.gui_message.emit('status', text)
            return False

        # Unknown keyword — emit as a MUD command (permits #north etc.)
        full = _subst(cmd[1:], self._variables, captures)   # strip leading #
        self.send_command.emit(full)
        return False

    # ── Timers ───────────────────────────────────────────────────────

    def _restart_timers(self, timer_configs: list[dict]):
        self._stop_all_timers()
        for tc in timer_configs:
            if not tc.get('enabled', True):
                continue
            name     = tc.get('name', '')
            interval = int(tc.get('interval', 0))
            command  = tc.get('command', '')
            if interval <= 0 or not command:
                continue
            t = QTimer(self)
            t.setInterval(interval * 1000)
            t.timeout.connect(lambda cmd=command: self._timer_fire(cmd))
            t.start()
            self._timers[name or str(id(t))] = t

    def _timer_fire(self, command: str):
        self._exec_body(command, [''], '')

    def _stop_all_timers(self):
        for t in self._timers.values():
            t.stop()
        self._timers.clear()
