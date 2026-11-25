"""The knowledge store: one table of entries plus an FTS5 index kept in sync by triggers.

Every entry says what it is (kind), where it came from (source_type + source_id: a fuzz finding, a remediation, a
benchmark run ...), which project and which firmware (build_id, src_hash). Entries made from records are upserted,
so collecting twice never duplicates anything.

search()   full-text search (porter stemming: "parsing" finds "parser"), ranked by BM25, with snippets
related()  the entries most relevant to a piece of text (a new task's goal), for the agents' context
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

from ..db import connect, now_iso
from ..errors import ApiError

KINDS = ["bug", "fix", "benchmark", "optimisation", "decision", "build_failure", "test_failure", "spec", "note"]

STOPWORDS = set("""a an and are as at be but by can do does for from has have how i if in into is it its make
must not of on or our should so that the their then there these this to use was we were when which while will with
you your all any each every one two new please firmware project app board command commands code write build test
""".split())


def _clean_query(text: str, limit: int = 24) -> str:
    """Turn free text into a safe FTS5 query: words OR-ed together (no operators from the user reach FTS5)."""
    words, seen = [], set()
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or ""):
        lw = w.lower()
        if lw in STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        words.append(f'"{lw}"')
        if len(words) >= limit:
            break
    return " OR ".join(words)


class KnowledgeStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    # ------------------------------------------------------------ writing
    def upsert(self, kind: str, title: str, body: str, *, project: Optional[str] = None,
               tags: Iterable[str] = (), source_type: Optional[str] = None, source_id: Optional[int] = None,
               build_id: Optional[int] = None, src_hash: Optional[str] = None, importance: int = 2,
               created_by: str = "collector") -> dict:
        if kind not in KINDS:
            raise ApiError(422, "bad_kind", f"kind must be one of {KINDS}")
        if not title.strip() or not body.strip():
            raise ApiError(422, "empty_entry", "an entry needs a title and a body")
        tag_text = " ".join(sorted({t.strip().lower() for t in tags if t and t.strip()}))[:300]
        now = now_iso()
        with connect(self.db_path) as c:
            old = None
            if source_type and source_id is not None:
                old = c.execute("SELECT id FROM knowledge WHERE source_type = ? AND source_id = ? AND kind = ?",
                                (source_type, source_id, kind)).fetchone()
            if old:
                c.execute("UPDATE knowledge SET title=?, body=?, project=?, tags=?, build_id=?, src_hash=?, "
                          "importance=?, updated_at=? WHERE id=?",
                          (title[:200], body[:8000], project, tag_text, build_id, src_hash, importance, now, old["id"]))
                kid = old["id"]
            else:
                cur = c.execute("INSERT INTO knowledge (kind, title, body, project, tags, source_type, source_id, "
                                "build_id, src_hash, importance, created_by, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (kind, title[:200], body[:8000], project, tag_text, source_type, source_id, build_id,
                                 src_hash, importance, created_by, now, now))
                kid = int(cur.lastrowid)
        return self.get(kid)

    def delete(self, kid: int) -> dict:
        self.get(kid)
        with connect(self.db_path) as c:
            c.execute("DELETE FROM knowledge WHERE id = ?", (kid,))
        return {"deleted": kid}

    # ------------------------------------------------------------ reading
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["tags"] = (d.get("tags") or "").split()
        return d

    def get(self, kid: int) -> dict:
        with connect(self.db_path) as c:
            r = c.execute("SELECT * FROM knowledge WHERE id = ?", (kid,)).fetchone()
        if not r:
            raise ApiError(404, "not_found", f"no knowledge entry {kid}")
        return self._row(r)

    def list(self, kind: Optional[str] = None, project: Optional[str] = None, limit: int = 50) -> List[dict]:
        sql, args, conds = "SELECT * FROM knowledge", [], []
        if kind:
            conds.append("kind = ?")
            args.append(kind)
        if project:
            conds.append("project = ?")
            args.append(project)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        with connect(self.db_path) as c:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
        return [self._row(r) for r in rows]

    def search(self, text: str, kind: Optional[str] = None, project: Optional[str] = None,
               limit: int = 10) -> List[dict]:
        q = _clean_query(text)
        if not q:
            return []
        sql = ("SELECT k.*, bm25(knowledge_fts, 4.0, 1.0, 2.0, 0.5) AS score, "
               "snippet(knowledge_fts, 1, '[', ']', ' ... ', 12) AS snippet "
               "FROM knowledge_fts JOIN knowledge k ON k.id = knowledge_fts.rowid WHERE knowledge_fts MATCH ?")
        args: list = [q]
        if kind:
            sql += " AND k.kind = ?"
            args.append(kind)
        if project:
            sql += " AND k.project = ?"
            args.append(project)
        sql += " ORDER BY score LIMIT ?"
        args.append(max(1, min(limit, 100)))
        with connect(self.db_path) as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = self._row(r)
            d["score"] = round(-d["score"], 3)          # bm25 is lower-is-better; show higher-is-better
            out.append(d)
        return out

    def related(self, text: str, project: Optional[str] = None, limit: int = 5,
                kinds: Iterable[str] = ("bug", "fix", "decision", "optimisation", "build_failure", "test_failure",
                                        "benchmark", "note")) -> List[dict]:
        """The most relevant past entries for a task: the same project first, then the whole lab; important ones
        win ties. Specs are left out (the agent gets the spec itself)."""
        kinds = set(kinds)
        hits = [h for h in self.search(text, limit=40) if h["kind"] in kinds]
        for h in hits:
            h["score"] += (1.5 if project and h["project"] == project else 0) + 0.3 * h["importance"]
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    def stats(self) -> dict:
        with connect(self.db_path) as c:
            by_kind = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) AS n FROM knowledge GROUP BY kind")}
            by_project = {r["project"] or "-": r["n"] for r in
                          c.execute("SELECT project, COUNT(*) AS n FROM knowledge GROUP BY project")}
        return {"total": sum(by_kind.values()), "by_kind": by_kind, "by_project": by_project}


def as_context(entries: List[dict], max_chars: int = 2500) -> str:
    """Past entries as a short text block for an agent's task."""
    if not entries:
        return ""
    lines = ["Lessons from earlier work in this lab (from the knowledge base; check them, they may not apply):"]
    for e in entries:
        where = f"{e['project'] or 'lab'}" + (f", build #{e['build_id']}" if e.get("build_id") else "")
        body = re.sub(r"\s+", " ", e["body"]).strip()
        lines.append(f"- [{e['kind']} #{e['id']}, {where}] {e['title']}: {body[:350]}")
    text = "\n".join(lines)
    return text[:max_chars]


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
