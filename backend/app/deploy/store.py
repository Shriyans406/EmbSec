"""Deployment history (SQLite) and per-deployment log files."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeploymentStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS deployments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    image_label TEXT,
                    image_origin TEXT,
                    image_sha256 TEXT,
                    build_id INTEGER,
                    board_uid TEXT,
                    approved_by TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_s REAL,
                    message TEXT,
                    result_json TEXT
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS devices (
                    uid TEXT PRIMARY KEY,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_deployment_id INTEGER
                )"""
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    # ---- deployments ----
    def start(self, kind: str, image_label: str, image_origin: str, image_sha256: str,
              build_id: Optional[int], board_uid: str, approved_by: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO deployments (kind, status, image_label, image_origin, image_sha256, build_id,
                   board_uid, approved_by, started_at) VALUES (?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?)""",
                (kind, image_label, image_origin, image_sha256, build_id, board_uid, approved_by, now_iso()),
            )
            return int(cur.lastrowid)

    def finish(self, dep_id: int, status: str, message: str, duration_s: float, board_uid: str, result: dict) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE deployments SET status=?, message=?, finished_at=?, duration_s=?, board_uid=?,
                   result_json=? WHERE id=?""",
                (status, message, now_iso(), duration_s, board_uid, json.dumps(result), dep_id),
            )

    def list(self, limit: int = 20) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id, kind, status, image_label, build_id, board_uid, approved_by, started_at, duration_s
                   FROM deployments ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get(self, dep_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
        return d

    def successful_for_build(self, build_id: int) -> Optional[dict]:
        """The latest deployment of this build that finished SUCCESS (proven to run on the board)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM deployments WHERE build_id = ? AND kind = 'deploy' AND status = 'SUCCESS' "
                "ORDER BY id DESC LIMIT 1", (build_id,)).fetchone()
        return dict(row) if row else None

    # ---- devices ----
    def devices(self) -> List[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM devices ORDER BY first_seen").fetchall()]

    def register_device(self, uid: str, deployment_id: Optional[int]) -> bool:
        """Record a board. Returns True if it was new."""
        if not uid:
            return False
        with self._conn() as c:
            row = c.execute("SELECT uid FROM devices WHERE uid = ?", (uid,)).fetchone()
            if row:
                c.execute("UPDATE devices SET last_seen = ?, last_deployment_id = ? WHERE uid = ?",
                          (now_iso(), deployment_id, uid))
                return False
            c.execute("INSERT INTO devices (uid, first_seen, last_seen, last_deployment_id) VALUES (?, ?, ?, ?)",
                      (uid, now_iso(), now_iso(), deployment_id))
            return True


class DeployLog:
    """Collects timestamped lines, optionally echoing them (the CLI prints them as they happen)."""

    def __init__(self, echo: Optional[Callable[[str], None]] = None) -> None:
        self.lines: List[str] = []
        self.echo = echo

    def __call__(self, text: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {text}"
        self.lines.append(line)
        if self.echo:
            self.echo(line)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")