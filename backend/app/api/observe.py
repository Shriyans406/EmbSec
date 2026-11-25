"""Observability endpoints: Prometheus metrics and the deep health check."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from ..observability import health, metrics
from ..security import Principal, require
from .deps import get_services

router = APIRouter(tags=["system"])


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(request: Request, p: Principal = Depends(require("viewer"))) -> PlainTextResponse:
    """Prometheus text format. Scrape it with the viewer token as a bearer token."""
    text = await run_in_threadpool(metrics, get_services(request))
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4")


@router.get("/system/health/deep")
async def deep_health(request: Request, p: Principal = Depends(require("viewer"))) -> dict:
    """Every part the lab depends on, each ok / warn / fail with a reason."""
    return await run_in_threadpool(health, get_services(request))
