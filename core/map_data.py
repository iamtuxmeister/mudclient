"""
MapData — data model for Mudlet-format JSON maps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger("mudclient.map_data")

STANDARD_DIRS = frozenset({
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
    "up", "down", "in", "out",
})

DIR_VECTOR: dict[str, tuple[int, int]] = {
    "north":     ( 0,  1),
    "south":     ( 0, -1),
    "east":      ( 1,  0),
    "west":      (-1,  0),
    "northeast": ( 1,  1),
    "northwest": (-1,  1),
    "southeast": ( 1, -1),
    "southwest": (-1, -1),
}


@dataclass
class MRoom:
    id:           int
    coords:       tuple[int, int, int]
    exits:        dict[str, int]
    environment:  int  = -1
    locked:       bool = False
    name:         str  = ""
    area_id:      int  = 0

    @property
    def x(self): return self.coords[0]
    @property
    def y(self): return self.coords[1]
    @property
    def z(self): return self.coords[2]

    def is_special_exit(self, direction: str) -> bool:
        return direction not in STANDARD_DIRS


@dataclass
class MArea:
    id:    int
    name:  str
    rooms: dict[int, MRoom] = field(default_factory=dict)

    def z_levels(self) -> list[int]:
        return sorted({r.z for r in self.rooms.values()})

    def rooms_at_z(self, z: int) -> list[MRoom]:
        return [r for r in self.rooms.values() if r.z == z]


class MapData:
    def __init__(self):
        self.areas:      dict[int, MArea] = {}
        self.rooms:      dict[int, MRoom] = {}
        self.current_id: Optional[int]    = None
        self._path:      Optional[str]    = None
        self._gmcp_exits: dict[str, int]  = {}
        self._gmcp_name:  str             = ""
        self._gmcp_area:  str             = ""

    # ── Loading ───────────────────────────────────────────────────────

    def load_json(self, path: str) -> tuple[bool, str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return False, f"Cannot open map file: {e}"

        areas_raw = data.get("areas", [])
        if not areas_raw:
            return False, "No areas found in map file"

        new_areas: dict[int, MArea] = {}
        new_rooms: dict[int, MRoom] = {}

        for area_raw in areas_raw:
            area_id   = int(area_raw.get("id", 0))
            area_name = str(area_raw.get("name", f"Area {area_id}"))
            area      = MArea(id=area_id, name=area_name)

            for room_raw in area_raw.get("rooms", []):
                try:
                    room_id = int(room_raw["id"])
                    coords  = tuple(int(c) for c in room_raw["coordinates"])
                    exits   = {}
                    for ex in room_raw.get("exits", []):
                        name   = str(ex.get("name", "")).strip()
                        dest   = int(ex.get("exitId", 0))
                        if name and dest:
                            exits[name] = dest
                    room = MRoom(
                        id          = room_id,
                        coords      = coords,           # type: ignore[arg-type]
                        exits       = exits,
                        environment = int(room_raw.get("environment", -1)),
                        locked      = bool(room_raw.get("locked", False)),
                        name        = str(room_raw.get("name", "")),
                        area_id     = area_id,
                    )
                    area.rooms[room_id]  = room
                    new_rooms[room_id]   = room
                except (KeyError, ValueError, TypeError) as e:
                    _log.warning("Skipping bad room in area %r: %s", area_name, e)

            new_areas[area_id] = area

        self.areas = new_areas
        self.rooms = new_rooms
        self._path = path
        _log.info("Loaded map: %d areas, %d rooms from %s",
                  len(new_areas), len(new_rooms), path)
        return True, f"Loaded {len(new_rooms):,} rooms in {len(new_areas)} areas"

    # ── GMCP ─────────────────────────────────────────────────────────

    def on_gmcp_room(self, data: dict) -> bool:
        vnum = data.get("vnum") or data.get("num") or data.get("id")
        if vnum is None:
            return False
        try:
            vnum = int(vnum)
        except (ValueError, TypeError):
            return False

        self.current_id  = vnum
        self._gmcp_name  = str(data.get("name", ""))
        self._gmcp_area  = str(data.get("area", ""))

        exits_raw = data.get("exits", {})
        self._gmcp_exits = {}
        if isinstance(exits_raw, dict):
            for d, dest in exits_raw.items():
                try:
                    self._gmcp_exits[d.lower()] = int(dest)
                except (ValueError, TypeError):
                    pass

        return vnum in self.rooms

    # ── Text-based lookup ─────────────────────────────────────────────

    def find_by_name_and_exits(
        self,
        name: str,
        exits: frozenset,
        near_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Find the best room ID matching name + exit set.

        1. Exact name match (case-insensitive)
        2. Among matches, prefer exact exit-set match
        3. If still ambiguous, prefer rooms adjacent to near_id
        4. Return first candidate or None
        """
        if not name or not self.rooms:
            return None

        name_lo = name.lower()
        by_name = [r for r in self.rooms.values()
                   if r.name.lower() == name_lo]
        if not by_name:
            return None
        if len(by_name) == 1:
            return by_name[0].id

        def std_exits(r: MRoom) -> frozenset:
            return frozenset(d for d in r.exits if d in STANDARD_DIRS)

        exact = [r for r in by_name if std_exits(r) == exits]
        if len(exact) == 1:
            return exact[0].id
        candidates = exact if exact else by_name

        if near_id is not None:
            near_room = self.rooms.get(near_id)
            if near_room:
                reachable = set(near_room.exits.values())
                adj = [r for r in candidates if r.id in reachable]
                if len(adj) == 1:
                    return adj[0].id
                if adj:
                    candidates = adj

        return candidates[0].id if candidates else None

    # ── Queries ───────────────────────────────────────────────────────

    @property
    def current(self) -> Optional[MRoom]:
        if self.current_id is None:
            return None
        return self.rooms.get(self.current_id)

    @property
    def loaded(self) -> bool:
        return bool(self.rooms)

    def get_z_levels(self, area_id: int) -> list[int]:
        area = self.areas.get(area_id)
        return area.z_levels() if area else []

    def get_rooms_at(self, area_id: int, z: int) -> list[MRoom]:
        area = self.areas.get(area_id)
        return area.rooms_at_z(z) if area else []

    def area_of_current(self) -> Optional[MArea]:
        cur = self.current
        if cur is None:
            return None
        return self.areas.get(cur.area_id)

    # ── Pathfinding ───────────────────────────────────────────────────

    def find_path(self, from_id: int, to_id: int) -> "Optional[list[str]]":
        """
        BFS shortest path.  Returns ordered list of exit names to walk,
        e.g. ["north", "east", "enter portal"], or None if unreachable.
        Only traverses unlocked rooms.
        """
        if from_id not in self.rooms or to_id not in self.rooms:
            return None
        if from_id == to_id:
            return []

        from collections import deque
        visited: dict = {}   # room_id -> (prev_id, exit_name)
        queue: deque = deque([from_id])
        visited[from_id] = (-1, "")

        while queue:
            cur_id = queue.popleft()
            cur = self.rooms.get(cur_id)
            if cur is None:
                continue
            for exit_name, dest_id in cur.exits.items():
                if dest_id not in self.rooms:
                    continue
                dest = self.rooms[dest_id]
                if dest.locked:
                    continue
                if dest_id not in visited:
                    visited[dest_id] = (cur_id, exit_name)
                    if dest_id == to_id:
                        path: list = []
                        nid = to_id
                        while nid != from_id:
                            prev_id, ex = visited[nid]
                            path.append(ex)
                            nid = prev_id
                        path.reverse()
                        return path
                    queue.append(dest_id)
        return None

    def search_rooms(self, pattern: str) -> "list[MRoom]":
        """Case-insensitive substring search. Returns rooms sorted by area, id."""
        pl = pattern.lower()
        matches = [r for r in self.rooms.values()
                   if pl in r.name.lower() and r.name]
        matches.sort(key=lambda r: (r.area_id, r.id))
        return matches

    def clear(self):
        self.areas.clear()
        self.rooms.clear()
        self.current_id = None
        self._path = None


def try_parse_gmcp_line(package: str, payload: object) -> Optional[dict]:
    if not isinstance(package, str):
        return None
    p = package.lower()
    if p in ("room.info", "room.char"):
        if isinstance(payload, dict):
            return payload
    return None
