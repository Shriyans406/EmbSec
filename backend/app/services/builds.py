"""Build service: starts builds as background jobs and reads the build history."""
from __future__ import annotations

from typing import List, Optional

from ..build.diagnostics import Diagnostic
from ..build.engine import SUCCESS, BuildStore, build_project
from ..deploy.engine import DeployConfig
from ..errors import ApiError
from ..jobs import JobContext, JobFailed, JobManager
from .firmware import FirmwareManager

MAX_DIAGNOSTICS = 50


def compact_result(result: dict) -> dict:
    """A build result without huge lists: at most MAX_DIAGNOSTICS diagnostics (the full log is always on disk)."""
    r = dict(result)
    diags = list(r.get("diagnostics") or [])
    r["diagnostics_total"] = len(diags)
    r["diagnostics"] = diags[:MAX_DIAGNOSTICS]
    return r


class BuildService:
    def __init__(self, cfg: DeployConfig, jobs: JobManager, firmware: FirmwareManager) -> None:
        self.cfg = cfg
        self.jobs = jobs
        self.firmware = firmware
        jobs.register("build", self._run_build, "build")

    # ------------------------------------------------------------ starting
    def start(self, project: str, requested_by: str, timeout_s: Optional[int] = None) -> dict:
        self.firmware._app_dir(project)  # fail fast (404 / 400) instead of creating a doomed job
        return self.jobs.submit("build", {"project": project, "timeout_s": timeout_s}, requested_by,
                                summary=f"build project '{project}'")

    def _run_build(self, ctx: JobContext) -> dict:
        res = build_project(ctx.params["project"], timeout_s=ctx.params.get("timeout_s"), cfg=self.cfg.build,
                            on_stage=ctx.log)
        out = compact_result(res.to_dict())
        for d in out["diagnostics"][:20]:
            ctx.log(Diagnostic(**d).format())
        ctx.log(f"build #{res.id}: {res.status} - {res.message}")
        if res.status != SUCCESS:
            raise JobFailed(f"build #{res.id} {res.status}: {res.message}", out)
        return out

    # ------------------------------------------------------------ reading
    def history(self, project: Optional[str] = None, limit: int = 20) -> List[dict]:
        return BuildStore(self.cfg.db_path).list(project, max(1, min(limit, 100)))

    def get(self, build_id: int) -> dict:
        row = BuildStore(self.cfg.db_path).get(build_id)
        if not row or not row.get("result"):
            raise ApiError(404, "not_found", f"no build #{build_id}")
        return compact_result(row["result"])

    def log_tail(self, build_id: int, tail: int = 200) -> List[str]:
        path = self.cfg.build.artifacts_dir / str(build_id) / "build.log"
        if not path.is_file():
            raise ApiError(404, "not_found", f"build #{build_id} has no log")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max(1, min(tail, 2000)):]