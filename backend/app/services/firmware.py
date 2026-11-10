"""Firmware projects and their source files: the thing the AI agents edit.

Every write goes through the same safety rules the build engine enforces (plain .c/.h names, size limits,
no '#include' of absolute or '..' paths), and writes are atomic, so a half-written file can never be built.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import List

from ..build.engine import (BAD_INCLUDE_RE, MAX_FILE_BYTES, MAX_FILES, SOURCE_NAME_RE, BuildStore, create_project,
                            hash_sources, list_projects, project_dir)
from ..deploy.engine import DeployConfig
from ..errors import ApiError


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FirmwareManager:
    def __init__(self, cfg: DeployConfig) -> None:
        self.cfg = cfg
        self.bcfg = cfg.build

    # ------------------------------------------------------------ helpers
    def _app_dir(self, project: str) -> Path:
        pdir = project_dir(self.bcfg, project)          # raises ProjectError (400) for a bad name
        app = pdir / "app"
        if not app.is_dir():
            raise ApiError(404, "not_found", f"no such project: {project!r}")
        return app

    @staticmethod
    def _check_filename(filename: str) -> None:
        if not SOURCE_NAME_RE.match(filename):
            raise ApiError(422, "bad_filename",
                           f"{filename!r} is not allowed: use a plain name (letters, digits, '_') ending in .c or .h")

    # ------------------------------------------------------------ projects
    def list_projects(self) -> List[dict]:
        store = BuildStore(self.cfg.db_path)
        out = []
        for name in list_projects(self.bcfg):
            app = self.bcfg.workspace_dir / name / "app"
            files = sorted(p for p in app.iterdir() if p.is_file())
            valid = [p for p in files if SOURCE_NAME_RE.match(p.name)]
            last = store.list(name, 1)
            out.append({
                "name": name,
                "files": [{"name": p.name, "size": p.stat().st_size} for p in files],
                "sources_sha256": hash_sources(valid) if valid else "",
                "last_build": ({"id": last[0]["id"], "status": last[0]["status"]} if last else None),
            })
        return out

    def create_project(self, name: str) -> dict:
        pdir = create_project(name, self.bcfg)           # ProjectError (400) if the name is bad or taken
        return {"name": name, "folder": str(pdir), "files": self.list_sources(name)}

    # ------------------------------------------------------------ sources
    def list_sources(self, project: str) -> List[dict]:
        app = self._app_dir(project)
        return [{"name": p.name, "size": p.stat().st_size, "sha256": _sha(p.read_bytes())}
                for p in sorted(app.iterdir()) if p.is_file()]

    def read_source(self, project: str, filename: str) -> dict:
        self._check_filename(filename)
        path = self._app_dir(project) / filename
        if not path.is_file():
            raise ApiError(404, "not_found", f"{project}/{filename} does not exist")
        data = path.read_bytes()
        return {"project": project, "name": filename, "size": len(data), "sha256": _sha(data),
                "content": data.decode("utf-8", errors="replace")}

    def write_source(self, project: str, filename: str, content: str) -> dict:
        self._check_filename(filename)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ApiError(422, "file_too_large", f"{len(data)} bytes is over the {MAX_FILE_BYTES}-byte limit per file")
        if "\x00" in content:
            raise ApiError(422, "bad_content", "the file contains a NUL character")
        if BAD_INCLUDE_RE.search(content):
            raise ApiError(422, "include_not_allowed", "#include of an absolute path or '..' is not allowed")
        app = self._app_dir(project)
        target = app / filename
        created = not target.exists()
        if created and sum(1 for p in app.iterdir() if p.is_file()) >= MAX_FILES:
            raise ApiError(422, "too_many_files", f"a project may have at most {MAX_FILES} source files")
        # write next to the app folder (not inside it, so a concurrent build never sees a half-written file),
        # then move it into place in one step
        tmp = app.parent / f".write_{uuid.uuid4().hex[:8]}.tmp"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"project": project, "name": filename, "size": len(data), "sha256": _sha(data), "created": created}

    def delete_source(self, project: str, filename: str) -> dict:
        self._check_filename(filename)
        path = self._app_dir(project) / filename
        if not path.is_file():
            raise ApiError(404, "not_found", f"{project}/{filename} does not exist")
        path.unlink()
        return {"project": project, "name": filename, "deleted": True}

    def restore_sources(self, project: str, build_id: int) -> dict:
        """Put the project's source files back exactly as they were when `build_id` was built."""
        row = BuildStore(self.cfg.db_path).get(build_id)
        if not row or not row.get("result"):
            raise ApiError(404, "not_found", f"no build #{build_id}")
        if row["result"]["project"] != project:
            raise ApiError(422, "wrong_project", f"build #{build_id} belongs to project "
                                                 f"'{row['result']['project']}', not '{project}'")
        snap = self.bcfg.artifacts_dir / str(build_id) / "app_src"
        if not snap.is_dir():
            raise ApiError(404, "not_found", f"build #{build_id} has no saved source snapshot")
        app = self._app_dir(project)
        for p in app.iterdir():                          # start from a clean folder so nothing stray remains
            if p.is_file():
                p.unlink()
        restored = []
        for p in sorted(snap.iterdir()):
            if p.is_file() and SOURCE_NAME_RE.match(p.name):
                shutil.copy2(p, app / p.name)
                restored.append(p.name)
        return {"project": project, "restored_from_build": build_id, "files": restored}