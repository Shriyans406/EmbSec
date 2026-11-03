"""HTTP endpoints for the device link (Phase 1)."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .discovery import list_all_ports
from .link import LinkError, LinkTimeout, NotConnected, SerialLink

router = APIRouter(prefix="/device", tags=["device"])

link = SerialLink()  # one shared link for the whole backend

ALLOWED_VERBS = {"HELLO", "PING", "ECHO", "UPTIME"}


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=120, pattern=r"^[^\r\n]+$")


@router.get("/ports")
def ports() -> list[dict]:
    return [p.to_dict() for p in list_all_ports()]


@router.get("/status")
def status() -> dict:
    if not link.is_connected:
        try:
            link.connect()
        except LinkError as e:
            return {"connected": False, "info": None, "error": str(e)}
    return {"connected": link.is_connected, "info": link.info.to_dict() if link.info else None, "error": None}


@router.post("/command")
def command(req: CommandRequest) -> dict:
    verb = req.command.split(" ", 1)[0]
    if verb not in ALLOWED_VERBS:
        raise HTTPException(status_code=422, detail=f"command not allowed: {verb}")
    t0 = time.perf_counter()
    try:
        reply = link.request(req.command)
    except LinkTimeout as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except NotConnected as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except LinkError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"command": req.command, "reply": reply, "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}