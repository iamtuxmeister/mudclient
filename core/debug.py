"""
debug.py — lightweight debug logger for MUD client.

Activated by passing -d or --debug on the command line.
All output goes to stderr so it stays separate from any stdout piping.

Usage:
    python main.py -d
    python main.py --debug

In code:
    from core.debug import dbg
    dbg("telnet", "connected to {host}:{port}")
    dbg("iac", f"WILL {opt}")
"""

import sys
import time
import threading

_enabled   = False
_start     = time.monotonic()
_lock      = threading.Lock()

# ANSI colours for categories (shown in terminals that support them)
_COLORS = {
    "telnet":  "\033[36m",   # cyan
    "iac":     "\033[33m",   # yellow
    "mccp":    "\033[35m",   # magenta
    "gmcp":    "\033[34m",   # blue
    "data":    "\033[32m",   # green
    "gui":     "\033[37m",   # white
    "error":   "\033[31m",   # red
    "script":  "\033[96m",   # bright cyan
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"


def configure(enabled: bool):
    global _enabled, _start
    _enabled = enabled
    _start   = time.monotonic()
    if enabled:
        _write("debug", "Debug mode ON — logging to stderr")
        # Route the trigger logger to our stderr output
        import logging
        class _TrigHandler(logging.Handler):
            def emit(self, record):
                _write("trigger", record.getMessage())
        _h = _TrigHandler()
        logging.getLogger("mudclient.triggers").setLevel(logging.DEBUG)
        logging.getLogger("mudclient.triggers").addHandler(_h)


def dbg(category: str, message: str):
    if not _enabled:
        return
    _write(category, message)


def _write(category: str, message: str):
    elapsed = time.monotonic() - _start
    color   = _COLORS.get(category, "\033[0m")
    thread  = threading.current_thread().name
    with _lock:
        print(
            f"{_BOLD}[{elapsed:8.3f}]{_RESET} "
            f"{color}[{category:7s}]{_RESET} "
            f"\033[2m({thread})\033[0m "
            f"{message}",
            file=sys.stderr,
            flush=True,
        )
