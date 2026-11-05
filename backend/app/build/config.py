"""Locations and limits for the build engine. Reads the project's .env (the same file doctor.py uses)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]  # project root (backend/app/build/config.py -> up 3)


def _read_env_file(root: Path) -> dict:
    env: dict = {}
    p = root / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


@dataclass(frozen=True)
class BuildConfig:
    root: Path
    sdk_path: Path
    toolchain_path: Path
    picotool_path: Path
    cmake_exe: Path
    ninja_exe: Path
    size_exe: Path
    harness_dir: Path
    workspace_dir: Path
    artifacts_dir: Path
    build_tree_root: Path
    db_path: Path
    configure_timeout_s: int = 120
    build_timeout_s: int = 600
    flash_capacity_bytes: int = 4 * 1024 * 1024   # Shrike Lite: 4 MiB QSPI flash
    ram_capacity_bytes: int = 264 * 1024          # RP2040: 264 KiB SRAM


def load_config(root: Optional[Path] = None) -> BuildConfig:
    root = Path(root) if root else ROOT
    env = _read_env_file(root)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or env.get(key, default)

    sdk = Path(get("PICO_SDK_PATH"))
    pico_root = sdk.parent  # e.g. C:\Program Files\Raspberry Pi\Pico SDK v1.5.1
    toolchain = Path(get("PICO_TOOLCHAIN_PATH"))
    tree_root = get("AEISP_BUILD_TREE_ROOT") or str(Path.home() / ".aeisp" / "build_trees")
    return BuildConfig(
        root=root,
        sdk_path=sdk,
        toolchain_path=toolchain,
        picotool_path=Path(get("PICOTOOL_PATH")),
        cmake_exe=pico_root / "cmake" / "bin" / "cmake.exe",
        ninja_exe=pico_root / "ninja" / "ninja.exe",
        size_exe=toolchain / "arm-none-eabi-size.exe",
        harness_dir=root / "firmware" / "harness",
        workspace_dir=root / "firmware" / "workspace",
        artifacts_dir=root / "data" / "artifacts",
        build_tree_root=Path(tree_root),
        db_path=root / "data" / "aeisp.db",
    )