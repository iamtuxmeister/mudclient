#!/usr/bin/env python3
"""
MUD Client — entry point.

Usage:
    python main.py          # normal mode
    python main.py -d       # debug mode: verbose logging to stderr
    python main.py --debug  # same as -d

In debug mode every telnet IAC negotiation, socket recv, GMCP packet,
MCCP2 event, and GUI slot call is printed to stderr with timestamps.
"""

import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Parse -d / --debug before QApplication sees argv ────────────────
_debug = "-d" in sys.argv or "--debug" in sys.argv
# Remove our flag so Qt doesn't complain about unknown args
_qt_argv = [a for a in sys.argv if a not in ("-d", "--debug")]

import core.debug as _debug_mod
_debug_mod.configure(_debug)

from core.debug import dbg

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore    import Qt

from ui.main_window import MainWindow


def main():
    dbg("gui", f"QApplication starting  debug={_debug}")

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(_qt_argv)
    app.setApplicationName("MUD Client")
    app.setOrganizationName("mud-client")
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QToolTip {
            background: #1a2e42;
            color: #cceeff;
            border: 1px solid #2a5a7a;
            padding: 2px 8px;
            font-size: 11px;
            border-radius: 3px;
        }
    """)

    dbg("gui", "creating MainWindow")
    win = MainWindow()
    win.show()
    dbg("gui", "entering Qt event loop")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
