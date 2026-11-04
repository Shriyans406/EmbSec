"""AEISP wire protocol v1 (host side). Mirrors firmware/phase2_device/src/proto.h.

Request :  [#<id> ]<COMMAND>[ <args>][*<CRC16>]
Reply   :  [#<id> ]OK <payload>[*<CRC16>]   or   [#<id> ]ERR <code> <NAME>[ <detail>][*<CRC16>]
CRC16   :  CRC-16/CCITT-FALSE over every character before the '*', written as 4 hex digits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

ERROR_NAMES = {
    1: "UNKNOWN_CMD",
    2: "BAD_ARGS",
    3: "BAD_RANGE",
    4: "BAD_CRC",
    5: "TOO_LONG",
    6: "BAD_FORMAT",
    7: "TIMEOUT",
    8: "BAD_STATE",
}

_CRC_TAIL = re.compile(r"\*([0-9A-Fa-f]{4})$")
_ID_HEAD = re.compile(r"^#(\d{1,5}) ")


def crc16(data) -> int:
    """CRC-16/CCITT-FALSE. Accepts str (ASCII/UTF-8) or bytes. crc16('123456789') == 0x29B1."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def add_crc(text: str) -> str:
    """Return `text` with the checksum suffix appended, e.g. 'PING' -> 'PING*6427'."""
    return f"{text}*{crc16(text):04X}"


def build_request(command: str, req_id: Optional[int] = None, with_crc: bool = False) -> str:
    """Build a request line (without the trailing newline)."""
    if req_id is not None and not 0 <= req_id <= 99999:
        raise ValueError("request id must be 0..99999")
    text = command if req_id is None else f"#{req_id} {command}"
    return add_crc(text) if with_crc else text


@dataclass
class Reply:
    raw: str
    ok: bool
    req_id: Optional[int]
    crc_present: bool
    crc_ok: Optional[bool]       # None when the reply carried no checksum
    body: str                    # text after "OK " / "ERR "
    code: Optional[int] = None   # error code for ERR replies
    name: Optional[str] = None   # error name for ERR replies
    detail: str = ""             # free text after the error name

    @property
    def fields(self) -> dict:
        """key=value pairs found in the body, e.g. 'LED state=ON' -> {'state': 'ON'}."""
        return dict(kv.split("=", 1) for kv in self.body.split() if "=" in kv)


def parse_reply(line: str) -> Reply:
    raw = line.strip()
    text = raw
    crc_present = False
    crc_ok: Optional[bool] = None
    m = _CRC_TAIL.search(text)
    if m:
        crc_present = True
        head = text[: m.start()]
        crc_ok = crc16(head) == int(m.group(1), 16)
        text = head
    req_id = None
    m = _ID_HEAD.match(text)
    if m:
        req_id = int(m.group(1))
        text = text[m.end():]
    if text == "OK" or text.startswith("OK "):
        return Reply(raw, True, req_id, crc_present, crc_ok, text[3:])
    if text.startswith("ERR "):
        parts = text[4:].split(" ", 2)
        try:
            code = int(parts[0])
        except ValueError:
            return Reply(raw, False, req_id, crc_present, crc_ok, text[4:])
        name = parts[1] if len(parts) > 1 else ERROR_NAMES.get(code, "?")
        detail = parts[2] if len(parts) > 2 else ""
        return Reply(raw, False, req_id, crc_present, crc_ok, text[4:], code, name, detail)
    return Reply(raw, False, req_id, crc_present, crc_ok, text)