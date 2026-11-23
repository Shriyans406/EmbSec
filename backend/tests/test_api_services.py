import json
import re
import time

import pytest

from app.build.engine import FAILED as BUILD_FAILED
from app.build.engine import SUCCESS as BUILD_SUCCESS
from app.build.engine import BuildResult
from app.deploy.board import ABSENT, BoardState
from app.security import Principal
from app.services.testing import evaluate

from conftest import FakeLink, auth
from deploy_fakes import UID, add_build, make_uf2, running, sha


# ------------------------------------------------------------------ helpers
def wait_done(client, job_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/jobs/{job_id}", headers=auth("viewer")).json()
        if j["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


def make_stock(services):
    services.cfg.stock_backup.parent.mkdir(parents=True, exist_ok=True)
    data = make_uf2(8, seed=200)
    services.cfg.stock_backup.write_bytes(data)
    return data


def prepare_deploy(services, board, build_id=1, seed=None, src=None):
    make_stock(services)
    data, src8 = add_build(services, build_id, seed=seed or build_id, src=src or (f"{build_id:02x}" * 32))
    board.outcomes[sha(data)] = running(build_id, src8)
    return data, src8


def approve(client, job_id, text):
    return client.post(f"/jobs/{job_id}/approve", headers=auth("operator"), json={"confirm": text})


def new_project(client, name="demo"):
    r = client.post("/projects", headers=auth("agent"), json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ projects and sources
def test_project_lifecycle(client):
    out = new_project(client)
    assert [f["name"] for f in out["files"]] == ["app.c"]
    assert client.post("/projects", headers=auth("agent"), json={"name": "demo"}).status_code == 400
    assert client.post("/projects", headers=auth("viewer"), json={"name": "other"}).status_code == 403
    listing = client.get("/projects", headers=auth("viewer")).json()
    assert listing[0]["name"] == "demo" and listing[0]["last_build"] is None and listing[0]["sources_sha256"]


def test_bad_project_names_and_unknown_projects(client):
    r = client.post("/projects", headers=auth("agent"), json={"name": "Bad Name"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "project_error"
    assert client.get("/projects/nosuch/sources", headers=auth("viewer")).status_code == 404


def test_source_roundtrip(client):
    new_project(client)
    body = {"content": "int f(void) { return 1; }\n"}
    first = client.put("/projects/demo/sources/util.c", headers=auth("agent"), json=body).json()
    assert first["created"] is True and first["size"] == len(body["content"])
    assert client.put("/projects/demo/sources/util.c", headers=auth("agent"), json=body).json()["created"] is False
    got = client.get("/projects/demo/sources/util.c", headers=auth("viewer")).json()
    assert got["content"] == body["content"] and got["sha256"] == first["sha256"]
    names = [f["name"] for f in client.get("/projects/demo/sources", headers=auth("viewer")).json()]
    assert names == ["app.c", "util.c"]
    assert client.delete("/projects/demo/sources/util.c", headers=auth("agent")).json()["deleted"] is True
    assert client.get("/projects/demo/sources/util.c", headers=auth("viewer")).status_code == 404
    assert client.delete("/projects/demo/sources/util.c", headers=auth("agent")).status_code == 404


def test_viewer_can_read_but_not_write(client):
    new_project(client)
    assert client.get("/projects/demo/sources/app.c", headers=auth("viewer")).status_code == 200
    assert client.put("/projects/demo/sources/app.c", headers=auth("viewer"), json={"content": "x"}).status_code == 403
    assert client.delete("/projects/demo/sources/app.c", headers=auth("viewer")).status_code == 403


@pytest.mark.parametrize("filename", ["../../evil.c", "evil.txt", "a b.c", "x.cpp", ".hidden.c", "1abc.c"])
def test_unsafe_file_names_are_refused(client, filename):
    new_project(client)
    r = client.post("/tools/write_source/call", headers=auth("agent"),
                    json={"arguments": {"project": "demo", "filename": filename, "content": "int x;"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_filename", filename


def test_unsafe_content_is_refused(client):
    new_project(client)
    put = lambda content: client.put("/projects/demo/sources/x.c", headers=auth("agent"), json={"content": content})
    r = put('#include "../../secret.h"\n')
    assert r.status_code == 422 and r.json()["error"]["code"] == "include_not_allowed"
    r = put("a\x00b")
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_content"
    r = put("é" * 40000)               # 80 000 bytes although only 40 000 characters
    assert r.status_code == 422 and r.json()["error"]["code"] == "file_too_large"
    assert put("x" * 65537).status_code == 422
    assert client.get("/projects/demo/sources/x.c", headers=auth("viewer")).status_code == 404


def test_file_count_limit(client):
    new_project(client)
    for i in range(19):                     # app.c + 19 = 20 files, the limit
        assert client.put(f"/projects/demo/sources/f{i}.c", headers=auth("agent"),
                          json={"content": "int a;"}).status_code == 200
    r = client.put("/projects/demo/sources/one_too_many.c", headers=auth("agent"), json={"content": "int a;"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_files"
    assert client.put("/projects/demo/sources/f3.c", headers=auth("agent"), json={"content": "int b;"}).status_code == 200


def test_writes_leave_no_temp_files_behind(client, services):
    new_project(client)
    client.put("/projects/demo/sources/a.c", headers=auth("agent"), json={"content": "int a;"})
    pdir = services.cfg.build.workspace_dir / "demo"
    assert sorted(p.name for p in pdir.iterdir()) == ["app", "project.json"]


def test_restore_sources_from_a_build(client, services):
    new_project(client)
    add_build(services, 1, project="demo", files={"app.c": "ORIGINAL\n", "util.c": "UTIL\n"})
    client.put("/projects/demo/sources/app.c", headers=auth("agent"), json={"content": "CHANGED\n"})
    client.put("/projects/demo/sources/extra.c", headers=auth("agent"), json={"content": "int e;"})
    r = client.post("/projects/demo/restore", headers=auth("agent"), json={"build_id": 1})
    assert r.status_code == 200 and r.json()["files"] == ["app.c", "util.c"]
    assert client.get("/projects/demo/sources/app.c", headers=auth("viewer")).json()["content"] == "ORIGINAL\n"
    assert client.get("/projects/demo/sources/extra.c", headers=auth("viewer")).status_code == 404
    new_project(client, "other")
    assert client.post("/projects/other/restore", headers=auth("agent"), json={"build_id": 1}).status_code == 422
    assert client.post("/projects/demo/restore", headers=auth("agent"), json={"build_id": 99}).status_code == 404


# ------------------------------------------------------------------ builds
def _fake_build(monkeypatch, status, diagnostics=None, build_id=7):
    def fake(name, timeout_s=None, cfg=None, on_stage=None, keep_tree=False):
        if on_stage:
            on_stage(f"build #{build_id}: compiling ...")
        return BuildResult(id=build_id, project=name, status=status, message="fake build",
                           diagnostics=diagnostics or [])
    monkeypatch.setattr("app.services.builds.build_project", fake)


def test_build_job_succeeds(client, monkeypatch):
    new_project(client)
    _fake_build(monkeypatch, BUILD_SUCCESS)
    r = client.post("/builds", headers=auth("agent"), json={"project": "demo"})
    assert r.status_code == 202 and r.json()["kind"] == "build"
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "SUCCEEDED" and job["result"]["id"] == 7 and job["result"]["status"] == "SUCCESS"
    texts = [l["text"] for l in client.get(f"/jobs/{job['id']}/logs", headers=auth("viewer")).json()["lines"]]
    assert "build #7: compiling ..." in texts and "build #7: SUCCESS - fake build" in texts


def test_failed_build_becomes_a_failed_job_with_truncated_diagnostics(client, monkeypatch):
    new_project(client)
    diags = [{"severity": "error", "message": f"problem {i}", "file": "app/app.c", "line": i + 1, "column": 1,
              "flag": None, "source": "app", "category": "compile", "context": []} for i in range(60)]
    _fake_build(monkeypatch, BUILD_FAILED, diags, build_id=8)
    jid = client.post("/builds", headers=auth("agent"), json={"project": "demo"}).json()["id"]
    job = wait_done(client, jid)
    assert job["status"] == "FAILED" and job["error"].startswith("build #8 FAILED")
    assert job["result"]["diagnostics_total"] == 60 and len(job["result"]["diagnostics"]) == 50
    texts = [l["text"] for l in client.get(f"/jobs/{jid}/logs", headers=auth("viewer")).json()["lines"]]
    assert any("error: app/app.c:1:1: problem 0" in t for t in texts)


def test_build_requests_are_validated_before_a_job_exists(client):
    assert client.post("/builds", headers=auth("agent"), json={"project": "nosuch"}).status_code == 404
    assert client.post("/builds", headers=auth("agent"), json={"project": "x"}).status_code == 422
    assert client.post("/builds", headers=auth("viewer"), json={"project": "demo"}).status_code == 403
    assert client.get("/jobs?kind=build", headers=auth("viewer")).json() == []


def test_build_history_and_details(client, services):
    diags = [{"severity": "warning", "message": f"w{i}", "file": None, "line": None, "column": None, "flag": None,
              "source": "app", "category": "compile", "context": []} for i in range(55)]
    add_build(services, 1, diagnostics=diags)
    hist = client.get("/builds", headers=auth("viewer")).json()
    assert [b["id"] for b in hist] == [1] and hist[0]["status"] == "SUCCESS"
    assert client.get("/builds?project=other", headers=auth("viewer")).json() == []
    b = client.get("/builds/1", headers=auth("viewer")).json()
    assert b["diagnostics_total"] == 55 and len(b["diagnostics"]) == 50
    assert client.get("/builds/99", headers=auth("viewer")).status_code == 404
    log = client.get("/builds/1/log?tail=2", headers=auth("viewer")).json()
    assert log["lines"] == ["line2", "line3"]


# ------------------------------------------------------------------ deployments: proposing
def test_proposal_waits_for_a_human_and_flashes_nothing(client, services, board):
    prepare_deploy(services, board)
    r = client.post("/deployments", headers=auth("agent"), json={"build_id": 1})
    assert r.status_code == 202
    job = r.json()
    assert job["status"] == "AWAITING_APPROVAL" and job["confirm_token"] == "1" and job["kind"] == "deploy"
    assert "human" in job["next_step"].lower() and "Nothing has been flashed" in job["next_step"]
    assert {c["name"]: c["status"] for c in job["params"]["checks"]}["image"] == "PASS"
    time.sleep(0.4)
    assert board.flashed == []
    assert client.get(f"/jobs/{job['id']}", headers=auth("viewer")).json()["status"] == "AWAITING_APPROVAL"


def test_proposals_need_the_agent_role(client, services, board):
    prepare_deploy(services, board)
    assert client.post("/deployments", headers=auth("viewer"), json={"build_id": 1}).status_code == 403
    assert client.post("/recoveries", headers=auth("viewer"), json={}).status_code == 403


def test_proposal_refused_when_preflight_fails_says_why(client, services, board):
    prepare_deploy(services, board)
    board.state = BoardState(ABSENT)
    r = client.post("/deployments", headers=auth("agent"), json={"build_id": 1})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "preflight_failed"
    assert "no board found on USB" in err["message"] and "nothing was proposed" in err["message"]
    assert any(c["name"] == "board" and c["status"] == "FAIL" for c in err["details"])
    assert client.get("/jobs?kind=deploy", headers=auth("viewer")).json() == []


def test_unknown_or_failed_builds_cannot_be_proposed(client, services, board):
    make_stock(services)
    add_build(services, 1, status=BUILD_FAILED)
    r = client.post("/deployments", headers=auth("agent"), json={"build_id": 1})
    assert r.status_code == 400 and r.json()["error"]["code"] == "deploy_error"
    assert client.post("/deployments", headers=auth("agent"), json={"build_id": 42}).status_code == 400


def test_proposal_while_board_is_busy(client, services, board):
    prepare_deploy(services, board)
    with services.device.exclusive("some other operation"):
        r = client.post("/deployments", headers=auth("agent"), json={"build_id": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "board_busy"


# ------------------------------------------------------------------ deployments: approving and running
def test_approved_deployment_flashes_and_records(client, services, board):
    data, src8 = prepare_deploy(services, board)
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": 1}).json()["id"]
    assert approve(client, jid, "1").status_code == 200
    job = wait_done(client, jid)
    assert job["status"] == "SUCCEEDED" and job["result"]["status"] == "SUCCESS"
    assert board.flashed == [sha(data)]
    dep = client.get("/deployments", headers=auth("viewer")).json()[0]
    assert dep["status"] == "SUCCESS" and dep["build_id"] == 1
    full = client.get(f"/deployments/{dep['id']}", headers=auth("viewer")).json()
    assert f"api:operator approved job #{jid}" in full["approved_by"]
    assert UID in client.get("/known-good", headers=auth("viewer")).json()["registered_boards"]
    texts = [l["text"] for l in client.get(f"/jobs/{jid}/logs", headers=auth("viewer")).json()["lines"]]
    assert any("verification passed" in t for t in texts)


def test_wrong_confirmation_does_not_flash(client, services, board):
    prepare_deploy(services, board)
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": 1}).json()["id"]
    assert approve(client, jid, "yes").status_code == 400
    assert approve(client, jid, "2").status_code == 400
    time.sleep(0.4)
    assert board.flashed == []
    assert client.get(f"/jobs/{jid}", headers=auth("viewer")).json()["status"] == "AWAITING_APPROVAL"


def test_agent_can_never_approve_its_own_proposal(client, services, board):
    prepare_deploy(services, board)
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": 1}).json()["id"]
    assert client.post(f"/jobs/{jid}/approve", headers=auth("agent"), json={"confirm": "1"}).status_code == 403
    time.sleep(0.4)
    assert board.flashed == []


def test_image_swapped_after_approval_is_refused(client, services, board):
    prepare_deploy(services, board)
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": 1}).json()["id"]
    (services.cfg.build.artifacts_dir / "1" / "firmware.uf2").write_bytes(make_uf2(4, seed=99))   # swapped!
    assert approve(client, jid, "1").status_code == 200
    job = wait_done(client, jid)
    assert job["status"] == "FAILED" and "changed after it was approved" in job["error"]
    assert board.flashed == []


def test_failed_verification_rolls_back_to_known_good(client, services, board):
    d1, _ = prepare_deploy(services, board, 1)
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": 1}).json()["id"]
    approve(client, jid, "1")
    assert wait_done(client, jid)["status"] == "SUCCEEDED"
    assert client.post("/known-good/promote", headers=auth("operator"),
                       json={"build_id": 1, "confirm": "PROMOTE 1"}).status_code == 200
    d2, _ = add_build(services, 2, seed=2, src="02" * 32)          # the board will NOT come up with this one
    jid2 = client.post("/deployments", headers=auth("agent"), json={"build_id": 2}).json()["id"]
    approve(client, jid2, "2")
    job = wait_done(client, jid2)
    assert job["status"] == "FAILED" and job["result"]["status"] == "ROLLED_BACK"
    assert board.flashed[-2:] == [sha(d2), sha(d1)]
    assert board.state.build_id == "1"


def test_recovery_flow(client, services, board):
    stock = make_stock(services)
    r = client.post("/recoveries", headers=auth("agent"), json={"target": "stock"})
    assert r.status_code == 202 and r.json()["confirm_token"] == "RECOVER"
    jid = r.json()["id"]
    assert approve(client, jid, "recover").status_code == 400
    assert approve(client, jid, "RECOVER").status_code == 200
    job = wait_done(client, jid)
    assert job["status"] == "SUCCEEDED" and job["result"]["kind"] == "recover"
    assert board.flashed == [sha(stock)]


def test_recovery_without_any_recovery_image(client, services, board):
    r = client.post("/recoveries", headers=auth("agent"), json={"target": "auto"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_recovery_image"
    assert client.post("/recoveries", headers=auth("agent"), json={"target": "banana"}).status_code == 422


def test_known_good_promotion_rules(client, services, board):
    prepare_deploy(services, board, 1)
    info = client.get("/known-good", headers=auth("viewer")).json()
    assert info["known_good"]["present"] is False and info["factory_backup"]["valid"] is True
    body = {"build_id": 1, "confirm": "PROMOTE 1"}
    assert client.post("/known-good/promote", headers=auth("agent"), json=body).status_code == 403
    assert client.post("/known-good/promote", headers=auth("operator"),
                       json={"build_id": 1, "confirm": "promote 1"}).status_code == 400
    r = client.post("/known-good/promote", headers=auth("operator"), json=body)       # never deployed yet
    assert r.status_code == 400 and "deploy it first" in r.json()["error"]["message"]
    jid = client.post("/deployments", headers=auth("agent"), json={"build_id": 1}).json()["id"]
    approve(client, jid, "1")
    wait_done(client, jid)
    ok = client.post("/known-good/promote", headers=auth("operator"), json=body)
    assert ok.status_code == 200 and ok.json()["build_id"] == 1
    kg = client.get("/known-good", headers=auth("viewer")).json()["known_good"]
    assert kg["present"] and kg["valid"]
    rows = client.get("/audit?action=known_good", headers=auth("operator")).json()
    assert rows[0]["outcome"] == "ok" and any(r["outcome"].startswith("refused") for r in rows)


# ------------------------------------------------------------------ test runs
def test_smoke_suite_passes_and_is_recorded(client):
    assert client.get("/tests/suites", headers=auth("viewer")).json()["smoke"]["cases"] == 13
    r = client.post("/tests/run", headers=auth("agent"), json={"suite": "smoke"})
    assert r.status_code == 202
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "SUCCEEDED", job["error"]
    res = job["result"]
    assert (res["total"], res["passed"], res["failed"]) == (13, 13, 0)
    run = client.get(f"/tests/runs/{res['run_id']}", headers=auth("viewer")).json()
    assert run["passed"] == 13 and len(run["result"]["cases"]) == 13
    assert client.get("/tests/runs", headers=auth("viewer")).json()[0]["id"] == res["run_id"]
    assert client.get("/tests/runs/999", headers=auth("viewer")).status_code == 404


def test_a_failing_check_fails_the_job(client):
    FakeLink.replies["PING"] = "OK PANG"
    job = wait_done(client, client.post("/tests/run", headers=auth("agent"), json={"suite": "smoke"}).json()["id"])
    assert job["status"] == "FAILED" and job["error"] == "1 of 13 checks failed"
    bad = [c for c in job["result"]["cases"] if not c["passed"]]
    assert bad[0]["name"] == "ping" and "exactly" in bad[0]["why"]


def test_custom_checks_and_repeat(client):
    cases = [{"name": "ping", "command": "PING", "expect": {"equals": "OK PONG"}},
             {"name": "led", "command": "LED ON", "expect": {"prefix": "OK LED", "contains": "ON"}}]
    jid = client.post("/tests/run", headers=auth("agent"), json={"cases": cases, "repeat": 3}).json()["id"]
    job = wait_done(client, jid)
    assert job["status"] == "SUCCEEDED" and job["result"]["total"] == 6
    assert sorted({c["repeat"] for c in job["result"]["cases"]}) == [1, 2, 3]


def test_dangerous_commands_cannot_be_tested(client):
    for cmd in ("BOOTSEL", "HANG", "REBOOT", "FORMAT"):
        r = client.post("/tests/run", headers=auth("agent"),
                        json={"cases": [{"name": "x", "command": cmd, "expect": {"prefix": "OK"}}]})
        assert r.status_code in (403, 422), cmd
    assert client.get("/jobs?kind=test", headers=auth("viewer")).json() == []
    assert "BOOTSEL" not in FakeLink.sent and "HANG" not in FakeLink.sent


def test_test_request_validation(client):
    post = lambda body, role="agent": client.post("/tests/run", headers=auth(role), json=body)
    assert post({}).status_code == 422
    assert post({"suite": "smoke", "cases": []}).status_code == 422
    assert post({"suite": "nope"}).status_code == 404
    assert post({"cases": [{"name": "a", "command": "PING", "expect": {}}]}).status_code == 422
    assert post({"cases": [{"name": "a", "command": "PING", "expect": {"regex": "("}}]}).status_code == 422
    assert post({"suite": "smoke", "repeat": 99}).status_code == 422
    assert post({"suite": "smoke"}, "viewer").status_code == 403


def test_run_stops_when_the_board_disappears_and_says_so(client):
    FakeLink.fail_after = 2
    job = wait_done(client, client.post("/tests/run", headers=auth("agent"), json={"suite": "smoke"}).json()["id"])
    assert job["status"] == "FAILED" and job["result"]["aborted"] is True
    assert job["result"]["total"] == 3 and "link lost" in job["result"]["cases"][-1]["why"]
    assert job["error"].startswith("the board stopped answering") and "3 of 13" in job["error"]


def test_evaluate_rules():
    assert evaluate("OK PONG", {"equals": "OK PONG"}) == (True, "")
    assert evaluate("OK PONG", {"equals": "OK"})[0] is False
    assert evaluate("OK PONG", {"prefix": "OK", "contains": "PO", "regex": r"^OK \w+$"})[0] is True
    assert evaluate("OK PONG", {"prefix": "ERR"})[0] is False
    assert evaluate("OK PONG", {"regex": r"\d+"})[0] is False
    assert evaluate(None, {"equals": "x"}) == (False, "no reply")


# ------------------------------------------------------------------ the tool registry
def call(client, name, args=None, role="agent"):
    return client.post(f"/tools/{name}/call", headers=auth(role), json={"arguments": args or {}})


def test_tool_lists_depend_on_role(client):
    viewer = client.get("/tools", headers=auth("viewer")).json()
    agent = client.get("/tools", headers=auth("agent")).json()
    assert len(viewer) == 13 and len(agent) == 22
    assert all(t["min_role"] == "viewer" for t in viewer)
    ws = next(t for t in agent if t["name"] == "write_source")
    assert set(ws["parameters"]["required"]) == {"project", "filename", "content"} and ws["risk"] == "write"
    assert client.get("/tools").status_code == 401


def test_no_tool_can_approve_flash_or_promote(client):
    names = {t["name"] for t in client.get("/tools", headers=auth("operator")).json()}
    for forbidden in ("approve", "promote", "flash", "bootsel", "hang", "reboot", "token", "secret"):
        assert not any(forbidden in n for n in names), forbidden
    assert call(client, "approve_job", {"job_id": 1}, "operator").status_code == 404


def test_calling_tools(client):
    ok = call(client, "device_status", role="viewer").json()
    assert ok["ok"] is True and ok["tool"] == "device_status" and ok["result"]["connected"] is True
    assert call(client, "nosuchtool").status_code == 404
    assert call(client, "start_build", {"project": "demo"}, role="viewer").status_code == 403
    bad = call(client, "start_build", {"timeout_s": 5})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_arguments"
    wheres = {d["where"] for d in bad.json()["error"]["details"]}
    assert "project" in wheres and "timeout_s" in wheres


def test_tool_calls_are_audited_without_file_contents(client):
    new_project(client)
    call(client, "write_source", {"project": "demo", "filename": "a.c", "content": "SECRET_MARKER_123"})
    call(client, "start_build", {"project": "demo"}, role="viewer")      # refused
    rows = client.get("/audit?limit=100", headers=auth("operator")).json()
    tool_rows = [r for r in rows if r["action"].startswith("tool.")]
    assert any(r["action"] == "tool.write_source" and r["outcome"] == "ok" for r in tool_rows)
    assert any(r["action"] == "tool.start_build" and r["outcome"] == "refused:forbidden" for r in tool_rows)
    assert "SECRET_MARKER_123" not in json.dumps(rows)


def test_start_build_and_wait_tools(client, monkeypatch):
    new_project(client)
    _fake_build(monkeypatch, BUILD_SUCCESS)
    job = call(client, "start_build", {"project": "demo"}).json()["result"]
    waited = call(client, "wait_for_job", {"job_id": job["id"], "timeout_s": 20}, "viewer").json()["result"]
    assert waited["finished"] is True and waited["job"]["status"] == "SUCCEEDED"


def test_wait_for_job_times_out_politely(client):
    jid = client.post("/selftest", headers=auth("agent"), json={"seconds": 3}).json()["id"]
    first = call(client, "wait_for_job", {"job_id": jid, "timeout_s": 1}, "viewer").json()["result"]
    assert first["finished"] is False and "ask again" in first["note"]
    second = call(client, "wait_for_job", {"job_id": jid, "timeout_s": 20}, "viewer").json()["result"]
    assert second["finished"] is True
    assert call(client, "get_job", {"job_id": 9999}, "viewer").status_code == 404


def test_propose_deployment_tool_never_flashes(client, services, board):
    prepare_deploy(services, board)
    r = call(client, "propose_deployment", {"build_id": 1}).json()["result"]
    assert r["status"] == "AWAITING_APPROVAL" and "human" in r["next_step"].lower()
    time.sleep(0.4)
    assert board.flashed == []
    assert call(client, "propose_deployment", {"build_id": 1}, "viewer").status_code == 403


def test_function_declarations_suit_the_gemini_api(services):
    decls = services.tools.declarations(Principal("agent", "agent"))
    assert len(decls) == 22
    text = json.dumps(decls)
    for junk in ('"title"', '"default"', '$ref', '"anyOf"', 'additionalProperties'):
        assert junk not in text, junk
    for d in decls:
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", d["name"]) and d["description"]
    by = {d["name"]: d for d in decls}
    assert "parameters" not in by["device_status"]
    assert by["propose_recovery"]["parameters"]["properties"]["target"]["enum"] == ["auto", "known-good", "stock"]
    cases = by["run_tests"]["parameters"]["properties"]["cases"]
    assert cases["type"] == "array" and {"name", "command", "expect"} <= set(cases["items"]["properties"])
