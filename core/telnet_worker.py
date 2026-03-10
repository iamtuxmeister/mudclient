"""
TelnetWorker — raw telnet connection running in a QThread.

Handles:
  - Full IAC negotiation  (WILL / WONT / DO / DONT)
  - Subnegotiation (SB … SE):
      • GMCP  (option 201) — decodes JSON, emits gmcp_received
      • MCCP2 (option  86) — enables zlib stream decompression
      • TTYPE (option  24) — responds "MUD Client"
      • NAWS  (option  31) — advertises terminal window size
  - Escaped IAC bytes (0xFF 0xFF → 0xFF)
  - Partial sequences held in buffer until rest arrives
  - MCCP2: zlib stream decompression after server enables it.

Signals are emitted from the worker thread; PyQt6 queues them
automatically so connected slots always run on the GUI thread.
"""

import json
import zlib
import socket
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from core.debug import dbg

# ── Telnet constants ─────────────────────────────────────────────────
IAC  = 255
DONT = 254
DO   = 253
WONT = 252
WILL = 251
SB   = 250
GA   = 249
SE   = 240

TELOPT_ECHO   =   1
TELOPT_SGA    =   3
TELOPT_TTYPE  =  24
TELOPT_NAWS   =  31
TELOPT_MCCP1  =  85
TELOPT_MCCP2  =  86
TELOPT_GMCP   = 201

_OPT_NAMES = {
    1: "ECHO", 3: "SGA", 24: "TTYPE", 31: "NAWS",
    85: "MCCP1", 86: "MCCP2", 201: "GMCP",
}
_CMD_NAMES = {251: "WILL", 252: "WONT", 253: "DO", 254: "DONT"}

_DO_ON_WILL = {TELOPT_ECHO, TELOPT_SGA, TELOPT_MCCP2, TELOPT_GMCP}
_WILL_ON_DO = {TELOPT_TTYPE, TELOPT_NAWS, TELOPT_SGA}

_COLS = 220
_ROWS =  50


def _opt(n: int) -> str:
    return _OPT_NAMES.get(n, str(n))

def _cmd(n: int) -> str:
    return _CMD_NAMES.get(n, str(n))


