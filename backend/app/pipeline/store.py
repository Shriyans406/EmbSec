"""Pipelines and their event log (tables pipelines / pipeline_events)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..db import connect, now_iso

# status
QUEUED, RUNNING, WAITING, FINISHED, FAILED, STOPPED = ("QUEUED", "RUNNING", "WAITING_APPROVAL", "FINISHED", "FAILED",
                                                       "STOPPED")
FINAL = {FINISHED, FAILED, STOPPED}

# stages, in order
SPEC, CODE, BUILD, SCAN, PROPOSE, DEPLOYED, TEST, REPORT, DONE = (
    "spec", "code", "build", "scan", "propose", "deployed", "test", "report", "done")
STAGES = [SPEC, CODE, BUILD, SCAN, PROPOSE, DEPLOYED, TEST, REPORT, DONE]

_FIELDS = {"project", "status", "stage", "outcome", "spec_json", "build_id", "deploy_job_id", "test_run_id",
           "fix_rounds", "test_rounds", "state_json", "report_json", "error", "stop_requested", "finished_at"}


class PipelineStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def create(self, requirement: str, requested_by: str, params: dict) -> dict:
        now = now_iso()
        with connect(self.db_path) as c:
            cur = c.execute("INSERT INTO pipelines (requirement, status, stage, requested_by, params_json, state_json, "
                            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (requirement, QUEUED, SPEC, requested_by, json.dumps(params), "{}", now, now))
            pid = int(cur.lastrowid)
        return self.get(pid)  # type: ignore[return-value]

    def update(self, pid: int, **fields) -> None:
        bad = set(fields) - _FIELDS
        if bad:
            raise ValueError(f"unknown pipeline fields {bad}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with connect(self.db_path) as c:
            c.execute(f"UPDATE pipelines SET {cols} WHERE id = ?", (*fields.values(), pid))

    def set_state(self, pid: int, **values) -> dict:
        p = self.get(pid)
        state = {**p["state"], **values}
        self.update(pid, state_json=json.dumps(state, default=str))
        return state

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for key, default in (("spec", None), ("params", {}), ("state", {}), ("report", None)):
            raw = d.pop(f"{key}_json")
            d[key] = json.loads(raw) if raw else default
        d["stop_requested"] = bool(d["stop_requested"])
        return d

    def get(self, pid: int) -> Optional[dict]:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()
        return self._row(r) if r else None

    def list(self, limit: int = 20) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM pipelines ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [self._row(r) for r in rows]

    def recover_orphans(self) -> int:
        with connect(self.db_path) as c:
            cur = c.execute("UPDATE pipelines SET status = ?, error = ?, finished_at = ? WHERE status IN (?, ?)",
                            (FAILED, "the server restarted while this pipeline was running", now_iso(), QUEUED,
                             RUNNING))
            return cur.rowcount

    # ------------------------------------------------------------ events
    def event(self, pid: int, stage: str, kind: str, message: str, detail: Optional[dict] = None) -> None:
        text = json.dumps(detail or {}, default=str)
        if len(text) > 6000:
            text = json.dumps({"truncated": True, "preview": text[:6000]})
        with connect(self.db_path) as c:
            c.execute("INSERT INTO pipeline_events (pipeline_id, ts, stage, kind, message, detail_json) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (pid, now_iso(), stage, kind, message[:500], text))

    def events(self, pid: int, after: int = 0) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM pipeline_events WHERE pipeline_id = ? AND id > ? ORDER BY id",
                             (pid, after)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
            out.append(d)
        return out
