"""Deployment tools.   Run from the backend folder:  python -m app.deploy.cli <command>

  status                          what is on the board right now, and what we can recover to
  deploy <build-number>           flash a successful build onto the board (asks for approval)
  deploy --file <path.uf2>        flash any UF2 file (extra warnings)
  recover [--stock]               bring the board back to known-good (or factory) firmware
  bootsel                         put the board into the UF2 bootloader and stop (no flashing)
  known-good show                 show the firmware we fall back to
  known-good promote <build>      make a build (already proven on the board) the known-good firmware
  history [--limit N]             past deployments
  show <id> [--log] [--json]      details of one deployment
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .board import BOOTSEL, RUNNING, Board, BootloaderError
from .engine import (
    SUCCESS, Approval, DeployError, deploy, known_good_manifest, load_deploy_config, match_build_record,
    promote_known_good, recover, recovery_image, resolve_build_image, resolve_file, resolve_known_good,
    resolve_stock, run_preflight,
)
from .store import DeploymentStore

MARK = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}


def _print_checks(checks) -> None:
    print("PRE-FLIGHT CHECKS")
    for c in checks:
        print(f"  {MARK[c.status]} {c.name:<16} {c.detail}")
    print()


def _ask(summary: str, token: str, yes: bool) -> "Approval | None":
    print(summary)
    if yes:
        print("(--yes given: approval taken from the command line)")
        return Approval("cli --yes flag")
    try:
        typed = input(f"To approve, type  {token}  and press Enter (anything else cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if typed == token:
        return Approval(f"cli: human typed '{token}'")
    print("Cancelled. Nothing was changed.")
    return None


def _print_result(res) -> None:
    print()
    print(f"{res.kind.upper()} #{res.id}: {res.status}   ({res.duration_s} s)")
    print(f"  {res.message}")
    if res.after:
        a = res.after
        print(f"  board now : mode={a.get('mode')} fw={a.get('fw_name')} {a.get('fw_version')} "
              f"build={a.get('build_id') or '-'} src={a.get('src_hash') or '-'} uid={a.get('uid') or '-'}")
    print(f"  log       : see 'python -m app.deploy.cli show {res.id} --log'")
    print()


def cmd_status(args) -> int:
    cfg = load_deploy_config()
    store = DeploymentStore(cfg.db_path)
    st = Board().identify()
    print("BOARD")
    print(f"  {st.summary()}")
    rec = match_build_record(cfg, st)
    if rec:
        if rec["found"]:
            print(f"  = build #{rec['build_id']} of project '{rec['project']}' (built {rec['started_at']}); "
                  f"sources match the record: {'yes' if rec['sources_match'] else 'NO - different code!'}")
        else:
            print(f"  = build #{rec['build_id']} is not in this computer's build history")
    elif st.mode == RUNNING:
        print("  = not a build made by our build system (older or foreign firmware)")
    print()
    print("RECOVERY OPTIONS")
    kg = resolve_known_good(cfg)
    if kg is None:
        print("  known-good firmware : none yet (promote a proven build with: known-good promote <build>)")
    else:
        print(f"  known-good firmware : {kg.label}  {'VALID' if not kg.problems else 'PROBLEM: ' + '; '.join(kg.problems)}")
    sk = resolve_stock(cfg)
    if sk is None:
        print(f"  factory backup      : MISSING ({cfg.stock_backup})")
    else:
        print(f"  factory backup      : {'valid' if not sk.problems else 'PROBLEM: ' + '; '.join(sk.problems)}, "
              f"{sk.size:,} bytes")
    devs = store.devices()
    print()
    print("REGISTERED BOARDS")
    print("  " + (", ".join(d["uid"] for d in devs) if devs else "none yet (the first successful deployment registers the board)"))
    last = store.list(3)
    if last:
        print()
        print("LAST DEPLOYMENTS")
        for d in last:
            print(f"  #{d['id']} {d['kind']:<8} {d['status']:<16} {d['image_label']}")
    return 0


def cmd_deploy(args) -> int:
    cfg = load_deploy_config()
    store = DeploymentStore(cfg.db_path)
    board = Board()
    if args.file:
        image = resolve_file(cfg, Path(args.file))
        token = "FLASH"
    elif args.build is not None:
        image = resolve_build_image(cfg, args.build)
        token = str(args.build)
    else:
        print("ERROR: give a build number, or --file <path.uf2>")
        return 2

    print(f"Deploying: {image.label}\n")
    pf = run_preflight(cfg, board, image, store, kind="deploy", trust_new_board=args.trust_new_board,
                       allow_no_recovery=args.allow_no_recovery)
    _print_checks(pf.checks)
    if not pf.ok:
        print("Cannot continue: fix the FAIL items above. The board was not touched.")
        return 1

    rec = pf.recovery.label if pf.recovery else "NONE"
    plan = (
        "WHAT WILL HAPPEN\n"
        f"  1. put the board into the UF2 bootloader ({'it is already there' if pf.before.mode == BOOTSEL else 'by software'})\n"
        f"  2. copy the image onto the board (this REPLACES the firmware currently on it)\n"
        f"  3. wait for the board to restart and check "
        + ("that it reports the right version, build number and passes a health check\n" if image.protocol_verifiable
           else "that it is back on USB (limited check for this image type)\n")
        + (f"  4. if the check fails, restore: {rec}\n" if args.rollback else "  4. rollback is DISABLED (--no-rollback)\n")
    )
    if args.dry_run:
        print(plan)
        print("Dry run: nothing was changed.")
        return 0
    approval = _ask(plan + f"\nThis will overwrite the firmware on board {pf.before.uid or '(id unknown)'}.",
                    token, args.yes)
    if approval is None:
        return 1
    print()
    res = deploy(cfg, board, image, approval, rollback=args.rollback, trust_new_board=args.trust_new_board,
                 allow_no_recovery=args.allow_no_recovery, store=store, on_line=print)
    _print_result(res)
    return 0 if res.status == SUCCESS else 1


def cmd_recover(args) -> int:
    cfg = load_deploy_config()
    store = DeploymentStore(cfg.db_path)
    board = Board()
    which = "stock" if args.stock else ("known-good" if args.known_good else "auto")
    try:
        probe = {"stock": resolve_stock, "known-good": resolve_known_good}.get(which, lambda c: recovery_image(c))(cfg)
    except DeployError as e:
        print(f"ERROR: {e}")
        return 2
    if probe is None:
        print("ERROR: no recovery image available for that choice.")
        return 2
    print(f"Recovery image: {probe.label}\n")
    pf = run_preflight(cfg, board, probe, store, kind="recover", trust_new_board=args.trust_new_board)
    _print_checks(pf.checks)
    if not pf.ok:
        print("Cannot continue: fix the FAIL items above. The board was not touched.")
        return 1
    if args.dry_run:
        print("Dry run: nothing was changed.")
        return 0
    approval = _ask("This will overwrite the firmware on the board with the recovery image above.",
                    "RECOVER", args.yes)
    if approval is None:
        return 1
    print()
    res = recover(cfg, board, which, approval, trust_new_board=args.trust_new_board, store=store, on_line=print)
    _print_result(res)
    return 0 if res.status == SUCCESS else 1


def cmd_bootsel(args) -> int:
    board = Board()
    try:
        method = board.enter_bootloader(print)
    except BootloaderError as e:
        print(f"ERROR: {e}")
        return 1
    st = board.identify()
    print(f"\nThe board is in the UF2 bootloader (reached via: {method}). {st.summary()}")
    print("Nothing was flashed. Unplug and replug the board to leave the bootloader, "
          "or run a deploy / recover command.")
    return 0


def cmd_known_good(args) -> int:
    cfg = load_deploy_config()
    if args.action == "show":
        m = known_good_manifest(cfg)
        kg = resolve_known_good(cfg)
        if not m or kg is None:
            print("No known-good firmware yet.")
            return 0
        print(json.dumps(m, indent=2))
        print("file check:", "VALID" if not kg.problems else "PROBLEM: " + "; ".join(kg.problems))
        return 0 if not kg.problems else 1
    if args.build is None:
        print("ERROR: known-good promote needs a build number")
        return 2
    img = resolve_build_image(cfg, args.build)
    store = DeploymentStore(cfg.db_path)
    dep = store.successful_for_build(args.build)
    print(f"Promote to known-good: {img.label}")
    print(f"  proven on the board by deployment #{dep['id']}" if dep else "  NOT yet proven on the board")
    if not dep:
        print(f"ERROR: deploy it first:  python -m app.deploy.cli deploy {args.build}")
        return 1
    approval = _ask("The board will be restored to this firmware whenever a later experiment fails.",
                    f"PROMOTE {args.build}", args.yes)
    if approval is None:
        return 1
    m = promote_known_good(cfg, args.build, store)
    print(f"Done. Known-good firmware is now {m['label']} (file {cfg.known_good_dir / 'known_good.uf2'}).")
    print("Remember to commit it:  git add firmware/known_good && git commit")
    return 0


def cmd_history(args) -> int:
    cfg = load_deploy_config()
    rows = DeploymentStore(cfg.db_path).list(args.limit)
    if not rows:
        print("No deployments recorded yet.")
        return 0
    print(f"{'ID':>4}  {'KIND':<8} {'STATUS':<16} {'TIME(s)':>7}  {'BOARD':<16}  IMAGE")
    for r in rows:
        print(f"{r['id']:>4}  {r['kind']:<8} {r['status']:<16} {r['duration_s'] if r['duration_s'] is not None else '':>7}  "
              f"{(r['board_uid'] or '-'):<16}  {r['image_label']}")
    return 0


def cmd_show(args) -> int:
    cfg = load_deploy_config()
    row = DeploymentStore(cfg.db_path).get(args.id)
    if not row:
        print(f"No deployment #{args.id}.")
        return 2
    res = row.get("result")
    if args.json:
        print(json.dumps(res or row, indent=2))
        return 0
    print(f"DEPLOYMENT #{row['id']}   kind={row['kind']}   status={row['status']}   approved by: {row['approved_by']}")
    print(f"  image   : {row['image_label']}  (origin {row['image_origin']}, sha256 {(row['image_sha256'] or '')[:12]})")
    print(f"  board   : {row['board_uid'] or '-'}")
    print(f"  started : {row['started_at']}   duration {row['duration_s']} s")
    print(f"  result  : {row['message']}")
    if res and res.get("checks"):
        print("  pre-flight:")
        for c in res["checks"]:
            print(f"    {MARK[c['status']]} {c['name']:<16} {c['detail']}")
    if args.log:
        print("  ----- log -----")
        for line in (res or {}).get("log", []):
            print("  " + line)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.deploy.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("bootsel").set_defaults(fn=cmd_bootsel)

    p = sub.add_parser("deploy")
    p.add_argument("build", nargs="?", type=int, help="build number from the build history")
    p.add_argument("--file", help="flash a UF2 file that did not come from the build system")
    p.add_argument("--dry-run", action="store_true", help="run the pre-flight checks and show the plan only")
    p.add_argument("--yes", action="store_true", help="skip the typed approval (human use only)")
    p.add_argument("--no-rollback", dest="rollback", action="store_false", help="do not restore on failure")
    p.add_argument("--trust-new-board", action="store_true", help="allow a board that is not registered yet")
    p.add_argument("--allow-no-recovery", action="store_true", help="flash even without a recovery image (dangerous)")
    p.set_defaults(fn=cmd_deploy)

    p = sub.add_parser("recover")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stock", action="store_true", help="restore the factory firmware backup")
    g.add_argument("--known-good", action="store_true", help="restore the known-good firmware (not the factory one)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--trust-new-board", action="store_true")
    p.set_defaults(fn=cmd_recover)

    p = sub.add_parser("known-good")
    p.add_argument("action", choices=["show", "promote"])
    p.add_argument("build", nargs="?", type=int)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_known_good)

    p = sub.add_parser("history")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("show")
    p.add_argument("id", type=int)
    p.add_argument("--log", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except DeployError as e:
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())