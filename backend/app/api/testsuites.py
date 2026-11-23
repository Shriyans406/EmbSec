"""Stored test suites: create, edit, run, compare runs, follow a suite across builds (regression), coverage."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..security import Principal, require
from ..services.testing import RunRequest, TestCase
from .deps import get_services

router = APIRouter(tags=["tests"])


class SuiteBody(BaseModel):
    name: str = Field(min_length=2, max_length=48)
    description: str = Field("", max_length=300)
    project: Optional[str] = Field(None, max_length=32)
    tags: List[str] = Field(default_factory=list, max_length=8)
    cases: List[TestCase] = Field(min_length=1, max_length=100)
    replace: bool = False


class CasesBody(BaseModel):
    cases: List[TestCase] = Field(min_length=1, max_length=50)


class CasePatch(BaseModel):
    enabled: bool


class SuiteRunBody(BaseModel):
    repeat: int = Field(1, ge=1, le=20)


def _t(request: Request):
    return get_services(request).tests


def _dump(cases: List[TestCase]) -> list:
    return [c.model_dump(exclude_none=True) for c in cases]


@router.post("/tests/suites", status_code=201)
def create_suite(body: SuiteBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _t(request).suites_db.create(body.name, _dump(body.cases), f"api:{p.name}", body.description, body.project,
                                        body.tags, "manual", replace=body.replace)


@router.get("/tests/suites/{name}")
def get_suite(name: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _t(request).suites_db.get(name)


@router.delete("/tests/suites/{name}")
def delete_suite(name: str, request: Request, p: Principal = Depends(require("operator"))) -> dict:
    return _t(request).suites_db.delete(name)


@router.post("/tests/suites/{name}/cases")
def add_cases(name: str, body: CasesBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _t(request).suites_db.add_cases(name, _dump(body.cases))


@router.patch("/tests/suites/{name}/cases/{case}")
def patch_case(name: str, case: str, body: CasePatch, request: Request,
               p: Principal = Depends(require("agent"))) -> dict:
    return _t(request).suites_db.set_case(name, case, enabled=body.enabled)


@router.delete("/tests/suites/{name}/cases/{case}")
def delete_case(name: str, case: str, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _t(request).suites_db.set_case(name, case, remove=True)


@router.post("/tests/suites/{name}/run", status_code=202)
def run_suite(name: str, body: SuiteRunBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _t(request).start(RunRequest(suite=name, repeat=body.repeat), f"api:{p.name}")


@router.get("/tests/suites/{name}/history")
def suite_history(name: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """The latest run of the suite on each firmware build."""
    return _t(request).history(name)


@router.get("/tests/suites/{name}/regression")
def suite_regression(name: str, request: Request, build_id: Optional[int] = Query(None, ge=1),
                     baseline_build: Optional[int] = Query(None, ge=1),
                     p: Principal = Depends(require("viewer"))) -> dict:
    return _t(request).regression(name, build_id, baseline_build)


@router.get("/tests/suites/{name}/coverage")
def suite_coverage(name: str, request: Request, project: Optional[str] = None,
                   p: Principal = Depends(require("viewer"))) -> dict:
    return _t(request).coverage(name, project)


@router.get("/tests/compare")
def compare_runs(request: Request, baseline: int = Query(ge=1), current: int = Query(ge=1),
                 p: Principal = Depends(require("viewer"))) -> dict:
    return _t(request).compare(baseline, current)
