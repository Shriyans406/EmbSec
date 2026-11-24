"""Optimisations: make a project's firmware faster or smaller, measured on the board, kept only if it is better
and still correct - with a human approving every deployment."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["optimizations"])


class OptimizationBody(BaseModel):
    project: str = Field(min_length=2, max_length=32)
    goal: Literal["speed", "flash", "ram"] = "speed"
    target: Optional[str] = Field(None, max_length=40, description="the benchmark command to speed up (default: "
                                                                   "the slowest one)")
    benchmark: Optional[str] = Field(None, description="default: the project's benchmark (made from its spec)")
    min_gain_pct: float = Field(10.0, ge=1, le=90, description="the least improvement worth keeping")
    max_variants: int = Field(2, ge=1, le=3)
    fuzz_check: bool = True


def _o(request: Request):
    return get_services(request).optimizations


@router.post("/optimizations", status_code=202)
def start_optimization(body: OptimizationBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _o(request).start(body.project, f"api:{p.name}", body.goal, body.target, body.benchmark,
                             body.min_gain_pct, body.max_variants, body.fuzz_check)


@router.get("/optimizations")
def list_optimizations(request: Request, project: Optional[str] = None, limit: int = Query(20, ge=1, le=200),
                       p: Principal = Depends(require("viewer"))) -> list:
    return _o(request).store.list(limit, project)


@router.get("/optimizations/{oid}")
def get_optimization(oid: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _o(request).get(oid)


@router.get("/optimizations/{oid}/events")
def optimization_events(oid: int, request: Request, after: int = Query(0, ge=0),
                        p: Principal = Depends(require("viewer"))) -> dict:
    eng = _o(request)
    eng.get(oid)
    events = eng.store.events(oid, after)
    return {"optimization_id": oid, "events": events, "next_after": events[-1]["id"] if events else after}


@router.post("/optimizations/{oid}/resume", status_code=202)
def resume_optimization(oid: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Continue after a human approved or rejected the deployment (or rollback) proposal."""
    return _o(request).resume(oid, f"api:{p.name}")


@router.post("/optimizations/{oid}/stop")
def stop_optimization(oid: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _o(request).stop(oid, f"api:{p.name}")


@router.get("/optimizations/{oid}/report", response_class=PlainTextResponse)
def optimization_report(oid: int, request: Request, p: Principal = Depends(require("viewer"))) -> str:
    """The finished optimisation as Markdown: rejected variants, the A/B table with noise margins, the diff."""
    return _o(request).report_markdown(oid)
