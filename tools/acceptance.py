"""The AEISP acceptance checklist, checked automatically.

    python tools\\acceptance.py                 every item (runs the whole test suite once, about 7 minutes)
    python tools\\acceptance.py --quick         only the checks that need no test run (seconds)
    python tools\\acceptance.py --hardware      also the hardware tests (board plugged in, backend server stopped)

Each item in docs/acceptance.md is either a check on the installation (configuration, secrets, toolchain,
documentation) or is proved by named test files. The test files run in one pytest call, and each item passes
only if all of its tests pass. Nothing here talks to the model, and only --hardware talks to the board.
Secrets are compared but never printed.
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DOCS = ("README.md", "docs/install.md", "docs/architecture.md", "docs/user-manual.md", "docs/security-review.md",
        "docs/acceptance.md")
TOKEN_KEYS = ("AEISP_OPERATOR_TOKEN", "AEISP_AGENT_TOKEN", "AEISP_VIEWER_TOKEN")
KEY_PATTERN = r"AIza[0-9A-Za-z_\-]{35}"           # the shape of a Google API key

Result = Tuple[str, str]                              # ("pass" | "fail" | "warn" | "skip", detail)


def read_env(path: Path = ROOT / ".env") -> Dict[str, str]:
    out: Dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


# ---------------------------------------------------------------- checks on the installation
def check_python() -> Result:
    if sys.version_info < (3, 11):
        return "fail", f"Python {sys.version.split()[0]}: 3.11 or newer is needed"
    missing = []
    for mod in ("fastapi", "uvicorn", "pydantic_settings", "serial", "langgraph", "google.genai", "tenacity",
                "httpx", "pytest"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return "fail", f"not installed: {', '.join(missing)} (pip install -r backend/requirements.txt)"
    return "pass", f"Python {sys.version.split()[0]}, all backend packages import"


def check_config() -> Result:
    env = read_env()
    if not env:
        return "fail", ".env is missing (copy .env.example, then python -m app.cli make-tokens)"
    absent = [k for k in TOKEN_KEYS if not env.get(k)]
    if absent:
        return "fail", f"tokens not set: {', '.join(absent)} (python -m app.cli make-tokens)"
    short = [k for k in TOKEN_KEYS if len(env[k]) < 24]
    if short:
        return "fail", f"tokens too short to be safe: {', '.join(short)}"
    if len({env[k] for k in TOKEN_KEYS}) != 3:
        return "fail", "two roles share a token"
    ignored = git("check-ignore", "-q", ".env").returncode == 0
    tracked = bool(git("ls-files", ".env").stdout.strip())
    if tracked or not ignored:
        return "fail", ".env is tracked by git or not ignored"
    note = "" if env.get("GEMINI_API_KEY") else " (no GEMINI_API_KEY: the agents are off)"
    return ("pass" if not note else "warn"), "three distinct role tokens, .env ignored by git" + note


def check_secrets() -> Result:
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return "skip", "not a git checkout"
    hits = git("grep", "-I", "-l", "-E", KEY_PATTERN).stdout.split()
    if hits:
        return "fail", f"something shaped like a Google API key is committed in: {', '.join(hits)}"
    env = read_env()
    leaked = []
    for k in TOKEN_KEYS + ("GEMINI_API_KEY",):
        v = env.get(k, "")
        if len(v) >= 12 and git("grep", "-I", "-q", "-F", v).returncode == 0:
            leaked.append(k)
    if leaked:
        return "fail", f"the value of {', '.join(leaked)} appears in a tracked file"
    return "pass", "no API key pattern and no .env secret value in any tracked file"


def check_toolchain() -> Result:
    env = read_env()
    paths = {"Pico SDK": Path(env.get("PICO_SDK_PATH", "")) / "pico_sdk_init.cmake",
             "ARM GCC": Path(env.get("PICO_TOOLCHAIN_PATH", "")) / "arm-none-eabi-gcc.exe",
             "picotool": Path(env.get("PICOTOOL_PATH", ""))}
    missing = [n for n, p in paths.items() if not str(p) or not p.is_file()]
    if missing:
        return "warn", f"not found: {', '.join(missing)} (firmware cannot be built here; see docs/install.md)"
    recovery = [p for p in (ROOT / "backups" / "shrike_stock_backup.uf2",) if p.is_file()]
    return "pass", "Pico SDK, ARM GCC and picotool found" + ("" if recovery else "; no factory backup yet")


def check_dashboard() -> Result:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        return "warn", "frontend/dist is not built (cd frontend; npm install; npm run build)"
    return "pass", "built dashboard present (served at /ui/)"


def check_docs_and_version() -> Result:
    missing = [d for d in DOCS if not (ROOT / d).is_file()]
    if missing:
        return "fail", f"missing: {', '.join(missing)}"
    sys.path.insert(0, str(BACKEND))
    from app.api.core import PHASE
    from app.settings import VERSION

    if (VERSION, PHASE) != ("1.0.0", 17):
        return "fail", f"version {VERSION} phase {PHASE}: expected 1.0.0 phase 17"
    links = []
    for d in DOCS:
        for target in re.findall(r"\]\(([^)#:]+\.md)\)", (ROOT / d).read_text(encoding="utf-8")):
            if not ((ROOT / d).parent / target).is_file():
                links.append(f"{d} -> {target}")
    if links:
        return "fail", f"broken links: {', '.join(links)}"
    return "pass", f"{len(DOCS)} documents, links resolve, version {VERSION}"


# ---------------------------------------------------------------- the checklist
ITEMS: List[Tuple[str, str, Optional[Callable[[], Result]], Tuple[str, ...]]] = [
    ("A1", "Installation: Python and every backend package", check_python, ()),
    ("A2", "Configuration: three role tokens, .env kept out of git", check_config, ("test_api_core",)),
    ("A3", "No secret is committed", check_secrets, ()),
    ("A4", "Firmware toolchain is installed", check_toolchain, ()),
    ("A5", "Board link: protocol, CRC, ids, reconnects, harness profiler", None,
     ("test_protocol", "test_link_fake", "test_discovery", "test_harness_host")),
    ("A6", "Builds, size reports and the security scan", None, ("test_analysis", "test_api_services")),
    ("A7", "Flashing only after approval; verify and roll back on any failure", None,
     ("test_api_services", "test_failures")),
    ("A8", "Agents stay inside their tools, roles and budgets", None, ("test_agents", "test_team", "test_failures")),
    ("A9", "Requirement to firmware pipeline", None, ("test_pipeline",)),
    ("A10", "Test suites, regressions and coverage", None, ("test_testfw",)),
    ("A11", "Fuzzing finds, reproduces and minimises bugs safely", None, ("test_fuzz",)),
    ("A12", "Remediation and optimisation with rollback and gates", None, ("test_remediate", "test_optimize")),
    ("A13", "Profiling, benchmarks and baselines", None, ("test_profiling",)),
    ("A14", "Knowledge base, task records, reports and the audit chain", None, ("test_knowledge", "test_records")),
    ("A15", "Dashboard, live feed, health and metrics", check_dashboard, ("test_live", "test_health")),
    ("A16", "The platform's own security (hardening and failure handling)", None,
     ("test_hardening", "test_failures")),
    ("A17", "Documentation, demo and version 1.0.0", check_docs_and_version, ("test_demo", "test_acceptance")),
]


def run_tests(files: List[str], hardware: bool) -> Tuple[Dict[str, List[str]], Dict[str, int], int]:
    """Run the test files once; return failures and counts per file, and the number of hardware tests passed."""
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "junit.xml"
        args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
        args += [f"tests/{f}.py" for f in files]
        subprocess.run(args, cwd=BACKEND, capture_output=True, text=True)
        failures: Dict[str, List[str]] = defaultdict(list)
        counts: Dict[str, int] = defaultdict(int)
        if xml.is_file():
            for case in ET.parse(xml).getroot().iter("testcase"):
                mod = case.get("classname", "").split(".")[-1]
                counts[mod] += 1
                if case.find("failure") is not None or case.find("error") is not None:
                    failures[mod].append(case.get("name", "?"))
        hw = -1
        if hardware:
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "hardware"],
                               cwd=BACKEND, capture_output=True, text=True)
            m = re.search(r"(\d+) passed", r.stdout)
            hw = int(m.group(1)) if m and r.returncode == 0 else 0
        return failures, counts, hw


def evaluate(quick: bool = False, hardware: bool = False, out: Callable[[str], None] = print) -> int:
    files = sorted({f for *_, fs in ITEMS for f in fs})
    failures: Dict[str, List[str]] = {}
    counts: Dict[str, int] = {}
    hw = -1
    if not quick:
        out(f"running {len(files)} test files (this takes a few minutes)...")
        failures, counts, hw = run_tests(files, hardware)
    rows = []
    for code, title, check, test_files in ITEMS:
        status, details = "pass", []
        if check:
            s, d = check()
            details.append(d)
            status = s
        if test_files:
            if quick:
                details.append("tests not run (--quick)")
                status = "skip" if not check else status
            else:
                n = sum(counts.get(f, 0) for f in test_files)
                bad = [f"{f}::{t}" for f in test_files for t in failures.get(f, [])]
                if n == 0:
                    status, bad = "fail", ["no tests ran"]
                if bad:
                    status = "fail"
                    details.append("failed: " + ", ".join(bad[:5]))
                else:
                    details.append(f"{n} tests pass")
        rows.append((code, title, status, "; ".join(details)))
    if hardware:
        rows.append(("H1", "On the real board (hardware tests)", "pass" if hw > 0 else "fail",
                     f"{hw} hardware tests pass" if hw > 0 else "the hardware tests did not all pass"))
    mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}
    for code, title, status, detail in rows:
        out(f"{mark[status]:<5} {code:<4} {title}\n            {detail}")
    tally = {s: sum(1 for r in rows if r[2] == s) for s in mark}
    out(f"\n{tally['pass']} pass, {tally['warn']} warn, {tally['skip']} skip, {tally['fail']} fail "
        f"of {len(rows)} items")
    return 1 if tally["fail"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip the test run")
    ap.add_argument("--hardware", action="store_true", help="also run the hardware tests on the board")
    args = ap.parse_args(argv)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return evaluate(args.quick, args.hardware)


if __name__ == "__main__":
    sys.exit(main())
