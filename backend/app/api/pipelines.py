"""Pipelines: give a requirement, get firmware that was specified, written, built, scanned, deployed (after a human
approved it) and tested on the board, with a report."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["pipelines"])


class PipelineBody(BaseModel):
    requirement: str = Field(min_length=10, max_length=4000, description="what the firmware must do, in plain words")
    max_fix_rounds: int = Field(3, ge=0, le=6, description="how often the code may be fixed after a failed build/scan")
    deploy: bool = Field(True, description="propose a deployment and test on the board (false: stop after the scan)")
    max_test_rounds: int = Field(1, ge=0, le=3, description="how often failing board checks may send it back to code")


def _p(request: Request):
    return get_services(request).pipelines


@router.post("/pipelines", status_code=202)
def start_pipeline(body: PipelineBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Start a pipeline. It pauses at the deployment proposal until a human approves or rejects it; then call
    /pipelines/{id}/resume."""
    return _p(request).start(body.requirement, f"api:{p.name}", body.max_fix_rounds, body.deploy,
                             body.max_test_rounds)


@router.post("/pipelines/{pid}/resume", status_code=202)
def resume_pipeline(pid: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Continue after a human approved or rejected the pipeline's deployment proposal."""
    return _p(request).resume(pid, f"api:{p.name}")


@router.post("/pipelines/{pid}/stop")
def stop_pipeline(pid: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _p(request).stop(pid, f"api:{p.name}")


@router.get("/pipelines")
def list_pipelines(request: Request, limit: int = Query(20, ge=1, le=200),
                   p: Principal = Depends(require("viewer"))) -> list:
    return _p(request).store.list(limit)


@router.get("/pipelines/{pid}")
def get_pipeline(pid: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _p(request).get(pid)


@router.get("/pipelines/{pid}/report", response_class=PlainTextResponse)
def pipeline_report(pid: int, request: Request, p: Principal = Depends(require("viewer"))) -> str:
    """The finished pipeline's report as Markdown (the JSON version is in GET /pipelines/{id} -> report)."""
    return _p(request).report_markdown(pid)


@router.get("/pipelines/{pid}/events")
def pipeline_events(pid: int, request: Request, after: int = Query(0, ge=0),
                    p: Principal = Depends(require("viewer"))) -> dict:
    eng = _p(request)
    eng.get(pid)
    events = eng.store.events(pid, after)
    return {"pipeline_id": pid, "events": events, "next_after": events[-1]["id"] if events else after}
