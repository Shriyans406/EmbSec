"""Fakes for tests that involve builds, firmware images and the board during a deployment."""
import hashlib
import struct

from app.build.engine import SUCCESS as BUILD_SUCCESS
from app.build.engine import BuildResult, BuildStore, hash_sources
from app.build.uf2 import MAGIC0, MAGIC1, MAGIC_END, RP2040_FAMILY_ID, XIP_BASE
from app.deploy.board import BOOTSEL, RUNNING, UNRESPONSIVE, BoardState, BootloaderError

UID = "DE61C48787331A35"


def make_uf2(n=4, seed=0):
    out = b""
    for i in range(n):
        hdr = struct.pack("<8I", MAGIC0, MAGIC1, 0x2000, XIP_BASE + 256 * i, 256, i, n, RP2040_FAMILY_ID)
        out += hdr + bytes([(seed + j) % 256 for j in range(256)]) + bytes(476 - 256) + struct.pack("<I", MAGIC_END)
    return out


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def running(build_id, src, fw="0.3.0", uid=UID):
    return BoardState(RUNNING, port="COM8", uid=uid, fw_name="AEISP-DEV", fw_version=fw,
                      build_id=str(build_id), src_hash=src)


class FakeBoard:
    """A board in memory. `outcomes` maps image sha256 -> the state the board shows after flashing that image."""

    def __init__(self, state=None):
        self.state = state or running(0, "none", fw="0.2.0")
        self.outcomes = {}
        self.fail_bootloader = False
        self.in_bootloader = False
        self.flashed = []

    def identify(self, handshake_tries=3):
        return BoardState(BOOTSEL, drive="E:\\") if self.in_bootloader else self.state

    def enter_bootloader(self, log):
        if self.fail_bootloader:
            raise BootloaderError("simulated: bootloader never appeared")
        self.in_bootloader = True
        return "serial-command"

    def flash(self, path, log):
        data = open(path, "rb").read()
        self.flashed.append(sha(data))
        self.in_bootloader = False
        self.state = self.outcomes.get(sha(data), BoardState(UNRESPONSIVE, port="COM8", uid=UID, detail="silent"))

    def wait_for_firmware(self, timeout, log):
        return self.state

    def health_check(self, log):
        return True, "ok"


def add_build(services, build_id=1, seed=1, src=None, status=BUILD_SUCCESS, project="demo",
              files=None, diagnostics=None, flash_bytes=0, ram_bytes=0):
    """Create a build record, its UF2 and its source snapshot, as the build engine would have."""
    cfg = services.cfg
    data = make_uf2(4, seed=seed)
    d = cfg.build.artifacts_dir / str(build_id)
    (d / "app_src").mkdir(parents=True, exist_ok=True)
    (d / "firmware.uf2").write_bytes(data)
    for name, text in (files or {"app.c": "int x;\n"}).items():
        (d / "app_src" / name).write_bytes(text.encode("utf-8"))
    (d / "build.log").write_bytes(b"line1\nline2\nline3\n")
    if src is None:                     # like the build engine: the hash of the saved sources
        src = hash_sources([p for p in (d / "app_src").iterdir() if p.is_file()])
    store = BuildStore(cfg.db_path)
    bid = store.start(project, "2026-01-01T00:00:00+00:00")
    assert bid == build_id, (bid, build_id)
    r = BuildResult(id=bid, project=project, status=status, sources_sha256=src, harness_version="0.3.0",
                    uf2_sha256=sha(data), uf2_valid=True, finished_at="x", duration_s=1.0,
                    artifact_dir=str(d), uf2_path=str(d / "firmware.uf2"), message="seeded for tests",
                    diagnostics=diagnostics or [], flash_bytes=flash_bytes, ram_bytes=ram_bytes)
    store.finish(r)
    return data, src[:8]
