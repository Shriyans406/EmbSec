"""The board manager: owns the one shared serial connection and a lock, so that only one operation at a time
uses the board.

Why a lock: on Windows a COM port can be open in only one place. A deployment needs the port for itself, so
while it runs, ordinary commands are refused with "board busy" instead of failing in confusing ways.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from ..errors import ApiError
from ..hardware.discovery import list_all_ports
from ..hardware.link import LinkError, SerialLink
from ..hardware.protocol import parse_reply

log = logging.getLogger("aeisp.device")

# Commands the API may send. BOOTSEL and HANG are deliberately NOT here: the bootloader is reached only through
# the deployment tools (which have safeguards), and HANG exists only for manual testing in the terminal tool.
SAFE_VERBS = {"HELLO", "PING", "ECHO", "UPTIME", "VERSION", "INFO", "STATUS", "LED", "BLINK", "TIMER", "TEMP",
              "RESETINFO", "APP"}
OPERATOR_VERBS = {"REBOOT"}

_VERB = re.compile(r"^(?:#\d{1,5}\s+)?([A-Z][A-Z0-9_]*)")


class DeviceManager:
    def __init__(self, link_factory: Callable[[], SerialLink] = SerialLink) -> None:
        self._mutex = threading.Lock()
        self._owner: Optional[str] = None
        self._link = link_factory()

    @property
    def owner(self) -> Optional[str]:
        return self._owner

    @contextmanager
    def exclusive(self, who: str, wait_s: float = 0.0, release_port: bool = True):
        """Hold the board for `who`. Raises 409 'board_busy' if somebody else has it.
        release_port=True closes the shared serial connection first, so the holder can open the COM port itself."""
        got = self._mutex.acquire(timeout=wait_s) if wait_s > 0 else self._mutex.acquire(blocking=False)
        if not got:
            raise ApiError(409, "board_busy", f"the board is busy: {self._owner or 'another operation'} is using it")
        self._owner = who
        try:
            if release_port:
                self._link.close()
            yield
        finally:
            self._owner = None
            self._mutex.release()

    # ---------------------------------------------------------------- read
    def ports(self) -> list:
        return [p.to_dict() for p in list_all_ports()]

    def status(self, who: str = "status") -> dict:
        if self._mutex.locked():
            return {"connected": None, "info": None, "busy": self._owner, "error": None}
        try:
            with self.exclusive(who, wait_s=2.0, release_port=False):
                if not self._link.is_connected:
                    try:
                        self._link.connect()
                    except LinkError as e:
                        return {"connected": False, "info": None, "busy": None, "error": str(e)}
                return {"connected": True, "info": self._link.info.to_dict() if self._link.info else None,
                        "busy": None, "error": None}
        except ApiError as e:
            if e.code == "board_busy":
                return {"connected": None, "info": None, "busy": self._owner, "error": None}
            raise

    # ---------------------------------------------------------------- command
    def command(self, text: str, who: str, operator: bool = False) -> dict:
        if not text or len(text) > 120 or "\n" in text or "\r" in text:
            raise ApiError(422, "bad_command", "a command must be 1-120 characters on a single line")
        m = _VERB.match(text)
        verb = m.group(1) if m else ""
        if verb in OPERATOR_VERBS and not operator:
            raise ApiError(403, "forbidden", f"{verb} needs the 'operator' role")
        if verb not in SAFE_VERBS | OPERATOR_VERBS:
            raise ApiError(422, "command_not_allowed",
                           f"command not allowed through the API: {verb or text.split(' ')[0]!r}")
        t0 = time.perf_counter()
        with self.exclusive(who, wait_s=3.0, release_port=False):
            # retries=0: never silently repeat a command that may already have had an effect (LED TOGGLE ...)
            reply = self._link.request(text, retries=0, reconnect_wait=5.0)
        parsed = parse_reply(reply)
        return {
            "command": text,
            "reply": reply,
            "ok": parsed.ok,
            "error_code": parsed.code,
            "error_name": parsed.name,
            "fields": parsed.fields if parsed.ok else {},
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }