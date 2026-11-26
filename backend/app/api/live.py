"""The live event feed for the dashboard: one WebSocket, /ws?token=<role token>.

Browsers cannot send an Authorization header on a WebSocket, so the token comes as a query parameter; it is checked
exactly like a bearer token and the connection is closed (code 4401) if it is wrong. Then the server pushes:

    {"type": "hello", "role": ..., "version": ...}                  once
    {"type": "job", "job": {...}}                                     a job appeared or changed status
    {"type": "log", "job_id": 7, "n": 123, "ts": ..., "text": ...}   a new job log line
    {"type": "board", "connected": true, "busy": "test job #9"}      board state (no I/O: it never disturbs a job)
    {"type": "audit", "entry": {...}}                                 operators only
The feed only reads the database; it never changes anything.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from ..db import connect
from ..security import auth_limiter, principal_for
from ..settings import VERSION

router = APIRouter(tags=["live"])

TICK_S = 0.7


class Feed:
    """What one client has seen so far, and what is new since."""

    def __init__(self, db_path, device, operator: bool) -> None:
        self.db_path = db_path
        self.device = device
        self.operator = operator
        self.last_log = self.last_audit = 0
        self.jobs: dict = {}
        self.board = None
        with connect(db_path) as c:
            self.last_log = c.execute("SELECT COALESCE(MAX(id), 0) FROM job_logs").fetchone()[0]
            self.last_audit = c.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]
            for r in c.execute("SELECT id, status FROM jobs ORDER BY id DESC LIMIT 100"):
                self.jobs[r["id"]] = r["status"]

    def poll(self) -> list:
        out = []
        with connect(self.db_path) as c:
            for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall():
                if self.jobs.get(r["id"]) != r["status"]:
                    self.jobs[r["id"]] = r["status"]
                    job = dict(r)
                    job["params"] = json.loads(job.pop("params_json") or "{}")
                    job.pop("result_json", None)
                    if not self.operator:
                        job.pop("confirm_token", None)
                    out.append({"type": "job", "job": job})
            for r in c.execute("SELECT id, job_id, ts, line FROM job_logs WHERE id > ? ORDER BY id LIMIT 300",
                               (self.last_log,)).fetchall():
                self.last_log = r["id"]
                out.append({"type": "log", "job_id": r["job_id"], "n": r["id"], "ts": r["ts"], "text": r["line"]})
            if self.operator:
                for r in c.execute("SELECT * FROM audit_log WHERE id > ? ORDER BY id LIMIT 100",
                                   (self.last_audit,)).fetchall():
                    self.last_audit = r["id"]
                    out.append({"type": "audit", "entry": dict(r)})
        link = getattr(self.device, "_link", None)
        board = {"connected": bool(getattr(link, "is_connected", False)), "busy": self.device.owner}
        if board != self.board:
            self.board = board
            out.append({"type": "board", **board})
        return out


@router.websocket("/ws")
async def live(websocket: WebSocket, token: str = Query("")):
    app = websocket.app
    client = websocket.client.host if websocket.client else "-"
    limiter = auth_limiter(app)
    if limiter.blocked(client):
        await websocket.close(code=4429)
        return
    principal = principal_for(app.state.settings.role_tokens, token) if token else None
    if principal is None:
        if token:
            limiter.failed(client)
        await websocket.close(code=4401)
        return
    await websocket.accept()
    services = app.state.services
    feed = await run_in_threadpool(Feed, services.settings.db_path, services.device, principal.at_least("operator"))
    try:
        await websocket.send_json({"type": "hello", "role": principal.role, "version": VERSION})
        while True:
            for event in await run_in_threadpool(feed.poll):
                await websocket.send_json(event)
            await asyncio.sleep(TICK_S)
    except (WebSocketDisconnect, RuntimeError):
        return
