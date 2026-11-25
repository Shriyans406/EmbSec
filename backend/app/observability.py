"""Observability: metrics in the Prometheus text format, and a deep health check.

metrics(services)  counters and gauges read from the database and the services (no board I/O):
                   jobs, durations, model calls and tokens, agent runs, test checks, builds, deployments,
                   fuzz findings, knowledge, queue lengths, the audit chain, the board link
health(services)   every part the lab depends on, each with ok / warn / fail and a reason
"""
from __future__ import annotations

import shutil
from datetime import datetime
from typing import Dict, List

from .db import connect, schema_version


def _q(c, sql, args=()) -> list:
    try:
        return c.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Metrics:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.seen: set = set()

    def add(self, name: str, kind: str, help_: str, samples: Dict[tuple, float]) -> None:
        if name not in self.seen:
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
            self.seen.add(name)
        for labels, value in samples.items():
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels)
            self.lines.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _seconds(a, b):
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return None


def metrics(s) -> str:
    m = Metrics()
    with connect(s.settings.db_path) as c:
        jobs = _q(c, "SELECT kind, status, COUNT(*) AS n FROM jobs GROUP BY kind, status")
        m.add("aeisp_jobs_total", "counter", "Background jobs by kind and final or current status.",
              {(("kind", r["kind"]), ("status", r["status"])): r["n"] for r in jobs})
        dur: Dict[str, list] = {}
        for r in _q(c, "SELECT kind, started_at, finished_at FROM jobs WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"):
            d = _seconds(r["started_at"], r["finished_at"])
            if d is not None:
                dur.setdefault(r["kind"], []).append(d)
        m.add("aeisp_job_duration_seconds_sum", "counter", "Total run time of finished jobs.",
              {(("kind", k),): round(sum(v), 3) for k, v in dur.items()})
        m.add("aeisp_job_duration_seconds_count", "counter", "Finished jobs that have a run time.",
              {(("kind", k),): len(v) for k, v in dur.items()})
        m.add("aeisp_job_duration_seconds_max", "gauge", "Longest run time of a finished job.",
              {(("kind", k),): round(max(v), 3) for k, v in dur.items()})
        llm = _q(c, "SELECT ok, COUNT(*) AS n, COALESCE(SUM(total_tokens), 0) AS t, COALESCE(SUM(latency_ms), 0) AS l "
                    "FROM llm_calls GROUP BY ok")
        m.add("aeisp_llm_calls_total", "counter", "Model calls (ok=0: failed after retries).",
              {(("ok", r["ok"]),): r["n"] for r in llm})
        m.add("aeisp_llm_tokens_total", "counter", "Model tokens used (prompt + output).", {(): sum(r["t"] for r in llm)})
        m.add("aeisp_llm_latency_seconds_sum", "counter", "Time spent waiting for the model.",
              {(): round(sum(r["l"] for r in llm) / 1000, 3)})
        m.add("aeisp_agent_runs_total", "counter", "Agent runs by agent and status.",
              {(("agent", r["agent"]), ("status", r["status"])): r["n"]
               for r in _q(c, "SELECT agent, status, COUNT(*) AS n FROM agent_runs GROUP BY agent, status")})
        t = _q(c, "SELECT COALESCE(SUM(passed), 0) AS p, COALESCE(SUM(failed), 0) AS f FROM test_runs")
        m.add("aeisp_test_checks_total", "counter", "Checks run on the board.",
              {(("result", "passed"),): t[0]["p"] if t else 0, (("result", "failed"),): t[0]["f"] if t else 0})
        m.add("aeisp_builds_total", "counter", "Firmware builds by status.",
              {(("status", r["status"]),): r["n"] for r in _q(c, "SELECT status, COUNT(*) AS n FROM builds GROUP BY status")})
        m.add("aeisp_deployments_total", "counter", "Deployments and recoveries by status.",
              {(("kind", r["kind"]), ("status", r["status"])): r["n"]
               for r in _q(c, "SELECT kind, status, COUNT(*) AS n FROM deployments GROUP BY kind, status")})
        m.add("aeisp_fuzz_findings", "gauge", "Fuzz findings by severity and status.",
              {(("severity", r["severity"]), ("status", r["status"])): r["n"]
               for r in _q(c, "SELECT severity, status, COUNT(*) AS n FROM fuzz_findings GROUP BY severity, status")})
        m.add("aeisp_knowledge_entries", "gauge", "Entries in the knowledge base by kind.",
              {(("kind", r["kind"]),): r["n"] for r in _q(c, "SELECT kind, COUNT(*) AS n FROM knowledge GROUP BY kind")})
        m.add("aeisp_awaiting_approval", "gauge", "Proposals waiting for a human.",
              {(): (_q(c, "SELECT COUNT(*) AS n FROM jobs WHERE status = 'AWAITING_APPROVAL'") or [{"n": 0}])[0]["n"]})
        m.add("aeisp_audit_entries", "gauge", "Entries in the audit log.",
              {(): (_q(c, "SELECT COUNT(*) AS n FROM audit_log") or [{"n": 0}])[0]["n"]})
    m.add("aeisp_queue_length", "gauge", "Jobs waiting in each lane.",
          {(("lane", k),): v for k, v in s.jobs.queue_sizes().items()})
    m.add("aeisp_audit_chain_ok", "gauge", "1 if the audit hash chain is intact.", {(): int(s.audit.verify()["ok"])})
    link = getattr(s.device, "_link", None)
    m.add("aeisp_board_link_open", "gauge", "1 while the serial link to the board is open.",
          {(): int(bool(getattr(link, "is_connected", False)))})
    m.add("aeisp_board_busy", "gauge", "1 while a job holds the board.", {(): int(bool(s.device.owner))})
    m.add("aeisp_schema_version", "gauge", "Database schema version.", {(): schema_version(s.settings.db_path)})
    return m.text()


