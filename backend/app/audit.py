"""Append-only audit trail of who did what. Stored in the audit_log table. Writing never raises."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from .db import connect, now_iso
from .logging_setup import request_id_var

log = logging.getLogger("aeisp.audit")


class AuditLog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def write(self, actor: Optional[str], role: Optional[str], action: str, target: str = "",
              outcome: str = "", detail: str = "") -> None:
        try:
            with connect(self.db_path) as c:
                c.execute(
                    "INSERT INTO audit_log (ts, actor, role, action, target, outcome, detail, request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (now_iso(), actor or "-", role or "-", action, target, outcome, detail[:500], request_id_var.get()),
                )
        except Exception:  # noqa: BLE001  the audit trail must never break a request
            log.exception("could not write audit entry %s", action)

    def list(self, limit: int = 50, action: Optional[str] = None, actor: Optional[str] = None) -> List[dict]:
        sql = "SELECT * FROM audit_log"
        conds, args = [], []
        if action:
            conds.append("action LIKE ?")
            args.append(action + "%")
        if actor:
            conds.append("actor = ?")
            args.append(actor)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with connect(self.db_path) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]