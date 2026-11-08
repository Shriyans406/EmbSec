"""Background jobs: long operations (builds, deployments, tests) run on worker threads, not inside web requests.

Life of a job:
    submit(...)  ->  QUEUED  ->  RUNNING  ->  SUCCEEDED | FAILED
    submit(needs_approval=True) -> AWAITING_APPROVAL -> (approve) QUEUED ... | (reject) REJECTED | (cancel) CANCELLED
                                                     -> (too old) EXPIRED
Jobs are stored in SQLite, so they survive a restart; a job that was running when the server stopped is marked
FAILED on the next start. Each kind of job runs in a "lane" (a queue with its own worker threads): builds and
board operations never block each other, and board operations run one at a time.
"""
from __future__ import annotations

import hmac
import json
import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .db import connect, now_iso
from .errors import ApiError

log = logging.getLogger("aeisp.jobs")

AWAITING = "AWAITING_APPROVAL"
QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
FINAL = {SUCCEEDED, FAILED, CANCELLED, REJECTED, EXPIRED}

MAX_LOG_LINES = 5000


class JobFailed(Exception):
    """Raise from a runner when the job ran but did not achieve its goal. `result` is stored with the job."""

    def __init__(self, message: str, result: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.result = result


@dataclass
class JobContext:
    job_id: int
    params: dict
    log: Callable[[str], None]


Runner = Callable[[JobContext], Optional[dict]]


def _dump(obj) -> str:
    return json.dumps(obj, default=str)


class JobManager:
    def __init__(self, db_path: Path, lanes: Dict[str, int], proposal_ttl_s: int = 900) -> None:
        self.db_path = Path(db_path)
        self.lanes = dict(lanes)
        self.ttl = proposal_ttl_s
        self._runners: Dict[str, Tuple[Runner, str]] = {}
        self._queues: Dict[str, "queue.Queue[Optional[int]]"] = {lane: queue.Queue() for lane in lanes}
        self._threads: List[threading.Thread] = []
        self._started = False
        self._counts: Dict[int, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ setup
    def register(self, kind: str, runner: Runner, lane: str) -> None:
        if lane not in self.lanes:
            raise ValueError(f"unknown lane {lane!r}")
        self._runners[kind] = (runner, lane)

    def kinds(self) -> List[str]:
        return sorted(self._runners)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.recover_orphans()
        self.expire_stale()
        for lane, n in self.lanes.items():
            for i in range(max(1, n)):
                t = threading.Thread(target=self._worker, args=(lane,), name=f"job-{lane}-{i}", daemon=True)
                t.start()
                self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        for lane, n in self.lanes.items():
            for _ in range(max(1, n)):
                self._queues[lane].put(None)
        end = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, end - time.monotonic()))
        self._threads.clear()
        self._started = False

    def recover_orphans(self) -> int:
        """Jobs that were queued or running when the server stopped can never finish: mark them failed."""
        with connect(self.db_path) as c:
            cur = c.execute(
                "UPDATE jobs SET status=?, error=?, finished_at=? WHERE status IN (?, ?)",
                (FAILED, "the server restarted before this job finished", now_iso(), QUEUED, RUNNING))
            return cur.rowcount

    def expire_stale(self) -> int:
        with connect(self.db_path) as c:
            cur = c.execute("UPDATE jobs SET status=?, finished_at=?, error=? WHERE status=? AND expires_at < ?",
                            (EXPIRED, now_iso(), "nobody approved this proposal in time", AWAITING, now_iso()))
            return cur.rowcount

    # ------------------------------------------------------------ creating and deciding
    def submit(self, kind: str, params: dict, requested_by: str, summary: str = "",
               needs_approval: bool = False, confirm_token: Optional[str] = None) -> dict:
        if kind not in self._runners:
            raise ApiError(400, "unknown_job_kind", f"unknown job kind {kind!r}; known: {', '.join(self.kinds())}")
        lane = self._runners[kind][1]
        status = AWAITING if needs_approval else QUEUED
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat(timespec="seconds") \
            if needs_approval else None
        with connect(self.db_path) as c:
            cur = c.execute(
                "INSERT INTO jobs (kind, lane, status, summary, params_json, requested_by, confirm_token, "
                "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, lane, status, summary, _dump(params), requested_by, confirm_token, expires, now_iso()))
            job_id = int(cur.lastrowid)
        self._append_log(job_id, f"created by {requested_by}" + (" - waiting for approval" if needs_approval else ""))
        if not needs_approval:
            self._queues[lane].put(job_id)
        return self.get(job_id)  # type: ignore[return-value]

    def approve(self, job_id: int, approver: str, confirm: str) -> dict:
        self.expire_stale()
        job = self.get(job_id)
        if job is None:
            raise ApiError(404, "not_found", f"no job {job_id}")
        if job["status"] != AWAITING:
            raise ApiError(409, "not_awaiting_approval", f"job {job_id} is {job['status']}, not waiting for approval")
        if not hmac.compare_digest((confirm or "").strip().encode(), (job["confirm_token"] or "").encode()):
            raise ApiError(400, "confirmation_mismatch",
                           "the confirmation text does not match; the job was NOT approved")
        with connect(self.db_path) as c:
            cur = c.execute("UPDATE jobs SET status=?, approved_by=? WHERE id=? AND status=?",
                            (QUEUED, approver, job_id, AWAITING))
            if cur.rowcount != 1:
                raise ApiError(409, "not_awaiting_approval", "the job changed while approving; try again")
        self._append_log(job_id, f"approved by {approver}")
        self._queues[job["lane"]].put(job_id)
        return self.get(job_id)  # type: ignore[return-value]

    def reject(self, job_id: int, who: str, reason: str = "") -> dict:
        return self._close(job_id, who, REJECTED, f"rejected by {who}" + (f": {reason}" if reason else ""),
                           allowed=(AWAITING,))

    def cancel(self, job_id: int, who: str) -> dict:
        return self._close(job_id, who, CANCELLED, f"cancelled by {who}", allowed=(AWAITING, QUEUED))

    def _close(self, job_id: int, who: str, status: str, message: str, allowed: tuple) -> dict:
        job = self.get(job_id)
        if job is None:
            raise ApiError(404, "not_found", f"no job {job_id}")
        if job["status"] == RUNNING:
            raise ApiError(409, "job_running", f"job {job_id} is already running and cannot be stopped")
        if job["status"] not in allowed:
            raise ApiError(409, "job_not_open", f"job {job_id} is {job['status']}")
        with connect(self.db_path) as c:
            cur = c.execute(
                f"UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=? AND status IN ({','.join('?' * len(allowed))})",
                (status, message, now_iso(), job_id, *allowed))
            if cur.rowcount != 1:
                raise ApiError(409, "job_not_open", "the job changed meanwhile; look at it again")
        self._append_log(job_id, message)
        return self.get(job_id)  # type: ignore[return-value]

    # ------------------------------------------------------------ reading
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
        d.pop("result_json", None)
        return d

    def get(self, job_id: int) -> Optional[dict]:
        self.expire_stale()
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row(r) if r else None

    def list(self, limit: int = 30, status: Optional[str] = None, kind: Optional[str] = None) -> List[dict]:
        self.expire_stale()
        sql, args, conds = "SELECT * FROM jobs", [], []
        if status:
            conds.append("status = ?")
            args.append(status)
        if kind:
            conds.append("kind = ?")
            args.append(kind)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with connect(self.db_path) as c:
            return [self._row(r) for r in c.execute(sql, args).fetchall()]

    def logs(self, job_id: int, after: int = 0, limit: int = 500) -> dict:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT id, ts, line FROM job_logs WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?",
                             (job_id, after, max(1, min(limit, 2000)))).fetchall()
        lines = [{"n": r["id"], "ts": r["ts"], "text": r["line"]} for r in rows]
        return {"job_id": job_id, "lines": lines, "next_after": lines[-1]["n"] if lines else after}

    def queue_sizes(self) -> Dict[str, int]:
        return {lane: q.qsize() for lane, q in self._queues.items()}

    def wait(self, job_id: int, timeout: float = 30.0) -> dict:
        """Block until the job reaches a final state (used by tests and scripts)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self.get(job_id)
            if job and job["status"] in FINAL:
                return job
            time.sleep(0.05)
        raise TimeoutError(f"job {job_id} did not finish within {timeout} s")

    # ------------------------------------------------------------ running
    def _append_log(self, job_id: int, text: str) -> None:
        with self._lock:
            n = self._counts.get(job_id, 0) + 1
            self._counts[job_id] = n
        if n > MAX_LOG_LINES + 1:
            return
        if n == MAX_LOG_LINES + 1:
            text = f"... log limit of {MAX_LOG_LINES} lines reached; further lines are dropped"
        try:
            with connect(self.db_path) as c:
                c.execute("INSERT INTO job_logs (job_id, ts, line) VALUES (?, ?, ?)", (job_id, now_iso(), text))
        except Exception:  # noqa: BLE001
            log.exception("could not store a log line for job %s", job_id)

    def _worker(self, lane: str) -> None:
        q = self._queues[lane]
        while True:
            job_id = q.get()
            if job_id is None:
                return
            try:
                self._execute(job_id)
            except Exception:  # noqa: BLE001
                log.exception("job %s crashed the worker loop", job_id)

    def _execute(self, job_id: int) -> None:
        with connect(self.db_path) as c:
            cur = c.execute("UPDATE jobs SET status=?, started_at=? WHERE id=? AND status=?",
                            (RUNNING, now_iso(), job_id, QUEUED))
            if cur.rowcount != 1:
                return  # cancelled while waiting in the queue
            r = c.execute("SELECT kind, params_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        kind, params = r["kind"], json.loads(r["params_json"] or "{}")
        runner, _lane = self._runners[kind]
        self._append_log(job_id, "started")
        status, result, error = SUCCEEDED, None, None
        try:
            result = runner(JobContext(job_id, params, lambda text, _id=job_id: self._append_log(_id, str(text))))
        except JobFailed as e:
            status, result, error = FAILED, e.result, e.message
        except Exception as e:  # noqa: BLE001
            status, error = FAILED, f"{type(e).__name__}: {e}"
            log.error("job %s (%s) crashed:\n%s", job_id, kind, traceback.format_exc())
        with connect(self.db_path) as c:
            c.execute("UPDATE jobs SET status=?, result_json=?, error=?, finished_at=? WHERE id=?",
                      (status, _dump(result) if result is not None else None, error, now_iso(), job_id))
        self._append_log(job_id, f"finished: {status}" + (f" - {error}" if error else ""))