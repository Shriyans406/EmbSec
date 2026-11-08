"""Board endpoints (the Phase 1 /device routes, now with roles and the board lock)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..security import Principal, require
from .deps import get_services

router = APIRouter(prefix="/device", tags=["device"])


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=120, description="one protocol line, e.g.  PING  or  LED ON")


@router.get("/ports")
def ports(request: Request, p: Principal = Depends(require("viewer"))) -> list:
    return get_services(request).device.ports()


@router.get("/status")
def status(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """connected = true/false, or null while a job is using the board (then 'busy' says which)."""
    return get_services(request).device.status(f"api:{p.name}")


@router.post("/command")
def command(body: CommandBody, request: Request, p: Principal = Depends(require("agent"))) -> dict:
    s = get_services(request)
    result = s.device.command(body.command, f"api:{p.name}", operator=p.at_least("operator"))
    s.audit.write(p.name, p.role, "device.command", body.command[:60], "ok" if result["ok"] else "device-error")
    return result