"""Minimal client for OpenTTD's admin port (protocol version 3, OpenTTD 15.x).

Only what the harness needs: join, remote console, and the GameScript JSON channel.
Wire format: little-endian uint16 packet length (including itself), uint8 type, payload.
Strings are NUL-terminated UTF-8.
"""

from __future__ import annotations

import json
import select
import socket
import struct
import time
from collections import deque

# Packets we send.
ADMIN_JOIN = 0
ADMIN_QUIT = 1
ADMIN_UPDATE_FREQUENCY = 2
ADMIN_RCON = 5
ADMIN_GAMESCRIPT = 6

# Packets we receive.
SERVER_FULL = 100
SERVER_BANNED = 101
SERVER_ERROR = 102
SERVER_PROTOCOL = 103
SERVER_WELCOME = 104
SERVER_NEWGAME = 105
SERVER_SHUTDOWN = 106
SERVER_DATE = 107
SERVER_RCON = 120
SERVER_CONSOLE = 121
SERVER_GAMESCRIPT = 124
SERVER_RCON_END = 125

UPDATE_DATE = 0
UPDATE_CONSOLE = 6
UPDATE_GAMESCRIPT = 9
FREQ_DAILY = 0x02
FREQ_AUTOMATIC = 0x40


class AdminError(RuntimeError):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def u8(self) -> int:
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        (v,) = struct.unpack_from("<H", self.data, self.pos)
        self.pos += 2
        return v

    def u32(self) -> int:
        (v,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return v

    def string(self) -> str:
        end = self.data.index(b"\0", self.pos)
        s = self.data[self.pos:end].decode("utf-8", "replace")
        self.pos = end + 1
        return s


class AdminClient:
    def __init__(self, host: str, port: int, password: str, name: str = "ottd-bench"):
        self.host, self.port, self.password, self.name = host, port, password, name
        self.sock: socket.socket | None = None
        self._buf = b""
        self.welcome: dict = {}
        self.gamescript: deque[dict] = deque()
        self.console: deque[str] = deque(maxlen=500)
        self.date_days: int | None = None     # latest game date seen, in OpenTTD day numbers

    # ------------------------------------------------------------ transport

    def connect(self, timeout: float = 10.0) -> dict:
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._send(ADMIN_JOIN, _s(self.password) + _s(self.name) + _s("0.1"))
        deadline = time.monotonic() + timeout
        while not self.welcome:
            self._pump(deadline)
        self._send(ADMIN_UPDATE_FREQUENCY, struct.pack("<HH", UPDATE_GAMESCRIPT, FREQ_AUTOMATIC))
        self._send(ADMIN_UPDATE_FREQUENCY, struct.pack("<HH", UPDATE_CONSOLE, FREQ_AUTOMATIC))
        self._send(ADMIN_UPDATE_FREQUENCY, struct.pack("<HH", UPDATE_DATE, FREQ_DAILY))
        return self.welcome

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send(ADMIN_QUIT, b"")
        except OSError:
            pass
        self.sock.close()
        self.sock = None

    def _send(self, ptype: int, payload: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(struct.pack("<HB", len(payload) + 3, ptype) + payload)

    def _next_buffered(self) -> tuple[int, _Reader] | None:
        """Pop one complete packet from the receive buffer, record it, and return it."""
        if len(self._buf) < 2:
            return None
        (size,) = struct.unpack_from("<H", self._buf)
        if len(self._buf) < size:
            return None
        ptype = self._buf[2]
        body = self._buf[3:size]
        self._buf = self._buf[size:]
        self._record(ptype, _Reader(body))
        return ptype, _Reader(body)

    def poll(self) -> None:
        """Process everything that has already arrived, without waiting for more."""
        assert self.sock is not None
        while select.select([self.sock], [], [], 0)[0]:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AdminError("OpenTTD closed the admin connection")
            self._buf += chunk
        while self._next_buffered() is not None:
            pass

    def _pump(self, deadline: float) -> tuple[int, _Reader]:
        """Read exactly one packet, record it, and return it."""
        assert self.sock is not None
        while True:
            packet = self._next_buffered()
            if packet is not None:
                return packet
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for the OpenTTD admin port")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout as e:
                raise TimeoutError("timed out waiting for the OpenTTD admin port") from e
            if not chunk:
                raise AdminError("OpenTTD closed the admin connection")
            self._buf += chunk

    def _record(self, ptype: int, r: _Reader) -> None:
        if ptype == SERVER_WELCOME:
            self.welcome = {
                "server_name": r.string(),
                "revision": r.string(),
                "dedicated": bool(r.u8()),
                "map_name": r.string(),
                "seed": r.u32(),
                "landscape": r.u8(),
                "start_date": r.u32(),
                "map_x": r.u16(),
                "map_y": r.u16(),
            }
        elif ptype == SERVER_GAMESCRIPT:
            try:
                self.gamescript.append(json.loads(r.string()))
            except json.JSONDecodeError:
                pass
        elif ptype == SERVER_DATE:
            self.date_days = r.u32()
        elif ptype == SERVER_CONSOLE:
            origin, text = r.string(), r.string()
            self.console.append(f"[{origin}] {text}")
        elif ptype in (SERVER_ERROR, SERVER_FULL, SERVER_BANNED):
            code = r.u8() if ptype == SERVER_ERROR else -1
            # 6 = NETWORK_ERROR_NOT_AUTHORIZED, 10 = NETWORK_ERROR_WRONG_PASSWORD
            hint = " (wrong admin password, or allow_insecure_admin_login is off)" if code in (6, 10) else ""
            raise AdminError(f"admin port refused us: packet {ptype}, error code {code}{hint}")
        elif ptype == SERVER_SHUTDOWN:
            raise AdminError("OpenTTD server is shutting down")

    # ----------------------------------------------------------------- API

    def rcon(self, command: str, timeout: float = 30.0) -> list[str]:
        """Run a console command on the server and return its output lines."""
        self._send(ADMIN_RCON, _s(command))
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while True:
            ptype, r = self._pump(deadline)
            if ptype == SERVER_RCON:
                r.u16()  # colour
                lines.append(r.string())
            elif ptype == SERVER_RCON_END:
                return lines

    def send_gamescript(self, obj: dict) -> None:
        self._send(ADMIN_GAMESCRIPT, _s(json.dumps(obj, separators=(",", ":"))))

    def pump(self, deadline: float) -> None:
        """Block until one more packet has been processed (or raise TimeoutError at `deadline`)."""
        self._pump(deadline)


def _s(text: str) -> bytes:
    return text.encode("utf-8") + b"\0"
