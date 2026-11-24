"""The remediation report, made from the records only: the bug, its reproduction, the root cause, the code diff,
the builds, the human decisions, the retest, the regression check and the final decision."""
from __future__ import annotations

from pathlib import Path


def build_report(r: dict, finding: dict, services) -> dict:
    st = r.get("state") or {}
    jobs = {}
    for key in ("deploy_job_id", "rollback_job_id"):
        if r.get(key):
            j = services.jobs.get(r[key]) or {}
            jobs[key] = {"job_id": r[key], "status": j.get("status"), "approved_by": j.get("approved_by")}
    tokens = sum(x.get("tokens", 0) for x in st.get("agent_runs", []))
    return {
        "remediation_id": r["id"], "outcome": r.get("outcome"), "error": r.get("error"),
        "finding": {k: finding.get(k) for k in ("id", "kind", "severity", "cwe", "input_text", "minimized", "detail",
                                                "status")},
        "project": r.get("project"), "before_build": r.get("before_build"), "after_build": r.get("after_build"),
        "reproduction": {k: (st.get("reproduction") or {}).get(k) for k in ("status", "hits", "attempts")},
        "root_cause": r.get("root_cause"), "diff_stats": st.get("diff_stats"), "fix_rounds": r.get("fix_rounds"),
        "builds": st.get("builds", []), "scan_after": st.get("scan_after"), "deployment": jobs.get("deploy_job_id"),
        "retest": st.get("retest"), "baseline": st.get("baseline"), "after_suites": st.get("after_suites"),
        "regression": st.get("regression"), "regressions": st.get("regressions", []),
        "fuzz_check": st.get("fuzz_check"), "rejection": st.get("rejection"),
        "rollback": jobs.get("rollback_job_id"), "agent_runs": st.get("agent_runs", []), "tokens": tokens,
    }


def to_markdown(rep: dict, diff: str, events: list) -> str:
    f = rep["finding"]
    L = [f"# Remediation {rep['remediation_id']}: {rep['outcome']}", "",
         f"**Bug:** finding #{f['id']} - {f['kind']} ({f['severity']}, {f['cwe']})  ",
         f"**Input:** `{f['minimized'] or f['input_text']}`  ", f"**Seen:** {f['detail']}", ""]
    if rep.get("error"):
        L += [f"**Note:** {rep['error']}", ""]
    rp = rep["reproduction"]
    L += [f"Reproduced on build #{rep['before_build']}: {rp.get('hits')} of {rp.get('attempts')} tries.", ""]
    if rep.get("root_cause"):
        L += ["## Root cause", "", rep["root_cause"], ""]
    if diff:
        L += ["## Fix", "", f"{rep['fix_rounds']} extra fix round(s). Build #{rep['after_build']}.", "", "```diff",
              diff.rstrip(), "```", ""]
    if rep.get("deployment"):
        d = rep["deployment"]
        L += [f"Deployment job #{d['job_id']}: {d['status']} (approved by {d['approved_by'] or '-'})", ""]
    if rep.get("retest"):
        L += ["## Retest of the original bug", ""] + [f"- `{x['input']}`: failed {x['hits']} of {x['attempts']} times"
                                                       for x in rep["retest"]] + [""]
    if rep.get("regression"):
        L += ["## Regression check", "", "| suite | verdict | regressed |", "|---|---|---|"]
        L += [f"| {n} | {v['verdict']} | {', '.join(v['regressed']) or '-'} |" for n, v in rep["regression"].items()]
        L.append("")
    if rep.get("fuzz_check"):
        s = rep["fuzz_check"]["serious"]
        L += [f"Quick fuzz campaign #{rep['fuzz_check']['campaign_id']}: {len(s)} serious findings.", ""]
    if rep.get("rejection"):
        L += ["## Rejected because", ""] + [f"- {x}" for x in rep["rejection"]] + [""]
    if rep.get("rollback"):
        L += [f"Rollback job #{rep['rollback']['job_id']}: {rep['rollback']['status']}", ""]
    L += [f"Model tokens: {rep['tokens']}.", "", "## Timeline", ""]
    L += [f"- {e['ts']} [{e['stage']}] {e['kind']}: {e['message']}" for e in events]
    return "\n".join(L) + "\n"


def write_markdown(root: Path, rid: int, text: str) -> Path:
    d = Path(root) / "data" / "remediations" / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "report.md"
    path.write_text(text, encoding="utf-8")
    return path
