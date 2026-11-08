"""System, jobs, audit and log endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..db import schema_version
from ..errors import ApiError
from ..security import Principal, require
from ..settings import VERSION
from .deps import get_services

router = APIRouter()

PHASE = 5


class ApproveBody(BaseModel):
    confirm: str = Field(min_length=1, max_length=64, description="the confirmation text shown with the proposal")


class RejectBody(BaseModel):
    reason: str = Field("", max_length=200)


class SelfTestBody(BaseModel):
    seconds: int = Field(3, ge=0, le=60)
    require_approval: bool = Field(False, description="make the job wait for an operator to approve it")
    fail: bool = Field(False, description="make the job end in FAILED, to see how failures look")


# ------------------------------------------------------------------ system
@router.get("/health", tags=["system"])
def health(request: Request) -> dict:
    """Public. Says the server is up and whether API tokens are configured."""
    return {"status": "ok", "phase": PHASE, "version": VERSION,
            "auth_configured": bool(request.app.state.settings.role_tokens)}


@router.get("/system/info", tags=["system"])
def system_info(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    s = get_services(request)
    return {
        "version": VERSION,
        "schema_version": schema_version(s.settings.db_path),
        "job_kinds": s.jobs.kinds(),
        "queues": s.jobs.queue_sizes(),
        "board_busy": s.device.owner,
        "you": {"name": p.name, "role": p.role},
    }


@router.get("/system/config", tags=["system"])
def system_config(request: Request, p: Principal = Depends(require("operator"))) -> dict:
    return request.app.state.settings.public_dict()


# ------------------------------------------------------------------ jobs
@router.get("/jobs", tags=["jobs"])
def list_jobs(request: Request, status: Optional[str] = None, kind: Optional[str] = None,
              limit: int = Query(30, ge=1, le=200), p: Principal = Depends(require("viewer"))) -> list:
    return get_services(request).jobs.list(limit, status, kind)


@router.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    job = get_services(request).jobs.get(job_id)
    if job is None:
        raise ApiError(404, "not_found", f"no job {job_id}")
    return job


@router.get("/jobs/{job_id}/logs", tags=["jobs"])
def job_logs(job_id: int, request: Request, after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000),
             p: Principal = Depends(require("viewer"))) -> dict:
    s = get_services(request)
    if s.jobs.get(job_id) is None:
        raise ApiError(404, "not_found", f"no job {job_id}")
    return s.jobs.logs(job_id, after, limit)


@router.post("/jobs/{job_id}/approve", tags=["jobs"])
def approve_job(job_id: int, body: ApproveBody, request: Request, p: Principal = Depends(require("operator"))) -> dict:
    s = get_services(request)
    try:
        job = s.jobs.approve(job_id, f"api:{p.name}", body.confirm)
    except ApiError as e:
        s.audit.write(p.name, p.role, "job.approve", f"job:{job_id}", f"refused:{e.code}", e.message)
        raise
    s.audit.write(p.name, p.role, "job.approve", f"job:{job_id}", "ok", job.get("summary") or "")
    return job


@router.post("/jobs/{job_id}/reject", tags=["jobs"])
def reject_job(job_id: int, body: RejectBody, request: Request, p: Principal = Depends(require("operator"))) -> dict:
    s = get_services(request)
    job = s.jobs.reject(job_id, f"api:{p.name}", body.reason)
    s.audit.write(p.name, p.role, "job.reject", f"job:{job_id}", "ok", body.reason)
    return job


@router.post("/jobs/{job_id}/cancel", tags=["jobs"])
def cancel_job(job_id: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    job = s.jobs.cancel(job_id, f"api:{p.name}")
    s.audit.write(p.name, p.role, "job.cancel", f"job:{job_id}", "ok")
    return job


@router.post("/selftest", tags=["jobs"], status_code=202)
def selftest(body: SelfTestBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """A harmless job for trying out the job system, approvals and logs. It does not touch the board."""
    s = get_services(request)
    job = s.jobs.submit("selftest", {"seconds": body.seconds, "fail": body.fail}, f"api:{p.name}",
                        summary=f"self-test, {body.seconds} s", needs_approval=body.require_approval,
                        confirm_token="SELFTEST" if body.require_approval else None)
    s.audit.write(p.name, p.role, "selftest.create", f"job:{job['id']}", "ok")
    return job


# ------------------------------------------------------------------ audit and logs
@router.get("/audit", tags=["system"])
def audit(request: Request, limit: int = Query(50, ge=1, le=500), action: Optional[str] = None,
          actor: Optional[str] = None, p: Principal = Depends(require("operator"))) -> list:
    return get_services(request).audit.list(limit, action, actor)


LOG_FILES = {"backend": "backend.log", "serial": "serial.jsonl"}


def tail_file(path: Path, lines: int) -> list:
    """Last `lines` lines of a text file, reading only the end of it."""
    if not path.is_file():
        return []
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - 512 * 1024))
        data = f.read()
    text = data.decode("utf-8", errors="replace").splitlines()
    return text[-lines:]


@router.get("/logs/{name}", tags=["system"])
def get_log(name: str, request: Request, tail: int = Query(100, ge=1, le=2000),
            p: Principal = Depends(require("viewer"))) -> dict:
    if name not in LOG_FILES:
        raise ApiError(404, "not_found", f"unknown log {name!r}; available: {', '.join(sorted(LOG_FILES))}")
    s = get_services(request)
    n = min(tail, s.settings.max_tail_lines)
    return {"name": name, "lines": tail_file(s.settings.log_dir / LOG_FILES[name], n)}