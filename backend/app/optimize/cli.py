"""Optimise a project's firmware from a terminal.   (from the backend folder; stop the server first: COM port)

  python -m app.optimize.cli run calc                       faster (the slowest benchmark command), >= 10 %
        --goal speed|flash|ram   --target mul   --min-gain 15   --variants 2
  python -m app.optimize.cli show 1                         report of optimisation 1
  python -m app.optimize.cli list

You approve every deployment (and rollback) in this terminal. The board must run a build of the project.
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


def _follow(s, oid: int, job_id: int, after: int) -> int:
    while True:
        for e in s.optimizations.store.events(oid, after):
            print(f"  [{e['stage']:<11}] {e['kind']:<8} {e['message']}")
            after = e["id"]
        if s.jobs.get(job_id)["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            for e in s.optimizations.store.events(oid, after):
                print(f"  [{e['stage']:<11}] {e['kind']:<8} {e['message']}")
                after = e["id"]
            return after
        time.sleep(0.5)


def cmd_run(args) -> int:
    s = _services()
    try:
        o = s.optimizations.start(args.project, "cli", args.goal, args.target, None, args.min_gain, args.variants)
        print(f"optimisation {o['id']} of {args.project} started")
        job_id, after = o["job_id"], 0
        while True:
            after = _follow(s, o["id"], job_id, after)
            o = s.optimizations.get(o["id"])
            if o["status"] != "WAITING_APPROVAL":
                break
            jid = o["rollback_job_id"] if o["stage"] == "rolled_back" else o["deploy_job_id"]
            job = s.jobs.get(jid)
            answer = input(f"  {job['summary']}. Type  {job['confirm_token']}  to approve (anything else rejects): ")
            if answer.strip() == job["confirm_token"]:
                s.jobs.approve(job["id"], "cli:operator", answer.strip())
            else:
                s.jobs.reject(job["id"], "cli:operator", "rejected at the terminal")
            job_id = s.optimizations.resume(o["id"], "cli")["job_id"]
        print("\n" + s.optimizations.report_markdown(o["id"]))
        return 0 if o["outcome"] in ("accepted", "no_gain", "rejected_rolled_back") else 1
    finally:
        s.stop()


def cmd_show(args) -> int:
    s = _services()
    try:
        print(s.optimizations.report_markdown(args.optimization_id))
    finally:
        s.stop()
    return 0


def cmd_list(args) -> int:
    s = _services()
    try:
        for o in s.optimizations.store.list(30):
            print(f"  {o['id']:>4}  {o['status']:<17} {str(o['outcome'] or ''):<24} {o['project']:<12} {o['goal']:<6} "
                  f"base #{o['base_build']} -> #{o['variant_build']}")
    finally:
        s.stop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.optimize.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("project")
    r.add_argument("--goal", choices=["speed", "flash", "ram"], default="speed")
    r.add_argument("--target", default=None)
    r.add_argument("--min-gain", type=float, default=10.0)
    r.add_argument("--variants", type=int, default=2)
    r.set_defaults(fn=cmd_run)
    sh = sub.add_parser("show")
    sh.add_argument("optimization_id", type=int)
    sh.set_defaults(fn=cmd_show)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
