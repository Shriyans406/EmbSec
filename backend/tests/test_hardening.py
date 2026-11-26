"""Fixes from the Phase 17 security review of the platform itself (docs/security-review.md)."""
import logging

import pytest
from starlette.websockets import WebSocketDisconnect

from app.logging_setup import RedactTokens, redact
from app.security import FailedAuthLimiter

from conftest import TOKENS, auth


def test_tokens_in_urls_never_reach_the_logs():
    assert redact('127.0.0.1:5000 - "GET /ws?token=s3cr3t-Value_x HTTP/1.1" 101') == \
        '127.0.0.1:5000 - "GET /ws?token=*** HTTP/1.1" 101'
    assert redact("/ws?a=1&TOKEN=abc&b=2") == "/ws?a=1&TOKEN=***&b=2"
    assert redact("nothing to hide") == "nothing to hide"
    # uvicorn's access logger passes the path as an argument, not inside the message
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s"', ("1.2.3.4", "GET",
                            "/ws?token=abcdef"), None)
    RedactTokens().filter(rec)
    assert "abcdef" not in rec.getMessage() and "token=***" in rec.getMessage()


def test_the_filter_is_installed_on_uvicorn_loggers(client):
    for name in ("uvicorn.access", "uvicorn.error", "aeisp"):
        assert any(isinstance(f, RedactTokens) for f in logging.getLogger(name).filters), name


def test_security_headers(client):
    r = client.get("/system/info", headers=auth("viewer"))
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert client.get("/system/info").headers["X-Frame-Options"] == "DENY"      # errors carry them too


def test_token_guessing_is_slowed_down(client):
    for _ in range(20):
        assert client.get("/system/info", headers={"Authorization": "Bearer guess"}).status_code == 401
    r = client.get("/system/info", headers={"Authorization": "Bearer guess"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_failures"
    # even the right token waits: the address is blocked, not the guess
    assert client.get("/system/info", headers=auth("viewer")).status_code == 429
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/ws?token={TOKENS['viewer']}") as ws:
            ws.receive_json()
    assert e.value.code == 4429


def test_missing_tokens_do_not_count_and_the_window_passes():
    lim = FailedAuthLimiter(limit=3, window_s=0.2)
    for _ in range(3):
        lim.failed("a")
    assert lim.blocked("a") and not lim.blocked("b")
    import time

    time.sleep(0.25)
    assert not lim.blocked("a")


def test_no_route_lets_a_viewer_change_anything(client):
    """Every state-changing route refuses the viewer token: walk the whole API instead of trusting a list."""
    offenders, checked = [], 0
    for route in client.app.routes:
        methods = getattr(route, "methods", None) or set()
        for m in methods - {"GET", "HEAD", "OPTIONS"}:
            path = "/".join("1" if seg.startswith("{") else seg for seg in route.path.split("/"))
            if route.path == "/tools/{name}/call":            # viewers may call read-only tools; try a write one
                path = "/tools/write_source/call"
            r = client.request(m, path, headers=auth("viewer"), json={})
            if r.status_code != 403:
                offenders.append(f"{m} {route.path} -> {r.status_code}")
            checked += 1
    assert checked > 40
    assert offenders == []
