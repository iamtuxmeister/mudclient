"""RightPanel — collapsible panel with a map pane and text info panes."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTabWidget, QTextEdit, QLabel, QSizePolicy,
)
from PyQt6.QtGui import QFont, QTextOption

from ui.map_widget import MapWidget


class RightPanel(QWidget):
    """
    A vertical panel containing:
      Tab 0 — ASCII map (MapWidget)
      Tab 1 — "Info" free-text pane (status messages from triggers)
      Tab 2 — "Log"  scrolling event log
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(160)
        self.setMaximumWidth(400)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                background: #1a1a1a; color: #888;
                padding: 4px 8px; font-size: 9pt;
            }
            QTabBar::tab:selected { background: #222; color: #ccc; }
        """)
        layout.addWidget(self._tabs)

        # Map tab
        self.map_widget = MapWidget()
        self._tabs.addTab(self.map_widget, "Map")

        # Info tab
        self._info = self._make_pane()
        self._tabs.addTab(self._info, "Info")

        # Log tab
        self._log = self._make_pane()
        self._tabs.addTab(self._log, "Log")

    def _make_pane(self) -> QTextEdit:
        w = QTextEdit()
        w.setReadOnly(True)
        w.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        f = QFont("Monospace", 9)
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        w.setFont(f)
        w.setStyleSheet("""
            QTextEdit { background: #0d0d0d; color: #aaa; border: none; padding: 4px; }
        """)
        return w

    def update_map(self, ascii_text: str):
        self.map_widget.update_map(ascii_text)
        self._tabs.setCurrentIndex(0)

    def write_info(self, text: str):
        self._info.append(text)
        self._tabs.setCurrentIndex(1)

    def write_log(self, text: str):
        self._log.append(text)
