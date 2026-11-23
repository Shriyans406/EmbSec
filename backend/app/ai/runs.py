"""Agent runs and their steps, stored in SQLite (tables agent_runs / agent_steps).

A run's life:
    QUEUED -> RUNNING -> FINISHED | FAILED | STOPPED
                      -> WAITING_APPROVAL (paused at a proposal) -> (resume) RUNNING -> ...
The conversation itself lives in the LangGraph checkpoint (data/agent_checkpoints.db) under thread_id;
this table is the summary that the API and the dashboard read.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..db import connect, now_iso

QUEUED = "QUEUED"
RUNNING = "RUNNING"
WAITING = "WAITING_APPROVAL"
FINISHED = "FINISHED"
FAILED = "FAILED"
STOPPED = "STOPPED"
FINAL = {FINISHED, FAILED, STOPPED}

MAX_DETAIL = 4000

_FIELDS = {"status", "outcome", "pending_job_id", "steps", "tokens", "stop_requested", "answer", "report_json",
           "error", "finished_at", "prompts_json", "thread_id"}


def _dump(obj) -> str:
    return json.dumps(obj, default=str)


class RunStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def create(self, agent: str, goal: str, requested_by: str, max_steps: int, params: Optional[dict] = None,
               parent_id: Optional[int] = None) -> dict:
        now = now_iso()
        with connect(self.db_path) as c:
            cur = c.execute(
                "INSERT INTO agent_runs (agent, goal, status, requested_by, parent_id, max_steps, params_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent, goal, QUEUED, requested_by, parent_id, max_steps, _dump(params or {}), now, now))
            run_id = int(cur.lastrowid)
            c.execute("UPDATE agent_runs SET thread_id = ? WHERE id = ?", (f"run-{run_id}", run_id))
        return self.get(run_id)  # type: ignore[return-value]

    def update(self, run_id: int, **fields) -> None:
        bad = set(fields) - _FIELDS
        if bad:
            raise ValueError(f"unknown run fields {bad}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with connect(self.db_path) as c:
            c.execute(f"UPDATE agent_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))

    def finish(self, run_id: int, status: str, **fields) -> None:
        self.update(run_id, status=status, finished_at=now_iso(), **fields)

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        d["prompts"] = json.loads(d.pop("prompts_json") or "[]")
        d["report"] = json.loads(d.pop("report_json")) if d.get("report_json") else None
        d.pop("report_json", None)
        d["stop_requested"] = bool(d["stop_requested"])
        return d

    def get(self, run_id: int) -> Optional[dict]:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, limit: int = 20, status: Optional[str] = None, parent_id: Optional[int] = None) -> List[dict]:
        sql, args, conds = "SELECT * FROM agent_runs", [], []
        if status:
            conds.append("status = ?")
            args.append(status)
        if parent_id is not None:
            conds.append("parent_id = ?")
            args.append(parent_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with connect(self.db_path) as c:
            return [self._row(r) for r in c.execute(sql, args).fetchall()]

    def stop_requested(self, run_id: int) -> bool:
        with connect(self.db_path) as c:
            r = c.execute("SELECT stop_requested FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return bool(r and r["stop_requested"])

    def recover_orphans(self) -> int:
        """Runs that were RUNNING or QUEUED when the server stopped cannot continue by themselves."""
        with connect(self.db_path) as c:
            cur = c.execute("UPDATE agent_runs SET status = ?, error = ?, finished_at = ? WHERE status IN (?, ?)",
                            (FAILED, "the server restarted while this run was active", now_iso(), QUEUED, RUNNING))
            return cur.rowcount

    # ------------------------------------------------------------ steps
    def add_step(self, run_id: int, kind: str, name: str = "", ok: Optional[bool] = None,
                 detail: Optional[dict] = None) -> None:
        text = _dump(detail or {})
        if len(text) > MAX_DETAIL:
            text = _dump({"truncated": True, "preview": text[:MAX_DETAIL]})
        with connect(self.db_path) as c:
            c.execute("INSERT INTO agent_steps (run_id, ts, kind, name, ok, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                      (run_id, now_iso(), kind, name, None if ok is None else int(ok), text))

    def steps(self, run_id: int, after: int = 0, limit: int = 500) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM agent_steps WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
                             (run_id, after, max(1, min(limit, 2000)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
            d["ok"] = None if d["ok"] is None else bool(d["ok"])
            out.append(d)
        return out
