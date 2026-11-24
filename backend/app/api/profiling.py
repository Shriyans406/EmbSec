"""Profiling and benchmarks: define, run on the board, compare, follow over builds, set baselines."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["profiling"])


class BenchCommand(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    command: str = Field(min_length=1, max_length=120)
    repeat: int = Field(10, ge=1, le=100)


class Burst(BaseModel):
    command: str = Field(min_length=1, max_length=120)
    count: int = Field(50, ge=5, le=200)


class BenchmarkBody(BaseModel):
    name: str = Field(min_length=2, max_length=48)
    project: Optional[str] = None
    description: str = Field("", max_length=300)
    commands: List[BenchCommand] = Field(min_length=1, max_length=12)
    runs: int = Field(5, ge=2, le=20)
    warmup: int = Field(2, ge=0, le=10)
    burst: Optional[Burst] = None
    replace: bool = False


class BaselineBody(BaseModel):
    run_id: int = Field(ge=1)


def _p(request: Request):
    return get_services(request).profiling


@router.get("/benchmarks")
def list_benchmarks(request: Request, project: Optional[str] = None,
                    p: Principal = Depends(require("viewer"))) -> list:
    return _p(request).list(project)


@router.post("/benchmarks", status_code=201)
def save_benchmark(body: BenchmarkBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    d = body.model_dump(exclude={"replace"}, exclude_none=True)
    return _p(request).save(d, f"api:{p.name}", body.replace)


@router.post("/benchmarks/for-project/{project}", status_code=201)
def benchmark_for_project(project: str, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """The project's benchmark, made from its specification (one successful example per command)."""
    return _p(request).for_project(project, f"api:{p.name}")


@router.get("/benchmarks/{name}")
def get_benchmark(name: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _p(request).get(name)


@router.post("/benchmarks/{name}/run", status_code=202)
def run_benchmark(name: str, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return _p(request).start(name, f"api:{p.name}")


@router.get("/benchmarks/{name}/runs")
def benchmark_runs(name: str, request: Request, build_id: Optional[int] = None,
                   limit: int = Query(50, ge=1, le=500), p: Principal = Depends(require("viewer"))) -> list:
    return _p(request).runs(name, build_id, limit)


@router.get("/benchmarks/{name}/history")
def benchmark_history(name: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _p(request).history(name)


@router.post("/benchmarks/{name}/baseline")
def set_baseline(name: str, body: BaselineBody, request: Request, p: Principal = Depends(require("operator"))) -> dict:
    """Operators choose the baseline (it decides what later builds are compared with). It must be repeatable."""
    return _p(request).set_baseline(name, body.run_id)


@router.get("/benchmarks/{name}/vs-baseline")
def vs_baseline(name: str, request: Request, run_id: Optional[int] = None,
                p: Principal = Depends(require("viewer"))) -> dict:
    return _p(request).against_baseline(name, run_id)


@router.get("/benchmark-runs/compare")
def compare_runs(request: Request, before: int = Query(ge=1), after: int = Query(ge=1),
                 p: Principal = Depends(require("viewer"))) -> dict:
    return _p(request).compare(before, after)


@router.get("/benchmark-runs/{run_id}")
def get_run(run_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _p(request).run(run_id)
