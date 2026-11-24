"""Remediations and their event log (tables remediations / remediation_events)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..db import connect, now_iso
from ..errors import ApiError

QUEUED, RUNNING, WAITING, FINISHED, FAILED, STOPPED = ("QUEUED", "RUNNING", "WAITING_APPROVAL", "FINISHED", "FAILED",
                                                       "STOPPED")
FINAL = {FINISHED, FAILED, STOPPED}

# stages, in order
(REPRODUCE, INSPECT, FIX, BUILD, SCAN, PROPOSE, DEPLOYED, RETEST, REGRESSION, DECIDE, ROLLBACK, ROLLED_BACK, REPORT,
 DONE) = ("reproduce", "inspect", "fix", "build", "scan", "propose", "deployed", "retest", "regression", "decide",
          "rollback", "rolled_back", "report", "done")

_FIELDS = {"project", "status", "stage", "outcome", "before_build", "after_build", "deploy_job_id", "rollback_job_id",
           "fix_rounds", "state_json", "diff_text", "root_cause", "report_json", "error", "stop_requested",
           "finished_at"}


class RemediationStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def create(self, finding_id: int, project: Optional[str], who: str, params: dict) -> dict:
        now = now_iso()
        with connect(self.db_path) as c:
            cur = c.execute("INSERT INTO remediations (finding_id, project, status, stage, requested_by, params_json, "
                            "state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (finding_id, project, QUEUED, REPRODUCE, who, json.dumps(params), "{}", now, now))
            rid = int(cur.lastrowid)
        return self.get(rid)

    def update(self, rid: int, **fields) -> None:
        bad = set(fields) - _FIELDS
        if bad:
            raise ValueError(f"unknown remediation fields {bad}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with connect(self.db_path) as c:
            c.execute(f"UPDATE remediations SET {cols} WHERE id = ?", (*fields.values(), rid))

    def set_state(self, rid: int, **values) -> dict:
        state = {**self.get(rid)["state"], **values}
        self.update(rid, state_json=json.dumps(state, default=str))
        return state

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for key, default in (("params", {}), ("state", {}), ("report", None)):
            raw = d.pop(f"{key}_json")
            d[key] = json.loads(raw) if raw else default
        d["stop_requested"] = bool(d["stop_requested"])
        return d

    def get(self, rid: int) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM remediations WHERE id = ?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no remediation {rid}")
        return self._row(r)

    def list(self, limit: int = 20, finding_id: Optional[int] = None) -> List[dict]:
        sql, args = "SELECT * FROM remediations", []
        if finding_id is not None:
            sql += " WHERE finding_id = ?"
            args.append(finding_id)
        with connect(self.db_path) as c:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 200)))).fetchall()
        return [self._row(r) for r in rows]

    def active_for(self, finding_id: int) -> Optional[dict]:
        with connect(self.db_path) as c:
            r = c.execute("SELECT id FROM remediations WHERE finding_id = ? AND status NOT IN (?, ?, ?)",
                          (finding_id, FINISHED, FAILED, STOPPED)).fetchone()
        return self.get(r["id"]) if r else None

    def recover_orphans(self) -> int:
        with connect(self.db_path) as c:
            return c.execute("UPDATE remediations SET status = ?, error = ?, finished_at = ? WHERE status IN (?, ?)",
                             (FAILED, "the server restarted during the remediation", now_iso(), QUEUED,
                              RUNNING)).rowcount

    # ------------------------------------------------------------ events
    def event(self, rid: int, stage: str, kind: str, message: str, detail: Optional[dict] = None) -> None:
        text = json.dumps(detail or {}, default=str)
        if len(text) > 8000:
            text = json.dumps({"truncated": True, "preview": text[:8000]})
        with connect(self.db_path) as c:
            c.execute("INSERT INTO remediation_events (remediation_id, ts, stage, kind, message, detail_json) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (rid, now_iso(), stage, kind, message[:500], text))

    def events(self, rid: int, after: int = 0) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM remediation_events WHERE remediation_id = ? AND id > ? ORDER BY id",
                             (rid, after)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
            out.append(d)
        return out
