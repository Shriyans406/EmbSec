"""Serial link to the RP2040: connect, handshake, request/response, reconnect, logging.

Protocol (Phase 1, line based): host sends "<COMMAND>\\n", device answers one line
starting with "OK" or "ERR". See firmware/phase1_echo/main.c.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import serial

from .discovery import find_rp2040_ports

ROOT = Path(__file__).resolve().parents[3]  # project root
DEFAULT_LOG = ROOT / "data" / "logs" / "serial.jsonl"


class LinkError(Exception):
    """Any communication failure."""


class NotConnected(LinkError):
    """No port / port lost / port could not be opened."""


class LinkTimeout(LinkError):
    """Device did not answer in time."""


@dataclass
class DeviceInfo:
    port: str
    firmware: str
    version: str
    uid: str

    def to_dict(self) -> dict:
        return asdict(self)


class SerialLink:
    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = 115200,
        timeout: float = 1.0,
        log_path: Optional[Path] = DEFAULT_LOG,
        serial_factory: Callable[..., "serial.Serial"] = serial.Serial,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.log_path = Path(log_path) if log_path else None
        self._factory = serial_factory
        self._lock = threading.RLock()
        self._ser = None
        self.info: Optional[DeviceInfo] = None

    # ---------- state ----------
    @property
    def is_connected(self) -> bool:
        return self._ser is not None and bool(getattr(self._ser, "is_open", False))

    # ---------- logging ----------
    def _log(self, direction: str, text: str) -> None:
        if not self.log_path:
            return
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "dir": direction,
            "text": text,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass  # logging must never break communication

    # ---------- connection ----------
    def _pick_port(self) -> str:
        if self.port:
            return self.port
        ports = find_rp2040_ports()
        if not ports:
            raise NotConnected("no RP2040 serial port found (USB vendor ID 2E8A)")
        return ports[0].device

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._log("EVT", "closed")
            self._ser = None
            self.info = None

    def connect(self, handshake_tries: int = 3) -> DeviceInfo:
        with self._lock:
            self.close()
            dev = self._pick_port()
            try:
                ser = self._factory(dev, self.baud, timeout=self.timeout, write_timeout=self.timeout)
                ser.dtr = True  # USB CDC firmware only talks once DTR is asserted
            except (serial.SerialException, OSError) as e:
                self._log("ERR", f"open {dev} failed: {e}")
                raise NotConnected(f"cannot open {dev}: {e}") from e
            self._ser = ser
            self._log("EVT", f"opened {dev}")

            last: Optional[Exception] = None
            for _ in range(handshake_tries):
                try:
                    reply = self._roundtrip("HELLO")
                    self.info = self._parse_hello(dev, reply)
                    self._log("EVT", f"handshake ok {self.info.firmware} {self.info.version} {self.info.uid}")
                    return self.info
                except NotConnected:
                    raise
                except LinkError as e:
                    last = e
                    time.sleep(0.2)
            self.close()
            raise LinkError(f"handshake failed on {dev}: {last}")

    @staticmethod
    def _parse_hello(dev: str, reply: str) -> DeviceInfo:
        parts = reply.split()
        if len(parts) < 5 or parts[0] != "OK" or parts[1] != "HELLO":
            raise LinkError(f"unexpected handshake reply: {reply!r}")
        return DeviceInfo(port=dev, firmware=parts[2], version=parts[3], uid=parts[4])

    def _reconnect(self, wait: float) -> None:
        deadline = time.monotonic() + wait
        last: Optional[Exception] = None
        while True:
            try:
                self.connect()
                return
            except LinkError as e:
                last = e
            if time.monotonic() >= deadline:
                raise NotConnected(f"could not reconnect within {wait:.0f}s: {last}")
            time.sleep(0.5)

    # ---------- I/O ----------
    def _roundtrip(self, command: str) -> str:
        if "\n" in command or "\r" in command:
            raise ValueError("command must not contain line breaks")
        if not self.is_connected:
            raise NotConnected("not connected")
        ser = self._ser
        try:
            ser.reset_input_buffer()
            self._log("TX", command)
            ser.write((command + "\n").encode("utf-8"))
            ser.flush()
            raw = ser.readline()
        except (serial.SerialException, OSError) as e:
            self._log("ERR", f"link lost: {e}")
            self.close()
            raise NotConnected(f"link lost: {e}") from e
        if not raw.endswith(b"\n"):
            self._log("ERR", f"timeout waiting for reply to {command[:40]!r}")
            raise LinkTimeout(f"no reply to {command[:40]!r} within {self.timeout}s")
        text = raw.decode("utf-8", errors="replace").strip()
        self._log("RX", text)
        return text

    def request(self, command: str, retries: int = 2, reconnect_wait: float = 10.0) -> str:
        """Send one command, return the raw reply line. Reconnects and retries on link failure.

        Only use retries > 0 for idempotent commands.
        """
        with self._lock:
            attempt = 0
            while True:
                try:
                    if not self.is_connected:
                        self._reconnect(reconnect_wait)
                    return self._roundtrip(command)
                except LinkError as e:
                    if attempt >= retries:
                        raise
                    attempt += 1
                    self._log("EVT", f"retry {attempt}/{retries} after: {e}")

    def command(self, command: str, **kw) -> str:
        """Like request(), but returns only the payload after 'OK ' and raises on 'ERR'."""
        reply = self.request(command, **kw)
        if reply == "OK" or reply.startswith("OK "):
            return reply[3:]
        raise LinkError(f"device rejected {command[:40]!r}: {reply}")