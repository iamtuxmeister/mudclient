"""
MapWidget — 2D graphical map renderer for Mudlet-format maps.

Features
--------
- Loads Mudlet JSON map files (all areas, all rooms)
- 2D canvas with pan (left-drag) and zoom (scroll wheel)
- Area dropdown and Z-level navigator
- Highlights the current room (from GMCP) in gold
- Draws standard exits as lines, up/down as in-room triangles,
  special exits as dashed stubs
- Fit-to-view button
- "Load Map…" via context menu or toolbar button
- Falls back to compass-rose text view when no map is loaded
"""

from __future__ import annotations

import math
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel,
    QPushButton, QFileDialog, QSizePolicy, QToolBar, QSpinBox,
    QFrame, QApplication,
)
from PyQt6.QtCore  import Qt, QRect, QRectF, QPointF, QSize, pyqtSignal
from PyQt6.QtGui   import (
    QPainter, QPen, QBrush, QColor, QFont, QPainterPath,
    QWheelEvent, QMouseEvent, QKeyEvent, QPaintEvent,
    QContextMenuEvent,
)

from core.map_data import MapData, MRoom, DIR_VECTOR, STANDARD_DIRS

# ── Visual constants ──────────────────────────────────────────────────────────

CELL     = 14       # room square side (px) at zoom 1.0
STRIDE   = 28       # world-unit to px multiplier at zoom 1.0 (cell + gap)
MIN_ZOOM = 0.15
MAX_ZOOM = 6.0

C_BG         = QColor(0x0a, 0x0a, 0x0a)   # match output bg
C_ROOM       = QColor(0x1e, 0x1e, 0x1e)   # slightly lighter than bg
C_ROOM_BORD  = QColor(0x44, 0x44, 0x44)   # match UI borders
C_CURRENT    = QColor(0xcc, 0x33, 0x33)   # you-are-here red
C_CURRENT_B  = QColor(0xff, 0x55, 0x55)   # bright red border
C_CURRENT_X  = QColor(0xff, 0xff, 0xff)   # white crosshair
C_EXIT       = QColor(0x55, 0x55, 0x55)   # dim gray exit lines
C_EXIT_SPEC  = QColor(0x77, 0x55, 0x99)   # muted purple special exits
C_EXIT_VZ    = QColor(0x44, 0x88, 0x66)   # muted green up/down
C_TEXT       = QColor(0x88, 0x88, 0x88)   # match UI dim text
C_DIM        = QColor(0x44, 0x44, 0x44)   # very dim
C_TOOLBAR_BG = QColor(0x11, 0x11, 0x11)   # match input bg

DIRS_8 = ("north", "south", "east", "west",
          "northeast", "northwest", "southeast", "southwest")


# ── Canvas widget ─────────────────────────────────────────────────────────────

class _MapCanvas(QWidget):
    """The actual OpenGL-free 2D painter canvas."""

    status_message = pyqtSignal(str)   # hover text → parent toolbar

    def __init__(self, map_data: MapData, parent=None):
        super().__init__(parent)
        self._data    = map_data
        self._zoom    = 1.0
        self._offset  = QPointF(0, 0)   # world-coord of canvas centre
        self._drag_start: Optional[QPointF] = None
        self._drag_offset_start: Optional[QPointF] = None
        self._hovered_room: Optional[MRoom] = None

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(200, 200)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    # ── Coordinate transforms ─────────────────────────────────────────

    def _world_to_screen(self, wx: float, wy: float) -> QPointF:
        """Convert world (map) coordinates to screen pixels."""
        cx = self.width()  / 2
        cy = self.height() / 2
        sx = cx + (wx - self._offset.x()) * STRIDE * self._zoom
        sy = cy - (wy - self._offset.y()) * STRIDE * self._zoom   # y-flip
        return QPointF(sx, sy)

    def _screen_to_world(self, sx: float, sy: float) -> QPointF:
        cx = self.width()  / 2
        cy = self.height() / 2
        wx = (sx - cx) / (STRIDE * self._zoom) + self._offset.x()
        wy = (cy - sy) / (STRIDE * self._zoom) + self._offset.y()
        return QPointF(wx, wy)

    def _cell_px(self) -> float:
        return CELL * self._zoom

    # ── View helpers ──────────────────────────────────────────────────

    def fit_rooms(self, rooms: list[MRoom]):
        """Fit all given rooms into the viewport.
        If the canvas has no real size yet (hidden dock), store the rooms
        and re-fit on the next resizeEvent."""
        if not rooms:
            return
        xs = [r.x for r in rooms]
        ys = [r.y for r in rooms]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        self._offset = QPointF(cx, cy)

        if self.width() < 10 or self.height() < 10:
            # Canvas not laid out yet — store and defer
            self._pending_fit_rooms = list(rooms)
            return

        self._pending_fit_rooms = None
        span_x = max(max(xs) - min(xs) + 2, 1)
        span_y = max(max(ys) - min(ys) + 2, 1)
        zoom_x = self.width()  / (span_x * STRIDE)
        zoom_y = self.height() / (span_y * STRIDE)
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, min(zoom_x, zoom_y) * 0.85))
        self.update()

    def resizeEvent(self, event):
        """If a fit was deferred (canvas had no size), execute it now."""
        super().resizeEvent(event)
        if getattr(self, "_pending_fit_rooms", None):
            rooms = self._pending_fit_rooms
            self._pending_fit_rooms = None
            self.fit_rooms(rooms)

    def center_on_room(self, room: MRoom):
        """Pan to put a room in the centre without changing zoom."""
        self._offset = QPointF(room.x, room.y)
        self.update()

    # ── Painting ─────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), C_BG)

        rooms = self._visible_rooms()
        if not rooms:
            self._draw_no_map(p)
            p.end()
            return

        # Build set of room IDs on this layer for fast lookup
        room_ids = {r.id for r in rooms}
        room_map  = {r.id: r for r in rooms}

        # Draw exit lines first (under rooms)
        self._draw_exits(p, rooms, room_ids, room_map)

        # Draw rooms on top
        cell = self._cell_px()
        half = cell / 2

        for room in rooms:
            sp   = self._world_to_screen(room.x, room.y)
            rect = QRectF(sp.x() - half, sp.y() - half, cell, cell)
            is_current = (room.id == self._data.current_id)

            if is_current:
                # Outer glow ring
                ring_pen = QPen(C_CURRENT_B, max(1, self._zoom * 1.5))
                ring_pen.setStyle(Qt.PenStyle.SolidLine)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(ring_pen)
                pad = cell * 0.35
                p.drawEllipse(QRectF(rect.x() - pad, rect.y() - pad,
                                     cell + pad*2, cell + pad*2))
                # Room fill
                p.setBrush(QBrush(C_CURRENT))
                p.setPen(QPen(C_CURRENT_B, max(1.5, self._zoom * 2)))
                p.drawRect(rect)
                # White crosshair
                cross_pen = QPen(C_CURRENT_X, max(1, self._zoom * 0.8))
                p.setPen(cross_pen)
                cx, cy = sp.x(), sp.y()
                arm = half * 0.6
                p.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
                p.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))
            else:
                p.setBrush(QBrush(C_ROOM))
                p.setPen(QPen(C_ROOM_BORD, max(1, self._zoom)))
                p.drawRect(rect)

            # Up/down indicators (small triangles inside room)
            self._draw_vz_indicators(p, room, rect, room_ids)

            # Room label only for hovered room
            if room is self._hovered_room and room.name:
                p.setPen(QPen(C_TEXT if not is_current else QColor(0xff,0x88,0x88)))
                font = QFont("sans-serif", max(5, int(7 * self._zoom)))
                p.setFont(font)
                label_rect = QRectF(rect.x() - cell, rect.y() + cell + 1,
                                    cell * 3, cell)
                p.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter,
                           room.name[:20])

        p.end()

    def _visible_rooms(self) -> list[MRoom]:
        """Return rooms that should be drawn on the current canvas."""
        parent = self.parent()
        while parent and not isinstance(parent, MapWidget):
            parent = parent.parent() if hasattr(parent, 'parent') else None
        if parent is None:
            return []
        return parent._current_rooms()

    def _draw_exits(self, p: QPainter, rooms: list[MRoom],
                    room_ids: set[int], room_map: dict[int, MRoom]):
        cell = self._cell_px()
        half = cell / 2
        pen_normal  = QPen(C_EXIT,      max(1, self._zoom))
        pen_special = QPen(C_EXIT_SPEC, max(1, self._zoom))
        pen_special.setStyle(Qt.PenStyle.DashLine)

        for room in rooms:
            sp = self._world_to_screen(room.x, room.y)

            for direction, dest_id in room.exits.items():
                if direction in ("up", "down"):
                    continue   # handled by _draw_vz_indicators

                dest  = self._data.rooms.get(dest_id)
                is_sp = direction not in STANDARD_DIRS

                if dest and dest.id in room_ids:
                    # Full line to destination room
                    dp = self._world_to_screen(dest.x, dest.y)
                    p.setPen(pen_special if is_sp else pen_normal)
                    p.drawLine(sp, dp)
                else:
                    # Stub line in the named direction
                    vec = DIR_VECTOR.get(direction)
                    if vec is None:
                        vec = (0, 0)
                    stub = STRIDE * self._zoom * 0.45
                    ex = sp.x() + vec[0] * stub
                    ey = sp.y() - vec[1] * stub    # y-flip
                    p.setPen(pen_special if is_sp else pen_normal)
                    p.drawLine(sp, QPointF(ex, ey))

    def _draw_vz_indicators(self, p: QPainter, room: MRoom,
                             rect: QRectF, room_ids: set[int]):
        """Draw small up/down triangles inside the room square."""
        has_up   = "up"   in room.exits
        has_down = "down" in room.exits
        if not (has_up or has_down):
            return

        pen = QPen(C_EXIT_VZ, max(1, self._zoom * 0.8))
        p.setPen(pen)
        p.setBrush(QBrush(C_EXIT_VZ))

        s  = rect.width() * 0.28
        cx = rect.center().x()
        cy = rect.center().y()

        if has_up:
            path = QPainterPath()
            path.moveTo(cx, cy - rect.height()*0.08 - s)
            path.lineTo(cx - s*0.7, cy - rect.height()*0.08)
            path.lineTo(cx + s*0.7, cy - rect.height()*0.08)
            path.closeSubpath()
            p.drawPath(path)

        if has_down:
            path = QPainterPath()
            path.moveTo(cx, cy + rect.height()*0.08 + s)
            path.lineTo(cx - s*0.7, cy + rect.height()*0.08)
            path.lineTo(cx + s*0.7, cy + rect.height()*0.08)
            path.closeSubpath()
            p.drawPath(path)

    def _draw_no_map(self, p: QPainter):
        """Fallback text when no map is loaded."""
        cur = self._data.current
        if cur is None:
            msg = "No map loaded.\nRight-click → Load Map File\nor use File → Load Map"
        else:
            exits = ", ".join(cur.exits.keys()) if cur.exits else "none"
            msg = f"Room #{cur.id}\n{cur.name or '(unnamed)'}\nExits: {exits}"
        p.setPen(QPen(C_DIM))
        p.setFont(QFont("Monospace", 12))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, msg)

    # ── Interaction ───────────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent):
        # Zoom toward mouse cursor
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1 / 1.12 if delta > 0 else 1.12
        # World pos under cursor before zoom
        mw = self._screen_to_world(event.position().x(), event.position().y())
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom * factor))
        # Adjust offset so the world point stays under the cursor
        mw2 = self._screen_to_world(event.position().x(), event.position().y())
        self._offset -= (mw2 - mw)
        self.update()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._drag_offset_start = QPointF(self._offset)
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_start is not None:
            delta = event.position() - self._drag_start
            ox = self._drag_offset_start.x() - delta.x() / (STRIDE * self._zoom)
            oy = self._drag_offset_start.y() + delta.y() / (STRIDE * self._zoom)
            self._offset = QPointF(ox, oy)
            self.update()
        else:
            # Hover — find nearest room
            wpos = self._screen_to_world(event.position().x(), event.position().y())
            self._update_hover(wpos)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = None
            self._drag_offset_start = None
        event.accept()

    def _update_hover(self, wpos: QPointF):
        rooms = self._visible_rooms()
        threshold = (CELL / 2 + 2) / (STRIDE * self._zoom)
        best: Optional[MRoom] = None
        best_d = float("inf")
        for room in rooms:
            dx = abs(room.x - wpos.x())
            dy = abs(room.y - wpos.y())
            d  = max(dx, dy)
            if d < threshold and d < best_d:
                best   = room
                best_d = d
        if best != self._hovered_room:
            self._hovered_room = best
            if best:
                exits_str = ", ".join(best.exits.keys()) or "none"
                self.status_message.emit(
                    f"  #{best.id}  {best.name or '(unnamed)'}  |  exits: {exits_str}")
            else:
                self.status_message.emit("")

    def keyPressEvent(self, event: QKeyEvent):
        step = 3.0
        if event.key() == Qt.Key.Key_Left:
            self._offset -= QPointF(step, 0); self.update()
        elif event.key() == Qt.Key.Key_Right:
            self._offset += QPointF(step, 0); self.update()
        elif event.key() == Qt.Key.Key_Up:
            self._offset += QPointF(0, step); self.update()
        elif event.key() == Qt.Key.Key_Down:
            self._offset -= QPointF(0, step); self.update()
        elif event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom = min(MAX_ZOOM, self._zoom * 1.2); self.update()
        elif event.key() == Qt.Key.Key_Minus:
            self._zoom = max(MIN_ZOOM, self._zoom / 1.2); self.update()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background:#1a1a1a; color:#cccccc; border:1px solid #444; }
            QMenu::item:selected { background:#2a2a2a; }
        """)
        load_act = menu.addAction("📂  Load Map File…")
        fit_act  = menu.addAction("⊡  Fit All Rooms")
        menu.addSeparator()
        cur_act  = menu.addAction("⊕  Go to Current Room")
        action = menu.exec(event.globalPos())
        if action == load_act:
            self.parent()._load_map_dialog()
        elif action == fit_act:
            self.parent()._fit_view()
        elif action == cur_act:
            self.parent()._go_to_current()


# ── MapWidget (public API) ────────────────────────────────────────────────────

class MapWidget(QWidget):
    """
    Full map panel: toolbar + 2D canvas.

    Public API (called from main_window / right_panel):
        on_gmcp_room(data: dict)   — Room.Info GMCP payload
        load_map_file(path: str)   — load a Mudlet JSON map
        update_map(text: str)      — compat shim (ignored when map loaded)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data   = MapData()
        self._area_id: Optional[int] = None
        self._z_level: int = 0

        self._build_ui()

        # Never let content changes drive the dock size.
        # The dock splitter controls our dimensions, not our sizeHint.
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Ignored)

    def sizeHint(self):
        """Return a stable hint so Qt stops trying to resize us."""
        from PyQt6.QtCore import QSize
        return QSize(300, 400)

    # ── UI Construction ───────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar ──────────────────────────────────────────────────
        tb = QWidget()
        tb.setStyleSheet("background:#111; border-bottom:1px solid #333;")
        tb_lay = QHBoxLayout(tb)
        tb_lay.setContentsMargins(4, 2, 4, 2)
        tb_lay.setSpacing(4)

        def _btn(label, tip, slot):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(24)
            b.setStyleSheet("""
                QPushButton{background:#1e1e1e;color:#aaa;border:1px solid #444;
                            border-radius:3px;padding:0 6px;font-size:13px;}
                QPushButton:hover{background:#2a2a2a;color:#ccc;}
                QPushButton:pressed{background:#333;}
            """)
            b.clicked.connect(slot)
            return b

        tb_lay.addWidget(_btn("📂", "Load Map File", self._load_map_dialog))
        tb_lay.addWidget(_btn("⊡", "Fit all rooms", self._fit_view))
        tb_lay.addWidget(_btn("⊕", "Go to current room", self._go_to_current))

        tb_lay.addSpacing(8)

        lbl = QLabel("Area:")
        lbl.setStyleSheet("color:#666; font-size:13px;")
        tb_lay.addWidget(lbl)

        self._area_combo = QComboBox()
        self._area_combo.setFixedHeight(24)
        self._area_combo.setFixedWidth(160)   # fixed — prevents dock resize on map load
        self._area_combo.setStyleSheet("""
            QComboBox{background:#1e1e1e;color:#aaa;border:1px solid #444;
                      border-radius:3px;font-size:13px;}
            QComboBox QAbstractItemView{background:#111;color:#aaa;
                                        selection-background-color:#333;}
        """)
        self._area_combo.currentIndexChanged.connect(self._on_area_changed)
        tb_lay.addWidget(self._area_combo)

        tb_lay.addSpacing(8)

        lbl2 = QLabel("Z:")
        lbl2.setStyleSheet("color:#666; font-size:13px;")
        tb_lay.addWidget(lbl2)

        self._z_down = _btn("▼", "Z level down", self._z_dec)
        self._z_down.setFixedWidth(24)
        self._z_label = QLabel("0")
        self._z_label.setFixedWidth(30)
        self._z_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._z_label.setStyleSheet("color:#aaa; font-size:13px;")
        self._z_up = _btn("▲", "Z level up", self._z_inc)
        self._z_up.setFixedWidth(24)
        tb_lay.addWidget(self._z_down)
        tb_lay.addWidget(self._z_label)
        tb_lay.addWidget(self._z_up)

        tb_lay.addStretch(1)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color:#555; font-size:12px;")
        tb_lay.addWidget(self._status_label)

        root.addWidget(tb)

        # ── Canvas ───────────────────────────────────────────────────
        self._canvas = _MapCanvas(self._data, self)
        self._canvas.status_message.connect(self._status_label.setText)
        root.addWidget(self._canvas, 1)

    # ── Public API ────────────────────────────────────────────────────

    def on_gmcp_room(self, data: dict):
        """Called from main_window when a Room.Info GMCP packet arrives."""
        in_map = self._data.on_gmcp_room(data)
        cur    = self._data.current
        if cur is None:
            self._canvas.update()
            return

        if in_map:
            area_changed = self._area_id != cur.area_id
            z_changed    = self._z_level != cur.z

            if area_changed:
                self._area_id = cur.area_id
                self._sync_area_combo()   # updates combo label only

            if z_changed:
                self._z_level = cur.z
                self._z_label.setText(str(self._z_level))

            # Always center on player and redraw
            self._canvas.center_on_room(cur)
        else:
            self._canvas.update()

        # Update the status bar with current room info
        exits_str = ", ".join(cur.exits.keys()) if cur.exits else "none"
        self._status_label.setText(
            f"  ⊕ #{cur.id}  {cur.name or '(unnamed)'}  |  exits: {exits_str}")

    def load_map_file(self, path: str) -> tuple[bool, str]:
        """Load a Mudlet JSON map file programmatically."""
        ok, msg = self._data.load_json(path)
        if ok:
            self._rebuild_area_combo()
            self._auto_select_start()
        return ok, msg

    def update_map(self, text: str):
        """Backward-compat shim — ignored when a proper map is loaded."""
        if not self._data.loaded:
            self._canvas.update()

    # ── Internal helpers ──────────────────────────────────────────────

    def _current_rooms(self) -> list[MRoom]:
        """Return the rooms to render on the canvas right now."""
        if self._area_id is None or not self._data.loaded:
            return []
        return self._data.get_rooms_at(self._area_id, self._z_level)

    def _rebuild_area_combo(self):
        self._area_combo.blockSignals(True)
        self._area_combo.clear()
        for area in sorted(self._data.areas.values(), key=lambda a: a.name):
            if area.rooms:
                self._area_combo.addItem(
                    f"{area.name} ({len(area.rooms)})", area.id)
        self._area_combo.blockSignals(False)
        if self._area_combo.count():
            self._area_combo.setCurrentIndex(0)
            self._on_area_changed(0)

    def _sync_area_combo(self):
        """Select the correct area in the combo without triggering a fit."""
        for i in range(self._area_combo.count()):
            if self._area_combo.itemData(i) == self._area_id:
                self._area_combo.blockSignals(True)
                self._area_combo.setCurrentIndex(i)
                self._area_combo.blockSignals(False)
                return

    def _auto_select_start(self):
        """After loading, try to show the area with the most rooms."""
        if not self._data.areas:
            return
        best = max(self._data.areas.values(), key=lambda a: len(a.rooms))
        self._area_id = best.id
        z_levels = best.z_levels()
        self._z_level = z_levels[len(z_levels)//2] if z_levels else 0
        self._z_label.setText(str(self._z_level))
        self._sync_area_combo()
        self._fit_view()

    def _on_area_changed(self, idx: int):
        if idx < 0:
            return
        self._area_id = self._area_combo.itemData(idx)
        z_levels = self._data.get_z_levels(self._area_id)
        if z_levels:
            # Pick the z-level with the most rooms
            area = self._data.areas.get(self._area_id)
            best_z = max(z_levels, key=lambda z: len(area.rooms_at_z(z)))
            self._z_level = best_z
            self._z_label.setText(str(self._z_level))
        self._fit_view()

    def _z_inc(self):
        z_levels = self._data.get_z_levels(self._area_id) if self._area_id else []
        idx = z_levels.index(self._z_level) if self._z_level in z_levels else -1
        if idx < len(z_levels) - 1:
            self._z_level = z_levels[idx + 1]
            self._z_label.setText(str(self._z_level))
            self._canvas.update()

    def _z_dec(self):
        z_levels = self._data.get_z_levels(self._area_id) if self._area_id else []
        idx = z_levels.index(self._z_level) if self._z_level in z_levels else -1
        if idx > 0:
            self._z_level = z_levels[idx - 1]
            self._z_label.setText(str(self._z_level))
            self._canvas.update()

    def _fit_view(self):
        rooms = self._current_rooms()
        self._canvas.fit_rooms(rooms)

    def _go_to_current(self):
        cur = self._data.current
        if cur is None:
            return
        if self._area_id != cur.area_id or self._z_level != cur.z:
            self._area_id = cur.area_id
            self._z_level = cur.z
            self._z_label.setText(str(self._z_level))
            self._sync_area_combo()
        self._canvas.center_on_room(cur)

    def _load_map_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Mudlet Map File", "",
            "JSON Map Files (*.json);;All Files (*)")
        if path:
            ok, msg = self.load_map_file(path)
            self._status_label.setText(f"  {'✓' if ok else '✗'} {msg}")
