"""AEISP backend: creates the FastAPI application.

Run it (from the backend folder):   python -m uvicorn app.main:app --port 8000
Interactive API page:               http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from .api import core as api_core
from .api import device as api_device
from .errors import install_error_handlers
from .logging_setup import request_id_var, setup_logging
from .services.container import Services, build_services
from .settings import VERSION, Settings

log = logging.getLogger("aeisp.http")


def create_app(settings: Optional[Settings] = None, services: Optional[Services] = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_dir, settings.log_level)
    services = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.start()
        log.info("AEISP backend %s started (roles enabled: %s)", VERSION, ", ".join(sorted(settings.role_tokens)) or "NONE")
        if not settings.role_tokens:
            log.warning("no API tokens configured: every protected endpoint will refuse. Run: python -m app.cli make-tokens")
        yield
        services.stop()
        log.info("AEISP backend stopped")

    app = FastAPI(title="AEISP Backend", version=VERSION, lifespan=lifespan,
                  description="Agentic Embedded Intelligence & Security Platform - engineering backend")
    app.state.settings = settings
    app.state.services = services

    install_error_handlers(app)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        request.state.request_id = rid
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            ms = (time.perf_counter() - t0) * 1000
            log.info("%s %s -> %s (%.0f ms)", request.method, request.url.path, status, ms)
            # audit every change, and every refused attempt (401/403), but not ordinary reads
            if request.method != "GET" or status in (401, 403):
                p = getattr(request.state, "principal", None)
                await run_in_threadpool(
                    services.audit.write, p.name if p else "-", p.role if p else "-",
                    f"http:{request.method}", request.url.path, str(status))
            request_id_var.reset(token)

    app.include_router(api_core.router)
    app.include_router(api_device.router)
    return app


app = create_app()