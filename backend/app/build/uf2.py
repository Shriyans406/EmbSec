"""Structural validation of UF2 firmware images for the RP2040."""
from __future__ import annotations

import hashlib
import struct

MAGIC0 = 0x0A324655  # "UF2\n"
MAGIC1 = 0x9E5D5157
MAGIC_END = 0x0AB16F30
FLAG_FAMILY_ID = 0x00002000
RP2040_FAMILY_ID = 0xE48BFF56
XIP_BASE = 0x10000000
BLOCK = 512


def validate_uf2(data: bytes, flash_capacity: int = 4 * 1024 * 1024) -> dict:
    """Return a report dict: {valid, problems, blocks, payload_bytes, start_address, end_address, sha256}."""
    problems = []
    report = {
        "valid": False,
        "problems": problems,
        "bytes": len(data),
        "blocks": 0,
        "payload_bytes": 0,
        "start_address": None,
        "end_address": None,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) == 0:
        problems.append("file is empty")
        return report
    if len(data) % BLOCK != 0:
        problems.append(f"size {len(data)} is not a multiple of {BLOCK}")
        return report

    n = len(data) // BLOCK
    report["blocks"] = n
    first = last_end = None
    payload_total = 0
    for i in range(n):
        blk = data[i * BLOCK:(i + 1) * BLOCK]
        m0, m1, flags, addr, size, block_no, num_blocks, family = struct.unpack_from("<8I", blk, 0)
        (m_end,) = struct.unpack_from("<I", blk, BLOCK - 4)
        if m0 != MAGIC0 or m1 != MAGIC1 or m_end != MAGIC_END:
            problems.append(f"block {i}: bad magic numbers")
            break
        if not (flags & FLAG_FAMILY_ID) or family != RP2040_FAMILY_ID:
            problems.append(f"block {i}: not tagged for the RP2040 (family 0x{family:08X})")
            break
        if block_no != i or num_blocks != n:
            problems.append(f"block {i}: numbering wrong (block_no={block_no}, num_blocks={num_blocks}, expected {n})")
            break
        if size != 256:
            problems.append(f"block {i}: payload size {size}, expected 256")
            break
        if addr < XIP_BASE or addr + size > XIP_BASE + flash_capacity:
            problems.append(f"block {i}: address 0x{addr:08X} outside flash (0x{XIP_BASE:08X}..0x{XIP_BASE + flash_capacity:08X})")
            break
        if last_end is not None and addr < last_end:
            problems.append(f"block {i}: address 0x{addr:08X} overlaps or goes backwards")
            break
        if first is None:
            first = addr
            if addr != XIP_BASE:
                problems.append(f"image starts at 0x{addr:08X}, expected 0x{XIP_BASE:08X} (boot loader stage 2)")
                break
        last_end = addr + size
        payload_total += size

    report["payload_bytes"] = payload_total
    report["start_address"] = first
    report["end_address"] = last_end
    report["valid"] = not problems
    return report