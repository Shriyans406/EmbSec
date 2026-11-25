"""The dashboard's live feed (WebSocket /ws)."""
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import TOKENS, auth


def receive_until(ws, pred, limit=60):
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("the expected event never came")


def test_a_wrong_token_is_refused(client):
    for bad in ("", "nope"):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(f"/ws?token={bad}") as ws:
                ws.receive_json()
        assert e.value.code == 4401


def test_jobs_and_their_log_lines_arrive_live(client):
    with client.websocket_connect(f"/ws?token={TOKENS['viewer']}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["role"] == "viewer"
        board = receive_until(ws, lambda m: m["type"] == "board")
        assert set(board) == {"type", "connected", "busy"}
        jid = client.post("/selftest", headers=auth("agent"), json={"seconds": 1}).json()["id"]
        job = receive_until(ws, lambda m: m["type"] == "job" and m["job"]["id"] == jid)
        assert job["job"]["kind"] == "selftest" and "confirm_token" not in job["job"]
        line = receive_until(ws, lambda m: m["type"] == "log" and m["job_id"] == jid and "working" in m["text"])
        assert line["n"] > 0
        done = receive_until(ws, lambda m: m["type"] == "job" and m["job"]["id"] == jid
                             and m["job"]["status"] == "SUCCEEDED")
        assert done["job"]["finished_at"]


def test_operators_also_see_the_audit_log(client):
    with client.websocket_connect(f"/ws?token={TOKENS['operator']}") as ws:
        assert ws.receive_json()["role"] == "operator"
        client.post("/selftest", headers=auth("agent"), json={"seconds": 0})
        entry = receive_until(ws, lambda m: m["type"] == "audit" and m["entry"]["action"] == "selftest.create")
        assert entry["entry"]["role"] == "agent"
