"""Optimisations and their event log (tables optimizations / optimization_events)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..db import connect, now_iso
from ..errors import ApiError

QUEUED, RUNNING, WAITING, FINISHED, FAILED, STOPPED = ("QUEUED", "RUNNING", "WAITING_APPROVAL", "FINISHED", "FAILED",
                                                       "STOPPED")
FINAL = {FINISHED, FAILED, STOPPED}

(BASELINE, ANALYSE, VARIANT, BUILD, SCAN, PROPOSE, DEPLOYED, MEASURE, CHECK, DECIDE, ROLLBACK, ROLLED_BACK, REPORT,
 DONE) = ("baseline", "analyse", "variant", "build", "scan", "propose", "deployed", "measure", "check", "decide",
          "rollback", "rolled_back", "report", "done")

GOALS = ("speed", "flash", "ram")

_FIELDS = {"benchmark", "status", "stage", "outcome", "base_build", "variant_build", "base_run_id", "variant_run_id",
           "deploy_job_id", "rollback_job_id", "variants_tried", "fix_rounds", "state_json", "diff_text",
           "report_json", "error", "stop_requested", "finished_at"}


class OptimizationStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def create(self, project: str, goal: str, target: Optional[str], min_gain_pct: float, who: str,
               params: dict) -> dict:
        now = now_iso()
        with connect(self.db_path) as c:
            cur = c.execute("INSERT INTO optimizations (project, goal, target, min_gain_pct, status, stage, "
                            "requested_by, params_json, state_json, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (project, goal, target, min_gain_pct, QUEUED, BASELINE, who, json.dumps(params), "{}",
                             now, now))
            oid = int(cur.lastrowid)
        return self.get(oid)

    def update(self, oid: int, **fields) -> None:
        bad = set(fields) - _FIELDS
        if bad:
            raise ValueError(f"unknown optimisation fields {bad}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with connect(self.db_path) as c:
            c.execute(f"UPDATE optimizations SET {cols} WHERE id = ?", (*fields.values(), oid))

    def set_state(self, oid: int, **values) -> dict:
        state = {**self.get(oid)["state"], **values}
        self.update(oid, state_json=json.dumps(state, default=str))
        return state

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for key, default in (("params", {}), ("state", {}), ("report", None)):
            raw = d.pop(f"{key}_json")
            d[key] = json.loads(raw) if raw else default
        d["stop_requested"] = bool(d["stop_requested"])
        return d

    def get(self, oid: int) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM optimizations WHERE id = ?", (oid,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no optimisation {oid}")
        return self._row(r)

    def list(self, limit: int = 20, project: Optional[str] = None) -> List[dict]:
        sql, args = "SELECT * FROM optimizations", []
        if project:
            sql += " WHERE project = ?"
            args.append(project)
        with connect(self.db_path) as c:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 200)))).fetchall()
        return [self._row(r) for r in rows]

    def active_for(self, project: str) -> Optional[dict]:
        with connect(self.db_path) as c:
            r = c.execute("SELECT id FROM optimizations WHERE project = ? AND status NOT IN (?, ?, ?)",
                          (project, FINISHED, FAILED, STOPPED)).fetchone()
        return self.get(r["id"]) if r else None

    def recover_orphans(self) -> int:
        with connect(self.db_path) as c:
            return c.execute("UPDATE optimizations SET status = ?, error = ?, finished_at = ? WHERE status IN (?, ?)",
                             (FAILED, "the server restarted during the optimisation", now_iso(), QUEUED,
                              RUNNING)).rowcount

    def event(self, oid: int, stage: str, kind: str, message: str, detail: Optional[dict] = None) -> None:
        text = json.dumps(detail or {}, default=str)
        if len(text) > 8000:
            text = json.dumps({"truncated": True, "preview": text[:8000]})
        with connect(self.db_path) as c:
            c.execute("INSERT INTO optimization_events (optimization_id, ts, stage, kind, message, detail_json) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (oid, now_iso(), stage, kind, message[:500], text))

    def events(self, oid: int, after: int = 0) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM optimization_events WHERE optimization_id = ? AND id > ? ORDER BY id",
                             (oid, after)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
            out.append(d)
        return out
