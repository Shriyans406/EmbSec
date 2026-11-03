"""Find serial ports and recognise RP2040 boards by USB vendor ID."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from serial.tools import list_ports as _lp

RP2040_VID = 0x2E8A  # Raspberry Pi


@dataclass(frozen=True)
class PortInfo:
    device: str
    vid: Optional[int]
    pid: Optional[int]
    serial_number: Optional[str]
    description: str

    @property
    def is_rp2040(self) -> bool:
        return self.vid == RP2040_VID

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vid"] = None if self.vid is None else f"{self.vid:04X}"
        d["pid"] = None if self.pid is None else f"{self.pid:04X}"
        d["is_rp2040"] = self.is_rp2040
        return d


def list_all_ports() -> list[PortInfo]:
    ports = [
        PortInfo(
            device=p.device,
            vid=p.vid,
            pid=p.pid,
            serial_number=p.serial_number,
            description=p.description or "",
        )
        for p in _lp.comports()
    ]
    return sorted(ports, key=lambda p: p.device)


def find_rp2040_ports(serial_number: Optional[str] = None) -> list[PortInfo]:
    found = [p for p in list_all_ports() if p.is_rp2040]
    if serial_number:
        found = [p for p in found if p.serial_number == serial_number]
    return found