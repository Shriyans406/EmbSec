"""The harness firmware's profiler, compiled and run on this PC (MinGW gcc), plus the simulated board's PROF."""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.device import validate_command
from sim_board import SimFirmware

HARNESS = Path(__file__).resolve().parents[2] / "firmware" / "harness"


def _gcc():
    for cand in (shutil.which("gcc"), r"C:\MinGW\bin\gcc.exe", r"C:\msys64\mingw64\bin\gcc.exe"):
        if cand and Path(cand).is_file():
            return cand
    return None


@pytest.mark.skipif(_gcc() is None, reason="no host C compiler (gcc) on this machine")
def test_the_profiler_in_c_on_the_host(tmp_path):
    gcc = _gcc()
    exe = tmp_path / "host_prof_test.exe"
    src = HARNESS / "src"
    env = None
    if Path(gcc).parent.is_dir():
        import os
        env = {**os.environ, "PATH": str(Path(gcc).parent) + os.pathsep + os.environ.get("PATH", "")}
    build = subprocess.run([gcc, "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{src}",
                            str(HARNESS / "test" / "host_prof_test.c"), str(src / "device.c"), str(src / "proto.c"),
                            str(src / "prof.c"), "-o", str(exe)], capture_output=True, text=True, env=env)
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=env)
    assert run.returncode == 0 and "ALL PASSED" in run.stdout, run.stdout


def test_the_firmware_version_is_04():
    text = (HARNESS / "src" / "device.h").read_text()
    assert '#define FW_VERSION "0.4.0"' in text
    assert "#define APP_API_VERSION 2" in (HARNESS / "src" / "app_api.h").read_text()
    assert "src/prof.c" in (HARNESS / "CMakeLists.txt").read_text()


def test_prof_is_a_safe_command():
    for cmd in ("PROF", "PROF RESET", "PROF CMD APP", "PROF APP 3"):
        assert validate_command(cmd) == "PROF"


def test_the_simulated_board_profiles_like_the_harness():
    fw = SimFirmware()
    assert fw.handle("PROF RESET") == "OK PROF reset"
    for _ in range(3):
        fw.handle("APP MUL 2 3")
    reply = fw.handle("PROF CMD APP")
    assert reply.startswith("OK PROF cmd=APP n=3 avg_us=41 min_us=40 max_us=42")
    assert fw.handle("PROF").startswith("OK PROF window_ms=")
    old = SimFirmware(fw="0.3.0")
    assert old.handle("PROF").startswith("ERR 1 UNKNOWN_CMD")
    slow = SimFirmware(bugs={"mul_slow"})
    slow.handle("APP MUL 2 3")
    assert "avg_us=400" in slow.handle("PROF CMD APP")
