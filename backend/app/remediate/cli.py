"""Fix a bug the fuzzer found, from a terminal.   (from the backend folder; stop the server first: COM port)

  python -m app.remediate.cli findings                 open fuzz findings, most severe first
  python -m app.remediate.cli fix 4                    remediate finding #4 (you approve every deployment here)
        --fix-rounds 2   --no-fuzz-check
  python -m app.remediate.cli show 1                   report of remediation 1
"""
from __future__ import annotations

import argparse
import sys
import time

from ..settings import Settings


def _services():
    from ..services.container import build_services

    s = build_services(Settings())
    s.start()
    return s


def _follow(s, rid: int, job_id: int, after: int) -> int:
    while True:
        for e in s.remediations.store.events(rid, after):
            print(f"  [{e['stage']:<11}] {e['kind']:<8} {e['message']}")
            after = e["id"]
        if s.jobs.get(job_id)["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            for e in s.remediations.store.events(rid, after):
                print(f"  [{e['stage']:<11}] {e['kind']:<8} {e['message']}")
                after = e["id"]
            return after
        time.sleep(0.5)


def cmd_fix(args) -> int:
    s = _services()
    try:
        r = s.remediations.start(args.finding_id, "cli", args.fix_rounds, not args.no_fuzz_check)
        print(f"remediation {r['id']} started for finding #{args.finding_id}")
        job_id, after = r["job_id"], 0
        while True:
            after = _follow(s, r["id"], job_id, after)
            r = s.remediations.get(r["id"])
            if r["status"] != "WAITING_APPROVAL":
                break
            jid = r["rollback_job_id"] if r["stage"] == "rolled_back" else r["deploy_job_id"]
            job = s.jobs.get(jid)
            answer = input(f"  {job['summary']}. Type  {job['confirm_token']}  to approve (anything else rejects): ")
            if answer.strip() == job["confirm_token"]:
                s.jobs.approve(job["id"], "cli:operator", answer.strip())
            else:
                s.jobs.reject(job["id"], "cli:operator", "rejected at the terminal")
            job_id = s.remediations.resume(r["id"], "cli")["job_id"]
        print("\n" + s.remediations.report_markdown(r["id"]))
        return 0 if r["outcome"] == "fixed" else 1
    finally:
        s.stop()


def cmd_findings(args) -> int:
    s = _services()
    try:
        for f in s.fuzz.store.findings(status=None, min_severity="low", limit=50):
            if f["status"] in ("fixed", "wont_fix"):
                continue
            print(f"  #{f['id']:<4} {f['severity']:<8} {f['kind']:<11} {f['status']:<15} build #{f['build_id']}  "
                  f"{(f['minimized'] or f['input_text'])[:50]!r}")
    finally:
        s.stop()
    return 0


def cmd_show(args) -> int:
    s = _services()
    try:
        print(s.remediations.report_markdown(args.remediation_id))
    finally:
        s.stop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.remediate.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fix")
    f.add_argument("finding_id", type=int)
    f.add_argument("--fix-rounds", type=int, default=2)
    f.add_argument("--no-fuzz-check", action="store_true")
    f.set_defaults(fn=cmd_fix)
    sub.add_parser("findings").set_defaults(fn=cmd_findings)
    sh = sub.add_parser("show")
    sh.add_argument("remediation_id", type=int)
    sh.set_defaults(fn=cmd_show)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
