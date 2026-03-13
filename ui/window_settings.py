"""
window_settings.py — persist window geometry and dock state.

Saves to ~/.config/mud-client/window_settings.json.
"""

from __future__ import annotations

import json
import os

from PyQt6.QtCore    import QByteArray
from PyQt6.QtWidgets import QWidget

_FILE = os.path.join(
    os.path.expanduser("~"), ".config", "mud-client", "window_settings.json"
)


def load_settings() -> dict:
    try:
        with open(_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    with open(_FILE, "w") as f:
        json.dump(data, f, indent=2)


# Short aliases used by main_window inline imports
_load = load_settings
_save = save_settings


def save_geometry(key: str, widget: QWidget) -> None:
    """Persist widget's current geometry under key."""
    data = load_settings()
    data[key] = widget.saveGeometry().toHex().data().decode()
    save_settings(data)


def restore_geometry(key: str, widget: QWidget) -> bool:
    """Restore widget's geometry from key. Returns True if found."""
    data = load_settings()
    hex_str = data.get(key)
    if not hex_str:
        return False
    return widget.restoreGeometry(QByteArray.fromHex(hex_str.encode()))
