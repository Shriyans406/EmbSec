"""Stored test suites (tables test_suites / test_cases).

A suite is a named, ordered list of cases ("send this line, expect that reply"). Every change raises the suite's
revision, and every run records the revision it ran, so results can be compared fairly. Built-in suites (smoke) are
seeded from code and cannot be edited. Suites made by people or agents are checked: only safe protocol commands.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

from pydantic import ValidationError

from ..db import connect, now_iso
from ..errors import ApiError
from ..services.device import validate_command

SUITE_NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,47}$")
MAX_CASES = 100


def _check_cases(cases: Iterable[dict], builtin: bool = False) -> List[dict]:
    from ..services.testing import TestCase

    out, names = [], set()
    for i, c in enumerate(cases):
        try:
            tc = TestCase.model_validate(c)
        except ValidationError as e:
            raise ApiError(422, "bad_case", f"case {i + 1} is not valid",
                           [{"where": ".".join(str(x) for x in err["loc"]), "problem": err["msg"]} for err in e.errors()])
        if tc.name in names:
            raise ApiError(422, "duplicate_case", f"two cases are called {tc.name!r}")
        names.add(tc.name)
        if not builtin:
            validate_command(tc.command, operator=False)
        d = tc.model_dump(exclude_none=True)
        d["expect"] = {k: v for k, v in d["expect"].items() if v is not None}
        d["tags"] = [t for t in (c.get("tags") or []) if isinstance(t, str)][:8]
        out.append(d)
    return out


class SuiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------ reading
    def _suite_row(self, c, key) -> Optional[dict]:
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            r = c.execute("SELECT * FROM test_suites WHERE id = ?", (int(key),)).fetchone()
        else:
            r = c.execute("SELECT * FROM test_suites WHERE name = ?", (key,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def _shape(s: dict) -> dict:
        s["tags"] = json.loads(s["tags"] or "[]")
        s["builtin"] = bool(s["builtin"])
        return s

    def get(self, key, enabled_only: bool = False) -> dict:
        with connect(self.db_path) as c:
            s = self._suite_row(c, key)
            if s is None:
                raise ApiError(404, "not_found", f"no test suite {key!r}")
            sql = "SELECT * FROM test_cases WHERE suite_id = ?" + (" AND enabled = 1" if enabled_only else "")
            rows = c.execute(sql + " ORDER BY position, id", (s["id"],)).fetchall()
        s = self._shape(s)
        s["cases"] = [{"id": r["id"], "name": r["name"], "command": r["command"],
                       "expect": json.loads(r["expect_json"]), "tags": json.loads(r["tags"] or "[]"),
                       "enabled": bool(r["enabled"])} for r in rows]
        return s

    def list(self, project: Optional[str] = None) -> List[dict]:
        sql = ("SELECT s.*, (SELECT COUNT(*) FROM test_cases c WHERE c.suite_id = s.id) AS cases, "
               "(SELECT COUNT(*) FROM test_cases c WHERE c.suite_id = s.id AND c.enabled = 1) AS enabled_cases "
               "FROM test_suites s")
        args: tuple = ()
        if project:
            sql += " WHERE s.project = ?"
            args = (project,)
        with connect(self.db_path) as c:
            return [self._shape(dict(r)) for r in c.execute(sql + " ORDER BY s.name", args).fetchall()]

    # ------------------------------------------------------------ writing
    def create(self, name: str, cases: List[dict], created_by: str, description: str = "",
               project: Optional[str] = None, tags: Optional[List[str]] = None, source: str = "manual",
               builtin: bool = False, replace: bool = False) -> dict:
        if not SUITE_NAME_RE.match(name):
            raise ApiError(422, "bad_suite_name", f"{name!r}: use lower-case letters, digits, '_' or '-' (2..48)")
        checked = _check_cases(cases, builtin)
        if not checked:
            raise ApiError(422, "bad_case", "a suite needs at least one case")
        if len(checked) > MAX_CASES:
            raise ApiError(422, "too_many_cases", f"a suite may have at most {MAX_CASES} cases")
        now = now_iso()
        with connect(self.db_path) as c:
            old = self._suite_row(c, name)
            if old and not replace:
                raise ApiError(409, "suite_exists", f"a suite called {name!r} exists already")
            if old and old["builtin"] and not builtin:
                raise ApiError(403, "builtin_suite", f"{name!r} is built in and cannot be changed")
            if old:
                c.execute("DELETE FROM test_cases WHERE suite_id = ?", (old["id"],))
                c.execute("UPDATE test_suites SET description=?, project=?, tags=?, source=?, revision=revision+1, "
                          "updated_at=? WHERE id=?", (description, project, json.dumps(tags or []), source, now,
                                                      old["id"]))
                sid = old["id"]
            else:
                cur = c.execute("INSERT INTO test_suites (name, description, project, tags, builtin, source, "
                                "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (name, description, project, json.dumps(tags or []), int(builtin), source, created_by,
                                 now, now))
                sid = int(cur.lastrowid)
            self._insert_cases(c, sid, checked, 0, now)
        return self.get(sid)

    def sync(self, name: str, cases: List[dict], created_by: str, description: str = "",
             project: Optional[str] = None, source: str = "spec") -> dict:
        """Create the suite, or replace its cases only if they really changed (so the revision - and with it the
        fairness of regression comparisons - stays the same when nothing changed)."""
        want = _check_cases(cases)
        try:
            cur = self.get(name)
        except ApiError:
            cur = None
        if cur and [(c["name"], c["command"], c["expect"]) for c in cur["cases"]] == \
                [(c["name"], c["command"], c["expect"]) for c in want]:
            return cur
        return self.create(name, cases, created_by, description, project, [source], source, replace=True)

    @staticmethod
    def _insert_cases(c, sid: int, cases: List[dict], start: int, now: str) -> None:
        for i, tc in enumerate(cases):
            c.execute("INSERT INTO test_cases (suite_id, position, name, command, expect_json, tags, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (sid, start + i, tc["name"], tc["command"], json.dumps(tc["expect"]), json.dumps(tc["tags"]),
                       now))

    def _editable(self, c, key) -> dict:
        s = self._suite_row(c, key)
        if s is None:
            raise ApiError(404, "not_found", f"no test suite {key!r}")
        if s["builtin"]:
            raise ApiError(403, "builtin_suite", f"{s['name']!r} is built in and cannot be changed")
        return s

    def add_cases(self, key, cases: List[dict]) -> dict:
        checked = _check_cases(cases)
        with connect(self.db_path) as c:
            s = self._editable(c, key)
            have = {r["name"] for r in c.execute("SELECT name FROM test_cases WHERE suite_id = ?", (s["id"],))}
            clash = [tc["name"] for tc in checked if tc["name"] in have]
            if clash:
                raise ApiError(409, "duplicate_case", f"the suite already has cases called {clash}")
            n = c.execute("SELECT COUNT(*) FROM test_cases WHERE suite_id = ?", (s["id"],)).fetchone()[0]
            if n + len(checked) > MAX_CASES:
                raise ApiError(422, "too_many_cases", f"a suite may have at most {MAX_CASES} cases")
            pos = c.execute("SELECT COALESCE(MAX(position), -1) FROM test_cases WHERE suite_id = ?",
                            (s["id"],)).fetchone()[0] + 1
            self._insert_cases(c, s["id"], checked, pos, now_iso())
            c.execute("UPDATE test_suites SET revision = revision + 1, updated_at = ? WHERE id = ?",
                      (now_iso(), s["id"]))
        return self.get(s["id"])

    def set_case(self, key, case_name: str, enabled: Optional[bool] = None, remove: bool = False) -> dict:
        with connect(self.db_path) as c:
            s = self._editable(c, key)
            if remove:
                cur = c.execute("DELETE FROM test_cases WHERE suite_id = ? AND name = ?", (s["id"], case_name))
            else:
                cur = c.execute("UPDATE test_cases SET enabled = ? WHERE suite_id = ? AND name = ?",
                                (int(bool(enabled)), s["id"], case_name))
            if cur.rowcount != 1:
                raise ApiError(404, "not_found", f"suite {s['name']!r} has no case {case_name!r}")
            c.execute("UPDATE test_suites SET revision = revision + 1, updated_at = ? WHERE id = ?",
                      (now_iso(), s["id"]))
        return self.get(s["id"])

    def delete(self, key) -> dict:
        with connect(self.db_path) as c:
            s = self._editable(c, key)
            c.execute("DELETE FROM test_cases WHERE suite_id = ?", (s["id"],))
            c.execute("DELETE FROM test_suites WHERE id = ?", (s["id"],))
        return {"deleted": s["name"]}

    def seed_builtin(self, suites: dict) -> None:
        """Make sure the built-in suites exist and match the code (their revision rises when the code changes)."""
        for name, s in suites.items():
            try:
                cur = self.get(name)
            except ApiError:
                cur = None
            want = _check_cases(s["cases"], builtin=True)
            if cur and [(c["name"], c["command"], c["expect"]) for c in cur["cases"]] == \
                    [(c["name"], c["command"], c["expect"]) for c in want]:
                continue
            self.create(name, s["cases"], "builtin", s["description"], tags=["builtin"], source="builtin",
                        builtin=True, replace=True)