def health(s) -> dict:
    checks = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    try:
        with connect(s.settings.db_path) as c:
            c.execute("SELECT 1")
        add("database", "ok", str(s.settings.db_path))
    except Exception as e:  # noqa: BLE001
        add("database", "fail", f"cannot open the database: {e}")
    add("schema", "ok", f"version {schema_version(s.settings.db_path)}")
    chain = s.audit.verify()
    add("audit chain", "ok" if chain["ok"] else "fail",
        f"{chain['checked']} entries intact" if chain["ok"] else f"broken at #{chain['first_bad']}: {chain['reason']}")
    free = shutil.disk_usage(s.settings.root).free
    add("disk space", "ok" if free > 2e9 else ("warn" if free > 5e8 else "fail"), f"{free / 1e9:.1f} GB free")
    add("model", "ok" if s.settings.ai_configured else "warn",
        f"{s.settings.gemini_model} configured" if s.settings.ai_configured else "no GEMINI_API_KEY: the agents cannot run")
    tool = getattr(s.cfg.build, "size_exe", None)
    add("toolchain", "ok" if tool and tool.exists() else "warn",
        "ARM toolchain found" if tool and tool.exists() else "the ARM toolchain was not found: builds will fail")
    backup = s.cfg.stock_backup
    add("factory backup", "ok" if backup.is_file() else "warn",
        "present" if backup.is_file() else "missing: a failed deployment could not be undone")
    roles = sorted(s.settings.role_tokens)
    add("API tokens", "ok" if "operator" in roles else "fail", f"roles enabled: {', '.join(roles) or 'none'}")
    with connect(s.settings.db_path) as c:
        failed = _q(c, "SELECT COUNT(*) AS n FROM jobs WHERE status = 'FAILED' AND finished_at >= datetime('now', '-1 day')")
    n = failed[0]["n"] if failed else 0
    add("recent failures", "ok" if n < 5 else "warn", f"{n} failed jobs in the last 24 h")
    link = getattr(s.device, "_link", None)
    add("board", "ok" if getattr(link, "is_connected", False) else "warn",
        "serial link open" if getattr(link, "is_connected", False) else "no open link (connect on first use)")
    worst = "fail" if any(c["status"] == "fail" for c in checks) else ("warn" if any(c["status"] == "warn" for c in checks) else "ok")
    return {"status": worst, "checks": checks}
