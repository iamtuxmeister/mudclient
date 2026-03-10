# MUD Client

A full-featured MUD (Multi-User Dungeon) client written in Python 3 with PyQt6.

## Features

- **Telnet** — full IAC negotiation (WILL/WONT/DO/DONT), TTYPE, NAWS
- **MCCP2** — Mud Client Compression Protocol v2 (zlib); status shown in status bar
- **GMCP** — Generic Mud Communication Protocol; Room.Info drives the ASCII map panel
- **ANSI** — full SGR colour/style support: 8-colour, 256-colour, 24-bit RGB
- **Scripting** — client-side aliases, trigger actions, interval timers, variables
- **Tab completion** — completes from recently-seen MUD output words
- **Command history** — Up/Down with prefix-search
- **Sessions** — saved connection profiles stored in `~/.config/mud-client/sessions.json`
- **Config dialog** — edit aliases, actions, timers, macro buttons, highlights per-session
- **Macro button bar** — up to 12 configurable one-click command buttons
- **Right panel** — tabbed pane with ASCII map, info pane, and GMCP log

## Requirements

- Python 3.10+
- PyQt6

## Installation

```bash
pip install PyQt6
```

## Running

```bash
python main.py
```

## Quick start

1. **File → Sessions…** (`Ctrl+Shift+N`) — create a session with a host and port, click Connect.
2. **File → Quick Connect…** (`Ctrl+O`) — connect directly without saving.
3. **Tools → Config…** (`Ctrl+,`) — add aliases, triggers, timers, and macro buttons.

## Keyboard shortcuts

| Shortcut     | Action                        |
|------------- |-------------------------------|
| Ctrl+Shift+N | Session manager               |
| Ctrl+O       | Quick connect                 |
| Ctrl+R       | Reconnect                     |
| Ctrl+D       | Disconnect                    |
| Ctrl+,       | Open config dialog            |
| Ctrl+L       | Clear output                  |
| Ctrl+=       | Increase font size            |
| Ctrl+-       | Decrease font size            |
| Ctrl+End     | Scroll output to bottom       |
| Ctrl+Q       | Quit                          |
| ↑ / ↓        | Command history (prefix-aware)|
| Tab          | Tab-complete from MUD words   |

## MCCP2 (compression)

When the server offers MCCP2 (`IAC WILL 86`), the client automatically accepts
and begins decompressing the stream with zlib.  The status bar shows **MCCP2: ✓**
in green when compression is active.

## Session config format (`sessions.json`)

```json
[
  {
    "name": "My MUD",
    "host": "mymud.example.com",
    "port": 4000,
    "config": {
      "aliases": [
        {"name": "k",  "body": "kill %1",  "enabled": true}
      ],
      "actions": [
        {"pattern": "You are hungry",  "command": "eat bread", "gui_target": "", "enabled": true}
      ],
      "timers": [
        {"name": "regen", "interval": 30, "command": "rest", "enabled": false}
      ],
      "buttons": [
        {"label": "North", "command": "north", "enabled": true}
      ],
      "highlights": [],
      "variables": []
    }
  }
]
```

## Project structure

```
mud-client/
├── main.py                  Entry point
├── requirements.txt
├── README.md
├── core/
│   ├── __init__.py
│   ├── telnet_worker.py     Telnet + MCCP2 + GMCP (QThread)
│   ├── ansi_parser.py       ANSI SGR → QTextCharFormat
│   ├── script_engine.py     Aliases, triggers, timers
│   └── map_parser.py        GMCP Room.Info → ASCII map
└── ui/
    ├── __init__.py
    ├── main_window.py       Top-level window
    ├── output_widget.py     ANSI terminal display
    ├── map_widget.py        ASCII map pane
    ├── right_panel.py       Tabbed right panel
    ├── button_bar.py        Macro button row
    ├── session_manager.py   Session pick/create dialog
    └── config_dialog.py     Alias/action/timer editor
```
