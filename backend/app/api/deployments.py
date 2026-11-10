"""Deployments, recoveries, known-good firmware and test runs."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..security import Principal, require
from ..services.testing import RunRequest
from .deps import get_services

router = APIRouter(tags=["deployments"])


class DeployBody(BaseModel):
    build_id: int = Field(ge=1, description="a successful build")
    rollback: bool = Field(True, description="restore the last good firmware if the new one fails verification")


class RecoverBody(BaseModel):
    target: Literal["auto", "known-good", "stock"] = "auto"


class PromoteBody(BaseModel):
    build_id: int = Field(ge=1)
    confirm: str = Field(min_length=1, max_length=40, description="type exactly:  PROMOTE <build_id>")


@router.post("/deployments", status_code=202)
def propose_deployment(body: DeployBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Runs the read-only pre-flight checks and creates a job that WAITS for an operator to approve it
    (POST /jobs/{id}/approve). Nothing is flashed by this call."""
    s = get_services(request)
    job = s.deploys.propose_deploy(body.build_id, f"api:{p.name}", body.rollback)
    s.audit.write(p.name, p.role, "deploy.propose", f"build:{body.build_id}", "ok", f"job {job['id']}")
    return job


@router.post("/recoveries", status_code=202)
def propose_recovery(body: RecoverBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    job = s.deploys.propose_recover(body.target, f"api:{p.name}")
    s.audit.write(p.name, p.role, "recover.propose", body.target, "ok", f"job {job['id']}")
    return job


@router.get("/deployments")
def deployment_history(request: Request, limit: int = Query(20, ge=1, le=100),
                       p: Principal = Depends(require("viewer"))) -> list:
    return get_services(request).deploys.history(limit)


@router.get("/deployments/{dep_id}")
def get_deployment(dep_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return get_services(request).deploys.get(dep_id)


@router.get("/known-good")
def known_good(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return get_services(request).deploys.known_good()


@router.post("/known-good/promote")
def promote(body: PromoteBody, request: Request, p: Principal = Depends(require("operator"))) -> dict:
    return get_services(request).deploys.promote(body.build_id, body.confirm, p)


# ------------------------------------------------------------------ tests
@router.get("/tests/suites")
def suites(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return get_services(request).tests.suites()


@router.post("/tests/run", status_code=202)
def run_tests(body: RunRequest, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    job = s.tests.start(body, f"api:{p.name}")
    s.audit.write(p.name, p.role, "tests.start", body.suite or "custom", "ok", f"job {job['id']}")
    return job


@router.get("/tests/runs")
def list_runs(request: Request, limit: int = Query(20, ge=1, le=100), p: Principal = Depends(require("viewer"))) -> list:
    return get_services(request).tests.list_runs(limit)


@router.get("/tests/runs/{run_id}")
def get_run(run_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return get_services(request).tests.get_run(run_id)