class TelnetWorker(QObject):
    """Telnet connection engine. Move to a QThread, then call connect_to()."""

    connected     = pyqtSignal()
    disconnected  = pyqtSignal(str)
    error         = pyqtSignal(str)
    data_received = pyqtSignal(bytes)
    gmcp_received = pyqtSignal(str, object)
    mccp_active   = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sock:     socket.socket | None    = None
        self._running:  bool                    = False
        self._lock      = threading.Lock()
        self._mccp2_on: bool                    = False
        self._zlib_dc:  zlib.Decompress | None  = None
        self._host:     str                     = ""
        self._port:     int                     = 0

    # ── Public API ──────────────────────────────────────────────────

    def set_target(self, host: str, port: int):
        """Store host/port before the thread starts."""
        self._host = host
        self._port = port

    def start(self):
        """
        Real bound-method slot connected to thread.started.
        Runs in the worker's thread (not a lambda, so PyQt6 dispatches
        it correctly to the thread this object was moved to).
        """
        self.connect_to(self._host, self._port)

    def connect_to(self, host: str, port: int):
        """Open TCP connection and enter the read loop (blocks)."""
        dbg("telnet", f"connect_to({host!r}, {port}) — resolving + connecting…")
        try:
            sock = socket.create_connection((host, port), timeout=15)
            sock.settimeout(None)
            dbg("telnet", f"TCP connected  local={sock.getsockname()}  remote={sock.getpeername()}")
            with self._lock:
                self._sock     = sock
                self._running  = True
                self._mccp2_on = False
                self._zlib_dc  = None
            dbg("telnet", "emitting connected signal")
            self.connected.emit()
            dbg("telnet", "entering read loop")
            self._read_loop()
        except OSError as exc:
            dbg("error", f"connect_to OSError: {exc}")
            self.error.emit(str(exc))

    def send(self, text: str):
        """Encode text as UTF-8, append CRLF, transmit."""
        dbg("telnet", f"send({text!r})")
        self._transmit((text + "\r\n").encode("utf-8", errors="replace"))

    def send_raw(self, data: bytes):
        """Transmit raw bytes without transformation."""
        dbg("telnet", f"send_raw len={len(data)} hex={data[:16].hex()}")
        self._transmit(data)

    def disconnect(self):
        """Request graceful shutdown (safe from any thread)."""
        dbg("telnet", "disconnect() called")
        with self._lock:
            self._running = False
            if self._sock:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                    dbg("telnet", "socket shutdown OK")
                except OSError as e:
                    dbg("error", f"shutdown error: {e}")

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running and self._sock is not None

    @property
    def mccp_enabled(self) -> bool:
        with self._lock:
            return self._mccp2_on

    # ── Internal ────────────────────────────────────────────────────

    def _transmit(self, data: bytes):
        with self._lock:
            if not self._sock:
                dbg("error", "_transmit: no socket, dropping")
                return
            try:
                self._sock.sendall(data)
                dbg("telnet", f"_transmit: sent {len(data)} bytes")
            except OSError as exc:
                dbg("error", f"_transmit OSError: {exc}")
                self.error.emit(str(exc))

    def _read_loop(self):
        text_buf   = bytearray()
        recv_count = 0

        while True:
            with self._lock:
                if not self._running:
                    dbg("telnet", "read loop: _running=False, exiting")
                    break
                sock = self._sock
            if not sock:
                dbg("telnet", "read loop: sock=None, exiting")
                break

            dbg("telnet", f"read loop iter {recv_count}: calling recv(4096)…")
            try:
                chunk = sock.recv(4096)
            except OSError as exc:
                dbg("error", f"recv() raised OSError: {exc}")
                break
            if not chunk:
                dbg("telnet", "recv() returned empty — server closed connection")
                break

            recv_count += 1
            dbg("telnet", f"recv() → {len(chunk)} bytes  (recv #{recv_count})")
            dbg("data",   f"raw hex: {chunk[:64].hex()}{'…' if len(chunk)>64 else ''}")

            # MCCP2 decompression
            with self._lock:
                mccp_on = self._mccp2_on
                dc      = self._zlib_dc

            if mccp_on and dc is not None:
                dbg("mccp", f"decompressing {len(chunk)} bytes")
                try:
                    decompressed = dc.decompress(chunk)
                    dbg("mccp", f"decompressed → {len(decompressed)} bytes")
                    text_buf.extend(decompressed)
                except zlib.error as e:
                    dbg("error", f"zlib error: {e} — using raw bytes")
                    text_buf.extend(chunk)
            else:
                text_buf.extend(chunk)

            dbg("telnet", f"text_buf before _process: {len(text_buf)} bytes")
            text_buf, output = self._process(text_buf)
            dbg("telnet", f"text_buf after  _process: {len(text_buf)} bytes  output={len(output)} bytes")

            if output:
                preview = bytes(b for b in output if 0x20 <= b < 0x7f or b in (0x09, 0x0a, 0x0d, 0x1b))
                dbg("data", f"emitting data_received: {len(output)} bytes  preview={preview[:80]!r}")
                self.data_received.emit(bytes(output))
            else:
                dbg("telnet", "no plain output this chunk (pure IAC negotiation)")

        reason = "Connection closed."
        dbg("telnet", f"read loop done — emitting disconnected")
        self.disconnected.emit(reason)
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock     = None
                self._running  = False
                self._mccp2_on = False
                self._zlib_dc  = None
        dbg("telnet", "worker fully stopped")

    def _process(self, buf: bytearray) -> tuple[bytearray, bytearray]:
        """Walk buf, handle IAC sequences, return (remainder, plain_output)."""
        out       = bytearray()
        i         = 0
        n         = len(buf)
        iac_count = 0

        while i < n:
            b = buf[i]

            if b != IAC:
                out.append(b)
                i += 1
                continue

            if i + 1 >= n:
                dbg("iac", f"incomplete IAC at buf[{i}] (buf len={n}) — holding")
                break

            cmd = buf[i + 1]
            iac_count += 1

            if cmd == IAC:
                out.append(IAC)
                i += 2
                continue

            if cmd in (WILL, WONT, DO, DONT):
                if i + 2 >= n:
                    dbg("iac", f"incomplete {_cmd(cmd)} at buf[{i}] — holding")
                    break
                opt = buf[i + 2]
                dbg("iac", f"recv  IAC {_cmd(cmd)} {_opt(opt)} (opt={opt})")
                self._negotiate(cmd, opt)
                i += 3
                continue

            if cmd == SB:
                j   = i + 2
                end = -1
                while j < n - 1:
                    if buf[j] == IAC and buf[j + 1] == SE:
                        end = j
                        break
                    j += 1
                if end == -1:
                    dbg("iac", f"incomplete SB at buf[{i}], no SE found — holding")
                    break
                opt     = buf[i + 2] if (i + 2) < end else -1
                sb_data = buf[i + 3 : end]
                dbg("iac", f"recv  IAC SB {_opt(opt)} (opt={opt})  data={len(sb_data)} bytes")
                self._subnegotiate(opt, sb_data, buf, end + 2)
                # buf may have been mutated by _start_mccp2; refresh n
                n = len(buf)
                i = end + 2
                continue

            dbg("iac", f"recv  IAC {cmd} (2-byte, ignored)")
            i += 2

        if iac_count:
            dbg("iac", f"_process: handled {iac_count} IAC seq(s)")

        return buf[i:], out

    # ── Negotiation ─────────────────────────────────────────────────

    def _negotiate(self, cmd: int, opt: int):
        if cmd == WILL:
            if opt in _DO_ON_WILL:
                dbg("iac", f"send  IAC DO {_opt(opt)}")
                self._transmit(bytes([IAC, DO, opt]))
                if opt == TELOPT_GMCP:
                    self._gmcp_hello()
            else:
                dbg("iac", f"send  IAC DONT {_opt(opt)} (unsupported)")
                self._transmit(bytes([IAC, DONT, opt]))

        elif cmd == DO:
            if opt in _WILL_ON_DO:
                dbg("iac", f"send  IAC WILL {_opt(opt)}")
                self._transmit(bytes([IAC, WILL, opt]))
                if opt == TELOPT_NAWS:
                    self._send_naws()
                elif opt == TELOPT_TTYPE:
                    self._send_ttype()
            else:
                dbg("iac", f"send  IAC WONT {_opt(opt)} (unsupported)")
                self._transmit(bytes([IAC, WONT, opt]))

        elif cmd == WONT:
            dbg("iac", f"server WONT {_opt(opt)} — sending DONT")
            self._transmit(bytes([IAC, DONT, opt]))

        elif cmd == DONT:
            dbg("iac", f"server DONT {_opt(opt)} — sending WONT")
            self._transmit(bytes([IAC, WONT, opt]))

    def _subnegotiate(self, opt: int, data: bytearray, full_buf: bytearray, after_se: int):
        if opt == TELOPT_TTYPE:
            if data and data[0] == 1:
                dbg("iac", "TTYPE SEND request — replying")
                self._send_ttype()
        elif opt == TELOPT_MCCP2:
            dbg("mccp", "IAC SB MCCP2 IAC SE — activating MCCP2")
            self._start_mccp2(full_buf, after_se)
        elif opt == TELOPT_GMCP:
            self._handle_gmcp(data)

    # ── MCCP2 ───────────────────────────────────────────────────────

    def _start_mccp2(self, buf: bytearray, offset: int):
        with self._lock:
            if self._mccp2_on:
                dbg("mccp", "_start_mccp2: already active")
                return
            self._mccp2_on = True
            self._zlib_dc  = zlib.decompressobj(zlib.MAX_WBITS)

        dbg("mccp", f"MCCP2 decompressor ready; buf len={len(buf)}, tail offset={offset}")
        self.mccp_active.emit(True)

        if offset < len(buf):
            tail = bytes(buf[offset:])
            del buf[offset:]
            dbg("mccp", f"decompressing initial tail: {len(tail)} bytes")
            with self._lock:
                dc = self._zlib_dc
            if dc:
                try:
                    decompressed = dc.decompress(tail)
                    buf.extend(decompressed)
                    dbg("mccp", f"initial tail → {len(decompressed)} decompressed bytes")
                except zlib.error as e:
                    dbg("error", f"MCCP2 initial tail decompression error: {e}")
                    buf.extend(tail)
        else:
            dbg("mccp", "no tail bytes to decompress yet")

    # ── Helpers ─────────────────────────────────────────────────────

    def _send_naws(self):
        dbg("iac", f"sending NAWS {_COLS}x{_ROWS}")
        self._transmit(bytes([
            IAC, SB, TELOPT_NAWS,
            (_COLS >> 8) & 0xFF, _COLS & 0xFF,
            (_ROWS >> 8) & 0xFF, _ROWS & 0xFF,
            IAC, SE,
        ]))

    def _send_ttype(self):
        name = b"MUD Client"
        dbg("iac", f"sending TTYPE {name!r}")
        self._transmit(bytes([IAC, SB, TELOPT_TTYPE, 0]) + name + bytes([IAC, SE]))

    def _gmcp_hello(self):
        payload = json.dumps({"client": "MUD Client", "version": "1.0"})
        dbg("gmcp", f"sending Core.Hello: {payload}")
        self._send_gmcp("Core.Hello", payload)

    def _send_gmcp(self, package: str, data: str):
        body = f"{package} {data}".encode("utf-8")
        self._transmit(bytes([IAC, SB, TELOPT_GMCP]) + body + bytes([IAC, SE]))

    def _handle_gmcp(self, data: bytearray):
        text    = data.decode("utf-8", errors="replace")
        parts   = text.split(" ", 1)
        package = parts[0].strip()
        payload: object = {}
        if len(parts) > 1:
            try:
                payload = json.loads(parts[1])
            except (json.JSONDecodeError, ValueError):
                payload = parts[1]
        dbg("gmcp", f"recv  package={package!r}  payload={str(payload)[:80]}")
        self.gmcp_received.emit(package, payload)
