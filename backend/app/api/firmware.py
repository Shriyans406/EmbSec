"""Firmware projects, source files and builds."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["firmware"])


class CreateProjectBody(BaseModel):
    name: str = Field(min_length=2, max_length=32)


class SourceBody(BaseModel):
    content: str = Field(max_length=65536, description="the complete new text of the file")


class RestoreBody(BaseModel):
    build_id: int = Field(ge=1)


class BuildBody(BaseModel):
    project: str = Field(min_length=2, max_length=32)
    timeout_s: Optional[int] = Field(None, ge=10, le=900)


@router.get("/projects")
def list_projects(request: Request, p: Principal = Depends(require("viewer"))) -> list:
    return get_services(request).firmware.list_projects()


@router.post("/projects", status_code=201)
def create_project(body: CreateProjectBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    out = s.firmware.create_project(body.name)
    s.audit.write(p.name, p.role, "project.create", body.name, "ok")
    return out


@router.get("/projects/{project}/sources")
def list_sources(project: str, request: Request, p: Principal = Depends(require("viewer"))) -> list:
    return get_services(request).firmware.list_sources(project)


@router.get("/projects/{project}/sources/{filename}")
def read_source(project: str, filename: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return get_services(request).firmware.read_source(project, filename)


@router.put("/projects/{project}/sources/{filename}")
def write_source(project: str, filename: str, body: SourceBody, request: Request,
                 p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    out = s.firmware.write_source(project, filename, body.content)
    s.audit.write(p.name, p.role, "source.write", f"{project}/{filename}", "ok", f"{out['size']} bytes")
    return out


@router.delete("/projects/{project}/sources/{filename}")
def delete_source(project: str, filename: str, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    out = s.firmware.delete_source(project, filename)
    s.audit.write(p.name, p.role, "source.delete", f"{project}/{filename}", "ok")
    return out


@router.post("/projects/{project}/restore")
def restore_sources(project: str, body: RestoreBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    out = s.firmware.restore_sources(project, body.build_id)
    s.audit.write(p.name, p.role, "source.restore", f"{project}", "ok", f"from build {body.build_id}")
    return out


# ------------------------------------------------------------------ builds
@router.post("/builds", status_code=202)
def start_build(body: BuildBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Starts a build job and returns at once. Follow it with GET /jobs/{id} and GET /jobs/{id}/logs."""
    s = get_services(request)
    job = s.builds.start(body.project, f"api:{p.name}", body.timeout_s)
    s.audit.write(p.name, p.role, "build.start", body.project, "ok", f"job {job['id']}")
    return job


@router.get("/builds")
def build_history(request: Request, project: Optional[str] = None, limit: int = Query(20, ge=1, le=100),
                  p: Principal = Depends(require("viewer"))) -> list:
    return get_services(request).builds.history(project, limit)


@router.get("/builds/{build_id}")
def get_build(build_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return get_services(request).builds.get(build_id)


@router.get("/builds/{build_id}/log")
def build_log(build_id: int, request: Request, tail: int = Query(200, ge=1, le=2000),
              p: Principal = Depends(require("viewer"))) -> dict:
    return {"build_id": build_id, "lines": get_services(request).builds.log_tail(build_id, tail)}