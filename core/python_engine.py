"""
PythonEngine — full Python execution environment for trigger bodies.

Architecture
------------
A single persistent namespace (like a module) lives for the lifetime of
the session.  Every Python trigger body is exec()'d into that namespace,
so you can:

    import requests                 # once, in any trigger body
    DB = sqlite3.connect("mud.db")  # persists across calls

Injected globals (always available, no import needed):
    send(*cmds)           send one or more commands to the MUD
    showme(text, win='')  display ANSI text; win='' → main, 'info'/'log'/'map' → panel
    log(text)             showme to main with italic client styling
    gag()                 suppress the triggering line from output
    m                     list of captures: m[0]=whole line, m[1]=first group, …
    m1 … m9               shorthand aliases for m[1] … m[9]
    raw                   original ANSI-coloured line string
    vars                  dict shared with TinTin++ #var system (mutable)

Detection
---------
A trigger body that begins with the line  #python  (or  # python ) is
executed as Python.  Everything after that first line is the code.

Example trigger body
--------------------
    #python
    xp = int(m1.replace(',', ''))
    showme(f"\\x1b[33mYou received {xp:,} XP ({m2})\\x1b[0m")
    if xp > 5000:
        send("say HUGE XP GAIN!")
    vars['total_xp'] = vars.get('total_xp', 0) + xp

Web / database example
-----------------------
    #python
    import requests, threading
    def _check():
        r = requests.get("https://example.com/api/xp", timeout=5)
        showme(f"API says: {r.text[:80]}")
    threading.Thread(target=_check, daemon=True).start()

venv support
------------
If  <project>/venv/  exists its site-packages are automatically added to
sys.path so you can:
    cd mudclient
    python -m venv venv
    venv/bin/pip install requests aiohttp psycopg2 ...
"""

from __future__ import annotations

import builtins
import sys
import traceback
import logging

_engine_log = logging.getLogger("mudclient.python_engine")


# ── Sentinel exception used to implement gag() ──────────────────────────────

class _GagException(BaseException):
    """Raised (not thrown — used via a flag) to signal gag from Python code."""


# ── PythonEngine ─────────────────────────────────────────────────────────────

class PythonEngine:
    """
    Owns a persistent Python namespace and exec()'s trigger bodies into it.
    Thread-safety: must be called from the GUI (main) thread only.
    """

    def __init__(self,
                 send_fn,        # callable(str) → send command to MUD
                 showme_fn,      # callable(target:str, ansi:str)
                 local_echo_fn,  # callable(ansi:str) → italic client message
                 ):
        self._send_fn       = send_fn
        self._showme_fn     = showme_fn
        self._local_echo_fn = local_echo_fn
        self._ns: dict      = {}
        self._bootstrap()

    # ── Public API ───────────────────────────────────────────────────

    def exec_body(self,
                  code: str,
                  captures: list[str],
                  raw_ansi: str,
                  variables: dict[str, str],
                  ) -> bool:
        """
        Execute one Python trigger body.

        Parameters
        ----------
        code      : Python source (first '#python' line already stripped)
        captures  : [whole_line, group1, group2, …]
        raw_ansi  : original ANSI-coloured line
        variables : live TinTin++ variable dict (mutated in-place)

        Returns
        -------
        True  if gag() was called inside the body
        """
        gag_called = [False]

        # ── Build per-invocation helpers that close over current state ────

        send_fn   = self._send_fn
        showme_fn = self._showme_fn
        echo_fn   = self._local_echo_fn

        def _send(*commands):
            for cmd in commands:
                send_fn(str(cmd))

        def _showme(text, window=''):
            showme_fn(str(window).lower(), str(text))

        def _log(text):
            echo_fn(f"\x1b[2;37m{text}\x1b[0m")

        def _gag():
            gag_called[0] = True

        def _var_get(name, default=''):
            return variables.get(str(name), default)

        def _var_set(name, value):
            variables[str(name)] = str(value)

        # ── Refresh mutable state in namespace ────────────────────────────
        ns = self._ns

        # Expose TinTin++ variables via var_get/var_set only — don't clobber
        # the Python vars dict (which holds typed Python values) with strings.
        # Use var_get('name') / var_set('name', val) to cross the boundary.

        # Per-call bindings
        ns['send']   = _send
        ns['showme'] = _showme
        ns['echo']   = _showme
        ns['log']    = _log
        ns['gag']    = _gag
        ns['var_get'] = _var_get
        ns['var_set'] = _var_set

        # Capture groups
        ns['m'] = captures
        for i in range(1, 10):
            ns[f'm{i}'] = captures[i] if i < len(captures) else ''
        ns['raw'] = raw_ansi

        # ── Execute ───────────────────────────────────────────────────────
        try:
            compiled = compile(code, '<mud_trigger>', 'exec')
            exec(compiled, ns)
        except SystemExit:
            pass   # don't let sys.exit() kill the client
        except Exception:
            tb_text = traceback.format_exc()
            # Show error in red in the output window
            self._local_echo_fn(
                f"\x1b[31m[Python trigger error]\x1b[0m\n"
                f"\x1b[31m{tb_text}\x1b[0m"
            )
            _engine_log.error("Python trigger raised:\n%s", tb_text)

        # var_set() already wrote directly into variables dict during exec.
        return gag_called[0]

    def reset_namespace(self):
        """Nuke and rebuild the persistent namespace (e.g. on session change)."""
        self._bootstrap()

    # ── Internal ─────────────────────────────────────────────────────

    def _bootstrap(self):
        """Build the initial persistent namespace."""
        # Preserve vars dict across resets so session state survives reload
        old_vars = self._ns.get('vars', {})

        self._ns = {
            '__builtins__': builtins,
            '__name__':     'mud_session',
            '__doc__':      'MUD client Python trigger namespace',
            'vars':         old_vars,
            # Placeholders — overwritten on each exec_body call
            'm':    [],
            'raw':  '',
            'send':    lambda *a: None,
            'showme':  lambda t, w='': None,
            'echo':    lambda t, w='': None,
            'log':     lambda t: None,
            'gag':     lambda: None,
            'var_get': lambda n, d='': d,
            'var_set': lambda n, v: None,
        }

        # Add venv site-packages if present (project-local venv)
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        venv_site = os.path.join(project_root, 'venv', 'lib')
        if os.path.isdir(venv_site):
            import glob
            for sp in glob.glob(os.path.join(venv_site, 'python*', 'site-packages')):
                if sp not in sys.path:
                    sys.path.insert(0, sp)
                    _engine_log.debug("Added venv site-packages: %s", sp)


# ── Module-level helper ──────────────────────────────────────────────────────

_PYTHON_SENTINELS = ('#python', '# python')

def is_python_body(body: str) -> bool:
    """Return True if the trigger body is a Python block."""
    first = body.lstrip().split('\n', 1)[0].strip().lower()
    return first in _PYTHON_SENTINELS

def strip_python_sentinel(body: str) -> str:
    """Remove the leading #python line and return the actual code."""
    lines = body.lstrip().split('\n', 1)
    return lines[1] if len(lines) > 1 else ''
