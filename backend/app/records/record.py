"""Task records: one bundle with everything about one task, taken from the database at one moment.

A task is an agent run, a pipeline, a remediation, an optimisation or a fuzz campaign. Its record holds
    task        the task's own row (and events / steps)
    agents      every agent run that worked for it: steps, model calls, tokens
    jobs        every job it started (builds, deployments, tests, benchmarks, fuzzing) with its log
    builds      the builds it made: sources hash, image hash, sizes, diagnostics
    deployments the deployments of those builds, with WHO approved them
    tests       its test runs (check by check) and benchmark runs
    audit       the audit entries in its time window (with their chain hashes)
    knowledge   what the knowledge base learned from it
plus a hash of the whole bundle and the audit head at the time, so a record can be checked later.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Optional

from ..db import connect, now_iso
from ..errors import ApiError

KINDS = {
    "agent_run": ("agent_runs", "run"),
    "pipeline": ("pipelines", "pipeline"),
    "remediation": ("remediations", "remediation"),
    "optimisation": ("optimizations", "optimisation"),
    "fuzz_campaign": ("fuzz_campaigns", "fuzz"),
}
EVENTS = {"pipeline": ("pipeline_events", "pipeline_id"), "remediation": ("remediation_events", "remediation_id"),
          "optimisation": ("optimization_events", "optimization_id")}
KNOWLEDGE_SOURCE = {"remediation": "remediation", "optimisation": "optimization", "pipeline": "pipeline"}


def _rows(c, sql, args=()) -> List[dict]:
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except Exception:  # noqa: BLE001  (an older database without the table)
        return []


def _json_fields(d: dict) -> dict:
    for k in list(d):
        if k.endswith("_json") and isinstance(d[k], str):
            try:
                d[k[:-5]] = json.loads(d.pop(k))
            except ValueError:
                pass
    return d


def canonical_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def build_record(db_path: Path, kind: str, task_id: int, audit=None) -> dict:
    if kind not in KINDS:
        raise ApiError(422, "bad_kind", f"kind must be one of {sorted(KINDS)}")
    table, tag = KINDS[kind]
    with connect(Path(db_path)) as c:
        rows = _rows(c, f"SELECT * FROM {table} WHERE id = ?", (task_id,))
        if not rows:
            raise ApiError(404, "not_found", f"no {kind} {task_id}")
        task = _json_fields(rows[0])
        start = task.get("created_at")
        end = task.get("finished_at") or now_iso()
        rec: dict = {"kind": kind, "id": task_id, "task": task}

        if kind in EVENTS:
            t, col = EVENTS[kind]
            rec["events"] = [_json_fields(e) for e in _rows(c, f"SELECT * FROM {t} WHERE {col} = ? ORDER BY id",
                                                             (task_id,))]

        # ---- agent runs: the run itself and its children, or the runs a workflow started
        run_ids: List[int] = []
        if kind == "agent_run":
            run_ids = [task_id] + [r["id"] for r in _rows(c, "SELECT id FROM agent_runs WHERE parent_id = ?",
                                                          (task_id,))]
        else:
            state = task.get("state") or {}
            run_ids = [r["run_id"] for r in state.get("agent_runs", []) if r.get("run_id")]
            run_ids += [r["id"] for r in _rows(c, "SELECT id FROM agent_runs WHERE requested_by = ?",
                                               (f"{tag}:{task_id}",)) if r["id"] not in run_ids]
        agents = []
        for rid in sorted(set(run_ids)):
            run = _rows(c, "SELECT * FROM agent_runs WHERE id = ?", (rid,))
            if not run:
                continue
            run = _json_fields(run[0])
            run["steps"] = [_json_fields(s) for s in _rows(c, "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY id",
                                                           (rid,))]
            run["llm_calls"] = _rows(c, "SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id", (rid,))
            agents.append(run)
        rec["agents"] = agents

        # ---- jobs: those it started (requested_by "<tag>:<id>"), the ones that drove it, and agent-started ones
        who = [f"{tag}:{task_id}", f"{tag}:{task_id} rollback"] + [f"run:{r}" for r in run_ids]
        marks = ",".join("?" * len(who))
        jobs = _rows(c, f"SELECT * FROM jobs WHERE requested_by IN ({marks}) ORDER BY id", who)
        key = {"pipeline": "pipeline_id", "remediation": "remediation_id", "optimisation": "optimization_id",
               "fuzz_campaign": "campaign_id", "agent_run": "run_id"}[kind]
        for j in _rows(c, "SELECT * FROM jobs WHERE params_json LIKE ? OR params_json LIKE ? ORDER BY id",
                       (f'%"{key}": {task_id}}}%', f'%"{key}": {task_id},%')):
            if j["id"] not in {x["id"] for x in jobs}:
                jobs.append(j)
        if kind == "fuzz_campaign" and task.get("job_id") and task["job_id"] not in {x["id"] for x in jobs}:
            jobs += _rows(c, "SELECT * FROM jobs WHERE id = ?", (task["job_id"],))
        for key_name in ("deploy_job_id", "rollback_job_id"):
            jid = task.get(key_name)
            if jid and jid not in {x["id"] for x in jobs}:
                jobs += _rows(c, "SELECT * FROM jobs WHERE id = ?", (jid,))
        jobs.sort(key=lambda j: j["id"])
        for j in jobs:
            _json_fields(j)
            j["log"] = [f"{x['ts']} {x['line']}" for x in _rows(c, "SELECT ts, line FROM job_logs WHERE job_id = ? "
                                                                   "ORDER BY id", (j["id"],))]
        rec["jobs"] = jobs
        job_ids = [j["id"] for j in jobs]
        jm = ",".join("?" * len(job_ids)) or "NULL"

        # ---- builds and deployments
        build_ids = sorted({b for j in jobs if j["kind"] == "build" for b in [(j.get("result") or {}).get("id")] if b}
                           | {task.get(k) for k in ("build_id", "before_build", "after_build", "base_build",
                                                    "variant_build") if task.get(k)})
        bm = ",".join("?" * len(build_ids)) or "NULL"
        rec["builds"] = [{k: v for k, v in _json_fields(b).items() if k != "result"} | {
            "result": {k: (b.get("result") or {}).get(k) for k in ("status", "flash_bytes", "ram_bytes", "harness_version",
                                                                   "sources_sha256", "uf2_sha256", "errors", "warnings")}}
                         for b in _rows(c, f"SELECT * FROM builds WHERE id IN ({bm}) ORDER BY id", build_ids)]
        rec["deployments"] = [_json_fields(d) for d in _rows(c, f"SELECT * FROM deployments WHERE build_id IN ({bm}) "
                                                                f"AND started_at >= ? ORDER BY id", (*build_ids, start))]
        rec["approvals"] = [{"job_id": j["id"], "kind": j["kind"], "status": j["status"],
                             "approved_by": j["approved_by"], "summary": j["summary"]}
                            for j in jobs if j["kind"] in ("deploy", "recover")]

        # ---- tests, benchmarks, fuzzing
        rec["test_runs"] = [_json_fields(t) for t in _rows(c, f"SELECT * FROM test_runs WHERE job_id IN ({jm}) "
                                                              f"ORDER BY id", job_ids)]
        rec["benchmark_runs"] = [{k: v for k, v in _json_fields(b).items()}
                                 for b in _rows(c, f"SELECT * FROM benchmark_runs WHERE job_id IN ({jm}) ORDER BY id",
                                                job_ids)]
        camp_ids = [task_id] if kind == "fuzz_campaign" else [
            x["id"] for x in _rows(c, f"SELECT id FROM fuzz_campaigns WHERE job_id IN ({jm})", job_ids)]
        cm = ",".join("?" * len(camp_ids)) or "NULL"
        rec["fuzz_findings"] = _rows(c, f"SELECT * FROM fuzz_findings WHERE campaign_id IN ({cm}) ORDER BY id", camp_ids)

        # ---- audit window and knowledge
        rec["audit"] = _rows(c, "SELECT * FROM audit_log WHERE ts >= ? AND ts <= ? ORDER BY id", (start, end))
        src = KNOWLEDGE_SOURCE.get(kind)
        rec["knowledge"] = _rows(c, "SELECT id, kind, title, source_type, source_id FROM knowledge WHERE "
                                    "source_type = ? AND source_id = ?", (src, task_id)) if src else []

    rec["window"] = {"from": start, "until": end}
    rec["counts"] = {k: len(rec[k]) for k in ("agents", "jobs", "builds", "deployments", "approvals", "test_runs",
                                               "benchmark_runs", "fuzz_findings", "audit", "knowledge")}
    rec["tokens"] = sum(call.get("total_tokens") or 0 for a in agents for call in a["llm_calls"])
    rec["taken_at"] = now_iso()
    rec["audit_head"] = audit.head() if audit else None
    rec["record_hash"] = canonical_hash({k: v for k, v in rec.items() if k not in ("taken_at", "record_hash")})
    return rec


def timeline(rec: dict) -> List[dict]:
    """Every timestamped thing in the record, in order: the task's story."""
    ev = []
    for e in rec.get("events", []):
        ev.append({"ts": e["ts"], "source": "task", "what": f"[{e['stage']}] {e['kind']}: {e['message']}"})
    for a in rec.get("agents", []):
        for s in a.get("steps", []):
            ev.append({"ts": s["ts"], "source": f"agent run {a['id']} ({a['agent']})",
                       "what": f"{s['kind']} {s['name']}" + ("" if s["ok"] is None else (" ok" if s["ok"] else " FAILED"))})
    for j in rec.get("jobs", []):
        for line in j.get("log", []):
            ts, _, text = line.partition(" ")
            ev.append({"ts": ts, "source": f"job {j['id']} ({j['kind']})", "what": text})
    for a in rec.get("audit", []):
        ev.append({"ts": a["ts"], "source": "audit", "what": f"{a['actor']}/{a['role']} {a['action']} "
                                                             f"{(a['target'] or '')[:60]} -> {a['outcome']}"})
    ev.sort(key=lambda e: (e["ts"] or ""))
    return ev
