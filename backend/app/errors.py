"""One error format for the whole API, and the mapping from our internal exceptions to HTTP errors.

Every error response looks like:
    {"error": {"code": "board_busy", "message": "the board is busy: ...", "details": ..., "request_id": "..."}}
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .build.engine import ProjectError
from .deploy.board import BoardError
from .deploy.engine import DeployError
from .hardware.link import LinkError, LinkTimeout, NotConnected

log = logging.getLogger("aeisp.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _envelope(request: Request, status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message,
                      "request_id": getattr(request.state, "request_id", "-")}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _envelope(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [{"where": ".".join(str(x) for x in e.get("loc", [])), "problem": e.get("msg", "")}
                   for e in exc.errors()]
        return _envelope(request, 422, "validation_error", "the request is not valid", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        names = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden"}
        return _envelope(request, exc.status_code, names.get(exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(ProjectError)
    async def _project(request: Request, exc: ProjectError):
        return _envelope(request, 400, "project_error", str(exc))

    @app.exception_handler(DeployError)
    async def _deploy(request: Request, exc: DeployError):
        return _envelope(request, 400, "deploy_error", str(exc))

    @app.exception_handler(NotConnected)
    async def _not_connected(request: Request, exc: NotConnected):
        return _envelope(request, 503, "board_not_connected", str(exc))

    @app.exception_handler(LinkTimeout)
    async def _timeout(request: Request, exc: LinkTimeout):
        return _envelope(request, 504, "board_timeout", str(exc))

    @app.exception_handler(LinkError)
    async def _link(request: Request, exc: LinkError):
        return _envelope(request, 502, "board_error", str(exc))

    @app.exception_handler(BoardError)
    async def _board(request: Request, exc: BoardError):
        return _envelope(request, 502, "board_error", str(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception("unhandled error in %s %s (request %s)", request.method, request.url.path, rid)
        # never leak internals to the caller; the full trace is in data/logs/backend.log under this request id
        return _envelope(request, 500, "internal_error", f"unexpected server error (request id {rid})")