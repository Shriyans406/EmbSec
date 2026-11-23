"""Run the requirement-to-firmware pipeline from a terminal.   (from the backend folder)

  python -m app.pipeline.cli run "APP ADD a b replies with the sum"     the whole way, board included
        --no-deploy        stop after build + security scan
        --fix-rounds 3     --test-rounds 1
  python -m app.pipeline.cli show 2                                     events and report of a pipeline
  python -m app.pipeline.cli list

Stop the backend server first: both would use the board's COM port. At the deployment proposal YOU are the human
approver: type the confirmation text to flash the board, anything else rejects it.
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


def _events(s, pid: int, after: int) -> int:
    for e in s.pipelines.store.events(pid, after):
        print(f"  [{e['stage']:<8}] {e['kind']:<8} {e['message']}")
        after = e["id"]
    return after


def _follow(s, pid: int, job_id: int) -> dict:
    after = 0
    while True:
        after = _events(s, pid, after)
        if s.jobs.get(job_id)["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    _events(s, pid, after)
    return s.pipelines.get(pid)


def cmd_run(args) -> int:
    s = _services()
    try:
        p = s.pipelines.start(args.requirement, "cli", args.fix_rounds, not args.no_deploy, args.test_rounds)
        print(f"pipeline {p['id']} started")
        job_id = p["job_id"]
        seen = 0
        while True:
            p = _follow(s, p["id"], job_id)
            if p["status"] != "WAITING_APPROVAL":
                break
            job = s.jobs.get(p["deploy_job_id"])
            for c in job["params"].get("checks") or []:
                print(f"     {c['status']:<5} {c['name']}: {c['detail']}")
            answer = input(f"  Flash build #{p['build_id']} onto the board? Type  {job['confirm_token']}  to approve: ")
            if answer.strip() == job["confirm_token"]:
                s.jobs.approve(job["id"], "cli:operator", answer.strip())
            else:
                s.jobs.reject(job["id"], "cli:operator", "rejected at the terminal")
            job_id = s.pipelines.resume(p["id"], "cli")["job_id"]
        print("\n" + s.pipelines.report_markdown(p["id"]))
        return 0 if p["outcome"] in ("success", "built") else 1
    finally:
        s.stop()


def cmd_show(args) -> int:
    s = _services()
    try:
        _events(s, args.pipeline_id, 0)
        p = s.pipelines.get(args.pipeline_id)
        if p["report"]:
            print("\n" + s.pipelines.report_markdown(args.pipeline_id))
        else:
            print(f"\npipeline {p['id']} is {p['status']} at stage {p['stage']}")
    finally:
        s.stop()
    return 0


def cmd_list(args) -> int:
    s = _services()
    try:
        for p in s.pipelines.store.list(args.limit):
            print(f"  {p['id']:>4}  {p['status']:<17} {str(p['outcome'] or ''):<13} {p['stage']:<9} "
                  f"{str(p['project'] or ''):<12} {p['requirement'][:50]}")
    finally:
        s.stop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.pipeline.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("requirement")
    r.add_argument("--no-deploy", action="store_true")
    r.add_argument("--fix-rounds", type=int, default=3)
    r.add_argument("--test-rounds", type=int, default=1)
    r.set_defaults(fn=cmd_run)
    sh = sub.add_parser("show")
    sh.add_argument("pipeline_id", type=int)
    sh.set_defaults(fn=cmd_show)
    ls = sub.add_parser("list")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
