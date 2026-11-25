"""Append-only, tamper-evident audit trail of who did what (table audit_log). Writing never raises.

Every entry stores the SHA-256 of its own content together with the hash of the entry before it (a hash chain), so
changing, deleting or reordering any past entry breaks the chain from that point on - verify() finds the first
broken entry. Database triggers also refuse UPDATE and DELETE on entries that are already chained. The newest hash
(the head) can be written down elsewhere: if it ever changes for an old id, history was rewritten.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import List, Optional

from .db import connect, now_iso
from .logging_setup import request_id_var

log = logging.getLogger("aeisp.audit")

GENESIS = "0" * 64
FIELDS = ("ts", "actor", "role", "action", "target", "outcome", "detail", "request_id")
_write_lock = threading.Lock()


def entry_hash(prev: str, row: dict) -> str:
    payload = json.dumps([prev] + [row.get(k) for k in FIELDS], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.ensure_chain()

    # ------------------------------------------------------------ writing
    def write(self, actor: Optional[str], role: Optional[str], action: str, target: str = "",
              outcome: str = "", detail: str = "") -> None:
        row = {"ts": now_iso(), "actor": actor or "-", "role": role or "-", "action": action, "target": target,
               "outcome": outcome, "detail": (detail or "")[:500], "request_id": request_id_var.get()}
        try:
            with _write_lock:
                c = connect(self.db_path)
                c.isolation_level = None
                try:
                    c.execute("BEGIN IMMEDIATE")            # nobody else appends between reading the head and writing
                    last = c.execute("SELECT hash FROM audit_log WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
                    prev = last["hash"] if last else GENESIS
                    c.execute("INSERT INTO audit_log (ts, actor, role, action, target, outcome, detail, request_id, "
                              "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (*[row[k] for k in FIELDS], prev, entry_hash(prev, row)))
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
                finally:
                    c.close()
        except Exception:  # noqa: BLE001  the audit trail must never break a request
            log.exception("could not write audit entry %s", action)

    def ensure_chain(self) -> int:
        """Chain entries written before the chain existed (or by an old version). Returns how many were chained."""
        n = 0
        try:
            with _write_lock, connect(self.db_path) as c:
                rows = c.execute("SELECT * FROM audit_log WHERE hash IS NULL ORDER BY id").fetchall()
                if not rows:
                    return 0
                first = rows[0]["id"]
                last = c.execute("SELECT hash FROM audit_log WHERE hash IS NOT NULL AND id < ? ORDER BY id DESC "
                                 "LIMIT 1", (first,)).fetchone()
                prev = last["hash"] if last else GENESIS
                for r in rows:
                    h = entry_hash(prev, dict(r))
                    c.execute("UPDATE audit_log SET prev_hash = ?, hash = ? WHERE id = ? AND hash IS NULL",
                              (prev, h, r["id"]))
                    prev, n = h, n + 1
        except Exception:  # noqa: BLE001  (a database from before the chain columns existed)
            log.exception("could not chain the audit log")
        return n

    # ------------------------------------------------------------ checking
    def verify(self) -> dict:
        """Walk the whole chain. ok=False names the first entry whose content or link does not match."""
        prev, checked = GENESIS, 0
        with connect(self.db_path) as c:
            for r in c.execute("SELECT * FROM audit_log ORDER BY id"):
                d = dict(r)
                checked += 1
                if d["hash"] is None:
                    return {"ok": False, "checked": checked, "first_bad": d["id"], "reason": "entry is not chained"}
                if d["prev_hash"] != prev:
                    return {"ok": False, "checked": checked, "first_bad": d["id"],
                            "reason": "the link to the previous entry is broken (an entry was removed or reordered)"}
                if entry_hash(prev, d) != d["hash"]:
                    return {"ok": False, "checked": checked, "first_bad": d["id"],
                            "reason": "the entry's content does not match its hash (it was changed)"}
                prev = d["hash"]
        return {"ok": True, "checked": checked, "head": prev, "first_bad": None, "reason": ""}

    def head(self) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT id, ts, hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else {"id": 0, "ts": None, "hash": GENESIS}

    # ------------------------------------------------------------ reading
    def list(self, limit: int = 50, action: Optional[str] = None, actor: Optional[str] = None,
             after: Optional[str] = None, before: Optional[str] = None) -> List[dict]:
        sql = "SELECT * FROM audit_log"
        conds, args = [], []
        if action:
            conds.append("action LIKE ?")
            args.append(action + "%")
        if actor:
            conds.append("actor = ?")
            args.append(actor)
        if after:
            conds.append("ts >= ?")
            args.append(after)
        if before:
            conds.append("ts <= ?")
            args.append(before)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 5000)))
        with connect(self.db_path) as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
