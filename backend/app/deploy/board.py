"""Talking to the physical board: what state is it in, how do we put it into the UF2 bootloader,
how do we copy a firmware image onto it, and how do we know the new firmware came up.

The deployment engine only uses the small interface of `Board`, so tests can swap in a simulated board.
"""
from __future__ import annotations

import ctypes
import shutil
import string
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

import serial

from ..hardware.discovery import find_rp2040_ports
from ..hardware.link import LinkError, SerialLink
from ..hardware.protocol import parse_reply

LogFn = Callable[[str], None]

RUNNING = "RUNNING"            # AEISP firmware is answering on the serial port
BOOTSEL = "BOOTSEL"            # the UF2 bootloader drive (RPI-RP2) is present
UNRESPONSIVE = "UNRESPONSIVE"  # a board is on USB but does not speak our protocol (stock / hung / unknown firmware)
ABSENT = "ABSENT"              # no board visible at all
MULTIPLE = "MULTIPLE"          # more than one board attached


class BoardError(Exception):
    pass


class BootloaderError(BoardError):
    """Could not get the board into the UF2 bootloader. Nothing was changed on the board."""


class FlashError(BoardError):
    pass


@dataclass
class BoardState:
    mode: str
    port: str = ""
    drive: str = ""
    uid: str = ""
    fw_name: str = ""
    fw_version: str = ""
    build_id: str = ""
    src_hash: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        if self.mode == RUNNING:
            b = f", build #{self.build_id} src {self.src_hash}" if self.build_id else ", no build number (older firmware)"
            return f"running {self.fw_name} {self.fw_version}{b}; port {self.port}; board id {self.uid}"
        if self.mode == BOOTSEL:
            return f"in the UF2 bootloader (drive {self.drive})"
        if self.mode == UNRESPONSIVE:
            return f"on USB ({self.port}, board id {self.uid or 'unknown'}) but not answering our protocol: {self.detail}"
        if self.mode == MULTIPLE:
            return f"more than one board attached: {self.detail}"
        return "no board found on USB"


# ---------------------------------------------------------------- Windows drive lookup
def find_boot_drive() -> Optional[str]:
    """Return 'E:\\' for the RPI-RP2 bootloader drive, or None. Only looks at local (removable or fixed) drives."""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except AttributeError:  # not Windows
        return None
    mask = kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        if kernel32.GetDriveTypeW(root) not in (2, 3):  # removable or fixed; skip network / CD / RAM drives
            continue
        label = ctypes.create_unicode_buffer(261)
        ok = kernel32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
        if ok and label.value == "RPI-RP2":
            return root
    return None


def read_boot_info(drive: str) -> str:
    try:
        text = (Path(drive) / "INFO_UF2.TXT").read_text(encoding="utf-8", errors="replace")
        return " / ".join(x.strip() for x in text.splitlines() if x.strip())[:160]
    except OSError:
        return ""


