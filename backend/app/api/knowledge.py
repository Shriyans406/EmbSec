"""The engineering knowledge base: search past bugs, fixes, benchmarks and decisions; project timelines; board
firmware history; decision records and notes."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..knowledge.history import add_decision, add_note, board_history, timeline
from ..knowledge.store import KINDS
from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["knowledge"])


class DecisionBody(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    context: str = Field(min_length=3, max_length=3000, description="why a decision was needed")
    decision: str = Field(min_length=3, max_length=3000)
    consequences: str = Field("", max_length=3000)
    project: Optional[str] = Field(None, max_length=32)
    tags: List[str] = Field(default_factory=list, max_length=8)


class NoteBody(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=6000)
    project: Optional[str] = Field(None, max_length=32)
    tags: List[str] = Field(default_factory=list, max_length=8)


def _k(request: Request):
    return get_services(request).knowledge


@router.get("/knowledge/search")
def search(request: Request, q: str = Query(min_length=2, max_length=500), kind: Optional[str] = None,
           project: Optional[str] = None, limit: int = Query(10, ge=1, le=100),
           p: Principal = Depends(require("viewer"))) -> list:
    """Full-text search (stemmed, ranked). Words are OR-ed; the best matches come first."""
    return _k(request).search(q, kind, project, limit)


@router.get("/knowledge/related")
def related(request: Request, q: str = Query(min_length=2, max_length=2000), project: Optional[str] = None,
            limit: int = Query(5, ge=1, le=20), p: Principal = Depends(require("viewer"))) -> dict:
    """What an agent would be told for this task text."""
    context, ids = _k(request).context_for(q, project, limit)
    return {"entries": ids, "context": context}


@router.get("/knowledge/stats")
def stats(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    k = _k(request)
    k.fresh()
    return {**k.store.stats(), "kinds": KINDS}


@router.get("/knowledge")
def list_entries(request: Request, kind: Optional[str] = None, project: Optional[str] = None,
                 limit: int = Query(50, ge=1, le=500), p: Principal = Depends(require("viewer"))) -> list:
    k = _k(request)
    k.fresh()
    return k.store.list(kind, project, limit)


@router.post("/knowledge/collect")
def collect(request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Read every record again into the knowledge base (it also happens by itself before lookups)."""
    return _k(request).collect()


@router.post("/knowledge/decisions", status_code=201)
def new_decision(body: DecisionBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return add_decision(_k(request).store, body.title, body.context, body.decision, body.consequences, body.project,
                        f"api:{p.name}", body.tags)


@router.post("/knowledge/notes", status_code=201)
def new_note(body: NoteBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    return add_note(_k(request).store, body.title, body.body, body.project, f"api:{p.name}", body.tags)


@router.get("/knowledge/{kid}")
def get_entry(kid: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _k(request).store.get(kid)


@router.delete("/knowledge/{kid}")
def delete_entry(kid: int, request: Request, p: Principal = Depends(require("operator"))) -> dict:
    return _k(request).store.delete(kid)


@router.get("/projects/{project}/timeline")
def project_timeline(project: str, request: Request, limit: int = Query(200, ge=1, le=1000),
                     p: Principal = Depends(require("viewer"))) -> list:
    """Everything that happened to a project, newest first: builds, deployments, tests, fuzzing, fixes, benchmarks,
    optimisations, pipelines, decisions."""
    return timeline(get_services(request).settings.db_path, project, limit)


@router.get("/boards/{uid}/history")
def board_firmware_history(uid: str, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """Which firmware a board ran, and when."""
    return board_history(get_services(request).settings.db_path, uid)
