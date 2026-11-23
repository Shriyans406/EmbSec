"""Run the AI agents from a terminal, without the web server.   (from the backend folder)

  python -m app.ai.cli status                      model, agents and token usage so far
  python -m app.ai.cli run "make the LED blink"    start a run and follow it step by step
        --agent lab   --max-steps 20
  python -m app.ai.cli team "add a TEMP_F command"  the Supervisor plans and delegates to the five specialists
  python -m app.ai.cli show 3                      a finished run: steps, report and its team
  python -m app.ai.cli runs                        recent runs

Stop the backend server first if the agent may talk to the board: both would use the COM port.
When a run reaches a deployment proposal, you are asked for the confirmation text in this terminal: YOU are the
human approver then. Typing anything else rejects the proposal.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from ..settings import Settings


def _services():
    from ..services.container import build_services

    s = build_services(Settings())
    s.start()
    return s


def _print_steps(agents, run_id: int, after: int) -> int:
    for st in agents.store.steps(run_id, after):
        d = st["detail"]
        if st["kind"] == "model":
            text = (d.get("text") or "").strip().replace("\n", " ")
            calls = ", ".join(d.get("calls") or [])
            print(f"  [model] {text[:160]}" + (f"  -> {calls}" if calls else "") + f"  ({d.get('tokens', 0)} tok)")
        elif st["kind"] == "tool":
            mark = "ok " if st["ok"] else "ERR"
            resp = d.get("response", {})
            extra = "" if st["ok"] else f" {resp.get('error', {}).get('message', '')[:120]}"
            print(f"  [tool ] {mark} {st['name']} {json.dumps(d.get('args', {}))[:120]}{extra}")
        else:
            print(f"  [{st['kind']}] {st['name']} {json.dumps(d)[:200]}")
        after = st["id"]
    return after


def _follow(s, run_id: int, job_id: int) -> dict:
    after = 0
    while True:
        job = s.jobs.get(job_id)
        after = _print_steps(s.agents, run_id, after)
        if job["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    _print_steps(s.agents, run_id, after)
    return s.agents.get(run_id)


def _decide(s, run: dict) -> bool:
    """Ask the human at the terminal about the pending proposal. True = the run should continue."""
    job = s.jobs.get(run["pending_job_id"])
    print(f"\n  The agent proposes job #{job['id']}: {job['summary']}")
    for c in (job["params"].get("checks") or []):
        print(f"     {c['status']:<5} {c['name']}: {c['detail']}")
    answer = input(f"  Type  {job['confirm_token']}  to approve (anything else rejects): ").strip()
    if answer == job["confirm_token"]:
        s.jobs.approve(job["id"], "cli:operator", answer)
        print("  approved - the deployment runs now")
    else:
        s.jobs.reject(job["id"], "cli:operator", "rejected at the terminal")
        print("  rejected")
    return True


def cmd_run(args) -> int:
    s = _services()
    try:
        run = s.agents.start(args.goal, "cli", args.agent, args.max_steps)
        print(f"agent run {run['id']} ({run['agent']}) started")
        job_id = run["job_id"]
        while True:
            run = _follow(s, run["id"], job_id)
            if run["status"] != "WAITING_APPROVAL":
                break
            _decide(s, run)
            job_id = s.agents.resume(run["id"], "cli")["job_id"]
        _show(s, run["id"])
        _print_tree(s, run["id"])
        return 0 if run["status"] == "FINISHED" and run["outcome"] in ("success", "partial") else 1
    finally:
        s.stop()


def _show(s, run_id: int) -> None:
    d = s.agents.details(run_id)
    u = d["usage"]
    print(f"\nrun {d['id']}  {d['status']}  outcome={d['outcome']}  steps={d['steps']}  "
          f"tokens={u['total']} in {u['calls']} model calls")
    if d.get("error"):
        print(f"  note: {d['error']}")
    r = d.get("report") or {}
    if r:
        print(f"  summary: {r.get('summary')}")
        for key in ("facts", "changes", "unverified", "next_steps"):
            for item in r.get(key) or []:
                print(f"    {key[:-1] if key.endswith('s') else key}: {item}")


def _print_tree(s, run_id: int) -> None:
    t = s.agents.tree(run_id)
    if not t["children"]:
        return
    print("\n  team:")
    for c in t["children"]:
        r = c.get("report") or {}
        verdict = f" verdict={r['verdict']}" if r.get("verdict") else ""
        print(f"    run {c['id']:>4} {c['agent']:<13} {c['status']:<17} {str(c['outcome']):<18}{verdict} "
              f"steps={c['steps']} tokens={c['tokens']}")
        if r.get("summary"):
            print(f"         {r['summary'][:200]}")
    print(f"  whole team: {t['team_usage']['total']} tokens in {t['team_usage']['calls']} model calls")


def cmd_team(args) -> int:
    args.agent = "supervisor"
    return cmd_run(args)


def cmd_show(args) -> int:
    s = _services()
    try:
        _print_steps(s.agents, args.run_id, 0)
        _show(s, args.run_id)
        _print_tree(s, args.run_id)
    finally:
        s.stop()
    return 0


def cmd_runs(args) -> int:
    s = _services()
    try:
        for r in s.agents.store.list(args.limit):
            print(f"  {r['id']:>4}  {r['status']:<17} {str(r['outcome'] or ''):<18} {r['agent']:<12} {r['goal'][:60]}")
    finally:
        s.stop()
    return 0


def cmd_status(args) -> int:
    s = _services()
    try:
        print(json.dumps(s.agents.status(), indent=2))
    finally:
        s.stop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.ai.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("goal")
    p.add_argument("--agent", default="lab")
    p.add_argument("--max-steps", type=int, default=None)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("team", help="the Supervisor and its specialists work on the goal")
    p.add_argument("goal")
    p.add_argument("--max-steps", type=int, default=None)
    p.set_defaults(fn=cmd_team)
    p = sub.add_parser("show")
    p.add_argument("run_id", type=int)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_runs)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
