"""Build engine: validates a project's app sources, builds them together with the harness firmware,
records the outcome (status, diagnostics, sizes, UF2 check) and keeps the artifacts.

Generated code only ever supplies .c/.h files. The CMake project, the harness sources and the
toolchain flags are ours, so a generated source file cannot run commands during the build.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import BuildConfig, load_config
from .diagnostics import Diagnostic, count, parse_output
from .uf2 import validate_uf2

# ---------------------------------------------------------------- statuses
RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
FAILED = "FAILED"        # the code does not compile / link
TIMEOUT = "TIMEOUT"      # the build ran out of time and was stopped
REJECTED = "REJECTED"    # the sources broke a safety rule; nothing was compiled
ERROR = "ERROR"          # the build system itself failed (not the code's fault)

# ---------------------------------------------------------------- source rules
PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
SOURCE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}\.(c|h)$")
BAD_INCLUDE_RE = re.compile(r'#\s*include\s*[<"]\s*(?:/|\\|[A-Za-z]:|\.\.)')
MAX_FILES = 20
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024

SKELETON_APP_C = '''\
/* Application code for project "__NAME__".
 * The harness firmware calls app_init() once, app_tick() about every millisecond (keep it short!)
 * and app_command() for every request line of the form  APP <args>.
 * See firmware/harness/src/app_api.h for the rules.
 */
#include <stdio.h>
#include <string.h>

#include "app_api.h"

void app_init(void) {
}

void app_tick(void) {
}

int app_command(const char *args, char *out, size_t cap) {
    if (strcmp(args, "HELLO") == 0) {
        snprintf(out, cap, "hello from __NAME__");
        return APP_OK;
    }
    snprintf(out, cap, "unknown app command");
    return APP_ERR_ARGS;
}
'''


class ProjectError(Exception):
    """A problem the caller can fix (unknown project, bad name, missing toolchain...)."""


# ---------------------------------------------------------------- result record
@dataclass
class BuildResult:
    id: int = 0
    project: str = ""
    status: str = RUNNING
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    errors: int = 0
    warnings: int = 0
    diagnostics: List[dict] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    sources_sha256: str = ""
    harness_version: str = ""
    harness_sha256: str = ""
    artifact_dir: str = ""
    uf2_path: str = ""
    uf2_bytes: int = 0
    uf2_sha256: str = ""
    uf2_valid: bool = False
    uf2_problems: List[str] = field(default_factory=list)
    flash_bytes: int = 0
    ram_bytes: int = 0
    flash_pct: float = 0.0
    ram_pct: float = 0.0
    picotool_name: str = ""
    log_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- history database
class BuildStore:
    """SQLite table of every build ever started. Schema version is kept in PRAGMA user_version."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS builds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_s REAL,
                    errors INTEGER,
                    warnings INTEGER,
                    sources_sha256 TEXT,
                    uf2_sha256 TEXT,
                    artifact_dir TEXT,
                    message TEXT,
                    result_json TEXT
                )"""
            )
            c.execute("PRAGMA user_version = 1")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    def start(self, project: str, started_at: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO builds (project, status, started_at) VALUES (?, ?, ?)",
                (project, RUNNING, started_at),
            )
            return int(cur.lastrowid)

    def finish(self, r: BuildResult) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE builds SET status=?, finished_at=?, duration_s=?, errors=?, warnings=?,
                   sources_sha256=?, uf2_sha256=?, artifact_dir=?, message=?, result_json=? WHERE id=?""",
                (r.status, r.finished_at, r.duration_s, r.errors, r.warnings, r.sources_sha256,
                 r.uf2_sha256, r.artifact_dir, r.message, json.dumps(r.to_dict()), r.id),
            )

    def list(self, project: Optional[str] = None, limit: int = 20) -> List[dict]:
        sql = "SELECT id, project, status, started_at, duration_s, errors, warnings, sources_sha256, message FROM builds"
        args: list = []
        if project:
            sql += " WHERE project = ?"
            args.append(project)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def get(self, build_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
        return d


# ---------------------------------------------------------------- projects
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_dir(cfg: BuildConfig, name: str) -> Path:
    if not PROJECT_NAME_RE.match(name):
        raise ProjectError(
            f"bad project name {name!r}: use 2-32 characters, lower-case letters, digits and '_', starting with a letter"
        )
    return cfg.workspace_dir / name


def create_project(name: str, cfg: Optional[BuildConfig] = None) -> Path:
    cfg = cfg or load_config()
    pdir = project_dir(cfg, name)
    if pdir.exists():
        raise ProjectError(f"project {name!r} already exists at {pdir}")
    try:
        (pdir / "app").mkdir(parents=True)
        (pdir / "app" / "app.c").write_text(
            SKELETON_APP_C.replace("__NAME__", name), encoding="utf-8", newline="\n"
        )
        (pdir / "project.json").write_text(
            json.dumps({"name": name, "created": _now(), "app_api_version": 1}, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        _rmtree(pdir)  # never leave a half-made project behind
        raise
    return pdir


def list_projects(cfg: Optional[BuildConfig] = None) -> List[str]:
    cfg = cfg or load_config()
    if not cfg.workspace_dir.is_dir():
        return []
    return sorted(p.name for p in cfg.workspace_dir.iterdir() if (p / "app").is_dir())


def check_sources(app_dir: Path) -> Tuple[List[Path], List[str]]:
    """Safety rules for generated code. Returns (files, problems); build only if problems is empty."""
    problems: List[str] = []
    files: List[Path] = []
    if not app_dir.is_dir():
        return files, [f"app folder not found: {app_dir}"]
    total = 0
    for p in sorted(app_dir.iterdir(), key=lambda x: x.name.lower()):
        if p.is_symlink():
            problems.append(f"{p.name}: links are not allowed")
            continue
        if p.is_dir():
            problems.append(f"{p.name}: sub-folders are not allowed (keep all files directly in app/)")
            continue
        if not SOURCE_NAME_RE.match(p.name):
            problems.append(
                f"{p.name}: only .c and .h files with plain names (letters, digits, '_'; no spaces) are allowed"
            )
            continue
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            problems.append(f"{p.name}: {size} bytes is over the {MAX_FILE_BYTES}-byte limit per file")
            continue
        raw = p.read_bytes()
        if b"\x00" in raw:
            problems.append(f"{p.name}: contains binary (NUL) data")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{p.name}: is not valid UTF-8 text")
            continue
        if BAD_INCLUDE_RE.search(text):
            problems.append(f"{p.name}: #include of an absolute path or '..' is not allowed")
            continue
        total += size
        files.append(p)
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} source files is over the limit of {MAX_FILES}")
    if total > MAX_TOTAL_BYTES:
        problems.append(f"{total} bytes of source is over the limit of {MAX_TOTAL_BYTES}")
    return files, problems


def hash_sources(files: List[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(files, key=lambda x: x.name):
        h.update(p.name.encode("utf-8") + b"\x00")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def hash_harness(cfg: BuildConfig) -> Tuple[str, str]:
    """(version, sha256) of the harness firmware that every build is linked with."""
    h = hashlib.sha256()
    names = sorted(
        [p for p in cfg.harness_dir.rglob("*") if p.is_file() and "build" not in p.relative_to(cfg.harness_dir).parts
         and "tests" not in p.relative_to(cfg.harness_dir).parts and p.suffix in (".c", ".h", ".txt", ".cmake")]
    )
    for p in names:
        h.update(str(p.relative_to(cfg.harness_dir)).replace("\\", "/").encode("utf-8") + b"\x00")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    version = ""
    dev_h = cfg.harness_dir / "src" / "device.h"
    if dev_h.exists():
        m = re.search(r'#\s*define\s+FW_VERSION\s+"([^"]+)"', dev_h.read_text(encoding="utf-8", errors="replace"))
        version = m.group(1) if m else ""
    return version, h.hexdigest()


# ---------------------------------------------------------------- running tools
def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:  # pragma: no cover
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


def run_tool(cmd: List[str], env: dict, cwd: Optional[Path], timeout: float) -> Tuple[Optional[int], str, bool]:
    """Run a command, capture stdout+stderr. Returns (exit_code, output, timed_out). Kills the whole
    process tree on timeout."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    p = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=str(cwd) if cwd else None,
        text=True, encoding="utf-8", errors="replace", creationflags=flags,
    )
    try:
        out, _ = p.communicate(timeout=max(timeout, 0.1))
        return p.returncode, out or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(p.pid)
        try:
            out, _ = p.communicate(timeout=15)
        except Exception:  # noqa: BLE001
            out = ""
        return None, out or "", True


def tool_env(cfg: BuildConfig) -> dict:
    env = dict(os.environ)
    env["PICO_SDK_PATH"] = str(cfg.sdk_path)
    env["PATH"] = os.pathsep.join(
        [str(cfg.toolchain_path), str(cfg.cmake_exe.parent), str(cfg.ninja_exe.parent), env.get("PATH", "")]
    )
    return env


def check_toolchain(cfg: BuildConfig) -> List[str]:
    """Return a list of problems (empty = everything needed to build is present)."""
    problems = []
    for label, path in (
        ("Pico SDK", cfg.sdk_path / "pico_sdk_init.cmake"),
        ("cmake", cfg.cmake_exe),
        ("ninja", cfg.ninja_exe),
        ("arm-none-eabi-gcc", cfg.toolchain_path / "arm-none-eabi-gcc.exe"),
        ("arm-none-eabi-size", cfg.size_exe),
        ("harness CMakeLists.txt", cfg.harness_dir / "CMakeLists.txt"),
        ("harness pico_sdk_import.cmake", cfg.harness_dir / "pico_sdk_import.cmake"),
    ):
        if not path.exists():
            problems.append(f"{label} not found at {path}")
    return problems


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # read-only files on Windows
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    for _ in range(3):
        if not path.exists():
            return
        shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            return
        time.sleep(0.5)


# ---------------------------------------------------------------- the build
def build_project(
    name: str,
    timeout_s: Optional[float] = None,
    keep_tree: bool = False,
    cfg: Optional[BuildConfig] = None,
    on_stage: Optional[Callable[[str], None]] = None,
) -> BuildResult:
    cfg = cfg or load_config()
    say = on_stage or (lambda _m: None)
    pdir = project_dir(cfg, name)
    app_dir = pdir / "app"
    if not app_dir.is_dir():
        raise ProjectError(f"no such project: {name!r} (create it with:  python -m app.build.cli new {name})")
    tc = check_toolchain(cfg)
    if tc:
        raise ProjectError("build tools missing: " + "; ".join(tc))

    budget = float(timeout_s) if timeout_s else float(cfg.build_timeout_s)
    t_start = time.monotonic()
    store = BuildStore(cfg.db_path)
    started = _now()
    build_id = store.start(name, started)
    adir = cfg.artifacts_dir / str(build_id)
    adir.mkdir(parents=True, exist_ok=True)
    tree = cfg.build_tree_root / str(build_id)

    res = BuildResult(id=build_id, project=name, started_at=started, artifact_dir=str(adir))
    log_parts: List[str] = []
    try:
        res.harness_version, res.harness_sha256 = hash_harness(cfg)
        files, problems = check_sources(app_dir)
        res.sources = [p.name for p in files]

        if problems:
            res.status = REJECTED
            res.message = f"{len(problems)} source rule violation(s); nothing was compiled"
            res.diagnostics = [
                Diagnostic("error", p, None, None, None, None, "policy", "policy").to_dict() for p in problems
            ]
            log_parts.append("SOURCE RULE VIOLATIONS:\n" + "\n".join(problems))
            return res

        # Snapshot: the build uses a private copy, so edits made while it runs cannot change the result.
        snap = adir / "app_src"
        snap.mkdir(exist_ok=True)
        for p in files:
            shutil.copy2(p, snap / p.name)
        res.sources_sha256 = hash_sources(files)
        short = res.sources_sha256[:8]

        env = tool_env(cfg)
        if tree.exists():
            _rmtree(tree)
        tree.parent.mkdir(parents=True, exist_ok=True)

        say(f"build #{build_id}: configuring (CMake) ...")
        cmd = [
            str(cfg.cmake_exe), "-S", str(cfg.harness_dir), "-B", str(tree), "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release", f"-DAEISP_APP_DIR={snap}", f"-DAEISP_BUILD_ID={build_id}",
            f"-DAEISP_SRC_HASH={short}",
        ]
        remaining = budget - (time.monotonic() - t_start)
        rc, out, timed_out = run_tool(cmd, env, None, min(cfg.configure_timeout_s, remaining))
        log_parts.append("=== CONFIGURE ===\n" + out)
        if timed_out:
            res.status = TIMEOUT
            res.message = f"stopped after {time.monotonic() - t_start:.0f} s while configuring (limit {budget:.0f} s)"
            return res
        if rc != 0:
            res.status = FAILED
            res.message = "CMake configuration failed"
            return res

        say(f"build #{build_id}: compiling ...")
        remaining = budget - (time.monotonic() - t_start)
        rc, out, timed_out = run_tool([str(cfg.cmake_exe), "--build", str(tree), "--", "-k", "0"], env, None, remaining)
        log_parts.append("=== BUILD ===\n" + out)
        if timed_out:
            res.status = TIMEOUT
            res.message = f"stopped after {time.monotonic() - t_start:.0f} s while compiling (limit {budget:.0f} s)"
            return res
        if rc != 0:
            res.status = FAILED
            res.message = "the code does not compile or link"
            return res

        uf2 = tree / "aeisp_firmware.uf2"
        elf = tree / "aeisp_firmware.elf"
        if not uf2.exists() or not elf.exists():
            res.status = ERROR
            res.message = "build reported success but no .uf2/.elf was produced"
            return res

        say(f"build #{build_id}: checking the result ...")
        shutil.copy2(uf2, adir / "firmware.uf2")
        shutil.copy2(elf, adir / "firmware.elf")
        mp = tree / "aeisp_firmware.elf.map"
        if mp.exists():
            shutil.copy2(mp, adir / "firmware.map")
        res.uf2_path = str(adir / "firmware.uf2")
        report = validate_uf2((adir / "firmware.uf2").read_bytes(), cfg.flash_capacity_bytes)
        res.uf2_bytes, res.uf2_sha256 = report["bytes"], report["sha256"]
        res.uf2_valid, res.uf2_problems = report["valid"], report["problems"]

        rc2, size_out, _ = run_tool([str(cfg.size_exe), str(elf)], env, None, 30)
        if rc2 == 0:
            parts = size_out.strip().splitlines()[-1].split()
            if len(parts) >= 3 and all(x.isdigit() for x in parts[:3]):
                text, data, bss = (int(x) for x in parts[:3])
                res.flash_bytes = text + data
                res.ram_bytes = data + bss
                res.flash_pct = round(100.0 * res.flash_bytes / cfg.flash_capacity_bytes, 1)
                res.ram_pct = round(100.0 * res.ram_bytes / cfg.ram_capacity_bytes, 1)

        if cfg.picotool_path.exists():  # independent second opinion on the image; never fatal
            rc3, pt, _ = run_tool([str(cfg.picotool_path), "info", str(adir / "firmware.uf2")], env, None, 20)
            (adir / "picotool_info.txt").write_text(pt, encoding="utf-8")
            m = re.search(r"^\s*name:\s*(.+)$", pt, re.M)
            res.picotool_name = m.group(1).strip() if (rc3 == 0 and m) else ""

        if not res.uf2_valid:
            res.status = ERROR
            res.message = "UF2 image failed validation: " + "; ".join(res.uf2_problems)
        else:
            res.status = SUCCESS
            res.message = "built successfully"
        return res
    except Exception as e:  # noqa: BLE001
        res.status = ERROR
        res.message = f"build system error: {type(e).__name__}: {e}"
        log_parts.append(f"INTERNAL ERROR: {type(e).__name__}: {e}")
        return res
    finally:
        text = "\n".join(log_parts)
        if text and res.status != REJECTED:
            diags = parse_output(text, adir / "app_src", cfg.harness_dir, cfg.sdk_path)
            res.diagnostics = [d.to_dict() for d in diags]
            res.errors, res.warnings = count(diags)
            if res.status == FAILED and res.errors == 0:
                res.errors = 1
                res.diagnostics.append(
                    Diagnostic("error", "build failed - see build.log for the raw output", None, None, None, None,
                               "other", "other").to_dict()
                )
        elif res.status == REJECTED:
            res.errors = len(res.diagnostics)
        res.finished_at = _now()
        res.duration_s = round(time.monotonic() - t_start, 1)
        res.log_path = str(adir / "build.log")
        (adir / "build.log").write_text(text, encoding="utf-8")
        (adir / "result.json").write_text(json.dumps(res.to_dict(), indent=2) + "\n", encoding="utf-8")
        store.finish(res)
        if not keep_tree and tree.exists():
            _rmtree(tree)