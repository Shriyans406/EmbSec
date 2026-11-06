"""Build tools.   Run from the backend folder:  python -m app.build.cli <command>

  check                      are all build tools present and working?
  new <name>                 create a firmware project with a starter app
  projects                   list projects
  build <name> [--timeout S] [--keep-tree] [--json]
  history [--project NAME] [--limit N]
  show <id> [--log] [--json]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .config import load_config
from .diagnostics import Diagnostic
from .engine import (
    ERROR, FAILED, REJECTED, SUCCESS, TIMEOUT, BuildStore, ProjectError, build_project, check_toolchain,
    create_project, hash_harness, list_projects, project_dir, tool_env,
)


def _fmt_bytes(n: int) -> str:
    return f"{n:,} B"


def _diag_line(d: dict) -> str:
    return Diagnostic(**d).format()


def print_result(r: dict, show_all: bool = True) -> None:
    print()
    print(f"BUILD #{r['id']}   project={r['project']}   status={r['status']}   ({r['duration_s']} s)")
    print(f"  {r['message']}")
    print(f"  errors: {r['errors']}   warnings: {r['warnings']}")
    if r["sources"]:
        print(f"  sources: {', '.join(r['sources'])}   (sha256 {r['sources_sha256'][:12]})")
    print(f"  harness: version {r['harness_version']}   (sha256 {r['harness_sha256'][:12]})")
    if r["status"] == SUCCESS:
        print(f"  flash used : {_fmt_bytes(r['flash_bytes'])}  ({r['flash_pct']}% of 4 MiB)")
        print(f"  RAM static : {_fmt_bytes(r['ram_bytes'])}  ({r['ram_pct']}% of 264 KiB)")
        print(f"  UF2        : {_fmt_bytes(r['uf2_bytes'])}   valid={r['uf2_valid']}   sha256 {r['uf2_sha256'][:12]}")
        if r.get("picotool_name"):
            print(f"  picotool   : recognises it as program '{r['picotool_name']}'")
        print(f"  file       : {r['uf2_path']}")
    elif r["uf2_problems"]:
        print("  UF2 problems: " + "; ".join(r["uf2_problems"]))
    print(f"  artifacts  : {r['artifact_dir']}")
    diags = r["diagnostics"]
    shown = [d for d in diags if d["severity"] != "note"] if not show_all else diags
    if shown:
        print("  diagnostics:")
        for d in shown:
            print("    " + _diag_line(d))
            for c in d.get("context", [])[:3] if d["severity"] == "error" else []:
                print("      " + c)
    print()


def cmd_check(args) -> int:
    cfg = load_config()
    ok = True

    def line(label: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'PASS' if good else 'FAIL'}] {label:<22} {detail}")

    problems = check_toolchain(cfg)
    env = tool_env(cfg)

    def ver(cmd, first=True):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            out = (p.stdout + p.stderr).strip().splitlines()
            return p.returncode == 0, (out[0] if out else "")
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    for label, cmd in (
        ("cmake", [str(cfg.cmake_exe), "--version"]),
        ("ninja", [str(cfg.ninja_exe), "--version"]),
        ("arm-none-eabi-gcc", [str(cfg.toolchain_path / "arm-none-eabi-gcc.exe"), "--version"]),
        ("arm-none-eabi-size", [str(cfg.size_exe), "--version"]),
        ("picotool", [str(cfg.picotool_path), "version"]),
    ):
        good, text = ver(cmd)
        line(label, good, text)
    line("Pico SDK", (cfg.sdk_path / "pico_sdk_init.cmake").exists(), str(cfg.sdk_path))
    if (cfg.harness_dir / "CMakeLists.txt").exists():
        v, h = hash_harness(cfg)
        line("harness firmware", bool(v), f"version {v}  sha256 {h[:12]}  ({cfg.harness_dir})")
    else:
        line("harness firmware", False, f"missing: {cfg.harness_dir}")
    for label, path in (("workspace folder", cfg.workspace_dir), ("artifacts folder", cfg.artifacts_dir),
                        ("build-tree folder", cfg.build_tree_root)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("x")
            probe.unlink()
            good = True
        except OSError:
            good = False
        extra = ""
        if label == "build-tree folder" and good:
            extra = f"   free disk: {shutil.disk_usage(path).free / 1e9:.1f} GB"
        line(label, good, f"{path}{extra}")
    try:
        BuildStore(cfg.db_path)
        line("history database", True, str(cfg.db_path))
    except Exception as e:  # noqa: BLE001
        line("history database", False, str(e))
    if problems:
        print("\nMissing:\n  " + "\n  ".join(problems))
    print("\nALL BUILD CHECKS PASSED" if ok and not problems else "\nSOME CHECKS FAILED")
    return 0 if ok and not problems else 1


def cmd_new(args) -> int:
    cfg = load_config()
    pdir = create_project(args.name, cfg)
    print(f"Created project '{args.name}'")
    print(f"  folder : {pdir}")
    print(f"  source : {pdir / 'app' / 'app.c'}   <- edit this file")
    return 0


def cmd_projects(args) -> int:
    cfg = load_config()
    names = list_projects(cfg)
    if not names:
        print("No projects yet. Create one with:  python -m app.build.cli new <name>")
        return 0
    for n in names:
        files = sorted(p.name for p in (project_dir(cfg, n) / "app").iterdir())
        print(f"{n:<20} {', '.join(files)}")
    return 0


def cmd_build(args) -> int:
    cfg = load_config()
    r = build_project(args.name, timeout_s=args.timeout, keep_tree=args.keep_tree, cfg=cfg,
                      on_stage=lambda m: print(m, flush=True))
    d = r.to_dict()
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print_result(d)
    return 0 if r.status == SUCCESS else (2 if r.status == ERROR else 1)


def cmd_history(args) -> int:
    cfg = load_config()
    rows = BuildStore(cfg.db_path).list(args.project, args.limit)
    if not rows:
        print("No builds recorded yet.")
        return 0
    print(f"{'ID':>4}  {'PROJECT':<16} {'STATUS':<9} {'TIME(s)':>7} {'ERR':>4} {'WARN':>4}  {'SOURCES':<12}  STARTED (UTC)")
    for r in rows:
        print(f"{r['id']:>4}  {r['project']:<16} {r['status']:<9} {r['duration_s'] if r['duration_s'] is not None else '':>7} "
              f"{r['errors'] if r['errors'] is not None else '':>4} {r['warnings'] if r['warnings'] is not None else '':>4}  "
              f"{(r['sources_sha256'] or '')[:12]:<12}  {r['started_at']}")
    return 0


def cmd_show(args) -> int:
    cfg = load_config()
    row = BuildStore(cfg.db_path).get(args.id)
    if not row:
        print(f"No build #{args.id}.")
        return 2
    if args.json:
        print(json.dumps(row["result"], indent=2))
    elif row["result"]:
        print_result(row["result"])
    else:
        print(f"Build #{args.id} has no result record (status {row['status']}).")
    if args.log:
        log = Path(row["artifact_dir"] or "") / "build.log"
        print("----- build.log -----")
        print(log.read_text(encoding="utf-8", errors="replace") if log.exists() else "(no log)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.build.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("projects").set_defaults(fn=cmd_projects)
    p = sub.add_parser("new")
    p.add_argument("name")
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("build")
    p.add_argument("name")
    p.add_argument("--timeout", type=float, help="give up after this many seconds (default 600)")
    p.add_argument("--keep-tree", action="store_true", help="keep the temporary build folder for inspection")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("history")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("show")
    p.add_argument("id", type=int)
    p.add_argument("--log", action="store_true", help="also print the full compiler log")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ProjectError as e:
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())