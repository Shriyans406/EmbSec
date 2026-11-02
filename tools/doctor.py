"""Phase 0 environment checker. Run: python tools/doctor.py"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
results = []


def record(name, ok, detail="", fix="", required=True):
    results.append((name, ok, detail, fix, required))


def run(cmd, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def cfg(key):
    return os.environ.get(key) or ENV.get(key, "")


def check_python():
    v = sys.version_info
    record("Python >= 3.11", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}", "Install Python 3.11+")


def check_cmd(name, args, minimum=None, fix=""):
    exe = shutil.which(name)
    if not exe:
        record(name, False, "not on PATH", fix)
        return
    rc, out = run([exe] + args)
    first = out.splitlines()[0] if out else ""
    record(name, rc == 0, first, fix)


def check_pico():
    sdk = cfg("PICO_SDK_PATH")
    ok = bool(sdk) and (Path(sdk) / "pico_sdk_init.cmake").exists()
    ver = ""
    if ok:
        t = (Path(sdk) / "pico_sdk_version.cmake").read_text()
        parts = [re.search(rf"PICO_SDK_VERSION_{n} (\d+)", t).group(1) for n in ("MAJOR", "MINOR", "REVISION")]
        ver = ".".join(parts)
    record("Pico SDK", ok, f"{sdk} (v{ver})" if ok else sdk or "PICO_SDK_PATH unset", "Install Pico SDK / set PICO_SDK_PATH in .env")
    tiny = Path(sdk) / "lib" / "tinyusb" / "src" / "tusb.h" if sdk else None
    record("TinyUSB submodule", bool(tiny and tiny.exists()), str(tiny or ""), "git submodule update --init in pico-sdk")

    tc = cfg("PICO_TOOLCHAIN_PATH")
    gcc = Path(tc) / "arm-none-eabi-gcc.exe" if tc else None
    if gcc and gcc.exists():
        rc, out = run([str(gcc), "--version"])
        record("ARM GCC (pinned)", rc == 0, out.splitlines()[0], "Fix PICO_TOOLCHAIN_PATH in .env")
    else:
        record("ARM GCC (pinned)", False, tc or "PICO_TOOLCHAIN_PATH unset", "Set PICO_TOOLCHAIN_PATH in .env")

    pt = cfg("PICOTOOL_PATH")
    if pt and Path(pt).exists():
        rc, out = run([pt, "version"])
        record("picotool", rc == 0, out.splitlines()[0] if out else "", "Fix PICOTOOL_PATH")
    else:
        record("picotool", False, pt or "unset", "Fix PICOTOOL_PATH in .env")


def check_cmake_ninja():
    check_cmd("cmake", ["--version"], fix="Install CMake >= 3.13")
    check_cmd("ninja", ["--version"], fix="Install Ninja")


def venv_python() -> Path:
    base = cfg("VENV_PATH") or str(Path.home() / ".venvs" / "aeisp")
    return Path(base) / "Scripts" / "python.exe"


def check_venv():
    py = venv_python()
    if not py.exists():
        record("Backend venv", False, f"{py} missing", "py -3.13 -m venv %USERPROFILE%\\.venvs\\aeisp")
        return
    rc, out = run([str(py), "-c", "import fastapi,serial,sqlalchemy,langgraph,langchain_google_genai;print('imports ok')"])
    record("Backend deps", rc == 0, out.splitlines()[-1] if out else "", "backend/.venv/Scripts/pip install -r backend/requirements.txt")


def check_frontend():
    nm = ROOT / "frontend" / "node_modules"
    record("Frontend deps", nm.exists(), str(nm), "cd frontend && npm install")


def check_env_file():
    key = cfg("GEMINI_API_KEY")
    record("Gemini API key set", bool(key), "set" if key else "empty", "Put GEMINI_API_KEY in .env")


def check_gemini():
    key = cfg("GEMINI_API_KEY")
    if not key:
        record("Gemini API reachable", False, "skipped: no key", "", required=False)
        return
    py = venv_python()
    code = (
        "from google import genai;import sys;"
        f"c=genai.Client(api_key={key!r});"
        f"r=c.models.generate_content(model={cfg('GEMINI_MODEL') or 'gemini-2.5-flash'!r},contents='Reply with the single word: pong');"
        "print(r.text.strip())"
    )
    rc, out = run([str(py), "-c", code])
    record("Gemini API reachable", rc == 0, out.splitlines()[-1][:80] if out else "", "Check key / model name / network", required=False)


def check_board():
    ps = (
        "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_2E8A' } | "
        "ForEach-Object { $_.FriendlyName + ' | ' + $_.InstanceId }"
    )
    rc, out = run(["powershell", "-NoProfile", "-Command", ps])
    record("RP2040 board on USB (VID 2E8A)", bool(out.strip()), out.strip()[:100] or "not detected",
           "Plug in the Shrike Lite (hold BOOT while plugging for bootloader mode)", required=False)


def check_git():
    check_cmd("git", ["--version"], fix="Install Git")
    rc, _ = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT)
    record("Git repository", rc == 0, "", "git init", required=True)


def check_node():
    check_cmd("node", ["--version"], fix="Install Node LTS")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        rc, out = run([npm, "--version"])
        record("npm", rc == 0, out.splitlines()[0] if out else "", "Install Node LTS")
    else:
        record("npm", False, "not on PATH", "Install Node LTS")


def main():
    check_python()
    check_git()
    check_node()
    check_cmake_ninja()
    check_pico()
    check_venv()
    check_frontend()
    check_env_file()
    check_gemini()
    check_board()

    width = max(len(r[0]) for r in results)
    failed_required = 0
    for name, ok, detail, fix, required in results:
        tag = "PASS" if ok else ("FAIL" if required else "WARN")
        print(f"[{tag}] {name.ljust(width)}  {detail}")
        if not ok:
            if fix:
                print(f"       fix: {fix}")
            if required:
                failed_required += 1
    print()
    print("ALL REQUIRED CHECKS PASSED" if failed_required == 0 else f"{failed_required} required check(s) failed")
    return 1 if failed_required else 0


if __name__ == "__main__":
    sys.exit(main())
