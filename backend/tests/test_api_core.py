import json
import logging
import threading
import time

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app import cli as app_cli
from app.db import MIGRATIONS, migrate, schema_version
from app.jobs import FAILED, JobManager
from app.logging_setup import setup_logging
from app.main import create_app
from app.security import principal_for
from app.services.container import build_services
from app.settings import Settings

from conftest import FakeLink, TOKENS, auth, make_settings


# ------------------------------------------------------------------ health, auth, roles
def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["phase"] == 14 and body["auth_configured"] is True
    assert "X-Request-ID" in r.headers


def test_root_redirects_to_the_api_page(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/docs"


def test_no_token_and_wrong_token_are_refused(client):
    r = client.get("/system/info")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "unauthorized" and err["request_id"] == r.headers["X-Request-ID"]
    assert client.get("/system/info", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/system/info", headers={"Authorization": "Basic abc"}).status_code == 401


def test_roles(client):
    assert client.get("/system/info", headers=auth("viewer")).json()["you"] == {"name": "viewer", "role": "viewer"}
    assert client.get("/system/config", headers=auth("viewer")).status_code == 403
    assert client.get("/system/config", headers=auth("agent")).status_code == 403
    cfg = client.get("/system/config", headers=auth("operator")).json()
    assert cfg["roles_enabled"] == ["agent", "operator", "viewer"]


def test_secrets_never_appear_in_config(client):
    text = json.dumps(client.get("/system/config", headers=auth("operator")).json())
    for tok in TOKENS.values():
        assert tok not in text


def test_fail_closed_when_no_tokens(tmp_path):
    s = Settings(_env_file=None, root=tmp_path)
    app = create_app(s, build_services(s))
    with TestClient(app) as c:
        assert c.get("/health").json()["auth_configured"] is False
        r = c.get("/system/info", headers={"Authorization": "Bearer anything"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "auth_not_configured"


def test_principal_for_matches_exact_token_only():
    t = {"operator": "aaa", "viewer": "bbb"}
    assert principal_for(t, "aaa").role == "operator"
    assert principal_for(t, "bbb").role == "viewer"
    assert principal_for(t, "aaaa") is None and principal_for(t, "") is None


# ------------------------------------------------------------------ error format
def test_error_envelope_for_unknown_route_and_bad_input(client):
    r = client.get("/nope", headers=auth("viewer"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.post("/selftest", headers=auth("agent"), json={"seconds": 9999})
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["code"] == "validation_error" and e["details"][0]["where"].endswith("seconds")


def test_unexpected_exceptions_do_not_leak_details(app):
    router = APIRouter()

    @router.get("/boom")
    def boom():
        raise RuntimeError("secret internal path C:\\Users\\HP\\x")

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    body = r.json()["error"]
    assert body["code"] == "internal_error" and "secret internal path" not in json.dumps(body)
    assert body["request_id"] in body["message"]


# ------------------------------------------------------------------ jobs
def wait_done(client, job_id, role="viewer", timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/jobs/{job_id}", headers=auth(role)).json()
        if j["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_selftest_job_runs_and_logs(client):
    r = client.post("/selftest", headers=auth("agent"), json={"seconds": 1})
    assert r.status_code == 202 and r.json()["status"] in ("QUEUED", "RUNNING")
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "SUCCEEDED" and job["result"] == {"slept": 1} and job["requested_by"] == "api:agent"
    lines = client.get(f"/jobs/{job['id']}/logs", headers=auth("viewer")).json()
    texts = [l["text"] for l in lines["lines"]]
    assert texts[0].startswith("created by") and "started" in texts and any("working" in t for t in texts)
    assert texts[-1].startswith("finished: SUCCEEDED")
    again = client.get(f"/jobs/{job['id']}/logs?after={lines['next_after']}", headers=auth("viewer")).json()
    assert again["lines"] == []


def test_failing_job_is_marked_failed_with_result(client):
    jid = client.post("/selftest", headers=auth("agent"), json={"seconds": 0, "fail": True}).json()["id"]
    job = wait_done(client, jid)
    assert job["status"] == "FAILED" and "asked to fail" in job["error"] and job["result"] == {"slept": 0}


def test_viewer_cannot_start_jobs(client):
    assert client.post("/selftest", headers=auth("viewer"), json={}).status_code == 403


def test_approval_flow(client):
    job = client.post("/selftest", headers=auth("agent"), json={"seconds": 0, "require_approval": True}).json()
    jid = job["id"]
    assert job["status"] == "AWAITING_APPROVAL" and job["confirm_token"] == "SELFTEST" and job["expires_at"]
    time.sleep(0.3)
    assert client.get(f"/jobs/{jid}", headers=auth("viewer")).json()["status"] == "AWAITING_APPROVAL"
    assert client.post(f"/jobs/{jid}/approve", headers=auth("agent"), json={"confirm": "SELFTEST"}).status_code == 403
    bad = client.post(f"/jobs/{jid}/approve", headers=auth("operator"), json={"confirm": "yes"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "confirmation_mismatch"
    assert client.get(f"/jobs/{jid}", headers=auth("viewer")).json()["status"] == "AWAITING_APPROVAL"
    ok = client.post(f"/jobs/{jid}/approve", headers=auth("operator"), json={"confirm": "SELFTEST"})
    assert ok.status_code == 200 and ok.json()["approved_by"] == "api:operator"
    assert wait_done(client, jid)["status"] == "SUCCEEDED"
    again = client.post(f"/jobs/{jid}/approve", headers=auth("operator"), json={"confirm": "SELFTEST"})
    assert again.status_code == 409


def test_reject_and_cancel(client):
    a = client.post("/selftest", headers=auth("agent"), json={"seconds": 0, "require_approval": True}).json()["id"]
    r = client.post(f"/jobs/{a}/reject", headers=auth("operator"), json={"reason": "not now"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED" and "not now" in r.json()["error"]
    b = client.post("/selftest", headers=auth("agent"), json={"seconds": 0, "require_approval": True}).json()["id"]
    assert client.post(f"/jobs/{b}/cancel", headers=auth("agent")).json()["status"] == "CANCELLED"
    assert client.post(f"/jobs/{b}/cancel", headers=auth("agent")).status_code == 409
    assert client.post("/jobs/9999/cancel", headers=auth("agent")).status_code == 404


def test_running_job_cannot_be_cancelled(client):
    jid = client.post("/selftest", headers=auth("agent"), json={"seconds": 2}).json()["id"]
    deadline = time.time() + 5
    while client.get(f"/jobs/{jid}", headers=auth("viewer")).json()["status"] != "RUNNING" and time.time() < deadline:
        time.sleep(0.05)
    r = client.post(f"/jobs/{jid}/cancel", headers=auth("agent"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "job_running"
    wait_done(client, jid)


def test_proposals_expire(tmp_path):
    s = make_settings(tmp_path, proposal_ttl_s=1)
    app = create_app(s, build_services(s))
    with TestClient(app) as c:
        jid = c.post("/selftest", headers=auth("agent"), json={"seconds": 0, "require_approval": True}).json()["id"]
        time.sleep(2.2)
        assert c.get(f"/jobs/{jid}", headers=auth("viewer")).json()["status"] == "EXPIRED"
        r = c.post(f"/jobs/{jid}/approve", headers=auth("operator"), json={"confirm": "SELFTEST"})
        assert r.status_code == 409


def test_jobs_listing_filters(client):
    ids = [client.post("/selftest", headers=auth("agent"), json={"seconds": 0}).json()["id"] for _ in range(3)]
    for i in ids:
        wait_done(client, i)
    allj = client.get("/jobs?limit=2", headers=auth("viewer")).json()
    assert [j["id"] for j in allj] == [ids[2], ids[1]]
    assert client.get("/jobs?status=FAILED", headers=auth("viewer")).json() == []
    assert len(client.get("/jobs?kind=selftest", headers=auth("viewer")).json()) == 3


def test_orphaned_jobs_are_failed_on_restart(tmp_path):
    s = make_settings(tmp_path)
    migrate(s.db_path)
    jm = JobManager(s.db_path, {"system": 1})
    jm.register("selftest", lambda ctx: {}, "system")
    job = jm.submit("selftest", {}, "test")           # QUEUED but no worker is running: simulates a crash
    jm2 = JobManager(s.db_path, {"system": 1})
    jm2.register("selftest", lambda ctx: {}, "system")
    jm2.start()
    try:
        j = jm2.get(job["id"])
        assert j["status"] == FAILED and "restarted" in j["error"]
    finally:
        jm2.stop()


def test_unknown_job_kind(services):
    from app.errors import ApiError
    with pytest.raises(ApiError) as e:
        services.jobs.submit("nothing", {}, "t")
    assert e.value.status == 400


def test_runner_crash_is_contained(tmp_path):
    s = make_settings(tmp_path)
    migrate(s.db_path)
    jm = JobManager(s.db_path, {"system": 1})

    def boom(ctx):
        raise ValueError("kaboom")

    jm.register("boom", boom, "system")
    jm.start()
    try:
        j = jm.wait(jm.submit("boom", {}, "t")["id"], 10)
        assert j["status"] == FAILED and "ValueError: kaboom" in j["error"]
        jm.register("ok", lambda ctx: {"fine": True}, "system")
        assert jm.wait(jm.submit("ok", {}, "t")["id"], 10)["result"] == {"fine": True}
    finally:
        jm.stop()


# ------------------------------------------------------------------ device endpoints and the board lock
def test_device_status_and_command(client):
    st = client.get("/device/status", headers=auth("viewer")).json()
    assert st["connected"] is True and st["info"]["uid"] == "DE61C48787331A35" and st["busy"] is None
    r = client.post("/device/command", headers=auth("agent"), json={"command": "PING"})
    assert r.status_code == 200 and r.json()["reply"] == "OK PONG" and r.json()["ok"] is True
    r = client.post("/device/command", headers=auth("agent"), json={"command": "BLINK 20"})
    assert r.json()["ok"] is False and r.json()["error_code"] == 3 and r.json()["error_name"] == "BAD_RANGE"
    r = client.post("/device/command", headers=auth("agent"), json={"command": "#7 PING"})
    assert r.status_code == 200 and "#7 PING" in FakeLink.sent


def test_dangerous_commands_are_not_allowed(client):
    for cmd in ("BOOTSEL", "HANG", "FORMAT", "ping"):
        r = client.post("/device/command", headers=auth("operator"), json={"command": cmd})
        assert r.status_code == 422 and r.json()["error"]["code"] == "command_not_allowed", cmd
    assert client.post("/device/command", headers=auth("viewer"), json={"command": "PING"}).status_code == 403
    assert "BOOTSEL" not in FakeLink.sent and "HANG" not in FakeLink.sent


def test_reboot_needs_operator(client):
    assert client.post("/device/command", headers=auth("agent"), json={"command": "REBOOT"}).status_code == 403
    assert client.post("/device/command", headers=auth("operator"), json={"command": "REBOOT"}).status_code == 200


def test_command_validation(client):
    assert client.post("/device/command", headers=auth("agent"), json={"command": ""}).status_code == 422
    assert client.post("/device/command", headers=auth("agent"), json={"command": "A" * 200}).status_code == 422


def test_board_not_connected_is_a_clear_error(client):
    FakeLink.fail_connect = True
    st = client.get("/device/status", headers=auth("viewer")).json()
    assert st["connected"] is False and st["error"]
    r = client.post("/device/command", headers=auth("agent"), json={"command": "PING"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "board_not_connected"


def test_board_lock_blocks_commands_while_a_job_holds_the_board(client, services):
    entered, release = threading.Event(), threading.Event()

    def hold():
        with services.device.exclusive("deploy job #7"):
            entered.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(5)
    try:
        st = client.get("/device/status", headers=auth("viewer")).json()
        assert st["connected"] is None and st["busy"] == "deploy job #7"
        r = client.post("/device/command", headers=auth("agent"), json={"command": "PING"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "board_busy"
        assert "deploy job #7" in r.json()["error"]["message"]
        assert client.get("/system/info", headers=auth("viewer")).json()["board_busy"] == "deploy job #7"
    finally:
        release.set()
        t.join()
    assert client.post("/device/command", headers=auth("agent"), json={"command": "PING"}).status_code == 200


def test_exclusive_closes_the_shared_port(services):
    services.device.status()
    assert services.device._link.is_connected
    with services.device.exclusive("job"):
        assert not services.device._link.is_connected


# ------------------------------------------------------------------ audit and logs
def test_audit_records_changes_and_refusals(client):
    client.post("/selftest", headers=auth("agent"), json={"seconds": 0})
    client.get("/system/config", headers=auth("viewer"))                 # 403 refused
    client.get("/system/info")                                           # 401 refused
    rows = client.get("/audit?limit=50", headers=auth("operator")).json()
    actions = {(r["action"], r["actor"], r["outcome"]) for r in rows}
    assert ("selftest.create", "agent", "ok") in actions
    assert ("http:POST", "agent", "202") in actions
    assert ("http:GET", "viewer", "403") in actions
    assert ("http:GET", "-", "401") in actions
    assert client.get("/audit", headers=auth("agent")).status_code == 403
    only = client.get("/audit?action=selftest.", headers=auth("operator")).json()
    assert only and all(r["action"].startswith("selftest.") for r in only)


def test_failed_approval_attempts_are_audited(client):
    jid = client.post("/selftest", headers=auth("agent"), json={"require_approval": True, "seconds": 0}).json()["id"]
    client.post(f"/jobs/{jid}/approve", headers=auth("operator"), json={"confirm": "wrong"})
    rows = client.get("/audit?action=job.approve", headers=auth("operator")).json()
    assert rows and rows[0]["outcome"] == "refused:confirmation_mismatch"


def test_log_endpoint(client):
    client.get("/system/info", headers=auth("viewer"))
    lines = client.get("/logs/backend?tail=50", headers=auth("viewer")).json()["lines"]
    assert any("GET /system/info -> 200" in l for l in lines)
    assert client.get("/logs/passwords", headers=auth("viewer")).status_code == 404


def test_logging_setup_is_idempotent(tmp_path):
    setup_logging(tmp_path / "logs")
    setup_logging(tmp_path / "logs")
    handlers = [h for h in logging.getLogger("aeisp").handlers if getattr(h, "_aeisp", False)]
    assert len(handlers) == 2


# ------------------------------------------------------------------ database and setup tools
def test_migrations_apply_once(tmp_path):
    db = tmp_path / "x.db"
    assert migrate(db) == len(MIGRATIONS)
    assert migrate(db) == len(MIGRATIONS) == schema_version(db)


def test_make_tokens_creates_only_missing_ones(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(app_cli, "ROOT", tmp_path)
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=abc\nAEISP_AGENT_TOKEN=keepme\nAEISP_VIEWER_TOKEN=\n", encoding="utf-8")
    assert app_cli.main(["make-tokens"]) == 0
    text = env.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=abc" in text and "AEISP_AGENT_TOKEN=keepme" in text
    lines = dict(l.split("=", 1) for l in text.splitlines())
    assert len(lines["AEISP_OPERATOR_TOKEN"]) >= 20 and len(lines["AEISP_VIEWER_TOKEN"]) >= 20
    before = text
    assert app_cli.main(["make-tokens"]) == 0
    assert env.read_text(encoding="utf-8") == before
    assert "already exist" in capsys.readouterr().out
