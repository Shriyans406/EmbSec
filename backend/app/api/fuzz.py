"""Fuzz testing: campaigns against the board, their findings, reproduction and minimisation of a finding."""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..fuzz.generators import FAMILIES
from ..fuzz.store import FINDING_STATUSES
from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["fuzz"])


class CampaignBody(BaseModel):
    project: Optional[str] = Field(None, description="use this project's spec and suites as the corpus")
    families: Optional[List[str]] = Field(None, description=f"any of {FAMILIES} (default: all)")
    seed: Optional[int] = Field(None, ge=0, le=2 ** 31, description="same seed + corpus = same inputs")
    max_inputs: Optional[int] = Field(None, ge=1)
    max_seconds: Optional[int] = Field(None, ge=1)
    max_resets: Optional[int] = Field(None, ge=0)
    slow_ms: Optional[float] = Field(None, gt=0, le=10000)


class ReproBody(BaseModel):
    attempts: int = Field(3, ge=1, le=10)
    minimise: bool = True
    max_tries: int = Field(40, ge=0, le=100)
    max_resets: int = Field(3, ge=0, le=3)


class StatusBody(BaseModel):
    status: Literal[tuple(FINDING_STATUSES)]  # type: ignore[valid-type]


def _f(request: Request):
    return get_services(request).fuzz


@router.post("/fuzz/campaigns", status_code=202)
def start_campaign(body: CampaignBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Agents may run up to 500 inputs / 600 s / 3 resets; an operator up to 2000 / 1800 s / 10."""
    return _f(request).start(f"api:{p.name}", body.project, body.families, body.seed,
                             elevated=p.at_least("operator"), max_inputs=body.max_inputs,
                             max_seconds=body.max_seconds, max_resets=body.max_resets, slow_ms=body.slow_ms)


@router.get("/fuzz/campaigns")
def list_campaigns(request: Request, limit: int = Query(20, ge=1, le=200),
                   p: Principal = Depends(require("viewer"))) -> list:
    return _f(request).store.list(limit)


@router.get("/fuzz/campaigns/{cid}")
def get_campaign(cid: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _f(request).campaign(cid)


@router.post("/fuzz/campaigns/{cid}/stop")
def stop_campaign(cid: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _f(request).stop(cid)


@router.get("/fuzz/findings")
def list_findings(request: Request, status: Optional[str] = None, min_severity: Optional[str] = None,
                  project: Optional[str] = None, campaign_id: Optional[int] = None,
                  limit: int = Query(100, ge=1, le=500), p: Principal = Depends(require("viewer"))) -> list:
    return _f(request).store.findings(campaign_id, status, min_severity, project, limit)


@router.get("/fuzz/findings/{fid}")
def get_finding(fid: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _f(request).finding(fid)


@router.post("/fuzz/findings/{fid}/reproduce", status_code=202)
def reproduce_finding(fid: int, body: ReproBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _f(request).start_reproduce(fid, f"api:{p.name}", body.attempts, body.minimise, body.max_tries,
                                       body.max_resets)


@router.patch("/fuzz/findings/{fid}")
def set_finding_status(fid: int, body: StatusBody, request: Request,
                       p: Principal = Depends(require("operator"))) -> dict:
    """Operators decide that a finding is fixed or will not be fixed (the remediation workflow does it too)."""
    return _f(request).set_status(fid, body.status)