# ---------------------------------------------------------------- the real board
class Board:
    def __init__(self, bootloader_wait_s: float = 10.0, copy_timeout_s: float = 240.0,
                 reboot_wait_s: float = 40.0) -> None:
        self.bootloader_wait_s = bootloader_wait_s
        self.copy_timeout_s = copy_timeout_s
        self.reboot_wait_s = reboot_wait_s

    # ---- state ----
    def identify(self, handshake_tries: int = 3) -> BoardState:
        drive = find_boot_drive()
        if drive:
            return BoardState(BOOTSEL, drive=drive, detail=read_boot_info(drive))
        ports = find_rp2040_ports()
        if not ports:
            return BoardState(ABSENT)
        uids = {p.serial_number for p in ports if p.serial_number}
        if len(uids) > 1 or (len(ports) > 1 and not uids):
            return BoardState(MULTIPLE, detail=", ".join(f"{p.device} ({p.serial_number})" for p in ports))
        port = ports[0]
        link = SerialLink(port=port.device, timeout=1.0)
        try:
            info = link.connect(handshake_tries=handshake_tries)
        except LinkError as e:
            link.close()
            return BoardState(UNRESPONSIVE, port=port.device, uid=port.serial_number or "", detail=str(e))
        st = BoardState(RUNNING, port=port.device, uid=info.uid, fw_name=info.firmware, fw_version=info.version)
        try:
            r = parse_reply(link.request("VERSION", retries=0))
            if r.ok:
                st.build_id = r.fields.get("build", "")
                st.src_hash = r.fields.get("src", "")
        except LinkError:
            pass
        link.close()
        return st

    # ---- bootloader ----
    def _wait_drive(self, seconds: float) -> Optional[str]:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            d = find_boot_drive()
            if d:
                return d
            time.sleep(0.4)
        return None

    def enter_bootloader(self, log: LogFn) -> str:
        """Returns the method used. Raises BootloaderError if the board never shows the RPI-RP2 drive."""
        if find_boot_drive():
            return "already-in-bootloader"
        ports = find_rp2040_ports()
        if not ports:
            raise BootloaderError("no board on USB and no RPI-RP2 drive. Plug the board in, or hold its BOOT "
                                  "button while plugging it in.")
        port = ports[0].device

        log(f"method 1: asking the firmware to reboot into the bootloader (BOOTSEL command on {port})")
        link = SerialLink(port=port, timeout=1.0)
        try:
            link.connect(handshake_tries=2)
            log(f"  board replied: {link.request('BOOTSEL', retries=0)}")
        except LinkError as e:
            log(f"  not available: {e}")
        finally:
            link.close()
        if self._wait_drive(self.bootloader_wait_s):
            return "serial-command"

        ports = find_rp2040_ports()
        if ports:
            log(f"method 2: 1200-baud reset on {ports[0].device} (works even if the program is stuck)")
            try:
                s = serial.Serial(ports[0].device, 1200, timeout=0.5)
                s.dtr = True
                time.sleep(0.4)
                s.close()
            except (serial.SerialException, OSError) as e:
                log(f"  failed: {e}")
            if self._wait_drive(self.bootloader_wait_s):
                return "1200-baud-reset"

        raise BootloaderError("the board did not enter the bootloader. Hold its BOOT button while plugging in "
                              "USB (release after the RPI-RP2 drive appears), then run the command again.")

    # ---- flashing ----
    def flash(self, image_path: Path, log: LogFn) -> None:
        drive = find_boot_drive()
        if not drive:
            raise FlashError("RPI-RP2 drive not present")
        dest = Path(drive) / "firmware.uf2"
        size = Path(image_path).stat().st_size
        log(f"copying {Path(image_path).name} ({size:,} bytes) to {drive}")
        t0 = time.monotonic()
        try:
            shutil.copyfile(image_path, dest)
        except OSError as e:
            # The drive vanishes the instant the board finishes writing, so Windows can report an error on
            # the very last write. That is normal if the drive is really gone.
            time.sleep(1.5)
            if find_boot_drive() is not None:
                raise FlashError(f"copy failed and the bootloader drive is still there: {e}") from e
            log(f"  (drive disconnected while closing the file - normal: {e})")
        end = time.monotonic() + self.copy_timeout_s
        while find_boot_drive() is not None:
            if time.monotonic() > end:
                raise FlashError(f"the board did not finish flashing within {self.copy_timeout_s:.0f} s")
            time.sleep(0.5)
        log(f"  flash finished in {time.monotonic() - t0:.1f} s; the board is rebooting")

    def wait_for_firmware(self, timeout: float, log: LogFn) -> BoardState:
        """Wait for the board to reappear after flashing and identify what is running."""
        end = time.monotonic() + (timeout or self.reboot_wait_s)
        while time.monotonic() < end:
            if not find_boot_drive() and find_rp2040_ports():
                break
            time.sleep(0.5)
        else:
            return BoardState(ABSENT, detail="the board did not reappear on USB")
        time.sleep(1.0)  # let the firmware finish starting
        log("board is back on USB; talking to it ...")
        state = BoardState(ABSENT)
        while time.monotonic() < end:
            state = self.identify(handshake_tries=2)
            if state.mode in (RUNNING, MULTIPLE):
                return state
            time.sleep(1.0)
        return state

    def health_check(self, log: LogFn) -> Tuple[bool, str]:
        ports = find_rp2040_ports()
        if not ports:
            return False, "no board on USB"
        link = SerialLink(port=ports[0].device, timeout=1.0)
        try:
            link.connect(handshake_tries=2)
            ping = link.request("PING", retries=0)
            status = link.request("STATUS", retries=0)
            link.close()
        except LinkError as e:
            link.close()
            return False, str(e)
        if ping != "OK PONG" or not status.startswith("OK STATUS"):
            return False, f"unexpected replies: PING -> {ping!r}, STATUS -> {status!r}"
        return True, "PING and STATUS answered normally"