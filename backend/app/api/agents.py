"""Agent runs: start an AI agent on a goal, follow its steps, resume it after a human decision, stop it."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ..ai import prompts
from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["agents"])


class StartRunBody(BaseModel):
    goal: str = Field(min_length=5, max_length=4000, description="what the agent should achieve, in plain words")
    agent: str = Field("lab", max_length=40, description="which agent (GET /agent/status lists them)")
    max_steps: Optional[int] = Field(None, ge=1, le=60, description="most tool calls for this run")
    context: str = Field("", max_length=8000, description="extra facts for the agent (optional)")
    project: Optional[str] = Field(None, max_length=32, description="the project the goal is about (for the "
                                                                     "knowledge base)")


def _agents(request: Request):
    return get_services(request).agents


@router.get("/agent/status")
def agent_status(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """Is a model configured, which agents exist, and how many tokens were used so far."""
    return _agents(request).status()


@router.get("/agent/prompts")
def agent_prompts(request: Request, p: Principal = Depends(require("viewer"))) -> list:
    """The prompt files with their versions and hashes."""
    return prompts.available()


@router.post("/agent/runs", status_code=202)
def start_run(body: StartRunBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Start an agent run (a background job). The agent acts with the AGENT role only: it can build and propose,
    never approve."""
    return _agents(request).start(body.goal, f"api:{p.name}", body.agent, body.max_steps, body.context,
                                  project=body.project)


class TeamBody(BaseModel):
    goal: str = Field(min_length=5, max_length=4000, description="what the team should achieve")
    max_steps: Optional[int] = Field(None, ge=1, le=60, description="most tool calls of the Supervisor")
    context: str = Field("", max_length=8000)


@router.post("/agent/team", status_code=202)
def start_team(body: TeamBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Start the Supervisor, which plans the goal and hands tasks to the firmware, build, security, performance and
    verification agents. Each task becomes a child run (see /agent/runs/{id}/tree)."""
    return _agents(request).start(body.goal, f"api:{p.name}", "supervisor", body.max_steps, body.context)


@router.get("/agent/runs")
def list_runs(request: Request, status: Optional[str] = None, limit: int = Query(20, ge=1, le=200),
              roots: bool = Query(False, description="only runs that were started directly (no child runs)"),
              p: Principal = Depends(require("viewer"))) -> list:
    return _agents(request).store.list(limit, status, roots_only=roots)


@router.get("/agent/runs/{run_id}/tree")
def run_tree(run_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """A team run with every specialist task, its outcome, its report and the tokens of the whole team."""
    return _agents(request).tree(run_id)


@router.get("/agent/runs/{run_id}")
def get_run(run_id: int, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    return _agents(request).details(run_id)


@router.get("/agent/runs/{run_id}/steps")
def run_steps(run_id: int, request: Request, after: int = Query(0, ge=0),
              p: Principal = Depends(require("viewer"))) -> dict:
    a = _agents(request)
    a.get(run_id)
    steps = a.store.steps(run_id, after)
    return {"run_id": run_id, "steps": steps, "next_after": steps[-1]["id"] if steps else after}


@router.get("/agent/runs/{run_id}/messages")
def run_messages(run_id: int, request: Request, p: Principal = Depends(require("operator"))) -> list:
    """The whole conversation, from the checkpoint. Operator only: it contains source code and board data."""
    return _agents(request).messages(run_id)


@router.post("/agent/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Continue a run that paused at a proposal, AFTER a human approved or rejected that proposal."""
    return _agents(request).resume(run_id, f"api:{p.name}")


@router.post("/agent/runs/{run_id}/stop")
def stop_run(run_id: int, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    """Ask a run to stop after its current step."""
    return _agents(request).stop(run_id, f"api:{p.name}")
