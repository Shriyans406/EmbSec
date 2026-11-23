"""SQLite access and schema migrations for the backend's own tables (jobs, audit, test runs).

The build and deployment tables are created by their own modules (app.build.engine, app.deploy.store);
they live in the same database file. Migrations here are numbered and applied once, in order.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

MIGRATIONS: List[Tuple[int, str, List[str]]] = [
    (1, "jobs", [
        """CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            lane TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT,
            params_json TEXT,
            requested_by TEXT,
            approved_by TEXT,
            confirm_token TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            result_json TEXT,
            error TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS job_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            line TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs (job_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)",
    ]),
    (2, "audit", [
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT,
            role TEXT,
            action TEXT NOT NULL,
            target TEXT,
            outcome TEXT,
            detail TEXT,
            request_id TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (id)",
    ]),
    (3, "test_runs", [
        """CREATE TABLE IF NOT EXISTS test_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            name TEXT NOT NULL,
            passed INTEGER NOT NULL,
            failed INTEGER NOT NULL,
            total INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            result_json TEXT
        )""",
    ]),
    (4, "llm_calls", [
        """CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            run_id INTEGER,
            purpose TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            output_tokens INTEGER,
            thought_tokens INTEGER,
            total_tokens INTEGER,
            latency_ms INTEGER,
            attempts INTEGER,
            ok INTEGER NOT NULL,
            error TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls (run_id)",
    ]),
    (5, "agent_runs", [
        """CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            outcome TEXT,
            requested_by TEXT,
            thread_id TEXT,
            parent_id INTEGER,
            pending_job_id INTEGER,
            steps INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL DEFAULT 0,
            max_steps INTEGER,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            params_json TEXT,
            prompts_json TEXT,
            answer TEXT,
            report_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            finished_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS agent_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT,
            ok INTEGER,
            detail_json TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps (run_id, id)",
    ]),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path), timeout=15)
    c.row_factory = sqlite3.Row
    return c


def schema_version(db_path: Path) -> int:
    with connect(db_path) as c:
        try:
            row = c.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row["v"] or 0)


def migrate(db_path: Path) -> int:
    """Create the database if needed and apply every migration not yet applied. Returns the schema version."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = connect(db_path)
    try:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
        done = {r["version"] for r in c.execute("SELECT version FROM schema_migrations")}
        for version, name, statements in MIGRATIONS:
            if version in done:
                continue
            with c:  # one transaction per migration
                for sql in statements:
                    c.execute(sql)
                c.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                          (version, name, now_iso()))
    finally:
        c.close()
    return schema_version(db_path)