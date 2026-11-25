"""Collectors: turn the lab's records into knowledge entries. Each collector reads one table and upserts entries
keyed by (source_type, source_id, kind), so running them again only refreshes what changed.

    fuzz_findings   -> bug            (what went wrong, the minimised input, the CWE class, the status)
    remediations    -> fix / decision (root cause, the diff, the retest; or why a fix was rejected)
    optimizations   -> optimisation   (what was tried, the measured effect, accepted or rejected and why)
    benchmarks      -> benchmark      (the baseline numbers of each benchmark)
    builds (FAILED) -> build_failure  (the compiler errors)
    test_runs       -> test_failure   (the checks that failed, with the replies)
    pipelines       -> spec           (what a project's firmware is supposed to do)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict

from ..db import connect
from .store import KnowledgeStore

SEV_IMPORTANCE = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


def _rows(db_path: Path, sql: str, args=()) -> list:
    try:
        with connect(db_path) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
    except Exception:  # noqa: BLE001  (a table that does not exist yet in an old database)
        return []


def _verb(text: str) -> str:
    words = re.sub(r"^#\d+\s+", "", text or "").split()
    if not words:
        return "<empty>"
    return " ".join(words[:2]).upper() if words[0].upper() == "APP" and len(words) > 1 else words[0].upper()


def findings(db: Path, k: KnowledgeStore) -> int:
    n = 0
    for f in _rows(db, "SELECT * FROM fuzz_findings WHERE severity != 'info'"):
        inp = f["minimized"] or f["input_text"]
        body = (f"{f['detail']}. Input {inp!r} (family {f['input_family']}, {f['input_note'] or ''}); reply "
                f"{f['reply']!r}. Class: {f['cwe']}. Seen {f['occurrences']} times on build #{f['build_id']}. "
                f"Status: {f['status']}.")
        k.upsert("bug", f"{f['kind']} on {_verb(inp)} in {f['project'] or 'the harness'}", body, project=f["project"],
                 tags=[f["kind"], f["severity"], (f["cwe"] or "").split(" ")[0], _verb(inp).replace(" ", "_"),
                       f["input_family"] or "", "fuzz"],
                 source_type="fuzz_finding", source_id=f["id"], build_id=f["build_id"], src_hash=f["src_hash"],
                 importance=SEV_IMPORTANCE.get(f["severity"], 1) + (0 if f["status"] != "fixed" else -1))
        n += 1
    return n


def remediations(db: Path, k: KnowledgeStore) -> int:
    n = 0
    for r in _rows(db, "SELECT * FROM remediations WHERE status IN ('FINISHED', 'FAILED') AND outcome IS NOT NULL"):
        st = json.loads(r["state_json"] or "{}")
        ev = (st.get("evidence") or {}).get("finding") or {}
        inp = ev.get("minimized") or ev.get("input_text") or ""
        diff = (r["diff_text"] or "")[:1800]
        if r["outcome"] == "fixed":
            retest = ", ".join(f"{x['input']!r} {x['hits']}/{x['attempts']}" for x in st.get("retest", []))
            body = (f"Bug: {ev.get('kind')} on {inp!r} ({ev.get('cwe')}). Root cause: {r['root_cause']}. "
                    f"Fixed in build #{r['after_build']} (was #{r['before_build']}); retest {retest}; no regressions."
                    f"\nDiff:\n{diff}")
            k.upsert("fix", f"fixed {ev.get('kind')} on {_verb(inp)} in {r['project']}", body, project=r["project"],
                     tags=[ev.get("kind") or "", "fix", _verb(inp).replace(" ", "_")], source_type="remediation",
                     source_id=r["id"], build_id=r["after_build"], importance=3)
        else:
            why = "; ".join(st.get("rejection") or []) or r["error"] or r["outcome"]
            body = (f"A fix for {ev.get('kind')} on {inp!r} in {r['project']} was not kept ({r['outcome']}): {why}. "
                    f"Root cause found: {r['root_cause'] or 'none'}.\nDiff tried:\n{diff}")
            k.upsert("decision", f"fix not kept for {_verb(inp)} in {r['project']} ({r['outcome']})", body,
                     project=r["project"], tags=["remediation", r["outcome"]], source_type="remediation",
                     source_id=r["id"], build_id=r["before_build"], importance=2)
        n += 1
    return n


def optimizations(db: Path, k: KnowledgeStore) -> int:
    n = 0
    for o in _rows(db, "SELECT * FROM optimizations WHERE status IN ('FINISHED', 'FAILED') AND outcome IS NOT NULL"):
        st = json.loads(o["state_json"] or "{}")
        cmp = st.get("comparison") or {}
        effect = ", ".join(f"{name} {m['before']} -> {m['after']} ({m['change_pct']} %)"
                           for name, c in (cmp.get("commands") or {}).items()
                           for m in [c.get("device_us") or c.get("host_ms")] if m)
        tried = "; ".join(f"variant {x['variant']}: {x['proposal']} - rejected: {x['why']}" for x in st.get("tried", []))
        body = (f"Goal {o['goal']} ({st.get('target') or 'all'}) for {o['project']} from build #{o['base_build']}: "
                f"{o['outcome']}. " + (f"Accepted: {st.get('proposal')}. Measured: {effect}. " if o["outcome"] ==
                                       "accepted" else "") + (f"Tried: {tried}. " if tried else "")
                + (f"Size: {st.get('size')}." if st.get("size") else ""))
        k.upsert("optimisation", f"{o['goal']} optimisation of {o['project']}: {o['outcome']}", body,
                 project=o["project"], tags=["optimisation", o["goal"], o["outcome"]], source_type="optimization",
                 source_id=o["id"], build_id=o["variant_build"] if o["outcome"] == "accepted" else o["base_build"],
                 importance=3 if o["outcome"] == "accepted" else 2)
        n += 1
    return n


def benchmarks(db: Path, k: KnowledgeStore) -> int:
    n = 0
    for b in _rows(db, "SELECT b.name, b.project, r.id AS run_id, r.build_id, r.src_hash, r.result_json, "
                       "r.flash_bytes, r.ram_bytes FROM benchmarks b JOIN benchmark_runs r ON r.id = b.baseline_run_id"):
        res = json.loads(b["result_json"])
        nums = ", ".join(f"{name} ({c['command']}) {(c['device_us'] or c['host_ms'])['median']}"
                         f"{' us' if c['device_us'] else ' ms host'}" for name, c in res["commands"].items())
        body = (f"Baseline of benchmark {b['name']} on build #{b['build_id']}: {nums}; flash {b['flash_bytes']}, "
                f"RAM {b['ram_bytes']} bytes; throughput {(res.get('throughput_cps') or {}).get('median')} commands/s.")
        k.upsert("benchmark", f"baseline of {b['name']}", body, project=b["project"], tags=["benchmark", "baseline"],
                 source_type="benchmark_run", source_id=b["run_id"], build_id=b["build_id"], src_hash=b["src_hash"])
        n += 1
    return n


def build_failures(db: Path, k: KnowledgeStore) -> int:
    n = 0
    for b in _rows(db, "SELECT id, project, result_json FROM builds WHERE status = 'FAILED'"):
        res = json.loads(b["result_json"] or "{}")
        errs = [d for d in res.get("diagnostics", []) if d.get("severity") == "error"][:6]
        if not errs:
            continue
        body = "; ".join(f"{d.get('file')}:{d.get('line')}: {d.get('message')}" for d in errs)
        k.upsert("build_failure", f"build #{b['id']} of {b['project']} failed", body, project=b["project"],
                 tags=["build", "compile_error"], source_type="build", source_id=b["id"], build_id=b["id"],
                 src_hash=res.get("sources_sha256"), importance=1)
        n += 1
    return n


def test_failures(db: Path, k: KnowledgeStore) -> int:
    n = 0
    for t in _rows(db, "SELECT r.*, s.project AS project FROM test_runs r LEFT JOIN test_suites s ON s.id = r.suite_id "
                       "WHERE r.failed > 0"):
        res = json.loads(t["result_json"] or "{}")
        bad = [c for c in res.get("cases", []) if not c.get("passed")][:6]
        body = "; ".join(f"{c['name']}: {c['command']!r} -> {c.get('reply')!r} ({c.get('why')})" for c in bad)
        k.upsert("test_failure", f"{t['failed']} of {t['total']} checks failed in {t['name']}", body,
                 project=t.get("project"), tags=["test", "failure"], source_type="test_run", source_id=t["id"],
                 build_id=t.get("build_id"), src_hash=t.get("src_hash"), importance=1)
        n += 1
    return n


def specs(db: Path, k: KnowledgeStore) -> int:
    n = 0
    for p in _rows(db, "SELECT id, project, requirement, spec_json, outcome FROM pipelines WHERE spec_json IS NOT NULL"):
        spec = json.loads(p["spec_json"])
        cmds = "; ".join(f"{c.get('usage')}: {c.get('description', '')}" for c in spec.get("commands", []))
        body = f"Requirement: {p['requirement']}\nCommands: {cmds}\nAssumptions: {'; '.join(spec.get('assumptions', []))}"
        k.upsert("spec", f"specification of {p['project']}: {spec.get('title', '')}", body, project=p["project"],
                 tags=["spec"], source_type="pipeline", source_id=p["id"], importance=1)
        n += 1
    return n


COLLECTORS: Dict[str, Callable] = {"bugs": findings, "fixes": remediations, "optimisations": optimizations,
                                   "benchmarks": benchmarks, "build_failures": build_failures,
                                   "test_failures": test_failures, "specs": specs}


def collect_all(db_path: Path, k: KnowledgeStore) -> dict:
    return {name: fn(Path(db_path), k) for name, fn in COLLECTORS.items()}
