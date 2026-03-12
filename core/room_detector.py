"""
TorilRoomDetector — parses MUD output to detect room name + exits.

Toril room format (one room view):
  [whitespace lines]        ← mini-map (all lines start with whitespace)
  Room Name                 ← first non-indented non-blank line
     Description line...    ← indented with spaces (optional, can be disabled)
  Exits: - North - East     ← exits line
  A mob stands here.        ← mobs/items (after exits)
  < 462h/462H 149v/150V >   ← prompt

We detect: room name + parsed exit set, then call on_room_detected(name, exits).
"""

from __future__ import annotations

import re
import logging
from typing import Callable, Optional

_log = logging.getLogger("mudclient.room_detector")

# ── Patterns ──────────────────────────────────────────────────────────────────

# Toril prompt: < 462h/462H 149v/150V P: std >
PROMPT_RE = re.compile(r'^<\s*\d+h/\d+H')

# Exits: - North - East  - South - West
# Also handles: Exits: North, East (alternate format)
EXITS_RE  = re.compile(r'^Exits:\s*(.+)', re.IGNORECASE)

# A line that is part of the mini-map: starts with whitespace (tabs or spaces)
# OR is effectively blank/empty
INDENTED_RE = re.compile(r'^[\s\t]')

# Lines that look like they could be a room name — used to reject obvious non-names
# Room names in Toril are Title Cased or All Caps, not sentences ending in punctuation
# We accept anything that passes the basic filter and reject known false positives
_REJECT_RE  = re.compile(
    r'^('
    r'You |You\'re |You\'ve |'         # player messages
    r'The (sun|moon|stars)|'           # sky messages
    r'\w+ (says|tells|asks|shouts)|'   # speech
    r'Your |HP:|Mana:|'                # stat lines
    r'\[|\(|--|=='                     # bracketed / separator lines
    r')', re.IGNORECASE
)

# Standard compass directions Toril uses
_STANDARD_DIRS = frozenset({
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
    "up", "down", "in", "out",
})


def _parse_exits(exits_str: str) -> frozenset[str]:
    """
    Parse '- North - East  - South - West' or 'North, East' into a frozenset.
    """
    # Find all words preceded by optional '- ' separator
    dirs = re.findall(r'(?:[-–]\s*)?(\w+)', exits_str)
    result = set()
    for d in dirs:
        d_low = d.lower()
        if d_low in _STANDARD_DIRS:
            result.add(d_low)
    return frozenset(result)


# ── Detector class ─────────────────────────────────────────────────────────────

class TorilRoomDetector:
    """
    Feed every plain-text MUD line into feed_line().
    When a complete room (name + exits) is detected, on_room_detected is called.

    Callback signature:  on_room_detected(name: str, exits: frozenset[str])
    """

    # States
    _IDLE        = "idle"         # waiting for any input / after prompt
    _IN_MINIMAP  = "in_minimap"   # consuming whitespace-indented minimap lines
    _HAVE_NAME   = "have_name"    # got room name, looking for exits
    _DONE        = "done"         # exits seen, waiting for next prompt

    def __init__(self, on_room_detected: Callable[[str, frozenset], None]):
        self._callback   = on_room_detected
        self._state      = self._IDLE
        self._name: Optional[str] = None
        self._exits: Optional[frozenset] = None
        self._saw_minimap = False
        self._forced_sync = False   # set by #map find — skip adjacency bias

    def force_next_sync(self):
        """Call before sending 'look' — the next detection ignores near_id."""
        self._forced_sync = True
        self._reset()

    @property
    def sync_pending(self) -> bool:
        """True when #map find has been issued and we're waiting for room data."""
        return self._forced_sync

    def feed_line(self, plain: str):
        """
        Feed a single plain-text (ANSI stripped) line.
        Call with the raw line including leading whitespace.
        """
        # ── Prompt always resets the state machine ────────────────────────────
        if PROMPT_RE.match(plain):
            self._reset()
            return

        # ── Exits line: finalize the room ─────────────────────────────────────
        m = EXITS_RE.match(plain)
        if m:
            exits = _parse_exits(m.group(1))
            if self._name and (self._state == self._HAVE_NAME):
                self._exits  = exits
                forced       = self._forced_sync
                self._forced_sync = False
                _log.debug("Room detected: %r  exits=%s  forced=%s",
                           self._name, sorted(exits), forced)
                self._callback(self._name, exits, forced)
                self._state  = self._DONE
            else:
                # Exits seen but no name yet — unusual, just store
                self._exits = exits
            return

        # ── State: IDLE / IN_MINIMAP ──────────────────────────────────────────
        if self._state in (self._IDLE, self._IN_MINIMAP, self._DONE):
            if INDENTED_RE.match(plain) or not plain.strip():
                # Whitespace or blank → mini-map territory
                if self._state == self._IDLE:
                    self._state = self._IN_MINIMAP
                return
            else:
                # Non-indented non-blank line after mini-map or prompt
                if self._state in (self._IN_MINIMAP, self._IDLE):
                    candidate = plain.strip()
                    if self._is_room_name(candidate):
                        self._name = candidate
                        self._state = self._HAVE_NAME
                        _log.debug("Room name candidate: %r", self._name)
                # In DONE state, a non-indented line after exits = mob — ignore
                return

        # ── State: HAVE_NAME ─────────────────────────────────────────────────
        if self._state == self._HAVE_NAME:
            stripped = plain.strip()
            if not stripped:
                return   # blank line — keep waiting

            # Description lines are indented with 3+ spaces
            if INDENTED_RE.match(plain):
                return   # description continuation — skip

            # Non-indented, non-exits line before exits:
            # Could be a second-line room name? Very unlikely on Toril.
            # Treat as noise and keep waiting.
            return

    def _is_room_name(self, line: str) -> bool:
        """Heuristic: does this line look like a Toril room name?"""
        if not line:
            return False
        if len(line) > 80:
            return False
        if _REJECT_RE.match(line):
            return False
        # Must start with a capital letter
        if not line[0].isupper():
            return False
        # Reject lines ending with common mob suffixes
        if line.endswith((' here.', ' here!', ' here?')):
            return False
        if line.endswith(('.', '!', '?')) and len(line.split()) > 5:
            return False
        return True

    def _reset(self):
        self._state     = self._IDLE
        self._name      = None
        self._exits     = None
        self._saw_minimap = False
