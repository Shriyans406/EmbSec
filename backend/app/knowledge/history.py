"""Histories across the whole lab, from the records:

    timeline(project)   everything that happened to a project, newest first: builds, deployments, test runs, fuzz
                        campaigns and findings, remediations, optimisations, benchmark runs, pipelines, decisions
    board_history(uid)  which firmware a board ran when (device configuration history)
    decision records    engineering decisions written down on purpose (context / decision / consequences), stored
                        as knowledge entries so agents find them later
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..db import connect
from ..errors import ApiError
from .store import KnowledgeStore


def _q(db: Path, sql: str, args=()) -> list:
    try:
        with connect(db) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def timeline(db_path: Path, project: str, limit: int = 200) -> List[dict]:
    db = Path(db_path)
    builds = {r["id"]: r for r in _q(db, "SELECT id, status, started_at, errors, warnings, sources_sha256 FROM builds "
                                         "WHERE project = ?", (project,))}
    ids = tuple(builds) or (-1,)
    marks = ",".join("?" * len(ids))
    ev: List[dict] = []
    for b in builds.values():
        ev.append({"ts": b["started_at"], "type": "build", "id": b["id"], "build_id": b["id"],
                   "summary": f"build #{b['id']} {b['status']} ({b['errors']} errors, {b['warnings']} warnings, "
                              f"sources {(b['sources_sha256'] or '')[:8]})"})
    for d in _q(db, f"SELECT * FROM deployments WHERE build_id IN ({marks})", ids):
        ev.append({"ts": d["started_at"], "type": "deployment", "id": d["id"], "build_id": d["build_id"],
                   "summary": f"{d['kind']} of build #{d['build_id']} to board {d['board_uid']}: {d['status']} "
                              f"(approved by {d['approved_by']})"})
    for t in _q(db, f"SELECT r.id, r.name, r.passed, r.failed, r.total, r.started_at, r.build_id FROM test_runs r "
                    f"LEFT JOIN test_suites s ON s.id = r.suite_id WHERE s.project = ? OR r.build_id IN ({marks})",
                (project, *ids)):
        ev.append({"ts": t["started_at"], "type": "test_run", "id": t["id"], "build_id": t["build_id"],
                   "summary": f"test run '{t['name']}' on build #{t['build_id']}: {t['passed']}/{t['total']} passed"})
    for c in _q(db, "SELECT * FROM fuzz_campaigns WHERE project = ?", (project,)):
        ev.append({"ts": c["created_at"], "type": "fuzz_campaign", "id": c["id"], "build_id": c["build_id"],
                   "summary": f"fuzz campaign #{c['id']} on build #{c['build_id']}: {c['inputs_sent']} inputs, "
                              f"{c['findings']} new findings, {c['resets']} resets ({c['status']})"})
    for f in _q(db, "SELECT * FROM fuzz_findings WHERE project = ?", (project,)):
        ev.append({"ts": f["created_at"], "type": "finding", "id": f["id"], "build_id": f["build_id"],
                   "summary": f"finding #{f['id']} [{f['severity']}] {f['kind']} on "
                              f"{(f['minimized'] or f['input_text'])[:40]!r} - {f['status']}"})
    for r in _q(db, "SELECT * FROM remediations WHERE project = ?", (project,)):
        ev.append({"ts": r["created_at"], "type": "remediation", "id": r["id"], "build_id": r["after_build"],
                   "summary": f"remediation #{r['id']} of finding #{r['finding_id']}: {r['outcome'] or r['status']} "
                              f"(#{r['before_build']} -> #{r['after_build']})"})
    for o in _q(db, "SELECT * FROM optimizations WHERE project = ?", (project,)):
        ev.append({"ts": o["created_at"], "type": "optimisation", "id": o["id"], "build_id": o["variant_build"],
                   "summary": f"{o['goal']} optimisation #{o['id']}: {o['outcome'] or o['status']} "
                              f"(#{o['base_build']} -> #{o['variant_build']}, {o['variants_tried']} variants)"})
    for b in _q(db, "SELECT r.id, r.created_at, r.build_id, r.repeatable, b.name, b.baseline_run_id FROM benchmark_runs r "
                    "JOIN benchmarks b ON b.id = r.benchmark_id WHERE b.project = ?", (project,)):
        ev.append({"ts": b["created_at"], "type": "benchmark_run", "id": b["id"], "build_id": b["build_id"],
                   "summary": f"benchmark {b['name']} run #{b['id']} on build #{b['build_id']}"
                              + (" (baseline)" if b["id"] == b["baseline_run_id"] else "")
                              + ("" if b["repeatable"] else " - not repeatable")})
    for p in _q(db, "SELECT * FROM pipelines WHERE project = ?", (project,)):
        ev.append({"ts": p["created_at"], "type": "pipeline", "id": p["id"], "build_id": p["build_id"],
                   "summary": f"pipeline #{p['id']}: {p['outcome'] or p['status']} - {p['requirement'][:60]}"})
    for k in _q(db, "SELECT * FROM knowledge WHERE project = ? AND source_type IN ('adr', 'note')", (project,)):
        ev.append({"ts": k["created_at"], "type": k["kind"], "id": k["id"], "build_id": k["build_id"],
                   "summary": k["title"]})
    ev.sort(key=lambda e: (e["ts"] or "", e["type"], e["id"]), reverse=True)
    return ev[:limit]


def board_history(db_path: Path, uid: str) -> dict:
    db = Path(db_path)
    dev = _q(db, "SELECT * FROM devices WHERE uid = ?", (uid,))
    if not dev:
        raise ApiError(404, "not_found", f"board {uid} is not registered")
    rows = _q(db, "SELECT d.id, d.kind, d.status, d.build_id, d.image_label, d.approved_by, d.started_at, "
                  "d.finished_at, b.project FROM deployments d LEFT JOIN builds b ON b.id = d.build_id "
                  "WHERE d.board_uid = ? ORDER BY d.id", (uid,))
    periods, current = [], None
    for r in rows:
        if r["status"] == "SUCCESS":
            if current:
                current["until"] = r["finished_at"] or r["started_at"]
            current = {"from": r["finished_at"] or r["started_at"], "until": None, "build_id": r["build_id"],
                       "project": r["project"], "image": r["image_label"], "deployment_id": r["id"],
                       "approved_by": r["approved_by"]}
            periods.append(current)
    return {"uid": uid, "first_seen": dev[0]["first_seen"], "last_seen": dev[0]["last_seen"],
            "deployments": rows, "firmware_periods": list(reversed(periods)),
            "current": periods[-1] if periods else None}


def add_decision(k: KnowledgeStore, title: str, context: str, decision: str, consequences: str,
                 project: Optional[str], who: str, tags=()) -> dict:
    """An engineering decision record (ADR-style). Its id doubles as the source id, so it can be looked up."""
    body = f"Context: {context}\nDecision: {decision}\nConsequences: {consequences}"
    with connect(k.db_path) as c:
        nxt = (c.execute("SELECT COALESCE(MAX(source_id), 0) + 1 FROM knowledge WHERE source_type = 'adr'")
               .fetchone()[0])
    return k.upsert("decision", title, body, project=project, tags=["adr", *tags], source_type="adr",
                    source_id=int(nxt), importance=3, created_by=who)


def add_note(k: KnowledgeStore, title: str, body: str, project: Optional[str], who: str, tags=()) -> dict:
    with connect(k.db_path) as c:
        nxt = (c.execute("SELECT COALESCE(MAX(source_id), 0) + 1 FROM knowledge WHERE source_type = 'note'")
               .fetchone()[0])
    return k.upsert("note", title, body, project=project, tags=["note", *tags], source_type="note",
                    source_id=int(nxt), importance=2, created_by=who)


