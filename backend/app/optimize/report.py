"""The optimisation report, from the records: the baseline, every variant tried (and why it was rejected), the A/B
comparison with its noise margins, the size change, the correctness check and the decision."""
from __future__ import annotations

from pathlib import Path


def build_report(o: dict, services) -> dict:
    st = o.get("state") or {}
    jobs = {}
    for key in ("deploy_job_id", "rollback_job_id"):
        if o.get(key):
            j = services.jobs.get(o[key]) or {}
            jobs[key] = {"job_id": o[key], "status": j.get("status"), "approved_by": j.get("approved_by")}
    return {
        "optimization_id": o["id"], "outcome": o.get("outcome"), "error": o.get("error"), "project": o["project"],
        "goal": o["goal"], "target": st.get("target"), "min_gain_pct": o["min_gain_pct"],
        "benchmark": o.get("benchmark"), "base_build": o.get("base_build"), "variant_build": o.get("variant_build"),
        "base_run_id": o.get("base_run_id"), "variant_run_id": o.get("variant_run_id"),
        "proposal": st.get("proposal"), "analysis": st.get("analysis"), "tried": st.get("tried", []),
        "variants_tried": o.get("variants_tried"), "size": st.get("size"), "comparison": st.get("comparison"),
        "check": st.get("check"), "check_problems": st.get("check_problems"), "fuzz_check": st.get("fuzz_check"),
        "deployment": jobs.get("deploy_job_id"), "rollback": jobs.get("rollback_job_id"),
        "tokens": sum(x.get("tokens", 0) for x in st.get("agent_runs", [])), "agent_runs": st.get("agent_runs", []),
    }


def to_markdown(r: dict, diff: str, events: list) -> str:
    L = [f"# Optimisation {r['optimization_id']}: {r['outcome']}", "",
         f"**Project:** {r['project']}  **Goal:** {r['goal']}" + (f" of `{r['target']}`" if r.get("target") else "")
         + f", at least {r['min_gain_pct']:g} %  ", f"**Baseline:** build #{r['base_build']}, benchmark "
         f"{r['benchmark']} run #{r['base_run_id']}", ""]
    if r.get("error"):
        L += [f"**Note:** {r['error']}", ""]
    if r.get("tried"):
        L += ["## Rejected variants", ""] + [f"- variant {x['variant']} (build #{x['build']}): {x['proposal']} - "
                                             f"**{x['why']}**" for x in r["tried"]] + [""]
    if r.get("proposal") and r["outcome"] == "accepted":
        L += ["## Accepted change", "", r["proposal"], ""]
    cmp = r.get("comparison")
    if cmp:
        L += [f"## A/B: run #{cmp['before']['run_id']} (build #{cmp['before']['build_id']}) vs run "
              f"#{cmp['after']['run_id']} (build #{cmp['after']['build_id']})", "",
              "| command | before | after | change | noise | verdict |", "|---|---|---|---|---|---|"]
        for name, c in cmp["commands"].items():
            m = c.get("device_us") or c.get("host_ms")
            unit = "us" if c.get("device_us") else "ms"
            if m:
                L.append(f"| {name} | {m['before']} {unit} | {m['after']} {unit} | {m['change_pct']} % | "
                         f"{m['noise']} | {m['verdict']} |")
        L.append("")
    if r.get("size"):
        L += ["Size: " + ", ".join(f"{k} {v['before']} -> {v['after']} ({v['change_pct']} %)"
                                   for k, v in r["size"].items()), ""]
    if r.get("check") is not None:
        L += [f"Correctness: {len(r.get('check_problems') or [])} problems; suites "
              + ", ".join(f"{n} {v['verdict']}" for n, v in r["check"].items()) + "", ""]
    if diff and r["outcome"] == "accepted":
        L += ["## Code change", "", "```diff", diff.rstrip(), "```", ""]
    for key, label in (("deployment", "Deployment"), ("rollback", "Rollback")):
        if r.get(key):
            L.append(f"{label} job #{r[key]['job_id']}: {r[key]['status']} (approved by {r[key]['approved_by'] or '-'})")
    L += ["", f"Model tokens: {r['tokens']}.", "", "## Timeline", ""]
    L += [f"- {e['ts']} [{e['stage']}] {e['kind']}: {e['message']}" for e in events]
    return "\n".join(L) + "\n"


def write_markdown(root: Path, oid: int, text: str) -> Path:
    d = Path(root) / "data" / "optimizations" / str(oid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "report.md"
    path.write_text(text, encoding="utf-8")
    return path
