"""Remediations: take a reproducible bug (a fuzz finding), fix it, prove the fix - with a human approving every
deployment."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["remediations"])


class RemediationBody(BaseModel):
    finding_id: int = Field(ge=1, description="the fuzz finding to fix")
    max_fix_rounds: int = Field(2, ge=0, le=4, description="how often a failed build / scan / retest may go back to "
                                                           "the fixer")
    fuzz_check: bool = Field(True, description="after the fix, fuzz the same command again to look for new problems")


def _r(request: Request):
    return get_services(request).remediations


@router.post("/remediations", status_code=202)
def start_remediation(body: RemediationBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _r(request).start(body.finding_id, f"api:{p.name}", body.max_fix_rounds, body.fuzz_check)


@router.get("/remediations")
def list_remediations(request: Request, limit: int = Query(20, ge=1, le=200),
                      p: Principal = Depends(require("viewer"))) -> list:
    return _r(request).store.list(limit)


@router.get("/remediations/{rid}")
def get_remediation(rid: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _r(request).get(rid)


@router.get("/remediations/{rid}/events")
def remediation_events(rid: int, request: Request, after: int = Query(0, ge=0),
                       p: Principal = Depends(require("viewer"))) -> dict:
    eng = _r(request)
    eng.get(rid)
    events = eng.store.events(rid, after)
    return {"remediation_id": rid, "events": events, "next_after": events[-1]["id"] if events else after}


@router.post("/remediations/{rid}/resume", status_code=202)
def resume_remediation(rid: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Continue after a human approved or rejected the remediation's deployment (or rollback) proposal."""
    return _r(request).resume(rid, f"api:{p.name}")


@router.post("/remediations/{rid}/stop")
def stop_remediation(rid: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _r(request).stop(rid, f"api:{p.name}")


@router.get("/remediations/{rid}/report", response_class=PlainTextResponse)
def remediation_report(rid: int, request: Request, p: Principal = Depends(require("viewer"))) -> str:
    """The finished remediation as Markdown, with the code diff."""
    return _r(request).report_markdown(rid)


@router.get("/remediations/{rid}/diff", response_class=PlainTextResponse)
def remediation_diff(rid: int, request: Request, p: Principal = Depends(require("viewer"))) -> str:
    return _r(request).get(rid).get("diff_text") or ""
