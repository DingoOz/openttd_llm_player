"""JSON-RPC over the admin port to the LLMBridge game script."""

from __future__ import annotations

import json
import time

from .admin import AdminClient, AdminError

# One game day is 74 ticks of 27 ms on a dedicated server; there is no fast-forward.
SECONDS_PER_GAME_DAY = 74 * 0.027


class BridgeError(RuntimeError):
    """The game script did not answer (crashed, or the server died)."""


class Bridge:
    """Requests are matched to replies by id, so a slow request (advance) can be pending while
    quick ones are made, and console commands can be interleaved freely."""

    def __init__(self, admin: AdminClient):
        self.admin = admin
        self._next_id = 1
        self._fragments: dict[int, dict[int, str]] = {}   # request id -> {seq: json fragment}
        self._last_seq: dict[int, int] = {}

    def send(self, cmd: str, **args) -> int:
        rid = self._next_id
        self._next_id += 1
        self._fragments[rid] = {}
        self.admin.send_gamescript({"id": rid, "cmd": cmd, "args": args})
        return rid

    def _absorb(self) -> None:
        while self.admin.gamescript:
            msg = self.admin.gamescript.popleft()
            store = self._fragments.get(msg.get("id"))
            if store is None:
                continue   # reply to a request we gave up on
            store[msg["seq"]] = msg["d"]
            if msg.get("last"):
                self._last_seq[msg["id"]] = msg["seq"]

    def _take(self, rid: int) -> dict | None:
        last = self._last_seq.get(rid)
        parts = self._fragments.get(rid, {})
        if last is None or len(parts) <= last:
            return None
        text = "".join(parts[i] for i in range(last + 1))
        del self._fragments[rid], self._last_seq[rid]
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise BridgeError(f"unparseable reply from the game script: {text[:200]!r}") from e

    def poll(self, rid: int) -> dict | None:
        """The reply to request `rid` if it has fully arrived, else None. Never blocks."""
        try:
            self.admin.poll()
        except AdminError as e:
            raise BridgeError(str(e)) from e
        self._absorb()
        return self._take(rid)

    def wait(self, rid: int, timeout: float, cmd: str = "request") -> dict:
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._absorb()
                reply = self._take(rid)
                if reply is not None:
                    return reply
                self.admin.pump(deadline)
        except TimeoutError as e:
            self._fragments.pop(rid, None)
            tail = "\n".join(list(self.admin.console)[-8:])
            raise BridgeError(f"game script did not answer '{cmd}' within {timeout:.0f}s. Recent console:\n{tail}") from e
        except AdminError as e:
            raise BridgeError(str(e)) from e

    def call(self, cmd: str, timeout: float = 30.0, **args) -> dict:
        """Send one command and return the decoded reply.

        Game-level failures come back as {"ok": False, "error": ...}; only transport
        failures raise.
        """
        return self.wait(self.send(cmd, **args), timeout, cmd)

    @staticmethod
    def advance_timeout(days: int) -> float:
        return days * SECONDS_PER_GAME_DAY * 2 + 30

    def advance(self, days: int) -> dict:
        """Let `days` game days pass, then re-pause. Blocks for about 2 s per day."""
        return self.call("advance", timeout=self.advance_timeout(days), days=int(days))
