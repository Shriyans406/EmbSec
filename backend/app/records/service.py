"""Records service: take, save, verify and replay task records."""
from __future__ import annotations

import json
from pathlib import Path

from ..errors import ApiError
from .record import build_record, timeline
from .replay import run_rebuild, start_rebuild, verify
from .report import to_markdown, to_pdf


class RecordService:
    def __init__(self, services) -> None:
        self.s = services
        services.jobs.register("rebuild", lambda ctx: run_rebuild(services, ctx), "build")

    @property
    def dir(self) -> Path:
        return self.s.settings.root / "data" / "records"

    def take(self, kind: str, task_id: int) -> dict:
        return build_record(self.s.settings.db_path, kind, task_id, self.s.audit)

    def save(self, kind: str, task_id: int) -> dict:
        rec = self.take(kind, task_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{kind}-{task_id}-{rec['record_hash'][:12]}.json"
        path.write_text(json.dumps(rec, default=str, indent=1), encoding="utf-8")
        return {"path": str(path), "name": path.name, "record_hash": rec["record_hash"], "counts": rec["counts"]}

    def saved(self) -> list:
        if not self.dir.is_dir():
            return []
        return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(self.dir.glob("*.json"), reverse=True)]

    def load(self, name: str) -> dict:
        path = (self.dir / name).resolve()
        if path.parent != self.dir.resolve() or not path.is_file():
            raise ApiError(404, "not_found", f"no saved record {name!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def verify(self, kind: str, task_id: int) -> dict:
        return verify(self.take(kind, task_id), self.s)

    def verify_saved(self, name: str) -> dict:
        return verify(self.load(name), self.s)

    def replay(self, kind: str, task_id: int) -> dict:
        rec = self.take(kind, task_id)
        return {"kind": kind, "id": task_id, "window": rec["window"], "counts": rec["counts"],
                "events": timeline(rec)}

    def rebuild(self, build_id: int, who: str) -> dict:
        return start_rebuild(self.s, build_id, who)

    # ------------------------------------------------------------ reports
    def report(self, kind: str, task_id: int, fmt: str = "md"):
        """(content, media type, file name) of a verified report in json, md or pdf."""
        if fmt not in ("json", "md", "pdf"):
            raise ApiError(422, "bad_format", "format must be json, md or pdf")
        rec = self.take(kind, task_id)
        ver = verify(rec, self.s)
        name = f"{kind}-{task_id}"
        if fmt == "json":
            return json.dumps({**rec, "verification": ver}, default=str, indent=1), "application/json", f"{name}.json"
        md = to_markdown(rec, ver)
        if fmt == "md":
            return md, "text/markdown; charset=utf-8", f"{name}.md"
        return to_pdf(md, f"AEISP - {kind.replace('_', ' ')} {task_id}"), "application/pdf", f"{name}.pdf"

    def save_report(self, kind: str, task_id: int, fmt: str = "pdf") -> dict:
        content, _, name = self.report(kind, task_id, fmt)
        d = self.s.settings.root / "data" / "reports"
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return {"path": str(path), "bytes": path.stat().st_size}
