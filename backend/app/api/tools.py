"""The tool endpoints: this is where the AI agents (Phase 6+) call in."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["tools"])


class ToolCallBody(BaseModel):
    arguments: Optional[dict] = Field(default=None, description="the tool's arguments, as listed by GET /tools")


@router.get("/tools")
def list_tools(request: Request, p: Principal = Depends(require("viewer"))) -> list:
    """The tools your role may call, with their argument schemas."""
    return get_services(request).tools.describe(p)


@router.post("/tools/{name}/call")
def call_tool(name: str, body: ToolCallBody, request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """Run a tool. Role and arguments are checked by the registry, and the call is audited."""
    result = get_services(request).tools.call(name, body.arguments, p)
    return {"ok": True, "tool": name, "result": result}