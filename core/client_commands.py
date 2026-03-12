"""
ClientCommandDispatcher — handles client-side #commands.

  #map find [pattern]   — search room names; no pattern = re-sync position
  #map walk <id|name>   — pathfind and walk to a room
  #map stop             — abort in-progress speedwalk
  #map add <n>         — bookmark current room as <n>
  #map del <n>       — delete a bookmark
  #map marks            — list all bookmarks with hop counts
  #map here             — show current room info
  #map room <id>        — force-set current room by ID
  #map clear            — clear current room marker
  #map debug [on|off]   — toggle room detector debug output
  #help                 — list all client commands
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.main_window import MainWindow

_log = logging.getLogger("mudclient.client_commands")


class ClientCommandDispatcher:
    def __init__(self, window: "MainWindow"):
        self._win   = window
        self._char  = "#"
        self._debug = False
        self._handlers = {
            "map":  self._cmd_map,
            "help": self._cmd_help,
        }

    def set_command_char(self, char: str):
        self._char = char or "#"

    def dispatch(self, text: str) -> bool:
        if not text or not text.startswith(self._char):
            return False
        body  = text[len(self._char):]
        parts = body.strip().split()
        if not parts:
            return True
        verb    = parts[0].lower()
        args    = parts[1:]
        handler = self._handlers.get(verb)
        if handler:
            try:
                handler(args)
            except Exception as e:
                self._echo(f"Command error: {e}", "#cc4444")
        else:
            self._echo(
                f"Unknown command: {self._char}{verb}  "
                f"(type {self._char}help for a list)",
                "#cc6644",
            )
        return True

    # ── Helpers ───────────────────────────────────────────────────────

    def _echo(self, msg: str, color: str = "#5599ff"):
        self._win._output.append_local(msg, color)

    def _send_to_mud(self, cmd: str):
        self._win._send_signal.emit(cmd)

    def _map_data(self):
        return self._win._right.map_widget._data

    def _bookmarks(self) -> dict:
        sess = self._win._session
        if sess is None:
            return {}
        if "map_bookmarks" not in sess.config:
            sess.config["map_bookmarks"] = {}
        return sess.config["map_bookmarks"]

    def _save_bookmarks(self):
        """Persist bookmarks (and any other session config) via the session manager."""
        sess = self._win._session
        if sess is None:
            return
        self._win._save_session_map()   # flushes current sess.config to disk

    # ── #map ─────────────────────────────────────────────────────────

    def _cmd_map(self, args: list):
        sub  = args[0].lower() if args else "help"
        rest = args[1:]
        {
            "find":   lambda: self._map_find(rest),
            "walk":   lambda: self._map_walk(rest),
            "fwalk":  lambda: self._map_fwalk(rest),
            "stop":   lambda: self._map_stop(),
            "add":    lambda: self._map_mark(rest),
            "del":    lambda: self._map_unmark(rest),
            "marks":  lambda: self._map_marks(),
            "here":   lambda: self._map_here(),
            "room":   lambda: self._map_set_room(rest),
            "clear":  lambda: self._map_clear(),
            "debug":  lambda: self._map_debug(rest),
        }.get(sub, self._map_help)()

    def _map_help(self, *_):
        c = self._char
        self._echo(
            f"Map commands:\n"
            f"  {c}map find [text]    search rooms by name (no arg = re-sync)\n"
            f"  {c}map walk <id|name> pathfind and walk (confirmed, step-by-step)\n"
            f"  {c}map fwalk <id|name> pathfind and spam all moves at once\n"
            f"  {c}map stop           abort walk\n"
            f"  {c}map add <n>       bookmark current room as <n>\n"
            f"  {c}map del <n>     delete bookmark <n>\n"
            f"  {c}map marks          list all bookmarks with hop counts\n"
            f"  {c}map here           show current room info\n"
            f"  {c}map room <id>      force-set room by ID\n"
            f"  {c}map clear          clear current room marker\n"
            f"  {c}map debug [on|off] toggle debug output",
        )

    # ── #map find ─────────────────────────────────────────────────────

    def _map_find(self, args: list):
        md = self._map_data()

        if not args:
            # Re-sync current position
            self._win._room_det.force_next_sync()
            self._send_to_mud("look")
            self._echo("Map: syncing — sent 'look', waiting for room data…", "#44aacc")
            return

        pattern = " ".join(args)

        if pattern.isdigit():
            room_id = int(pattern)
            room = md.rooms.get(room_id)
            if room is None:
                self._echo(f"Room #{room_id} not found in loaded map.", "#cc4444")
                return
            area = md.areas.get(room.area_id)
            hops = self._hop_count(md, room_id)
            hops_str = f"[{hops:3d}]" if hops is not None else "[  ?]"
            self._echo(
                f"{hops_str}  #{room_id:6d}  "
                f"{(area.name if area else '?'):32s}  {room.name}",
                "#44cc88",
            )
            return

        if not md.rooms:
            self._echo("No map loaded. Use File → Load Map File.", "#cc9944")
            return

        results = md.search_rooms(pattern)
        if not results:
            self._echo(f"No rooms matching '{pattern}'.", "#cc9944")
            return

        self._echo(
            f"Rooms matching '{pattern}'  [{len(results)} found]  "
            f"(#map walk <id> to go there)",
            "#44aacc",
        )
        for room in results:
            area = md.areas.get(room.area_id)
            hops = self._hop_count(md, room.id) if md.current_id else None
            hops_str = f"[{hops:3d}]" if hops is not None else "[  ?]"
            self._echo(
                f"  {hops_str}  #{room.id:6d}  "
                f"{(area.name if area else '?'):32s}  {room.name}",
                "#aaddff",
            )

    def _hop_count(self, md, target_id: int):
        if md.current_id is None or md.current_id == target_id:
            return None
        path = md.find_path(md.current_id, target_id)
        return len(path) if path is not None else None

    # ── #map walk ─────────────────────────────────────────────────────

    def _map_walk(self, args: list):
        if not args:
            self._echo("Usage: #map walk <room_id | bookmark_name>", "#cc9944")
            return
        md = self._map_data()
        if md.current_id is None:
            self._echo("Map: current position unknown. Use #map find first.", "#cc9944")
            return
        target_arg = " ".join(args)
        target_id  = self._resolve_target(md, target_arg)
        if target_id is None:
            return
        if target_id == md.current_id:
            self._echo("Map: already there.", "#44cc88")
            return
        path = md.find_path(md.current_id, target_id)
        if path is None:
            self._echo(
                f"Map: no path found to room #{target_id}. "
                f"Rooms may be disconnected or locked.",
                "#cc9944",
            )
            return
        target_room = md.rooms[target_id]
        self._echo(
            f"Map: walking to #{target_id} '{target_room.name}'  "
            f"({len(path)} steps)",
            "#44aacc",
        )
        self._win.start_walk(path)

    def _resolve_target(self, md, arg: str):
        if arg.isdigit():
            rid = int(arg)
            if rid not in md.rooms:
                self._echo(f"Room #{rid} not found in loaded map.", "#cc4444")
                return None
            return rid
        bm = self._bookmarks()
        if arg in bm:
            rid = bm[arg]
            if rid not in md.rooms:
                self._echo(
                    f"Bookmark '{arg}' → #{rid} not found in loaded map.",
                    "#cc4444",
                )
                return None
            return rid
        al = arg.lower()
        for room in md.rooms.values():
            if room.name.lower() == al:
                return room.id
        self._echo(
            f"Cannot resolve '{arg}'. Use a room ID, bookmark, or exact room name.\n"
            f"  Try: #map find {arg}",
            "#cc9944",
        )
        return None

    def _map_fwalk(self, args: list):
        if not args:
            self._echo("Usage: #map fwalk <room_id | bookmark_name>", "#cc9944")
            return
        md = self._map_data()
        if md.current_id is None:
            self._echo("Map: current position unknown. Use #map find first.", "#cc9944")
            return
        target_arg = " ".join(args)
        target_id  = self._resolve_target(md, target_arg)
        if target_id is None:
            return
        if target_id == md.current_id:
            self._echo("Map: already there.", "#44cc88")
            return
        path = md.find_path(md.current_id, target_id)
        if path is None:
            self._echo(
                f"Map: no path found to room #{target_id}.",
                "#cc9944",
            )
            return
        target_room = md.rooms[target_id]
        self._echo(
            f"Map: fwalk to #{target_id} '{target_room.name}'  "
            f"({len(path)} steps)",
            "#44aacc",
        )
        self._win.start_fwalk(path)

    # ── #map stop ─────────────────────────────────────────────────────

    def _map_stop(self):
        self._win.stop_walk()
        self._echo("Map: speedwalk stopped.", "#cc9944")

    # ── #map add / del / marks ───────────────────────────────────────

    def _map_mark(self, args: list):
        if not args:
            self._echo("Usage: #map add <name>", "#cc9944")
            return
        name = " ".join(args)
        md   = self._map_data()
        cur  = md.current
        if cur is None:
            self._echo("Map: current room unknown. Use #map find first.", "#cc9944")
            return
        bm       = self._bookmarks()
        bm[name] = cur.id
        self._save_bookmarks()
        self._echo(f"Map: bookmarked '{name}' → #{cur.id} '{cur.name}'", "#44cc88")

    def _map_unmark(self, args: list):
        if not args:
            self._echo("Usage: #map del <name>", "#cc9944")
            return
        name = " ".join(args)
        bm   = self._bookmarks()
        if name not in bm:
            self._echo(f"No bookmark named '{name}'.", "#cc9944")
            return
        del bm[name]
        self._save_bookmarks()
        self._echo(f"Map: deleted bookmark '{name}'.", "#44cc88")

    def _map_marks(self):
        bm = self._bookmarks()
        if not bm:
            self._echo("No bookmarks. Use #map add <name> to add one.", "#cc9944")
            return
        md = self._map_data()
        self._echo(f"Bookmarks [{len(bm)}]  (#map walk <name> to go there):", "#44aacc")
        for name, rid in sorted(bm.items()):
            room = md.rooms.get(rid)
            area = md.areas.get(room.area_id) if room else None
            hops = self._hop_count(md, rid) if md.current_id else None
            hops_str = f"[{hops:3d}]" if hops is not None else "[  ?]"
            rname = room.name if room else "(not in map)"
            aname = (area.name if area else "?")
            self._echo(
                f"  {hops_str}  {name:20s}  #{rid:6d}  {aname:32s}  {rname}",
                "#aaddff",
            )

    # ── #map here / room / clear / debug ─────────────────────────────

    def _map_here(self):
        md  = self._map_data()
        cur = md.current
        if cur is None:
            self._echo("Map: no current room set.  Try #map find", "#cc9944")
            return
        exits_str = ", ".join(sorted(cur.exits.keys())) or "none"
        area      = md.areas.get(cur.area_id)
        self._echo(
            f"Map: room #{cur.id}  '{cur.name or '(unnamed)'}'\n"
            f"     Area: {area.name if area else '?'}   Coords: {cur.coords}\n"
            f"     Exits: {exits_str}",
        )

    def _map_set_room(self, args: list):
        if not args:
            self._echo("Usage: #map room <id>", "#cc9944")
            return
        try:
            room_id = int(args[0])
        except ValueError:
            self._echo(f"Not a valid room id: {args[0]}", "#cc4444")
            return
        md = self._map_data()
        if room_id not in md.rooms:
            self._echo(f"Room #{room_id} not found in loaded map.", "#cc4444")
            return
        self._win._set_map_room({"num": room_id})
        room = md.rooms[room_id]
        self._echo(f"Map: forced to room #{room_id} '{room.name}'", "#44cc88")

    def _map_clear(self):
        md = self._map_data()
        md.current_id = None
        self._win._right.map_widget._canvas.update()
        self._echo("Map: current room cleared.", "#44cc88")

    def _map_debug(self, args: list):
        val = args[0].lower() if args else "toggle"
        if val in ("on", "1", "true"):
            self._debug = True
        elif val in ("off", "0", "false"):
            self._debug = False
        else:
            self._debug = not self._debug
        logging.getLogger("mudclient.room_detector").setLevel(
            logging.DEBUG if self._debug else logging.WARNING
        )
        self._echo(f"Map debug: {'ON' if self._debug else 'OFF'}", "#44cc88")

    # ── #help ─────────────────────────────────────────────────────────

    def _cmd_help(self, args: list):
        c = self._char
        self._echo(
            f"Client commands (char: '{c}'):\n"
            f"  {c}map find [text]    search rooms / re-sync position\n"
            f"  {c}map walk <id|name> pathfind, step-by-step\n"
            f"  {c}map fwalk <id|name> pathfind, blast all moves\n"
            f"  {c}map stop           abort walk\n"
            f"  {c}map add <name>    bookmark current room\n"
            f"  {c}map del <name>  delete bookmark\n"
            f"  {c}map marks          list bookmarks with hop counts\n"
            f"  {c}map here           show current room info\n"
            f"  {c}map room <id>      force-set current room\n"
            f"  {c}map clear          clear position marker\n"
            f"  {c}map debug [on|off] toggle map debug logging\n"
            f"  {c}help               this list",
        )
