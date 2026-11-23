"""Size report of a build: flash / RAM use, the biggest symbols, the app's own symbols and the change against the
project's previous successful build. Uses arm-none-eabi-nm on the build's firmware.elf."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .security_scan import _FUNC, strip_code

NM_LINE = re.compile(r"^([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([A-Za-z])\s+(\S+)$")
KIND = {"t": "code", "T": "code", "d": "data", "D": "data", "b": "bss", "B": "bss", "r": "rodata", "R": "rodata"}


def read_symbols(nm_exe: Path, elf: Path, timeout_s: int = 30) -> List[dict]:
    if not Path(nm_exe).is_file() or not Path(elf).is_file():
        return []
    try:
        out = subprocess.run([str(nm_exe), "--print-size", "--size-sort", "--radix=x", str(elf)],
                             capture_output=True, text=True, timeout=timeout_s).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    syms = []
    for line in out.splitlines():
        m = NM_LINE.match(line.strip())
        if m and m.group(3) in KIND:
            syms.append({"name": m.group(4), "size": int(m.group(2), 16), "kind": KIND[m.group(3)]})
    syms.sort(key=lambda s: -s["size"])
    return syms


def app_names(sources: Dict[str, str]) -> set:
    names = set()
    for text in sources.values():
        code = strip_code(text)
        names.update(m.group(1) for m in _FUNC.finditer(code))
        names.update(re.findall(r"^\s*(?:static\s+)?(?:const\s+)?[A-Za-z_]\w*\s+\**\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:=|;)",
                                code, re.M))
    return names


def size_report(build: dict, previous: Optional[dict], nm_exe: Path, sources: Dict[str, str],
                top: int = 15) -> dict:
    adir = Path(build.get("artifact_dir") or "")
    syms = read_symbols(nm_exe, adir / "firmware.elf")
    mine = app_names(sources)
    app_syms = [s for s in syms if s["name"] in mine]
    out = {
        "build_id": build["id"], "project": build["project"], "status": build["status"],
        "flash_bytes": build.get("flash_bytes", 0), "ram_bytes": build.get("ram_bytes", 0),
        "flash_pct": build.get("flash_pct", 0.0), "ram_pct": build.get("ram_pct", 0.0),
        "uf2_bytes": build.get("uf2_bytes", 0),
        "symbols_available": bool(syms),
        "largest_symbols": syms[:top],
        "app_symbols": app_syms[:40],
        "app_bytes": {k: sum(s["size"] for s in app_syms if s["kind"] == k) for k in ("code", "rodata", "data", "bss")},
        "compared_to": None,
    }
    if previous:
        out["compared_to"] = {
            "build_id": previous["id"],
            "flash_delta": out["flash_bytes"] - previous.get("flash_bytes", 0),
            "ram_delta": out["ram_bytes"] - previous.get("ram_bytes", 0),
        }
    return out
