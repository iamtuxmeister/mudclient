"""
window_settings.py — persist window geometry across sessions.

Saves to ~/.config/mud-client/window_settings.json as plain hex strings
produced by QWidget.saveGeometry(), which encodes position, size, and
maximised/fullscreen state in a Qt-portable binary blob.
"""

from __future__ import annotations

import json
import os

from PyQt6.QtCore  import QByteArray
from PyQt6.QtWidgets import QWidget

_FILE = os.path.join(
    os.path.expanduser("~"), ".config", "mud-client", "window_settings.json"
)


def _load() -> dict:
    try:
        with open(_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    with open(_FILE, "w") as f:
        json.dump(data, f, indent=2)


def save_geometry(key: str, widget: QWidget) -> None:
    """Persist *widget*'s current geometry under *key*."""
    data = _load()
    data[key] = widget.saveGeometry().toHex().data().decode()
    _save(data)


def restore_geometry(key: str, widget: QWidget) -> bool:
    """
    Restore *widget*'s geometry from *key*.
    Returns True if geometry was found and applied.
    """
    data = _load()
    hex_str = data.get(key)
    if not hex_str:
        return False
    blob = QByteArray.fromHex(hex_str.encode())
    return widget.restoreGeometry(blob)
