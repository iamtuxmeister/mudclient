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
import logging
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.python_engine import PythonEngine, is_python_body, strip_python_sentinel

_trig_log = logging.getLogger("mudclient.triggers")


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
    """Split on ';' or newline but not inside {} braces."""
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
        elif ch in (';', '\n') and depth == 0:
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
      %0        whole matched line
      %1–%9     numbered regex capture groups  (TinTin++ style)
      $1–$9     same numbered captures         (PCRE / user-friendly style)
      $name or ${name}  variable lookup
    """
    # %0–%9 (TinTin++ style)
    for idx in range(min(10, len(captures))):
        text = text.replace(f'%{idx}', captures[idx] if captures[idx] is not None else '')

    # $1–$9 (PCRE style) — must run before $name so $10 etc don't mangle
    def _cap(m: re.Match) -> str:
        idx = int(m.group(1))
        return captures[idx] if idx < len(captures) else ''
    text = re.sub(r'\$([1-9])', _cap, text)

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

    send_command   = pyqtSignal(str)   # command from alias / direct body
    triggered_send = pyqtSignal(str)   # command fired by a trigger
    local_echo     = pyqtSignal(str)
    showme       = pyqtSignal(str, str)   # (window-target, ansi-coloured text)
    gui_message  = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folders:    list[dict] = []   # unified folder model
        self._aliases:    list[dict] = []   # extracted from folders
        self._triggers:   list[tuple]= []   # (folder_enabled, trig_dict) extracted
        self._timers_cfg: list[dict] = []   # extracted from folders
        self._buttons:    list[dict] = []   # extracted from folders
        self._in_trigger: bool = False
        self._in_alias:   int  = 0   # depth counter; prevents alias→alias recursion
        self._highlights: list[dict] = []
        self._variables:  dict[str, str] = {}
        self._timers:     dict[str, QTimer] = {}
        self._cmd_sep:    str = ';'

        # Python execution environment
        self._python = PythonEngine(
            send_fn       = self._emit_triggered_send,
            showme_fn     = self.showme.emit,
            local_echo_fn = self._emit_ansi_echo,
        )

    # ── Config ───────────────────────────────────────────────────────

    def load_config(self, config: dict):
        self._folders    = list(config.get('folders', []))
        self._highlights = list(config.get('highlights', []))
        self._cmd_sep    = config.get('cmd_separator', ';') or ';'
        # Extract typed items from unified folder model
        self._aliases    = []
        self._triggers   = []
        self._timers_cfg = []
        self._buttons    = []
        self._variables  = {}
        root_enabled = True
        root = next((f for f in self._folders if f.get("_root")), None)
        if root:
            root_enabled = root.get("enabled", True)
        for folder in self._folders:
            folder_enabled = folder.get("enabled", True) and root_enabled
            for item in folder.get("items", []):
                itype = item.get("type", "")
                if itype == "alias" and item.get("enabled", True):
                    # normalise: alias uses "match" as the command word
                    self._aliases.append({
                        "name":    item.get("match") or item.get("name",""),
                        "body":    item.get("body",""),
                        "enabled": folder_enabled,
                    })
                elif itype == "trigger" and item.get("enabled", True):
                    self._triggers.append((folder_enabled, item))
                elif itype == "variable":
                    n = item.get("name","").strip()
                    if n:
                        self._variables[n] = item.get("value","")
                elif itype == "timer" and item.get("enabled", True):
                    self._timers_cfg.append(item)
                elif itype == "button":
                    self._buttons.append(item)
        self._restart_timers(self._timers_cfg)

    def set_cmd_sep(self, sep: str):
        self._cmd_sep = sep or ';'

    def stop(self):
        self._stop_all_timers()

    def clear(self):
        self.stop()
        self._folders.clear()
        self._aliases.clear()
        self._triggers.clear()
        self._timers_cfg.clear()
        self._buttons.clear()
        self._highlights.clear()
        self._variables.clear()
        self._python.reset_namespace()

    # ── Python engine helpers ────────────────────────────────────────

    def _emit_triggered_send(self, cmd: str):
        """Used by PythonEngine.send() — always treated as triggered."""
        self.triggered_send.emit(cmd)

    def _emit_ansi_echo(self, ansi: str):
        """Used by PythonEngine.log() — route as local_echo."""
        self.local_echo.emit(ansi)

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
            self._in_alias += 1
            try:
                self._exec_body(body, captures, '')
            finally:
                self._in_alias -= 1
            return True
        return False

    # ── Public: trigger processing ───────────────────────────────────

    def process_line(self, plain: str, raw_ansi: str = '') -> bool:
        """
        Run all enabled triggers against one line of MUD output.
        Returns True if any trigger gagged the line.

        plain    — ANSI-stripped text (for matching)
        raw_ansi — original coloured text (available as %%raw in scripts)

        """
        gagged = False

        # self._triggers is pre-filtered list of (folder_enabled, trig_dict)
        # root kill-switch already embedded via folder_enabled=False in load_config
        for folder_enabled, trig in self._triggers:
            if not folder_enabled:
                continue
            patterns = trig.get('patterns', [])
            if not patterns:
                continue
            body = trig.get('body', '')
            for pattern in patterns:
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
                        _trig_log.debug("TRIGGER HIT  trig=%r  pattern=%r  plain=%r  captures=%r",
                                        trig.get('name','?'), pattern, plain, captures)
                    else:
                        _trig_log.debug("TRIGGER MISS trig=%r  pattern=%r  plain=%r",
                                        trig.get('name','?'), pattern, plain)
                except re.error as _re_err:
                    _trig_log.debug("TRIGGER RE.ERROR trig=%r  pattern=%r  error=%s  "
                                    "plain=%r  fallback-match=%r",
                                    trig.get('name','?'), pattern, _re_err, plain,
                                    pattern in plain)
                    if pattern in plain:
                        matched  = True
                        captures = [plain]
                if matched:
                    _trig_log.debug("TRIGGER EXEC trig=%r  body_raw=%r",
                                    trig.get('name','?'), body)
                    if is_python_body(body):
                        # ── Python path ───────────────────────────────
                        code = strip_python_sentinel(body)
                        _trig_log.debug("TRIGGER PYTHON trig=%r  code=%r", trig.get('name','?'), code[:120])
                        hit = self._python.exec_body(
                            code, captures, raw_ansi, self._variables)
                        gagged = gagged or hit
                    else:
                        # ── TinTin++ path ─────────────────────────────
                        b = _subst(body, self._variables, captures)
                        b = b.replace('%%raw', raw_ansi)
                        _trig_log.debug("TRIGGER TINTIN trig=%r  body_subst=%r", trig.get('name','?'), b)
                        self._in_trigger = True
                        try:
                            gagged = self._exec_body(b, captures, raw_ansi) or gagged
                        finally:
                            self._in_trigger = False
                    break

        return gagged

    # ── Public: highlights ───────────────────────────────────────────

    def get_highlights(self) -> list[dict]:
        return list(self._highlights)

    def get_buttons(self) -> list[dict]:
        """Return enabled button items for the ButtonBar.
        Returns dicts with keys: label, color, body, enabled.
        """
        return [b for b in self._buttons if b.get("enabled", True)]

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
                # Bare text: if we're not already inside an alias body, try
                # alias expansion (lets triggers call aliases by name).
                # _in_alias guards against infinite alias→alias recursion.
                if not self._in_alias and self.process_alias(cmd):
                    pass  # alias handled it
                else:
                    if self._in_trigger:
                        self.triggered_send.emit(cmd)
                    else:
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
        _trig_log.debug("EXEC_CMD keyword=%r  rest=%r  args=%r", keyword, rest, args)

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
                    if self._in_trigger:
                        self.triggered_send.emit(part)
                    else:
                        self.send_command.emit(part)
            return False

        if keyword in ('showme', 'echo'):
            # TinTin++ syntax:
            #   #showme {text}           → main window, text is de-braced
            #   #showme {text} {window}  → named panel, both de-braced
            #   #showme bare text        → main window, whole rest is text
            #   #showme bare text $1 $2  → same (no window arg possible without braces)
            if rest.startswith('{'):
                # brace-delimited — first arg is text, optional second is window
                ansi_text = args[0] if args else ''
                target    = args[1].lower() if len(args) > 1 else ''
            else:
                # no braces — entire rest is the message (substitution already applied)
                ansi_text = rest
                target    = ''
            ansi_text = _subst(ansi_text, self._variables, captures)
            ansi_text = tt_color_to_ansi(ansi_text)
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
            command  = tc.get('body', tc.get('command', ''))
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
