"""
MapGraph — lightweight in-memory map built from GMCP Room.Info packets.

Stores rooms and exits.  Provides a simple ASCII renderer used by MapWidget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Room:
    vnum:  int
    name:  str = ""
    area:  str = ""
    exits: dict[str, int] = field(default_factory=dict)   # dir → dest_vnum


class MapGraph:
    """Simple room graph built incrementally from GMCP data."""

    DIR_ABBREV = {
        "north": "N", "south": "S", "east": "E", "west": "W",
        "up":    "U", "down":  "D", "northeast": "NE", "northwest": "NW",
        "southeast": "SE", "southwest": "SW",
    }

    def __init__(self):
        self._rooms:   dict[int, Room] = {}
        self._current: Optional[int]   = None

    def update(self, data: dict):
        """Ingest a Room.Info GMCP payload dict."""
        vnum = data.get("vnum") or data.get("num")
        if vnum is None:
            return
        try:
            vnum = int(vnum)
        except (ValueError, TypeError):
            return

        room = self._rooms.get(vnum)
        if room is None:
            room = Room(vnum=vnum)
            self._rooms[vnum] = room

        room.name = str(data.get("name", room.name))
        room.area = str(data.get("area", room.area))

        exits_raw = data.get("exits", {})
        if isinstance(exits_raw, dict):
            for d, dest in exits_raw.items():
                try:
                    room.exits[d.lower()] = int(dest)
                except (ValueError, TypeError):
                    pass

        self._current = vnum

    @property
    def current(self) -> Optional[Room]:
        if self._current is None:
            return None
        return self._rooms.get(self._current)

    def render_ascii(self, width: int = 40, height: int = 20) -> str:
        """
        Very simple ASCII map showing the current room and immediate exits.
        """
        room = self.current
        if room is None:
            return "(no map data)"

        lines: list[str] = []
        lines.append(f"[{room.vnum}] {room.name[:30]}")
        lines.append(f"Area: {room.area[:34]}")
        lines.append("")

        # Compass rose
        exits = room.exits
        N  = "N"  if "north"     in exits else "."
        S  = "S"  if "south"     in exits else "."
        E  = "E"  if "east"      in exits else "."
        W  = "W"  if "west"      in exits else "."
        U  = "U"  if "up"        in exits else "."
        D  = "D"  if "down"      in exits else "."
        NE = "NE" if "northeast" in exits else ".."
        NW = "NW" if "northwest" in exits else ".."
        SE = "SE" if "southeast" in exits else ".."
        SW = "SW" if "southwest" in exits else ".."

        lines.append(f"  {NW}  {N}  {NE}")
        lines.append(f"  {W}  [*]  {E}")
        lines.append(f"  {SW}  {S}  {SE}")
        lines.append(f"        {U}/{D}")
        lines.append("")

        for d, dest in sorted(exits.items()):
            dest_room = self._rooms.get(dest)
            dest_name = dest_room.name if dest_room else f"#{dest}"
            lines.append(f"  {d.capitalize():<10} → {dest_name[:22]}")

        return "\n".join(lines)

    def clear(self):
        self._rooms.clear()
        self._current = None


def try_parse_gmcp_line(package: str, payload: object) -> Optional[dict]:
    """
    Return a room-info dict if *package* looks like a Room.Info packet,
    else None.
    """
    if not isinstance(package, str):
        return None
    p = package.lower()
    if p in ("room.info", "room.char"):
        if isinstance(payload, dict):
            return payload
    return None
