"""Task records and reports: everything about one task, verified against the records, replayed as a timeline,
downloadable as Markdown, PDF or JSON. Plus the audit chain check and rebuilding a build from its saved sources."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["records"])

Kind = Literal["agent_run", "pipeline", "remediation", "optimisation", "fuzz_campaign"]


def _r(request: Request):
    return get_services(request).records


@router.get("/records/saved")
def saved_records(request: Request, p: Principal = Depends(require("viewer"))) -> list:
    return _r(request).saved()


@router.get("/records/saved/{name}/verify")
async def verify_saved(name: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """Check a saved record file against what exists now (and against its own hash)."""
    return await run_in_threadpool(_r(request).verify_saved, name)


@router.get("/records/{kind}/{task_id}")
async def get_record(kind: Kind, task_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return await run_in_threadpool(_r(request).take, kind, task_id)


@router.get("/records/{kind}/{task_id}/verify")
async def verify_record(kind: Kind, task_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return await run_in_threadpool(_r(request).verify, kind, task_id)


@router.get("/records/{kind}/{task_id}/replay")
async def replay_record(kind: Kind, task_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """The task's story: every event, step, log line and audit entry in time order."""
    return await run_in_threadpool(_r(request).replay, kind, task_id)


@router.get("/records/{kind}/{task_id}/report")
async def record_report(kind: Kind, task_id: int, request: Request, format: Literal["md", "pdf", "json"] = "md",
                        p: Principal = Depends(require("viewer"))) -> Response:
    content, media, name = await run_in_threadpool(_r(request).report, kind, task_id, format)
    disposition = "attachment" if format == "pdf" else "inline"
    return Response(content, media_type=media, headers={"Content-Disposition": f'{disposition}; filename="{name}"'})


@router.post("/records/{kind}/{task_id}/save", status_code=201)
async def save_record(kind: Kind, task_id: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return await run_in_threadpool(_r(request).save, kind, task_id)


@router.post("/builds/{build_id}/rebuild", status_code=202)
def rebuild(build_id: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Build a build's saved sources again and compare (a job)."""
    return _r(request).rebuild(build_id, f"api:{p.name}")


@router.get("/audit/verify")
async def audit_verify(request: Request, p: Principal = Depends(require("operator"))) -> dict:
    """Walk the whole audit hash chain."""
    return await run_in_threadpool(get_services(request).audit.verify)


@router.get("/audit/head")
def audit_head(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """The newest audit entry and its hash: write it down somewhere else to detect rewritten history later."""
    return get_services(request).audit.head()
