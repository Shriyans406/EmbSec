"""The pipeline report: every fact comes from the records (spec, builds, scan, deployment job, test run, agent
runs, token log), never from a model's summary. Written as JSON (in the database) and Markdown (on disk)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional


def _seconds(a: Optional[str], b: Optional[str]) -> Optional[float]:
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def build_report(p: dict, services, finished_at: str) -> dict:
    spec, state = p.get("spec") or {}, p.get("state") or {}
    build = None
    if p.get("build_id"):
        try:
            b = services.builds.get(p["build_id"])
            build = {"id": b["id"], "status": b["status"], "flash_bytes": b.get("flash_bytes"),
                     "ram_bytes": b.get("ram_bytes"), "flash_pct": b.get("flash_pct"), "uf2_sha256": b.get("uf2_sha256"),
                     "sources_sha256": b.get("sources_sha256")}
        except Exception:  # noqa: BLE001
            build = {"id": p["build_id"]}
    deployment = None
    if p.get("deploy_job_id"):
        job = services.jobs.get(p["deploy_job_id"]) or {}
        deployment = {"job_id": p["deploy_job_id"], "status": job.get("status"), "approved_by": job.get("approved_by"),
                      "error": job.get("error")}
    tests, checks = None, []
    if p.get("test_run_id"):
        try:
            run = services.tests.get_run(p["test_run_id"])
            tests = {"run_id": run["id"], "total": run["total"], "passed": run["passed"], "failed": run["failed"]}
            checks = [{k: c.get(k) for k in ("name", "command", "reply", "passed", "why", "latency_ms")}
                      for c in (run.get("result") or {}).get("cases", [])]
        except Exception:  # noqa: BLE001
            tests = {"run_id": p["test_run_id"]}
    agent_tokens = sum(r.get("tokens", 0) for r in state.get("agent_runs", []))
    return {
        "pipeline_id": p["id"], "outcome": p.get("outcome"), "error": p.get("error"),
        "requirement": p["requirement"], "project": p.get("project"),
        "spec": {"title": spec.get("title"), "summary": spec.get("summary"),
                 "commands": [c.get("usage") for c in spec.get("commands", [])],
                 "examples": sum(len(c.get("examples", [])) for c in spec.get("commands", [])),
                 "constraints": spec.get("constraints", []), "assumptions": spec.get("assumptions", [])} if spec else None,
        "builds": state.get("builds", []), "final_build": build,
        "fix_rounds": p.get("fix_rounds", 0), "test_rounds": p.get("test_rounds", 0),
        "security_scan": state.get("scan"), "deployment": deployment,
        "tests": tests, "checks": checks, "test_history": state.get("tests", []),
        "agent_runs": state.get("agent_runs", []),
        "tokens": {"spec": state.get("spec_tokens", 0), "agents": agent_tokens,
                   "total": state.get("spec_tokens", 0) + agent_tokens},
        "duration_s": _seconds(p.get("created_at"), finished_at),
    }


def to_markdown(r: dict, events: list) -> str:
    L = [f"# Pipeline {r['pipeline_id']}: {r['outcome']}", "", f"**Requirement:** {r['requirement']}", ""]
    if r.get("error"):
        L += [f"**Stopped because:** {r['error']}", ""]
    s = r.get("spec")
    if s:
        L += [f"## Specification: {s['title']}", "", s.get("summary") or "", ""]
        L += [f"- `{c}`" for c in s["commands"]] + ["", f"{s['examples']} examples became board tests."]
        if s["assumptions"]:
            L += ["", "Assumptions:"] + [f"- {a}" for a in s["assumptions"]]
        L.append("")
    L += ["## Builds", "", "| build | status | errors | warnings |", "|---|---|---|---|"]
    L += [f"| #{b.get('build_id')} | {b.get('status')} | {b.get('errors')} | {b.get('warnings')} |"
          for b in r["builds"]]
    fb = r.get("final_build")
    if fb:
        L += ["", f"Final build #{fb['id']}: {fb.get('flash_bytes')} bytes flash ({fb.get('flash_pct')} %), "
                  f"{fb.get('ram_bytes')} bytes RAM."]
    L += ["", f"Fix rounds: {r['fix_rounds']}, test rounds: {r['test_rounds']}."]
    if r.get("security_scan"):
        L += [f"Security scan: **{r['security_scan']['verdict']}** {r['security_scan']['counts']}"]
    d = r.get("deployment")
    L += ["", "## Deployment", "", (f"Job #{d['job_id']}: {d['status']} (approved by {d['approved_by'] or '-'})"
                                    if d else "No deployment was proposed.")]
    t = r.get("tests")
    L += ["", "## Tests on the board", ""]
    if t:
        L += [f"{t.get('passed')} of {t.get('total')} checks passed (test run #{t.get('run_id')}).", "",
              "| check | request | reply | result |", "|---|---|---|---|"]
        L += [f"| {c['name']} | `{c['command']}` | `{c['reply']}` | {'PASS' if c['passed'] else 'FAIL: ' + c['why']} |"
              for c in r["checks"]]
    else:
        L.append("No tests were run.")
    L += ["", "## Cost", "", f"{r['tokens']['total']} model tokens ({r['tokens']['spec']} for the spec, "
                             f"{r['tokens']['agents']} for {len(r['agent_runs'])} agent runs). "
                             f"Time: {r['duration_s']} s.", "", "## Timeline", ""]
    L += [f"- {e['ts']} [{e['stage']}] {e['kind']}: {e['message']}" for e in events]
    return "\n".join(L) + "\n"


def write_markdown(root: Path, pid: int, text: str) -> Path:
    d = Path(root) / "data" / "pipelines" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "report.md"
    path.write_text(text, encoding="utf-8")
    return path
