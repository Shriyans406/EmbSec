"""Fuzz campaigns and findings (tables fuzz_campaigns / fuzz_findings)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from ..db import connect, now_iso
from ..errors import ApiError
from .oracle import RANK

FINDING_STATUSES = ["open", "reproduced", "flaky", "not_reproduced", "fixed", "wont_fix"]

_CAMPAIGN_FIELDS = {"status", "job_id", "build_id", "src_hash", "fw_version", "inputs_planned", "inputs_sent",
                    "anomalies", "resets", "findings", "stop_reason", "stop_requested", "summary_json", "started_at",
                    "finished_at"}
_FINDING_FIELDS = {"status", "minimized", "reproductions_json", "occurrences", "updated_at"}


class FuzzStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------ campaigns
    def create(self, project: Optional[str], seed: int, families: List[str], budget: dict, who: str) -> dict:
        with connect(self.db_path) as c:
            cur = c.execute("INSERT INTO fuzz_campaigns (status, project, seed, families_json, budget_json, "
                            "requested_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            ("QUEUED", project, seed, json.dumps(families), json.dumps(budget), who, now_iso()))
            cid = int(cur.lastrowid)
        return self.get(cid)

    def update(self, cid: int, **fields) -> None:
        bad = set(fields) - _CAMPAIGN_FIELDS
        if bad:
            raise ValueError(f"unknown campaign fields {bad}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with connect(self.db_path) as c:
            c.execute(f"UPDATE fuzz_campaigns SET {cols} WHERE id = ?", (*fields.values(), cid))

    @staticmethod
    def _campaign(r) -> dict:
        d = dict(r)
        d["families"] = json.loads(d.pop("families_json") or "[]")
        d["budget"] = json.loads(d.pop("budget_json") or "{}")
        d["summary"] = json.loads(d.pop("summary_json") or "null")
        d["stop_requested"] = bool(d["stop_requested"])
        return d

    def get(self, cid: int) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM fuzz_campaigns WHERE id = ?", (cid,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no fuzz campaign {cid}")
        return self._campaign(r)

    def list(self, limit: int = 20) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM fuzz_campaigns ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))
            return [self._campaign(r) for r in rows.fetchall()]

    def stop_requested(self, cid: int) -> bool:
        with connect(self.db_path) as c:
            r = c.execute("SELECT stop_requested FROM fuzz_campaigns WHERE id = ?", (cid,)).fetchone()
        return bool(r and r["stop_requested"])

    def recover_orphans(self) -> int:
        with connect(self.db_path) as c:
            return c.execute("UPDATE fuzz_campaigns SET status = 'FAILED', stop_reason = ?, finished_at = ? "
                             "WHERE status IN ('QUEUED', 'RUNNING')",
                             ("the server restarted during the campaign", now_iso())).rowcount

    # ------------------------------------------------------------ findings
    def record(self, campaign: dict, signature: str, verdict, obs, project: Optional[str]) -> tuple:
        """Store a finding, or count one more occurrence of it. Returns (finding id, new?)."""
        with connect(self.db_path) as c:
            r = c.execute("SELECT id FROM fuzz_findings WHERE campaign_id = ? AND signature = ?",
                          (campaign["id"], signature)).fetchone()
            if r:
                c.execute("UPDATE fuzz_findings SET occurrences = occurrences + 1, updated_at = ? WHERE id = ?",
                          (now_iso(), r["id"]))
                return int(r["id"]), False
            cur = c.execute(
                "INSERT INTO fuzz_findings (campaign_id, signature, kind, severity, cwe, detail, input_text, "
                "input_family, input_note, partial, sequence_json, reply, build_id, src_hash, project, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (campaign["id"], signature, verdict.kind, verdict.severity, verdict.cwe, verdict.detail, obs.text,
                 obs.family, obs.note, int(obs.partial), json.dumps(obs.sequence),
                 obs.reply, campaign.get("build_id"), campaign.get("src_hash"), project, now_iso(), now_iso()))
            return int(cur.lastrowid), True

    def update_finding(self, fid: int, **fields) -> None:
        bad = set(fields) - _FINDING_FIELDS
        if bad:
            raise ValueError(f"unknown finding fields {bad}")
        if "status" in fields and fields["status"] not in FINDING_STATUSES:
            raise ApiError(422, "bad_status", f"status must be one of {FINDING_STATUSES}")
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with connect(self.db_path) as c:
            c.execute(f"UPDATE fuzz_findings SET {cols} WHERE id = ?", (*fields.values(), fid))

    @staticmethod
    def _finding(r) -> dict:
        d = dict(r)
        d["sequence"] = json.loads(d.pop("sequence_json") or "[]")
        d["reproductions"] = json.loads(d.pop("reproductions_json") or "[]")
        d["partial"] = bool(d["partial"])
        return d

    def finding(self, fid: int) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM fuzz_findings WHERE id = ?", (fid,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no fuzz finding {fid}")
        return self._finding(r)

    def findings(self, campaign_id: Optional[int] = None, status: Optional[str] = None,
                 min_severity: Optional[str] = None, project: Optional[str] = None, limit: int = 100) -> List[dict]:
        sql, args, conds = "SELECT * FROM fuzz_findings", [], []
        if campaign_id is not None:
            conds.append("campaign_id = ?")
            args.append(campaign_id)
        if status:
            conds.append("status = ?")
            args.append(status)
        if project:
            conds.append("project = ?")
            args.append(project)
        if min_severity:
            keep = RANK[: RANK.index(min_severity) + 1]
            conds.append(f"severity IN ({','.join('?' * len(keep))})")
            args += keep
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with connect(self.db_path) as c:
            rows = [self._finding(r) for r in c.execute(sql, args).fetchall()]
        return sorted(rows, key=lambda f: (RANK.index(f["severity"]), -f["id"]))
